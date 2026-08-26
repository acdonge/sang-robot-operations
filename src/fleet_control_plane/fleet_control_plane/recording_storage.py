"""Immutable filesystem Object Store and SQLite recording catalog."""

from __future__ import annotations

from collections.abc import AsyncIterable
from datetime import datetime
import errno
from functools import partial
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import sqlite3
from typing import Any

from anyio import to_thread

from fleet_control_plane.recording_models import RecordingManifest, validate_artifact_path
from fleet_control_plane.status import isoformat_z


RECORDING_MIGRATION = """
CREATE TABLE IF NOT EXISTS recording_sessions (
    session_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('STAGING', 'COMPLETE')),
    manifest_schema_version TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    manifest_json TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    agent_id TEXT NOT NULL REFERENCES agent_registrations(agent_id) ON DELETE RESTRICT,
    robot_id TEXT NOT NULL,
    run_id TEXT REFERENCES task_runs(run_id) ON DELETE RESTRICT,
    task_definition_id TEXT,
    task_definition_version INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    record_start_ns INTEGER NOT NULL,
    record_end_ns INTEGER NOT NULL,
    source_start_ns INTEGER,
    source_end_ns INTEGER,
    total_size_bytes INTEGER NOT NULL CHECK (total_size_bytes >= 0),
    expected_file_count INTEGER NOT NULL CHECK (expected_file_count >= 1),
    completed_file_count INTEGER NOT NULL DEFAULT 0 CHECK (completed_file_count >= 0),
    object_prefix TEXT NOT NULL UNIQUE,
    created_by TEXT REFERENCES principals(principal_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK (
        (task_definition_id IS NULL AND task_definition_version IS NULL)
        OR (task_definition_id IS NOT NULL AND task_definition_version >= 1)
    ),
    CHECK (
        (state = 'STAGING' AND completed_at IS NULL)
        OR (state = 'COMPLETE' AND completed_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS recording_sessions_run_started
    ON recording_sessions (run_id, started_at DESC);
CREATE INDEX IF NOT EXISTS recording_sessions_agent_started
    ON recording_sessions (agent_id, started_at DESC);

CREATE TABLE IF NOT EXISTS recording_artifacts (
    session_id TEXT NOT NULL REFERENCES recording_sessions(session_id) ON DELETE RESTRICT,
    relative_path TEXT NOT NULL,
    object_key TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    expected_size_bytes INTEGER NOT NULL CHECK (expected_size_bytes >= 0),
    expected_sha256 TEXT NOT NULL CHECK (length(expected_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'COMPLETE')),
    stored_at TEXT,
    PRIMARY KEY (session_id, relative_path),
    CHECK (
        (state = 'PENDING' AND stored_at IS NULL)
        OR (state = 'COMPLETE' AND stored_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS recording_artifacts_session_state
    ON recording_artifacts (session_id, state);

CREATE TABLE IF NOT EXISTS recording_topics (
    session_id TEXT NOT NULL REFERENCES recording_sessions(session_id) ON DELETE RESTRICT,
    topic_key TEXT NOT NULL,
    topic_name TEXT NOT NULL,
    message_type TEXT NOT NULL,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE RESTRICT,
    calibration_ref TEXT NOT NULL,
    timestamp_provenance TEXT NOT NULL,
    message_count INTEGER NOT NULL CHECK (message_count >= 1),
    source_start_ns INTEGER,
    source_end_ns INTEGER,
    PRIMARY KEY (session_id, topic_key),
    UNIQUE (session_id, topic_name)
);
"""


