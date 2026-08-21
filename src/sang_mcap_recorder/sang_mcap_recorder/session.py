"""MCAP inspection and atomic session finalization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
from typing import Any, Callable

import yaml

from .profile import CaptureProfile, IDENTIFIER_PATTERN


MANIFEST_NAME = "session-manifest.json"


class SessionError(RuntimeError):
    """Raised when a session cannot be safely recorded or finalized."""


@dataclass(frozen=True)
class CaptureContext:
    session_id: str
    run_id: str | None = None
    task_definition_id: str | None = None
    task_definition_version: int | None = None


@dataclass(frozen=True)
class SessionPaths:
    root: Path
    staging: Path
    final: Path


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp_ns_iso(value: int) -> str:
    seconds, nanoseconds = divmod(value, 1_000_000_000)
    base = datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{nanoseconds:09d}Z"


def validate_context(context: CaptureContext) -> None:
    for label, value in (
        ("session_id", context.session_id),
        ("run_id", context.run_id),
        ("task_definition_id", context.task_definition_id),
    ):
        if value is not None and not IDENTIFIER_PATTERN.fullmatch(value):
            raise SessionError(f"{label} is not a safe identifier")
    if (context.task_definition_id is None) != (
        context.task_definition_version is None
    ):
        raise SessionError(
            "task_definition_id and task_definition_version must be provided together"
        )
    if context.task_definition_version is not None and context.task_definition_version < 1:
        raise SessionError("task_definition_version must be at least 1")


def prepare_paths(root: str | Path, context: CaptureContext) -> SessionPaths:
    validate_context(context)
    capture_root = Path(root).expanduser().resolve()
    capture_root.mkdir(parents=True, exist_ok=True)
    staging = capture_root / f"{context.session_id}.partial"
    final = capture_root / context.session_id
    if staging.exists():
        raise SessionError(f"partial session already exists: {staging}")
    if final.exists():
        raise SessionError(f"final session already exists: {final}")
    return SessionPaths(root=capture_root, staging=staging, final=final)


def _read_metadata(staging: Path) -> dict[str, Any]:
    metadata_path = staging / "metadata.yaml"
    try:
        raw = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
        info = raw["rosbag2_bagfile_information"]
    except (OSError, yaml.YAMLError, KeyError, TypeError) as exc:
        raise SessionError(f"invalid ROS bag metadata: {exc}") from exc
    if not isinstance(info, dict):
        raise SessionError("ROS bag metadata information must be a mapping")
    return info


def _safe_artifact_path(staging: Path, relative_path: str) -> Path:
    unresolved = staging / relative_path
    if unresolved.is_symlink():
        raise SessionError(f"artifact may not be a symbolic link: {relative_path}")
    candidate = unresolved.resolve()
    try:
        candidate.relative_to(staging.resolve())
    except ValueError as exc:
        raise SessionError(f"artifact path escapes session: {relative_path}") from exc
    if not candidate.is_file():
        raise SessionError(f"artifact is not a regular file: {relative_path}")
    return candidate


def _topic_metadata(info: dict[str, Any]) -> dict[str, dict[str, Any]]:
    topics: dict[str, dict[str, Any]] = {}
    raw_topics = info.get("topics_with_message_count")
    if not isinstance(raw_topics, list):
        raise SessionError("ROS bag metadata has no topic list")
    for item in raw_topics:
        try:
            metadata = item["topic_metadata"]
            name = metadata["name"]
            message_type = metadata["type"]
            message_count = int(item["message_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionError(f"malformed topic metadata: {exc}") from exc
        if not isinstance(name, str) or not isinstance(message_type, str):
            raise SessionError("topic name and type must be strings")
        if name in topics:
            raise SessionError(f"duplicate topic metadata: {name}")
        topics[name] = {"type": message_type, "message_count": message_count}
    return topics


def validate_metadata(
    staging: Path, profile: CaptureProfile
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[Path]]:
    info = _read_metadata(staging)
    if info.get("storage_identifier") != "mcap":
        raise SessionError("ROS bag storage_identifier must be mcap")

    actual_topics = _topic_metadata(info)
    expected_names = {topic.name for topic in profile.topics}
    if set(actual_topics) != expected_names:
        missing = sorted(expected_names - set(actual_topics))
        unexpected = sorted(set(actual_topics) - expected_names)
        raise SessionError(
            f"recorded topic set mismatch; missing={missing}, unexpected={unexpected}"
        )
    for topic in profile.topics:
        actual = actual_topics[topic.name]
        if actual["type"] != topic.message_type:
            raise SessionError(
                f"{topic.name} type is {actual['type']}, expected {topic.message_type}"
            )
        if actual["message_count"] < 1:
            raise SessionError(f"{topic.name} recorded no messages")
    try:
        total_message_count = int(info["message_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SessionError(f"invalid total message count: {exc}") from exc
    if total_message_count != sum(
        topic["message_count"] for topic in actual_topics.values()
    ):
        raise SessionError("total message count does not match per-topic counts")

    relative_paths = info.get("relative_file_paths")
    if not isinstance(relative_paths, list) or not relative_paths:
        raise SessionError("ROS bag metadata has no MCAP files")
    artifact_paths = [_safe_artifact_path(staging, "metadata.yaml")]
    artifact_paths.extend(
        _safe_artifact_path(staging, relative_path)
        for relative_path in relative_paths
        if isinstance(relative_path, str)
    )
    if len(artifact_paths) != len(relative_paths) + 1:
        raise SessionError("ROS bag relative file paths must be strings")
    return info, actual_topics, artifact_paths


def inspect_header_timestamps(staging: Path) -> dict[str, dict[str, int | None]]:
    try:
        import rosbag2_py
        from rclpy.serialization import deserialize_message
        from rosidl_runtime_py.utilities import get_message
    except ImportError as exc:
        raise SessionError(f"ROS bag inspection dependencies are unavailable: {exc}") from exc

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(staging), storage_id="mcap"),
        rosbag2_py.ConverterOptions("", ""),
    )
    topic_types = {
        item.name: item.type for item in reader.get_all_topics_and_types()
    }
    message_classes = {name: get_message(value) for name, value in topic_types.items()}
    stats = {
        name: {
            "message_count": 0,
            "valid_header_count": 0,
            "zero_header_count": 0,
            "timestamp_regressions": 0,
            "first_header_ns": None,
            "last_header_ns": None,
            "minimum_header_ns": None,
            "maximum_header_ns": None,
        }
        for name in topic_types
    }
    previous: dict[str, int] = {}
    while reader.has_next():
        topic_name, serialized, _record_time_ns = reader.read_next()
        message = deserialize_message(serialized, message_classes[topic_name])
        topic_stats = stats[topic_name]
        topic_stats["message_count"] += 1
        header = getattr(message, "header", None)
        stamp = getattr(header, "stamp", None)
        header_ns = (
            int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
            if stamp is not None
            else 0
        )
        if header_ns <= 0:
            topic_stats["zero_header_count"] += 1
            continue
        topic_stats["valid_header_count"] += 1
        if topic_stats["first_header_ns"] is None:
            topic_stats["first_header_ns"] = header_ns
        topic_stats["last_header_ns"] = header_ns
        minimum = topic_stats["minimum_header_ns"]
        maximum = topic_stats["maximum_header_ns"]
        topic_stats["minimum_header_ns"] = header_ns if minimum is None else min(minimum, header_ns)
        topic_stats["maximum_header_ns"] = header_ns if maximum is None else max(maximum, header_ns)
        if topic_name in previous and header_ns < previous[topic_name]:
            topic_stats["timestamp_regressions"] += 1
        previous[topic_name] = header_ns
    return stats


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_entries(staging: Path, paths: list[Path]) -> list[dict[str, Any]]:
    entries = []
    for path in paths:
        _fsync_file(path)
        entries.append(
            {
                "path": path.relative_to(staging).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    _fsync_directory(staging)
    return entries


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _record_time(info: dict[str, Any]) -> dict[str, Any]:
    try:
        start_ns = int(info["starting_time"]["nanoseconds_since_epoch"])
        duration_ns = int(info["duration"]["nanoseconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SessionError(f"invalid ROS bag record time: {exc}") from exc
    return {
        "start_ns": start_ns,
        "start": timestamp_ns_iso(start_ns),
        "end_ns": start_ns + duration_ns,
        "end": timestamp_ns_iso(start_ns + duration_ns),
        "duration_ns": duration_ns,
    }


def _source_time_range(header_stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    minimums = [
        int(stats["minimum_header_ns"])
        for stats in header_stats.values()
        if stats["minimum_header_ns"] is not None
    ]
    maximums = [
        int(stats["maximum_header_ns"])
        for stats in header_stats.values()
        if stats["maximum_header_ns"] is not None
    ]
    if not minimums or not maximums:
        return {"start_ns": None, "start": None, "end_ns": None, "end": None}
    start_ns = min(minimums)
    end_ns = max(maximums)
    return {
        "start_ns": start_ns,
        "start": timestamp_ns_iso(start_ns),
        "end_ns": end_ns,
        "end": timestamp_ns_iso(end_ns),
    }


def finalize_session(
    *,
    paths: SessionPaths,
    profile: CaptureProfile,
    context: CaptureContext,
    started_at: datetime,
    finished_at: datetime,
    header_inspector: Callable[[Path], dict[str, dict[str, Any]]] = inspect_header_timestamps,
    extra_artifact_names: tuple[str, ...] = (),
    recorder_diagnostics: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    info, actual_topics, artifact_paths = validate_metadata(paths.staging, profile)
    for artifact_name in extra_artifact_names:
        artifact_path = _safe_artifact_path(paths.staging, artifact_name)
        if artifact_path not in artifact_paths:
            artifact_paths.append(artifact_path)
    header_stats = header_inspector(paths.staging)
    expected_names = {topic.name for topic in profile.topics}
    if set(header_stats) != expected_names:
        raise SessionError("inspected topic set does not match the capture profile")

    topic_entries = []
    for topic in profile.topics:
        stats = header_stats[topic.name]
        if int(stats["message_count"]) != actual_topics[topic.name]["message_count"]:
            raise SessionError(f"{topic.name} inspected message count does not match metadata")
        if topic.require_header_timestamp:
            if int(stats["valid_header_count"]) < 1:
                raise SessionError(f"{topic.name} has no valid source header timestamps")
            if int(stats["zero_header_count"]) > 0:
                raise SessionError(f"{topic.name} contains zero source header timestamps")
            if int(stats["timestamp_regressions"]) > 0:
                raise SessionError(f"{topic.name} contains source timestamp regressions")
        topic_entries.append(
            {
                "key": topic.key,
                "name": topic.name,
                "type": topic.message_type,
                "asset_id": topic.asset_id,
                "calibration_ref": topic.calibration_ref,
                "timestamp_provenance": topic.timestamp_provenance,
                "message_count": actual_topics[topic.name]["message_count"],
                "source_timestamp": stats,
            }
        )

    file_entries = _file_entries(paths.staging, artifact_paths)
    manifest = {
        "schema_version": "1.0",
        "artifact_id": f"mcap:{context.session_id}",
        "session_id": context.session_id,
        "state": "complete",
        "storage_identifier": "mcap",
        "profile_id": profile.profile_id,
        "agent_id": profile.agent_id,
        "robot_id": profile.robot_id,
        "run_id": context.run_id,
        "task_definition": (
            {
                "id": context.task_definition_id,
                "version": context.task_definition_version,
            }
            if context.task_definition_id is not None
            else None
        ),
        "started_at": utc_iso(started_at),
        "finished_at": utc_iso(finished_at),
        "record_time": _record_time(info),
        "source_time_range": _source_time_range(header_stats),
        "clock": profile.clock,
        "topics": topic_entries,
        "files": file_entries,
        "total_size_bytes": sum(entry["size_bytes"] for entry in file_entries),
        "checksum_algorithm": "sha256",
        "parent_artifacts": [],
        "provenance": {
            "host": platform.node(),
            "ros_domain_id": os.environ.get("ROS_DOMAIN_ID"),
            "image_payload_recorded": any(
                topic.message_type
                in ("sensor_msgs/msg/CompressedImage", "sensor_msgs/msg/Image")
                for topic in profile.topics
            ),
            "image_decode_or_reencode": False,
            "live_preview_recorded": False,
        },
        "recorder_diagnostics": recorder_diagnostics or {},
        "safety": {
            "read_only_observation": True,
            "robot_command_interfaces": False,
            "vendor_command_channel": False,
        },
    }
    _atomic_json(paths.staging / MANIFEST_NAME, manifest)
    if paths.final.exists():
        raise SessionError(f"final session appeared during capture: {paths.final}")
    try:
        os.rename(paths.staging, paths.final)
        _fsync_directory(paths.root)
    except OSError as exc:
        raise SessionError(f"cannot atomically finalize session: {exc}") from exc
    return manifest, paths.final / MANIFEST_NAME


def write_failure_manifest(
    *,
    paths: SessionPaths,
    profile: CaptureProfile,
    context: CaptureContext,
    started_at: datetime,
    finished_at: datetime,
    reason: str,
    recorder_diagnostics: dict[str, Any] | None = None,
) -> Path:
    paths.staging.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "artifact_id": f"mcap:{context.session_id}",
        "session_id": context.session_id,
        "state": "failed",
        "profile_id": profile.profile_id,
        "agent_id": profile.agent_id,
        "robot_id": profile.robot_id,
        "started_at": utc_iso(started_at),
        "finished_at": utc_iso(finished_at),
        "failure": {"reason": reason},
        "recorder_diagnostics": recorder_diagnostics or {},
        "partial_artifacts_retained": True,
        "safety": {
            "read_only_observation": True,
            "robot_command_interfaces": False,
            "vendor_command_channel": False,
        },
    }
    path = paths.staging / MANIFEST_NAME
    _atomic_json(path, payload)
    return path
