"""Bounded, metadata-only inventory of local MCAP recording sessions."""

from __future__ import annotations

from datetime import datetime
from collections import Counter
import heapq
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Any


MANIFEST_NAME = "session-manifest.json"
MAX_MANIFEST_BYTES = 128 * 1024
MAX_RECENT_SESSIONS = 20
MAX_SCANNED_SESSIONS = 1_000
MAX_DISCOVERED_ENTRIES = 10_000
SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_ENTITY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def disabled_inventory() -> dict[str, Any]:
    return {
        "enabled": False,
        "state": "disabled",
        "error_code": None,
        "complete_count": 0,
        "failed_count": 0,
        "partial_count": 0,
        "invalid_count": 0,
        "scanned_count": 0,
        "total_size_bytes": 0,
        "truncated": False,
        "recent": [],
    }


def _degraded_inventory(error_code: str) -> dict[str, Any]:
    inventory = disabled_inventory()
    inventory.update(
        {
            "enabled": True,
            "state": "degraded",
            "error_code": error_code,
        }
    )
    return inventory


def _session_summary(session_id: str, state: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "state": state,
        "profile_id": None,
        "robot_id": None,
        "run_id": None,
        "task_definition_id": None,
        "task_definition_version": None,
        "started_at": None,
        "finished_at": None,
        "total_size_bytes": None,
        "topic_count": None,
        "message_count": None,
        "transport_messages_lost": None,
    }