RECORDING_QUALITY_MIGRATION = """
CREATE TABLE IF NOT EXISTS recording_quality_reports (
    session_id TEXT PRIMARY KEY REFERENCES recording_sessions(session_id) ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK (state IN ('PASS', 'WARNING', 'FAIL')),
    report_schema_version TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL CHECK (length(policy_sha256) = 64),
    report_sha256 TEXT NOT NULL UNIQUE CHECK (length(report_sha256) = 64),
    report_size_bytes INTEGER NOT NULL CHECK (report_size_bytes > 0),
    object_key TEXT NOT NULL UNIQUE,
    clock_state TEXT NOT NULL,
    stream_count INTEGER NOT NULL CHECK (stream_count >= 0),
    pass_stream_count INTEGER NOT NULL CHECK (pass_stream_count >= 0),
    warning_stream_count INTEGER NOT NULL CHECK (warning_stream_count >= 0),
    fail_stream_count INTEGER NOT NULL CHECK (fail_stream_count >= 0),
    failed_check_count INTEGER NOT NULL CHECK (failed_check_count >= 0),
    warning_check_count INTEGER NOT NULL CHECK (warning_check_count >= 0),
    summary_json TEXT NOT NULL,
    analyzed_at TEXT NOT NULL,
    CHECK (stream_count = pass_stream_count + warning_stream_count + fail_stream_count)
);

CREATE INDEX IF NOT EXISTS recording_quality_reports_state_analyzed
    ON recording_quality_reports (state, analyzed_at DESC);

CREATE TABLE IF NOT EXISTS recording_quality_streams (
    session_id TEXT NOT NULL REFERENCES recording_quality_reports(session_id) ON DELETE RESTRICT,
    topic_key TEXT NOT NULL,
    topic_name TEXT NOT NULL,
    asset_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('state', 'camera')),
    required INTEGER NOT NULL CHECK (required IN (0, 1)),
    state TEXT NOT NULL CHECK (state IN ('PASS', 'WARNING', 'FAIL')),
    configured_width INTEGER,
    configured_height INTEGER,
    configured_minimum_rate_hz REAL,
    measured_width INTEGER,
    measured_height INTEGER,
    measured_rate_hz REAL NOT NULL CHECK (measured_rate_hz >= 0),
    message_count INTEGER NOT NULL CHECK (message_count >= 0),
    timestamp_regressions INTEGER NOT NULL CHECK (timestamp_regressions >= 0),
    p95_gap_ms REAL,
    max_gap_ms REAL,
    average_jpeg_bytes REAL,
    p95_jpeg_bytes REAL,
    p95_joint_skew_ms REAL,
    max_joint_skew_ms REAL,
    PRIMARY KEY (session_id, topic_key)
);

CREATE INDEX IF NOT EXISTS recording_quality_streams_state
    ON recording_quality_streams (state, kind, session_id);
"""


class RecordingConflict(ValueError):
    """A recording ID or immutable object conflicts with existing data."""


class RecordingInvalid(ValueError):
    """A recording manifest or transition violates the storage contract."""


class RecordingNotFound(ValueError):
    """A recording session or artifact does not exist."""


class ObjectStoreError(RuntimeError):
    """An immutable Object Store operation failed."""


def _write_block(descriptor: int, digest: Any, block: bytes) -> None:
    digest.update(block)
    view = memoryview(block)
    while view:
        written = os.write(descriptor, view)
        view = view[written:]


def _fsync_descriptor(descriptor: int) -> None:
    os.fsync(descriptor)


