"""Continuously upload finalized Edge recording sessions with durable retries."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import signal
import sqlite3
import ssl
import sys
import threading
from typing import Any
from urllib.parse import urlsplit

from fleet_control_plane.recording_storage import manifest_sha256
from fleet_control_plane.recording_upload import (
    create_client_context,
    read_manifest,
    upload_session,
    validate_central_url,
)
from fleet_control_plane.status import isoformat_z, parse_datetime, utc_now


UPLOAD_CHECKPOINT_MIGRATION = """
CREATE TABLE IF NOT EXISTS recording_upload_checkpoints (
    session_id TEXT PRIMARY KEY,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN ('FAILED', 'COMPLETE')),
    attempts INTEGER NOT NULL CHECK (attempts >= 1),
    last_attempt_at TEXT NOT NULL,
    next_attempt_at TEXT,
    completed_at TEXT,
    last_error TEXT,
    CHECK (
        (state = 'FAILED' AND next_attempt_at IS NOT NULL
            AND completed_at IS NULL AND last_error IS NOT NULL)
        OR
        (state = 'COMPLETE' AND next_attempt_at IS NULL
            AND completed_at IS NOT NULL AND last_error IS NULL)
    )
);
"""


class UploadCheckpointStore:
    """Crash-safe local state; capture directories remain untouched and read-only."""

    def __init__(self, path: str | Path) -> None:
        unresolved = Path(path).expanduser()
        if unresolved.exists() and unresolved.is_symlink():
            raise ValueError("upload checkpoint database may not be a symbolic link")
        unresolved.parent.mkdir(parents=True, exist_ok=True)
        self.path = unresolved
        with self._connect() as connection:
            connection.executescript(UPLOAD_CHECKPOINT_MIGRATION)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def should_attempt(
        self,
        session_id: str,
        digest: str,
        *,
        occurred_at: datetime,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recording_upload_checkpoints WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return True
        if row["manifest_sha256"] != digest:
            raise ValueError(
                f"finalized session manifest changed after an upload attempt: {session_id}"
            )
        if row["state"] == "COMPLETE":
            return False
        return parse_datetime(row["next_attempt_at"]) <= occurred_at

    def mark_complete(
        self,
        session_id: str,
        digest: str,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        timestamp = isoformat_z(occurred_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM recording_upload_checkpoints WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is not None and row["manifest_sha256"] != digest:
                raise ValueError("cannot complete a changed recording manifest")
            attempts = 1 if row is None else row["attempts"] + 1
            connection.execute(
                """
                INSERT INTO recording_upload_checkpoints (
                    session_id, manifest_sha256, state, attempts,
                    last_attempt_at, next_attempt_at, completed_at, last_error
                ) VALUES (?, ?, 'COMPLETE', ?, ?, NULL, ?, NULL)
                ON CONFLICT(session_id) DO UPDATE SET
                    state = 'COMPLETE', attempts = excluded.attempts,
                    last_attempt_at = excluded.last_attempt_at,
                    next_attempt_at = NULL, completed_at = excluded.completed_at,
                    last_error = NULL
                """,
                (session_id, digest, attempts, timestamp, timestamp),
            )
        return self.get(session_id)

    def mark_failed(
        self,
        session_id: str,
        digest: str,
        error: str,
        *,
        occurred_at: datetime,
        retry_base_s: float,
        retry_max_s: float,
    ) -> dict[str, Any]:
        message = error.strip()[:2048] or "recording upload failed"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM recording_upload_checkpoints WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is not None and row["manifest_sha256"] != digest:
                raise ValueError("cannot retry a changed recording manifest")
            attempts = 1 if row is None else row["attempts"] + 1
            delay_s = min(retry_max_s, retry_base_s * (2 ** min(attempts - 1, 20)))
            attempted_at = isoformat_z(occurred_at)
            next_attempt_at = isoformat_z(occurred_at + timedelta(seconds=delay_s))
            connection.execute(
                """
                INSERT INTO recording_upload_checkpoints (
                    session_id, manifest_sha256, state, attempts,
                    last_attempt_at, next_attempt_at, completed_at, last_error
                ) VALUES (?, ?, 'FAILED', ?, ?, ?, NULL, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    state = 'FAILED', attempts = excluded.attempts,
                    last_attempt_at = excluded.last_attempt_at,
                    next_attempt_at = excluded.next_attempt_at,
                    completed_at = NULL, last_error = excluded.last_error
                """,
                (
                    session_id,
                    digest,
                    attempts,
                    attempted_at,
                    next_attempt_at,
                    message,
                ),
            )
        return self.get(session_id)

    def get(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recording_upload_checkpoints WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown upload checkpoint: {session_id}")
        return dict(row)


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True), flush=True)


def scan_once(
    capture_root: Path,
    *,
    agent_id: str,
    profile_ids: set[str],
    checkpoints: UploadCheckpointStore,
    upload: Callable[[Path], dict],
    now: Callable[[], datetime] = utc_now,
    retry_base_s: float = 10.0,
    retry_max_s: float = 300.0,
    emit: Callable[[dict[str, Any]], None] = _emit,
) -> dict[str, int]:
    if capture_root.is_symlink() or not capture_root.is_dir():
        raise ValueError("capture root must be a regular non-symlink directory")
    summary = {
        "discovered": 0,
        "eligible": 0,
        "completed": 0,
        "failed": 0,
        "skipped": 0,
        "invalid": 0,
    }
    for session_dir in sorted(capture_root.iterdir(), key=lambda item: item.name):
        if not session_dir.is_dir() or session_dir.name.endswith(".partial"):
            continue
        if not (session_dir / "session-manifest.json").is_file():
            continue
        summary["discovered"] += 1
        try:
            manifest = read_manifest(session_dir)
        except (OSError, ValueError) as exc:
            summary["invalid"] += 1
            emit(
                {
                    "event": "recording_upload_invalid_session",
                    "session_dir": session_dir.name,
                    "error": str(exc),
                }
            )
            continue
        if manifest.agent_id != agent_id or (
            profile_ids and manifest.profile_id not in profile_ids
        ):
            summary["skipped"] += 1
            continue
        digest = manifest_sha256(manifest)
        attempted_at = now()
        try:
            eligible = checkpoints.should_attempt(
                manifest.session_id,
                digest,
                occurred_at=attempted_at,
            )
        except ValueError as exc:
            summary["failed"] += 1
            emit(
                {
                    "event": "recording_upload_manifest_conflict",
                    "session_id": manifest.session_id,
                    "error": str(exc),
                }
            )
            continue
        if not eligible:
            summary["skipped"] += 1
            continue
        summary["eligible"] += 1
        try:
            result = upload(session_dir)
            if result.get("state") != "COMPLETE":
                raise RuntimeError("Central did not finalize the recording session")
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            failed_at = now()
            failed = checkpoints.mark_failed(
                manifest.session_id,
                digest,
                str(exc),
                occurred_at=failed_at,
                retry_base_s=retry_base_s,
                retry_max_s=retry_max_s,
            )
            summary["failed"] += 1
            emit(
                {
                    "event": "recording_upload_failed",
                    "session_id": manifest.session_id,
                    "attempts": failed["attempts"],
                    "next_attempt_at": failed["next_attempt_at"],
                    "error": failed["last_error"],
                }
            )
            continue
        completed = checkpoints.mark_complete(
            manifest.session_id,
            digest,
            occurred_at=now(),
        )
        summary["completed"] += 1
        emit(
            {
                "event": "recording_upload_complete",
                "session_id": manifest.session_id,
                "attempts": completed["attempts"],
                "total_size_bytes": manifest.total_size_bytes,
            }
        )
    return summary


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Continuously upload finalized Edge recordings over Agent mTLS"
    )
    parser.add_argument("--capture-root", type=Path, required=True)
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--central-url", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--profile-id", action="append", default=[])
    parser.add_argument("--ca-bundle", type=Path, required=True)
    parser.add_argument("--client-certificate", type=Path, required=True)
    parser.add_argument("--client-private-key", type=Path, required=True)
    parser.add_argument("--poll-interval-s", type=float, default=10.0)
    parser.add_argument("--retry-base-s", type=float, default=10.0)
    parser.add_argument("--retry-max-s", type=float, default=300.0)
    parser.add_argument("--once", action="store_true")
    options = parser.parse_args(args)
    try:
        options.central_url = validate_central_url(options.central_url)
    except ValueError as exc:
        parser.error(str(exc))
    if urlsplit(options.central_url).scheme != "https":
        parser.error("automatic Agent upload requires HTTPS")
    for label, value in (
        ("poll-interval-s", options.poll_interval_s),
        ("retry-base-s", options.retry_base_s),
        ("retry-max-s", options.retry_max_s),
    ):
        if not math.isfinite(value) or value <= 0:
            parser.error(f"{label} must be positive")
    if options.retry_max_s < options.retry_base_s:
        parser.error("retry-max-s must be greater than or equal to retry-base-s")
    return options


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    try:
        context = create_client_context(
            ca_bundle=options.ca_bundle,
            client_certificate=options.client_certificate,
            client_private_key=options.client_private_key,
        )
        checkpoints = UploadCheckpointStore(options.state_db)
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise SystemExit(f"invalid automatic upload configuration: {exc}") from exc

    def upload(path: Path) -> dict:
        return upload_session(
            path,
            central_url=options.central_url,
            context=context,
        )

    stop = threading.Event()

    def request_stop(signum, frame) -> None:
        del signum, frame
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    while not stop.is_set():
        try:
            summary = scan_once(
                options.capture_root,
                agent_id=options.agent_id,
                profile_ids=set(options.profile_id),
                checkpoints=checkpoints,
                upload=upload,
                retry_base_s=options.retry_base_s,
                retry_max_s=options.retry_max_s,
            )
        except (OSError, ValueError) as exc:
            _emit({"event": "recording_upload_scan_failed", "error": str(exc)})
        else:
            if any(summary[key] for key in ("completed", "failed", "invalid")):
                _emit({"event": "recording_upload_scan", **summary})
        if options.once or stop.wait(options.poll_interval_s):
            break


if __name__ == "__main__":
    main(sys.argv[1:])