def _aware_timestamp(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError(f"{field} must be a bounded timestamp string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value


def _bounded_id(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not SAFE_ENTITY_ID.fullmatch(value):
        raise ValueError(f"{field} is invalid")
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _read_manifest(
    session_path: Path,
    *,
    session_id: str,
    expected_agent_id: str,
    partial_directory: bool,
) -> dict[str, Any]:
    manifest_path = session_path / MANIFEST_NAME
    directory_descriptor = None
    manifest_descriptor = None
    try:
        directory_descriptor = os.open(
            session_path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            manifest_descriptor = os.open(
                MANIFEST_NAME,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError:
            return _session_summary(
                session_id,
                "partial" if partial_directory else "invalid",
            )
        manifest_stat = os.fstat(manifest_descriptor)
        if not stat.S_ISREG(manifest_stat.st_mode):
            raise ValueError("manifest is not a regular file")
        if manifest_stat.st_size <= 0 or manifest_stat.st_size > MAX_MANIFEST_BYTES:
            raise ValueError("manifest size is invalid")
        with os.fdopen(manifest_descriptor, "r", encoding="utf-8") as stream:
            manifest_descriptor = None
            encoded = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(encoded.encode("utf-8")) > MAX_MANIFEST_BYTES:
            raise ValueError("manifest grew beyond the size limit")
        payload = json.loads(encoded)
        if not isinstance(payload, dict) or payload.get("schema_version") != "1.0":
            raise ValueError("unsupported manifest")
        if payload.get("session_id") != session_id:
            raise ValueError("session identity mismatch")
        if payload.get("agent_id") != expected_agent_id:
            raise ValueError("Agent identity mismatch")
        state_value = payload.get("state")
        expected_state = "failed" if partial_directory else "complete"
        if state_value != expected_state:
            raise ValueError("directory and manifest state mismatch")

        summary = _session_summary(session_id, state_value)
        summary["profile_id"] = _bounded_id(payload.get("profile_id"), "profile_id")
        summary["robot_id"] = _bounded_id(payload.get("robot_id"), "robot_id")
        summary["run_id"] = _bounded_id(payload.get("run_id"), "run_id", optional=True)
        summary["started_at"] = _aware_timestamp(payload.get("started_at"), "started_at")
        summary["finished_at"] = _aware_timestamp(payload.get("finished_at"), "finished_at")
        if (
            summary["started_at"] is not None
            and summary["finished_at"] is not None
            and datetime.fromisoformat(
                summary["finished_at"].replace("Z", "+00:00")
            )
            < datetime.fromisoformat(
                summary["started_at"].replace("Z", "+00:00")
            )
        ):
            raise ValueError("finished_at precedes started_at")

        task_definition = payload.get("task_definition")
        if task_definition is not None:
            if not isinstance(task_definition, dict):
                raise ValueError("task_definition must be an object")
            summary["task_definition_id"] = _bounded_id(
                task_definition.get("id"), "task_definition.id"
            )
            version = _non_negative_int(
                task_definition.get("version"), "task_definition.version"
            )
            if version < 1:
                raise ValueError("task_definition.version must be at least one")
            summary["task_definition_version"] = version

        diagnostics = payload.get("recorder_diagnostics", {})
        if not isinstance(diagnostics, dict):
            raise ValueError("recorder_diagnostics must be an object")
        lost = diagnostics.get("transport_messages_lost")
        if lost is not None:
            summary["transport_messages_lost"] = _non_negative_int(
                lost, "transport_messages_lost"
            )

        if state_value == "complete":
            if payload.get("storage_identifier") != "mcap":
                raise ValueError("complete session must use MCAP")
            summary["total_size_bytes"] = _non_negative_int(
                payload.get("total_size_bytes"), "total_size_bytes"
            )
            topics = payload.get("topics")
            if not isinstance(topics, list) or not 1 <= len(topics) <= 128:
                raise ValueError("topics must be a bounded non-empty list")
            message_count = 0
            for topic in topics:
                if not isinstance(topic, dict):
                    raise ValueError("topic entry must be an object")
                message_count += _non_negative_int(
                    topic.get("message_count"), "topic.message_count"
                )
            summary["topic_count"] = len(topics)
            summary["message_count"] = message_count
        return summary
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return _session_summary(session_id, "invalid")
    finally:
        if manifest_descriptor is not None:
            os.close(manifest_descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)


def _candidate_sessions(root: Path, limit: int) -> tuple[list[tuple[int, str, bool]], bool]:
    newest: list[tuple[int, str, bool]] = []
    eligible_count = 0
    discovered_count = 0
    discovery_truncated = False
    with os.scandir(root) as entries:
        for entry in entries:
            discovered_count += 1
            if discovered_count > MAX_DISCOVERED_ENTRIES:
                discovery_truncated = True
                break
            if entry.name.startswith("."):
                continue
            partial = entry.name.endswith(".partial")
            session_id = entry.name.removesuffix(".partial") if partial else entry.name
            if not SAFE_SESSION_ID.fullmatch(session_id):
                continue
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            is_directory = stat.S_ISDIR(entry_stat.st_mode)
            is_symlink = stat.S_ISLNK(entry_stat.st_mode)
            if not is_directory and not is_symlink:
                continue
            eligible_count += 1
            candidate = (entry_stat.st_mtime_ns, entry.name, is_symlink)
            if len(newest) < limit:
                heapq.heappush(newest, candidate)
            elif candidate > newest[0]:
                heapq.heapreplace(newest, candidate)
    return (
        sorted(newest, reverse=True),
        discovery_truncated or eligible_count > limit,
    )


def _summary_time_ns(summary: dict[str, Any], fallback_ns: int) -> int:
    value = summary.get("finished_at") or summary.get("started_at")
    if not isinstance(value, str):
        return fallback_ns
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(parsed.timestamp() * 1_000_000_000)
    except (OverflowError, ValueError):
        return fallback_ns


def scan_recording_inventory(
    capture_root: str | Path,
    *,
    agent_id: str,
    recent_limit: int = MAX_RECENT_SESSIONS,
    scan_limit: int = MAX_SCANNED_SESSIONS,
) -> dict[str, Any]:
    if not 1 <= recent_limit <= MAX_RECENT_SESSIONS:
        raise ValueError(f"recent_limit must be between 1 and {MAX_RECENT_SESSIONS}")
    if not 1 <= scan_limit <= MAX_SCANNED_SESSIONS:
        raise ValueError(f"scan_limit must be between 1 and {MAX_SCANNED_SESSIONS}")
    root = Path(capture_root)
    try:
        if root.is_symlink() or not root.is_dir():
            return _degraded_inventory("capture_root_unavailable")
        candidates, truncated = _candidate_sessions(root, scan_limit)
    except OSError:
        return _degraded_inventory("capture_root_unreadable")

    ordered_summaries = []
    session_id_counts = Counter(
        directory_name.removesuffix(".partial")
        if directory_name.endswith(".partial")
        else directory_name
        for _, directory_name, _ in candidates
    )
    for modified_ns, directory_name, is_symlink in candidates:
        partial = directory_name.endswith(".partial")
        session_id = directory_name.removesuffix(".partial") if partial else directory_name
        if is_symlink or session_id_counts[session_id] > 1:
            summary = _session_summary(session_id, "invalid")
        else:
            summary = _read_manifest(
                root / directory_name,
                session_id=session_id,
                expected_agent_id=agent_id,
                partial_directory=partial,
            )
        ordered_summaries.append(
            (_summary_time_ns(summary, modified_ns), session_id, summary)
        )
    summaries = [
        item[2] for item in sorted(ordered_summaries, reverse=True)
    ]
    recent = []
    recent_ids = set()
    for summary in summaries:
        if summary["session_id"] in recent_ids:
            continue
        recent.append(summary)
        recent_ids.add(summary["session_id"])
        if len(recent) == recent_limit:
            break

    counts = {state: 0 for state in ("complete", "failed", "partial", "invalid")}
    for summary in summaries:
        counts[summary["state"]] += 1
    return {
        "enabled": True,
        "state": "degraded" if counts["invalid"] else "ready",
        "error_code": "invalid_session_metadata" if counts["invalid"] else None,
        "complete_count": counts["complete"],
        "failed_count": counts["failed"],
        "partial_count": counts["partial"],
        "invalid_count": counts["invalid"],
        "scanned_count": len(summaries),
        "total_size_bytes": sum(
            summary["total_size_bytes"] or 0 for summary in summaries
        ),
        "truncated": truncated,
        "recent": recent,
    }


class RecordingInventory:
    def __init__(
        self,
        capture_root: str | Path | None,
        *,
        agent_id: str,
        refresh_interval_s: float = 10.0,
    ) -> None:
        if refresh_interval_s <= 0:
            raise ValueError("refresh_interval_s must be positive")
        self.capture_root = None if capture_root is None else Path(capture_root)
        self.agent_id = agent_id
        self.refresh_interval_s = refresh_interval_s
        self._last_scan_monotonic: float | None = None
        self._cached = disabled_inventory()

    def sample(self, *, now_monotonic: float | None = None) -> dict[str, Any]:
        if self.capture_root is None:
            return self._cached
        now = time.monotonic() if now_monotonic is None else now_monotonic
        if (
            self._last_scan_monotonic is not None
            and now - self._last_scan_monotonic < self.refresh_interval_s
        ):
            return self._cached
        try:
            self._cached = scan_recording_inventory(
                self.capture_root,
                agent_id=self.agent_id,
            )
        except Exception:
            self._cached = _degraded_inventory("inventory_scan_failed")
        self._last_scan_monotonic = now
        return self._cached