def canonical_manifest(manifest: RecordingManifest) -> bytes:
    return (
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def manifest_sha256(manifest: RecordingManifest) -> str:
    return hashlib.sha256(canonical_manifest(manifest)).hexdigest()


def artifact_media_type(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    return {
        ".mcap": "application/octet-stream",
        ".yaml": "application/yaml",
        ".yml": "application/yaml",
        ".json": "application/json",
        ".log": "text/plain; charset=utf-8",
        ".mp4": "video/mp4",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
    }.get(suffix, "application/octet-stream")


class FilesystemObjectStore:
    """A path-confined, atomic and immutable filesystem Object Store backend."""

    def __init__(self, root: str | Path) -> None:
        unresolved = Path(root).expanduser()
        if unresolved.exists() and unresolved.is_symlink():
            raise ObjectStoreError("Object Store root may not be a symbolic link")
        unresolved.mkdir(parents=True, exist_ok=True)
        self.root = unresolved.resolve()
        if not self.root.is_dir():
            raise ObjectStoreError("Object Store root is not a directory")

    @staticmethod
    def session_prefix(session_id: str) -> str:
        if not session_id or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-" for character in session_id):
            raise ObjectStoreError("session_id is not safe for an object key")
        return f"sessions/{session_id}"

    def artifact_key(self, session_id: str, relative_path: str) -> str:
        return f"{self.session_prefix(session_id)}/{validate_artifact_path(relative_path)}"

    def manifest_key(self, session_id: str) -> str:
        return f"{self.session_prefix(session_id)}/session-manifest.json"

    def quality_key(self, session_id: str, sha256: str) -> str:
        if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
            raise ObjectStoreError("quality report SHA-256 is invalid")
        return f"quality-reports/{self.session_prefix(session_id).removeprefix('sessions/')}/{sha256}.json"

    def _path(self, object_key: str, *, create_parent: bool = False) -> Path:
        if "\\" in object_key or "\x00" in object_key:
            raise ObjectStoreError("object key is not canonical")
        pure = PurePosixPath(object_key)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise ObjectStoreError("object key escapes the Object Store")
        current = self.root
        for part in pure.parts[:-1]:
            current = current / part
            if current.exists():
                if current.is_symlink() or not current.is_dir():
                    raise ObjectStoreError("Object Store namespace contains an unsafe component")
            elif create_parent:
                current.mkdir(mode=0o750)
            else:
                break
        candidate = self.root.joinpath(*pure.parts)
        try:
            candidate.parent.resolve().relative_to(self.root)
        except ValueError as exc:
            raise ObjectStoreError("object key escapes the Object Store") from exc
        if candidate.exists() and candidate.is_symlink():
            raise ObjectStoreError("Object Store object may not be a symbolic link")
        return candidate

    @staticmethod
    def _hash_file(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                size += len(block)
                digest.update(block)
        finally:
            os.close(descriptor)
        return size, digest.hexdigest()

    def verify(self, object_key: str, *, size_bytes: int, sha256: str) -> Path:
        path = self._path(object_key)
        if not path.is_file():
            raise ObjectStoreError(f"Object Store object is missing: {object_key}")
        actual_size, actual_sha256 = self._hash_file(path)
        if actual_size != size_bytes or actual_sha256 != sha256:
            raise ObjectStoreError(f"Object Store object integrity mismatch: {object_key}")
        return path

    def _existing_or_conflict(
        self,
        path: Path,
        *,
        size_bytes: int,
        sha256: str,
    ) -> bool:
        if not path.exists():
            return False
        actual_size, actual_sha256 = self._hash_file(path)
        if actual_size == size_bytes and actual_sha256 == sha256:
            return True
        raise RecordingConflict("immutable Object Store key already contains different data")

    async def put_stream(
        self,
        object_key: str,
        stream: AsyncIterable[bytes],
        *,
        size_bytes: int,
        sha256: str,
    ) -> Path:
        destination = self._path(object_key, create_parent=True)
        if await to_thread.run_sync(
            partial(
                self._existing_or_conflict,
                destination,
                size_bytes=size_bytes,
                sha256=sha256,
            )
        ):
            return destination
        temporary = destination.with_name(
            f".{destination.name}.{secrets.token_hex(12)}.partial"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o640)
        digest = hashlib.sha256()
        received = 0
        try:
            async for block in stream:
                if not block:
                    continue
                received += len(block)
                if received > size_bytes:
                    raise RecordingInvalid("artifact body exceeds declared size")
                await to_thread.run_sync(_write_block, descriptor, digest, block)
            await to_thread.run_sync(_fsync_descriptor, descriptor)
        except BaseException:
            os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise
        else:
            os.close(descriptor)
        if received != size_bytes:
            temporary.unlink(missing_ok=True)
            raise RecordingInvalid("artifact body size differs from the manifest")
        if digest.hexdigest() != sha256:
            temporary.unlink(missing_ok=True)
            raise RecordingInvalid("artifact SHA-256 differs from the manifest")
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if not await to_thread.run_sync(
                partial(
                    self._existing_or_conflict,
                    destination,
                    size_bytes=size_bytes,
                    sha256=sha256,
                )
            ):
                raise AssertionError("destination disappeared during immutable write")
        finally:
            temporary.unlink(missing_ok=True)
        directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            await to_thread.run_sync(_fsync_descriptor, directory_fd)
        finally:
            os.close(directory_fd)
        return destination

    async def put_bytes(
        self,
        object_key: str,
        payload: bytes,
        *,
        sha256: str,
    ) -> Path:
        async def blocks():
            yield payload

        return await self.put_stream(
            object_key,
            blocks(),
            size_bytes=len(payload),
            sha256=sha256,
        )

    def open_path(self, object_key: str, *, size_bytes: int, sha256: str) -> Path:
        return self.verify(object_key, size_bytes=size_bytes, sha256=sha256)


class RecordingCatalog:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _session_row(connection: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT sessions.*, quality.state AS quality_state,
                   quality.report_sha256 AS quality_report_sha256,
                   quality.clock_state AS quality_clock_state,
                   quality.failed_check_count AS quality_failed_check_count,
                   quality.warning_check_count AS quality_warning_check_count,
                   quality.analyzed_at AS quality_analyzed_at
            FROM recording_sessions AS sessions
            LEFT JOIN recording_quality_reports AS quality
                ON quality.session_id = sessions.session_id
            WHERE sessions.session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            raise RecordingNotFound(f"unknown recording session: {session_id}")
        return row

    @staticmethod
    def _validate_references(
        connection: sqlite3.Connection,
        manifest: RecordingManifest,
    ) -> None:
        agent = connection.execute(
            "SELECT enabled FROM agent_registrations WHERE agent_id = ?",
            (manifest.agent_id,),
        ).fetchone()
        if agent is None or not bool(agent["enabled"]):
            raise RecordingInvalid("recording Agent must be registered and enabled")

        asset_ids = sorted({topic.asset_id for topic in manifest.topics})
        placeholders = ",".join("?" for _ in asset_ids)
        rows = connection.execute(
            f"SELECT * FROM assets WHERE asset_id IN ({placeholders})",
            asset_ids,
        ).fetchall()
        by_id = {row["asset_id"]: row for row in rows}
        missing = [asset_id for asset_id in asset_ids if asset_id not in by_id]
        if missing:
            raise RecordingInvalid(f"recording references unknown assets: {missing}")
        for asset_id in asset_ids:
            asset = by_id[asset_id]
            if asset["lifecycle_state"] != "active":
                raise RecordingInvalid(f"recording asset is decommissioned: {asset_id}")
            if asset["asset_type"] not in {"robot", "camera"}:
                raise RecordingInvalid(f"recording asset is not a robot or camera: {asset_id}")
            if (
                asset["agent_id"] != manifest.agent_id
                or asset["robot_id"] != manifest.robot_id
            ):
                raise RecordingInvalid(
                    f"recording asset binding does not match Agent/robot: {asset_id}"
                )

        if manifest.task_definition is not None:
            task = connection.execute(
                "SELECT 1 FROM task_definitions WHERE task_definition_id = ? AND version = ?",
                (manifest.task_definition.id, manifest.task_definition.version),
            ).fetchone()
            if task is None:
                raise RecordingInvalid("recording references an unknown Task Definition")

        if manifest.run_id is None:
            return
        run = connection.execute(
            "SELECT * FROM task_runs WHERE run_id = ?",
            (manifest.run_id,),
        ).fetchone()
        if run is None:
            raise RecordingInvalid("recording references an unknown Task Run")
        if run["state"] == "DRAFT":
            raise RecordingInvalid("DRAFT Task Runs cannot own recordings")
        if manifest.task_definition is None or (
            manifest.task_definition.id != run["task_definition_id"]
            or manifest.task_definition.version != run["task_version"]
        ):
            raise RecordingInvalid("recording Task Definition does not match its Task Run")
        assigned = {
            row["asset_id"]
            for row in connection.execute(
                "SELECT asset_id FROM assignments WHERE run_id = ?",
                (manifest.run_id,),
            ).fetchall()
        }
        unassigned = [asset_id for asset_id in asset_ids if asset_id not in assigned]
        if unassigned:
            raise RecordingInvalid(
                f"recording contains assets outside its Task Run: {unassigned}"
            )

    def register(
        self,
        manifest: RecordingManifest,
        *,
        object_store: FilesystemObjectStore,
        created_by: str | None,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        encoded = canonical_manifest(manifest)
        digest = hashlib.sha256(encoded).hexdigest()
        occurred = isoformat_z(occurred_at)
        prefix = object_store.session_prefix(manifest.session_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT manifest_sha256 FROM recording_sessions WHERE session_id = ?",
                (manifest.session_id,),
            ).fetchone()
            if existing is not None:
                if existing["manifest_sha256"] != digest:
                    raise RecordingConflict(
                        "recording session_id already has a different immutable manifest"
                    )
                return self._get_session(connection, manifest.session_id)
            self._validate_references(connection, manifest)
            capture_job = connection.execute(
                "SELECT * FROM capture_jobs WHERE session_id = ?",
                (manifest.session_id,),
            ).fetchone()
            if capture_job is not None:
                expected = (
                    capture_job["run_id"],
                    capture_job["agent_id"],
                    capture_job["robot_id"],
                    capture_job["profile_id"],
                )
                actual = (
                    manifest.run_id,
                    manifest.agent_id,
                    manifest.robot_id,
                    manifest.profile_id,
                )
                if actual != expected:
                    raise RecordingInvalid(
                        "recording manifest identity differs from its Capture Job"
                    )
                if capture_job["state"] not in {
                    "CLAIMED",
                    "RECORDING",
                    "FINALIZING",
                    "UPLOADING",
                }:
                    raise RecordingInvalid(
                        "Capture Job cannot accept a recording in its current state"
                    )
            task = manifest.task_definition
            source = manifest.source_time_range
            connection.execute(
                """
                INSERT INTO recording_sessions (
                    session_id, artifact_id, state, manifest_schema_version,
                    manifest_sha256, manifest_json, profile_id, agent_id, robot_id,
                    run_id, task_definition_id, task_definition_version,
                    started_at, finished_at, record_start_ns, record_end_ns,
                    source_start_ns, source_end_ns, total_size_bytes,
                    expected_file_count, completed_file_count, object_prefix,
                    created_by, created_at, updated_at, completed_at
                ) VALUES (?, ?, 'STAGING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, NULL)
                """,
                (
                    manifest.session_id,
                    manifest.artifact_id,
                    manifest.schema_version,
                    digest,
                    encoded.decode("utf-8"),
                    manifest.profile_id,
                    manifest.agent_id,
                    manifest.robot_id,
                    manifest.run_id,
                    None if task is None else task.id,
                    None if task is None else task.version,
                    manifest.started_at.isoformat(),
                    manifest.finished_at.isoformat(),
                    manifest.record_time.start_ns,
                    manifest.record_time.end_ns,
                    source.start_ns,
                    source.end_ns,
                    manifest.total_size_bytes,
                    len(manifest.files),
                    prefix,
                    created_by,
                    occurred,
                    occurred,
                ),
            )
            for item in manifest.files:
                connection.execute(
                    """
                    INSERT INTO recording_artifacts (
                        session_id, relative_path, object_key, media_type,
                        expected_size_bytes, expected_sha256, state, stored_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', NULL)
                    """,
                    (
                        manifest.session_id,
                        item.path,
                        object_store.artifact_key(manifest.session_id, item.path),
                        artifact_media_type(item.path),
                        item.size_bytes,
                        item.sha256,
                    ),
                )
            for topic in manifest.topics:
                connection.execute(
                    """
                    INSERT INTO recording_topics (
                        session_id, topic_key, topic_name, message_type, asset_id,
                        calibration_ref, timestamp_provenance, message_count,
                        source_start_ns, source_end_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        manifest.session_id,
                        topic.key,
                        topic.name,
                        topic.type,
                        topic.asset_id,
                        topic.calibration_ref,
                        topic.timestamp_provenance,
                        topic.message_count,
                        topic.source_timestamp.minimum_header_ns,
                        topic.source_timestamp.maximum_header_ns,
                    ),
                )
            if capture_job is not None and capture_job["state"] != "UPLOADING":
                connection.execute(
                    """
                    UPDATE capture_jobs
                    SET state = 'UPLOADING', revision = revision + 1,
                        updated_at = ?, last_worker_seen_at = ?
                    WHERE job_id = ?
                    """,
                    (occurred, occurred, capture_job["job_id"]),
                )
                connection.execute(
                    """
                    INSERT INTO capture_job_events (
                        job_id, occurred_at, actor_kind, actor_id,
                        event_type, from_state, to_state, details_json
                    ) VALUES (?, ?, 'central', 'recording-catalog',
                              'recording_registered', ?, 'UPLOADING', '{}')
                    """,
                    (
                        capture_job["job_id"],
                        occurred,
                        capture_job["state"],
                    ),
                )
            return self._get_session(connection, manifest.session_id)

    def artifact_expectation(self, session_id: str, relative_path: str) -> dict[str, Any]:
        relative_path = validate_artifact_path(relative_path)
        with self._connect() as connection:
            session = self._session_row(connection, session_id)
            if session["state"] != "STAGING":
                raise RecordingConflict("completed recording sessions are immutable")
            row = connection.execute(
                "SELECT * FROM recording_artifacts WHERE session_id = ? AND relative_path = ?",
                (session_id, relative_path),
            ).fetchone()
            if row is None:
                raise RecordingNotFound(
                    f"recording artifact is not declared by its manifest: {relative_path}"
                )
            return dict(row)

    def mark_uploaded(
        self,
        session_id: str,
        relative_path: str,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        occurred = isoformat_z(occurred_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._session_row(connection, session_id)
            row = connection.execute(
                "SELECT state FROM recording_artifacts WHERE session_id = ? AND relative_path = ?",
                (session_id, relative_path),
            ).fetchone()
            if row is None:
                raise RecordingNotFound(f"unknown recording artifact: {relative_path}")
            if row["state"] == "PENDING":
                connection.execute(
                    """
                    UPDATE recording_artifacts
                    SET state = 'COMPLETE', stored_at = ?
                    WHERE session_id = ? AND relative_path = ?
                    """,
                    (occurred, session_id, relative_path),
                )
                connection.execute(
                    """
                    UPDATE recording_sessions
                    SET completed_file_count = completed_file_count + 1,
                        updated_at = ?
                    WHERE session_id = ?
                    """,
                    (occurred, session_id),
                )
            return self._get_session(connection, session_id)

    async def finalize_async(
        self,
        session_id: str,
        *,
        expected_manifest_sha256: str,
        object_store: FilesystemObjectStore,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._session_row(connection, session_id)
            if row["manifest_sha256"] != expected_manifest_sha256:
                raise RecordingConflict("manifest revision guard does not match")
            if row["state"] == "COMPLETE":
                return self._get_session(connection, session_id)
            artifacts = connection.execute(
                "SELECT * FROM recording_artifacts WHERE session_id = ? ORDER BY relative_path",
                (session_id,),
            ).fetchall()
            pending = [item["relative_path"] for item in artifacts if item["state"] != "COMPLETE"]
            if pending:
                raise RecordingInvalid(f"recording artifacts are still pending: {pending}")
            manifest_payload = row["manifest_json"].encode("utf-8")
        for item in artifacts:
            await to_thread.run_sync(
                partial(
                    object_store.verify,
                    item["object_key"],
                    size_bytes=item["expected_size_bytes"],
                    sha256=item["expected_sha256"],
                )
            )
        await object_store.put_bytes(
            object_store.manifest_key(session_id),
            manifest_payload,
            sha256=expected_manifest_sha256,
        )
        occurred = isoformat_z(occurred_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._session_row(connection, session_id)
            if row["state"] == "STAGING":
                connection.execute(
                    """
                    UPDATE recording_sessions
                    SET state = 'COMPLETE', completed_at = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (occurred, occurred, session_id),
                )
                capture_job = connection.execute(
                    "SELECT * FROM capture_jobs WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if capture_job is not None and capture_job["state"] != "COMPLETE":
                    connection.execute(
                        """
                        UPDATE capture_jobs
                        SET state = 'COMPLETE', lease_id = NULL,
                            revision = revision + 1, updated_at = ?,
                            finished_at = ?, error = NULL
                        WHERE job_id = ?
                        """,
                        (occurred, occurred, capture_job["job_id"]),
                    )
                    connection.execute(
                        """
                        INSERT INTO capture_job_events (
                            job_id, occurred_at, actor_kind, actor_id,
                            event_type, from_state, to_state, details_json
                        ) VALUES (?, ?, 'central', 'recording-catalog',
                                  'recording_complete', ?, 'COMPLETE', '{}')
                        """,
                        (
                            capture_job["job_id"],
                            occurred,
                            capture_job["state"],
                        ),
                    )
            return self._get_session(connection, session_id)

    @staticmethod
    def _session_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "session_id": row["session_id"],
            "artifact_id": row["artifact_id"],
            "state": row["state"],
            "manifest_schema_version": row["manifest_schema_version"],
            "manifest_sha256": row["manifest_sha256"],
            "profile_id": row["profile_id"],
            "agent_id": row["agent_id"],
            "robot_id": row["robot_id"],
            "run_id": row["run_id"],
            "task_definition": (
                None
                if row["task_definition_id"] is None
                else {
                    "id": row["task_definition_id"],
                    "version": row["task_definition_version"],
                }
            ),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "record_time": {
                "start_ns": row["record_start_ns"],
                "end_ns": row["record_end_ns"],
            },
            "source_time_range": {
                "start_ns": row["source_start_ns"],
                "end_ns": row["source_end_ns"],
            },
            "total_size_bytes": row["total_size_bytes"],
            "expected_file_count": row["expected_file_count"],
            "completed_file_count": row["completed_file_count"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
            "quality": (
                None
                if "quality_state" not in row.keys() or row["quality_state"] is None
                else {
                    "state": row["quality_state"],
                    "report_sha256": row["quality_report_sha256"],
                    "clock_state": row["quality_clock_state"],
                    "failed_check_count": row["quality_failed_check_count"],
                    "warning_check_count": row["quality_warning_check_count"],
                    "analyzed_at": row["quality_analyzed_at"],
                }
            ),
        }

    @staticmethod
    def _get_session(connection: sqlite3.Connection, session_id: str) -> dict[str, Any]:
        row = RecordingCatalog._session_row(connection, session_id)
        artifacts = connection.execute(
            "SELECT * FROM recording_artifacts WHERE session_id = ? ORDER BY relative_path",
            (session_id,),
        ).fetchall()
        topics = connection.execute(
            "SELECT * FROM recording_topics WHERE session_id = ? ORDER BY topic_key",
            (session_id,),
        ).fetchall()
        return {
            **RecordingCatalog._session_summary(row),
            "artifacts": [
                {
                    "path": item["relative_path"],
                    "media_type": item["media_type"],
                    "size_bytes": item["expected_size_bytes"],
                    "sha256": item["expected_sha256"],
                    "state": item["state"],
                    "stored_at": item["stored_at"],
                    "download_url": (
                        f"/api/v1/recording-sessions/{session_id}/artifacts/"
                        f"{item['relative_path']}"
                        if row["state"] == "COMPLETE"
                        else None
                    ),
                }
                for item in artifacts
            ],
            "topics": [
                {
                    "key": item["topic_key"],
                    "name": item["topic_name"],
                    "type": item["message_type"],
                    "asset_id": item["asset_id"],
                    "calibration_ref": item["calibration_ref"],
                    "timestamp_provenance": item["timestamp_provenance"],
                    "message_count": item["message_count"],
                    "source_start_ns": item["source_start_ns"],
                    "source_end_ns": item["source_end_ns"],
                }
                for item in topics
            ],
        }

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            return self._get_session(connection, session_id)

    def manifest(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._session_row(connection, session_id)
            return json.loads(row["manifest_json"])

    def list_sessions(
        self,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("recording session limit must be between 1 and 500")
        clauses = []
        parameters: list[Any] = []
        for column, value in (
            ("sessions.run_id", run_id),
            ("sessions.agent_id", agent_id),
            ("sessions.state", state),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sessions.*, quality.state AS quality_state,
                       quality.report_sha256 AS quality_report_sha256,
                       quality.clock_state AS quality_clock_state,
                       quality.failed_check_count AS quality_failed_check_count,
                       quality.warning_check_count AS quality_warning_check_count,
                       quality.analyzed_at AS quality_analyzed_at
                FROM recording_sessions AS sessions
                LEFT JOIN recording_quality_reports AS quality
                    ON quality.session_id = sessions.session_id
                """
                + where
                + " ORDER BY sessions.started_at DESC, sessions.session_id LIMIT ?",
                parameters,
            ).fetchall()
            return [self._session_summary(row) for row in rows]

    @staticmethod
    def _quality_result(
        report: sqlite3.Row,
        streams: list[sqlite3.Row],
    ) -> dict[str, Any]:
        summary = json.loads(report["summary_json"])
        return {
            "available": True,
            "session_id": report["session_id"],
            "state": report["state"],
            "report_schema_version": report["report_schema_version"],
            "policy_sha256": report["policy_sha256"],
            "report_sha256": report["report_sha256"],
            "report_size_bytes": report["report_size_bytes"],
            "clock_state": report["clock_state"],
            "analyzed_at": report["analyzed_at"],
            "summary": summary,
            "report_url": (
                f"/api/v1/recording-sessions/{report['session_id']}/quality/report"
            ),
            "streams": [
                {
                    "key": item["topic_key"],
                    "topic": item["topic_name"],
                    "asset_id": item["asset_id"],
                    "kind": item["kind"],
                    "required": bool(item["required"]),
                    "state": item["state"],
                    "configured": {
                        "width": item["configured_width"],
                        "height": item["configured_height"],
                        "minimum_rate_hz": item["configured_minimum_rate_hz"],
                    },
                    "measured": {
                        "width": item["measured_width"],
                        "height": item["measured_height"],
                        "rate_hz": item["measured_rate_hz"],
                        "message_count": item["message_count"],
                        "timestamp_regressions": item["timestamp_regressions"],
                        "p95_gap_ms": item["p95_gap_ms"],
                        "max_gap_ms": item["max_gap_ms"],
                        "average_jpeg_bytes": item["average_jpeg_bytes"],
                        "p95_jpeg_bytes": item["p95_jpeg_bytes"],
                        "p95_joint_skew_ms": item["p95_joint_skew_ms"],
                        "max_joint_skew_ms": item["max_joint_skew_ms"],
                    },
                }
                for item in streams
            ],
        }

    def quality_report(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            self._session_row(connection, session_id)
            row = connection.execute(
                "SELECT * FROM recording_quality_reports WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            streams = connection.execute(
                """
                SELECT * FROM recording_quality_streams
                WHERE session_id = ? ORDER BY kind, topic_key
                """,
                (session_id,),
            ).fetchall()
            return self._quality_result(row, streams)

    def save_quality_report(
        self,
        report: dict[str, Any],
        *,
        object_key: str,
        report_sha256: str,
        report_size_bytes: int,
    ) -> dict[str, Any]:
        session_id = report["session_id"]
        summary = report["summary"]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self._session_row(connection, session_id)
            if session["state"] != "COMPLETE":
                raise RecordingConflict("quality reports require a complete session")
            existing = connection.execute(
                "SELECT * FROM recording_quality_reports WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing is not None:
                if existing["report_sha256"] != report_sha256:
                    raise RecordingConflict("recording quality report is immutable")
            else:
                clock_state = str(report.get("clock", {}).get("quality", "UNKNOWN")).upper()
                connection.execute(
                    """
                    INSERT INTO recording_quality_reports (
                        session_id, state, report_schema_version, policy_sha256,
                        report_sha256, report_size_bytes, object_key, clock_state,
                        stream_count, pass_stream_count, warning_stream_count,
                        fail_stream_count, failed_check_count, warning_check_count,
                        summary_json, analyzed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        report["state"],
                        report["schema_version"],
                        report["policy"]["sha256"],
                        report_sha256,
                        report_size_bytes,
                        object_key,
                        clock_state,
                        summary["stream_count"],
                        summary["pass_stream_count"],
                        summary["warning_stream_count"],
                        summary["fail_stream_count"],
                        summary["failed_check_count"],
                        summary["warning_check_count"],
                        json.dumps(summary, separators=(",", ":"), sort_keys=True),
                        report["analyzed_at"],
                    ),
                )
                for stream in report["streams"]:
                    configured = stream.get("configured", {})
                    measured = stream.get("measured", {})
                    gap = measured.get("source_gap") or {}
                    jpeg = measured.get("jpeg_bytes") or {}
                    skew = measured.get("nearest_camera_joint_skew") or {}
                    connection.execute(
                        """
                        INSERT INTO recording_quality_streams (
                            session_id, topic_key, topic_name, asset_id, kind,
                            required, state, configured_width, configured_height,
                            configured_minimum_rate_hz, measured_width,
                            measured_height, measured_rate_hz, message_count,
                            timestamp_regressions, p95_gap_ms, max_gap_ms,
                            average_jpeg_bytes, p95_jpeg_bytes,
                            p95_joint_skew_ms, max_joint_skew_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            stream["key"],
                            stream["topic"],
                            stream.get("asset_id"),
                            stream["kind"],
                            int(bool(configured.get("required"))),
                            stream["state"],
                            configured.get("width"),
                            configured.get("height"),
                            configured.get("minimum_rate_hz"),
                            measured.get("width"),
                            measured.get("height"),
                            float(measured.get("rate_hz") or 0.0),
                            int(measured.get("message_count") or 0),
                            int(measured.get("timestamp_regressions") or 0),
                            gap.get("p95_ms"),
                            gap.get("max_ms"),
                            jpeg.get("average"),
                            jpeg.get("p95"),
                            skew.get("p95_ms"),
                            skew.get("max_ms"),
                        ),
                    )
            row = connection.execute(
                "SELECT * FROM recording_quality_reports WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            streams = connection.execute(
                """
                SELECT * FROM recording_quality_streams
                WHERE session_id = ? ORDER BY kind, topic_key
                """,
                (session_id,),
            ).fetchall()
            return self._quality_result(row, streams)

    def quality_object(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recording_quality_reports WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise RecordingNotFound(f"quality report is not available: {session_id}")
            return dict(row)

    def artifact(self, session_id: str, relative_path: str) -> dict[str, Any]:
        relative_path = validate_artifact_path(relative_path)
        with self._connect() as connection:
            session = self._session_row(connection, session_id)
            if session["state"] != "COMPLETE":
                raise RecordingConflict("recording session is not complete")
            row = connection.execute(
                "SELECT * FROM recording_artifacts WHERE session_id = ? AND relative_path = ?",
                (session_id, relative_path),
            ).fetchone()
            if row is None:
                raise RecordingNotFound(f"unknown recording artifact: {relative_path}")
            return dict(row)
