"""Observation-only MCAP to LeRobot v3 derivation and provenance catalog."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import sys
import tarfile
import tempfile
from typing import Any, Callable, Iterator, Protocol

from mcap.exceptions import McapError
from mcap.reader import SeekingReader
import yaml

from fleet_control_plane.recording_playback import (
    MAX_MCAP_RECORD_BYTES,
    PlaybackInvalid,
    decode_compressed_image,
)
from fleet_control_plane.recording_storage import FilesystemObjectStore, RecordingCatalog
from fleet_control_plane.status import isoformat_z


DERIVATION_SCHEMA_VERSION = "1.0"
DERIVATION_CONTRACT_VERSION = "observation-v1"
LEROBOT_PACKAGE_VERSION = "0.4.3"
LEROBOT_CODEBASE_VERSION = "v3.0"
LEROBOT_SOURCE_COMMIT = "b0c3f2291b3f7753fd530172ec4bca0ed0a0d8ce"
LEROBOT_DATASET_SHA256 = "3fc9731a29e4395215d882cc6c052b4f98866d3f583fa769451a71218cb5a45b"
LEROBOT_UTILS_SHA256 = "b1d32074e843880ef81436d7357bd1ac7b8d26ad9d30a0594c38620d36fae692"
MAX_DERIVATION_DURATION_S = 1_800
MAX_DERIVATION_FPS = 60
MAX_SAMPLE_SKEW_NS = 50_000_000
JOINT_STATE_TYPE = "sensor_msgs/msg/JointState"
COMPRESSED_IMAGE_TYPE = "sensor_msgs/msg/CompressedImage"


DERIVATION_MIGRATION = """
CREATE TABLE IF NOT EXISTS dataset_derivations (
    derivation_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL UNIQUE CHECK (length(request_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN ('RUNNING', 'COMPLETE', 'FAILED')),
    session_id TEXT NOT NULL REFERENCES recording_sessions(session_id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES task_runs(run_id) ON DELETE RESTRICT,
    robot_id TEXT NOT NULL,
    task_text TEXT NOT NULL,
    fps INTEGER NOT NULL CHECK (fps BETWEEN 1 AND 60),
    requested_start_offset_ms INTEGER NOT NULL CHECK (requested_start_offset_ms >= 0),
    requested_end_offset_ms INTEGER NOT NULL,
    target_start_ns INTEGER NOT NULL,
    target_end_ns INTEGER NOT NULL CHECK (target_end_ns > target_start_ns),
    state_topic_key TEXT NOT NULL,
    camera_topic_keys_json TEXT NOT NULL,
    request_json TEXT NOT NULL,
    source_manifest_sha256 TEXT NOT NULL CHECK (length(source_manifest_sha256) = 64),
    quality_report_sha256 TEXT NOT NULL CHECK (length(quality_report_sha256) = 64),
    robot_manifest_sha256 TEXT NOT NULL CHECK (length(robot_manifest_sha256) = 64),
    converter_contract_version TEXT NOT NULL,
    lerobot_version TEXT NOT NULL,
    lerobot_codebase_version TEXT NOT NULL,
    frame_count INTEGER CHECK (frame_count >= 1),
    episode_count INTEGER CHECK (episode_count = 1),
    output_object_key TEXT UNIQUE,
    output_size_bytes INTEGER CHECK (output_size_bytes >= 1),
    output_sha256 TEXT UNIQUE CHECK (output_sha256 IS NULL OR length(output_sha256) = 64),
    manifest_object_key TEXT UNIQUE,
    manifest_size_bytes INTEGER CHECK (manifest_size_bytes >= 1),
    manifest_sha256 TEXT UNIQUE CHECK (manifest_sha256 IS NULL OR length(manifest_sha256) = 64),
    error TEXT,
    created_by TEXT REFERENCES principals(principal_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    CHECK (
        (state = 'RUNNING' AND frame_count IS NULL AND output_object_key IS NULL
            AND completed_at IS NULL AND error IS NULL)
        OR (state = 'COMPLETE' AND frame_count >= 1 AND episode_count = 1
            AND output_object_key IS NOT NULL AND output_size_bytes >= 1
            AND output_sha256 IS NOT NULL AND manifest_object_key IS NOT NULL
            AND manifest_size_bytes >= 1 AND manifest_sha256 IS NOT NULL
            AND completed_at IS NOT NULL AND error IS NULL)
        OR (state = 'FAILED' AND completed_at IS NOT NULL AND error IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS dataset_derivations_session_created
    ON dataset_derivations (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS dataset_derivations_run_created
    ON dataset_derivations (run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS dataset_derivations_state_created
    ON dataset_derivations (state, created_at DESC);
"""


class DerivationError(RuntimeError):
    """A derivation request, source stream, or output is invalid."""


class DerivationConflict(DerivationError):
    """An idempotent request is already running or conflicts with stored state."""


class DerivationNotFound(DerivationError):
    """A requested derivation does not exist."""


def canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> tuple[int, str]:
    if path.is_symlink() or not path.is_file():
        raise DerivationError(f"derivation input is not a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
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


@dataclass(frozen=True)
class StreamContract:
    key: str
    topic: str
    message_type: str
    asset_id: str
    calibration_ref: str
    timestamp_provenance: str
    source_start_ns: int
    source_end_ns: int
    shape: tuple[int, ...]
    names: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "topic": self.topic,
            "message_type": self.message_type,
            "asset_id": self.asset_id,
            "calibration_ref": self.calibration_ref,
            "timestamp_provenance": self.timestamp_provenance,
            "source_start_ns": self.source_start_ns,
            "source_end_ns": self.source_end_ns,
            "shape": list(self.shape),
            "names": list(self.names),
        }


@dataclass(frozen=True)
class McapObject:
    path: Path
    object_key: str
    size_bytes: int
    sha256: str
    start_time_ns: int
    end_time_ns: int

    def provenance(self) -> dict[str, Any]:
        return {
            "object_key": self.object_key,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "log_start_ns": self.start_time_ns,
            "log_end_ns": self.end_time_ns,
        }


@dataclass(frozen=True)
class PreparedDerivation:
    derivation_id: str
    request_sha256: str
    session_id: str
    run_id: str | None
    robot_id: str
    task_text: str
    fps: int
    requested_start_offset_ms: int
    requested_end_offset_ms: int
    target_start_ns: int
    target_end_ns: int
    state: StreamContract
    cameras: tuple[StreamContract, ...]
    source_manifest_sha256: str
    quality_report_sha256: str
    robot_manifest_sha256: str
    source_objects: tuple[McapObject, ...]
    request: dict[str, Any]

    @property
    def selected_streams(self) -> tuple[StreamContract, ...]:
        return (self.state, *self.cameras)

    @property
    def expected_frame_count(self) -> int:
        duration_ns = self.target_end_ns - self.target_start_ns
        return math.floor(duration_ns * self.fps / 1_000_000_000) + 1


@dataclass(frozen=True)
class JointObservation:
    source_time_ns: int
    log_time_ns: int
    names: tuple[str, ...]
    positions: tuple[float, ...]


@dataclass(frozen=True)
class CameraObservation:
    source_time_ns: int
    log_time_ns: int
    jpeg: bytes


Observation = JointObservation | CameraObservation


@dataclass(frozen=True)
class SampledFrame:
    target_time_ns: int
    observations: dict[str, Observation]


class _CdrReader:
    def __init__(self, payload: bytes) -> None:
        if len(payload) < 4:
            raise DerivationError("JointState CDR payload is truncated")
        kind = int.from_bytes(payload[:2], byteorder="big")
        if kind == 0:
            self.endian = ">"
        elif kind == 1:
            self.endian = "<"
        else:
            raise DerivationError("JointState uses an unsupported CDR encoding")
        self.payload = payload
        self.offset = 4
        self.origin = 4

    def _align(self, size: int) -> None:
        relative = self.offset - self.origin
        self.offset = self.origin + ((relative + size - 1) // size) * size

    def _read(self, size: int) -> bytes:
        end = self.offset + size
        if end > len(self.payload):
            raise DerivationError("JointState CDR payload is truncated")
        value = self.payload[self.offset:end]
        self.offset = end
        return value

    def int32(self) -> int:
        self._align(4)
        return struct.unpack(f"{self.endian}i", self._read(4))[0]

    def uint32(self) -> int:
        self._align(4)
        return struct.unpack(f"{self.endian}I", self._read(4))[0]

    def string(self, *, maximum_bytes: int = 4096) -> str:
        length = self.uint32()
        if not 1 <= length <= maximum_bytes:
            raise DerivationError("JointState CDR string length is invalid")
        raw = self._read(length)
        if raw[-1] != 0:
            raise DerivationError("JointState CDR string is not terminated")
        try:
            return raw[:-1].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DerivationError("JointState CDR string is not UTF-8") from exc

    def string_sequence(self, *, maximum_items: int = 256) -> tuple[str, ...]:
        length = self.uint32()
        if length > maximum_items:
            raise DerivationError("JointState name sequence is too large")
        return tuple(self.string() for _ in range(length))

    def float64_sequence(self, *, maximum_items: int = 256) -> tuple[float, ...]:
        length = self.uint32()
        if length > maximum_items:
            raise DerivationError("JointState numeric sequence is too large")
        if not length:
            return ()
        values = struct.unpack(f"{self.endian}{length}d", self._read(length * 8))
        if not all(math.isfinite(value) for value in values):
            raise DerivationError("JointState contains a non-finite position")
        return tuple(values)


def decode_joint_state(payload: bytes, *, log_time_ns: int) -> JointObservation:
    reader = _CdrReader(payload)
    seconds = reader.int32()
    nanoseconds = reader.uint32()
    if seconds < 0 or nanoseconds >= 1_000_000_000:
        raise DerivationError("JointState source timestamp is invalid")
    reader.string()
    names = reader.string_sequence()
    positions = reader.float64_sequence()
    reader.float64_sequence()
    reader.float64_sequence()
    if not names or len(names) != len(positions) or len(set(names)) != len(names):
        raise DerivationError("JointState names and positions do not form a unique vector")
    return JointObservation(
        source_time_ns=seconds * 1_000_000_000 + nanoseconds,
        log_time_ns=log_time_ns,
        names=names,
        positions=positions,
    )


def _load_robot_manifest(path: Path) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        raise DerivationError("robot manifest must be a regular file")
    payload = path.read_bytes()
    try:
        manifest = yaml.safe_load(payload)
    except yaml.YAMLError as exc:
        raise DerivationError(f"robot manifest is invalid YAML: {exc}") from exc
    if not isinstance(manifest, dict):
        raise DerivationError("robot manifest must contain an object")
    return manifest, sha256_bytes(payload)


def _stream_contracts(
    session: dict[str, Any],
    robot_manifest: dict[str, Any],
    state_topic_key: str,
    camera_topic_keys: tuple[str, ...],
) -> tuple[StreamContract, tuple[StreamContract, ...]]:
    topics = {item["key"]: item for item in session["topics"]}
    selected_keys = (state_topic_key, *camera_topic_keys)
    missing = [key for key in selected_keys if key not in topics]
    if missing:
        raise DerivationError(f"selected recording topics do not exist: {missing}")
    if topics[state_topic_key]["type"] != JOINT_STATE_TYPE:
        raise DerivationError("selected state topic is not sensor_msgs/msg/JointState")
    for key in camera_topic_keys:
        if topics[key]["type"] != COMPRESSED_IMAGE_TYPE:
            raise DerivationError(f"selected camera is not CompressedImage: {key}")

    if robot_manifest.get("robot", {}).get("robot_id") != session["robot_id"]:
        raise DerivationError("robot manifest robot_id differs from the recording session")
    interfaces = robot_manifest.get("interfaces")
    if not isinstance(interfaces, dict):
        raise DerivationError("robot manifest interfaces are missing")
    state_entries = interfaces.get("state") or []
    state_entry = next(
        (
            item
            for item in state_entries
            if topics[state_topic_key]["name"] in (item.get("topics") or [])
        ),
        None,
    )
    if state_entry is None:
        raise DerivationError("robot manifest does not describe the selected state topic")
    names = tuple(state_entry.get("feature_names") or ())
    shape = tuple(state_entry.get("shape") or ())
    if shape != (len(names),) or not names or len(set(names)) != len(names):
        raise DerivationError("robot manifest state feature names/shape are invalid")

    camera_entries = {item.get("key"): item for item in interfaces.get("cameras") or []}

    def common(topic: dict[str, Any]) -> dict[str, Any]:
        start = topic.get("source_start_ns")
        end = topic.get("source_end_ns")
        if not isinstance(start, int) or not isinstance(end, int) or end <= start:
            raise DerivationError(f"selected topic has no valid source-time range: {topic['key']}")
        return {
            "key": topic["key"],
            "topic": topic["name"],
            "message_type": topic["type"],
            "asset_id": topic["asset_id"],
            "calibration_ref": topic["calibration_ref"],
            "timestamp_provenance": topic["timestamp_provenance"],
            "source_start_ns": start,
            "source_end_ns": end,
        }

    state_contract = StreamContract(
        **common(topics[state_topic_key]),
        shape=shape,
        names=names,
    )
    camera_contracts = []
    for key in camera_topic_keys:
        entry = camera_entries.get(key)
        if entry is None or entry.get("topic") != topics[key]["name"]:
            raise DerivationError(f"robot manifest does not describe selected camera: {key}")
        width = entry.get("width")
        height = entry.get("height")
        if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
            raise DerivationError(f"robot manifest camera shape is invalid: {key}")
        camera_contracts.append(
            StreamContract(
                **common(topics[key]),
                shape=(height, width, 3),
                names=("height", "width", "channels"),
            )
        )
    return state_contract, tuple(camera_contracts)


def _mcap_objects(
    session: dict[str, Any],
    recordings: RecordingCatalog,
    object_store: FilesystemObjectStore,
) -> tuple[McapObject, ...]:
    sources = []
    for item in session["artifacts"]:
        if item["state"] != "COMPLETE" or not item["path"].endswith(".mcap"):
            continue
        artifact = recordings.artifact(session["session_id"], item["path"])
        path = object_store.open_path(
            artifact["object_key"],
            size_bytes=artifact["expected_size_bytes"],
            sha256=artifact["expected_sha256"],
        )
        try:
            with path.open("rb") as stream:
                summary = SeekingReader(
                    stream,
                    record_size_limit=MAX_MCAP_RECORD_BYTES,
                ).get_summary()
        except (McapError, OSError, ValueError) as exc:
            raise DerivationError("source MCAP seek index cannot be read") from exc
        if summary is None or summary.statistics is None:
            raise DerivationError("source MCAP has no seek index")
        sources.append(
            McapObject(
                path=path,
                object_key=artifact["object_key"],
                size_bytes=artifact["expected_size_bytes"],
                sha256=artifact["expected_sha256"],
                start_time_ns=summary.statistics.message_start_time,
                end_time_ns=summary.statistics.message_end_time,
            )
        )
    if not sources:
        raise DerivationError("recording session has no complete MCAP object")
    return tuple(sorted(sources, key=lambda item: (item.start_time_ns, item.object_key)))


def prepare_derivation(
    *,
    recordings: RecordingCatalog,
    object_store: FilesystemObjectStore,
    session_id: str,
    robot_manifest_path: Path,
    state_topic_key: str,
    camera_topic_keys: list[str],
    start_offset_ms: int,
    end_offset_ms: int,
    fps: int,
    task_text: str,
) -> PreparedDerivation:
    session = recordings.get_session(session_id)
    if session["state"] != "COMPLETE":
        raise DerivationError("LeRobot derivation requires a COMPLETE recording session")
    quality = session.get("quality") or {}
    if quality.get("state") != "PASS" or not quality.get("report_sha256"):
        raise DerivationError("LeRobot derivation requires a quality PASS report")
    task_text = task_text.strip()
    if not task_text or len(task_text) > 500:
        raise DerivationError("task text must contain 1 through 500 characters")
    if not 1 <= fps <= MAX_DERIVATION_FPS:
        raise DerivationError("derivation fps must be between 1 and 60")
    if start_offset_ms < 0 or end_offset_ms <= start_offset_ms:
        raise DerivationError("derivation offsets must define a positive interval")
    if end_offset_ms - start_offset_ms > MAX_DERIVATION_DURATION_S * 1000:
        raise DerivationError("derivation interval exceeds 1,800 seconds")
    camera_keys = tuple(sorted(set(camera_topic_keys)))
    if not camera_keys or len(camera_keys) != len(camera_topic_keys):
        raise DerivationError("select one or more unique camera topic keys")

    robot_manifest, robot_manifest_sha256 = _load_robot_manifest(robot_manifest_path)
    state, cameras = _stream_contracts(
        session,
        robot_manifest,
        state_topic_key,
        camera_keys,
    )
    selected = (state, *cameras)
    record_start_ns = int(session["record_time"]["start_ns"])
    record_end_ns = int(session["record_time"]["end_ns"])
    requested_start_ns = record_start_ns + start_offset_ms * 1_000_000
    requested_end_ns = record_start_ns + end_offset_ms * 1_000_000
    if requested_end_ns > record_end_ns:
        raise DerivationError("derivation interval extends beyond the recording")
    target_start_ns = max(requested_start_ns, *(item.source_start_ns for item in selected))
    target_end_ns = min(requested_end_ns, *(item.source_end_ns for item in selected))
    if target_end_ns <= target_start_ns:
        raise DerivationError("selected streams have no common source-time interval")

    source_objects = _mcap_objects(session, recordings, object_store)
    request = {
        "schema_version": DERIVATION_SCHEMA_VERSION,
        "contract_version": DERIVATION_CONTRACT_VERSION,
        "session_id": session_id,
        "run_id": session["run_id"],
        "robot_id": session["robot_id"],
        "task_text": task_text,
        "fps": fps,
        "requested_start_offset_ms": start_offset_ms,
        "requested_end_offset_ms": end_offset_ms,
        "target_start_ns": target_start_ns,
        "target_end_ns": target_end_ns,
        "state": state.as_dict(),
        "cameras": [camera.as_dict() for camera in cameras],
        "source_manifest_sha256": session["manifest_sha256"],
        "quality_report_sha256": quality["report_sha256"],
        "robot_manifest_sha256": robot_manifest_sha256,
        "source_objects": [item.provenance() for item in source_objects],
        "lerobot": {
            "package_version": LEROBOT_PACKAGE_VERSION,
            "codebase_version": LEROBOT_CODEBASE_VERSION,
            "source_commit": LEROBOT_SOURCE_COMMIT,
            "dataset_module_sha256": LEROBOT_DATASET_SHA256,
            "utils_module_sha256": LEROBOT_UTILS_SHA256,
            "video_codec": "h264",
        },
        "safety": {
            "observation_only": True,
            "action_feature": False,
            "robot_command_interfaces": False,
        },
    }
    request_sha256 = sha256_bytes(canonical_json(request))
    return PreparedDerivation(
        derivation_id=f"lerobot-{request_sha256[:24]}",
        request_sha256=request_sha256,
        session_id=session_id,
        run_id=session["run_id"],
        robot_id=session["robot_id"],
        task_text=task_text,
        fps=fps,
        requested_start_offset_ms=start_offset_ms,
        requested_end_offset_ms=end_offset_ms,
        target_start_ns=target_start_ns,
        target_end_ns=target_end_ns,
        state=state,
        cameras=cameras,
        source_manifest_sha256=session["manifest_sha256"],
        quality_report_sha256=quality["report_sha256"],
        robot_manifest_sha256=robot_manifest_sha256,
        source_objects=source_objects,
        request=request,
    )


class DatasetDerivationCatalog:
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
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["camera_topic_keys"] = json.loads(result.pop("camera_topic_keys_json"))
        result["request"] = json.loads(result.pop("request_json"))
        result["download_url"] = (
            f"/api/v1/dataset-derivations/{result['derivation_id']}/download"
            if result["state"] == "COMPLETE"
            else None
        )
        return result

    def begin(
        self,
        prepared: PreparedDerivation,
        *,
        created_by: str | None,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        timestamp = isoformat_z(occurred_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM dataset_derivations WHERE request_sha256 = ?",
                (prepared.request_sha256,),
            ).fetchone()
            if existing is not None:
                if existing["state"] == "COMPLETE":
                    return self._decode(existing)
                if existing["state"] == "RUNNING":
                    raise DerivationConflict("the identical derivation is already running")
                connection.execute(
                    """
                    UPDATE dataset_derivations
                    SET state = 'RUNNING', frame_count = NULL, episode_count = NULL,
                        output_object_key = NULL, output_size_bytes = NULL,
                        output_sha256 = NULL, manifest_object_key = NULL,
                        manifest_size_bytes = NULL, manifest_sha256 = NULL,
                        error = NULL, started_at = ?, updated_at = ?,
                        completed_at = NULL, revision = revision + 1
                    WHERE derivation_id = ?
                    """,
                    (timestamp, timestamp, existing["derivation_id"]),
                )
                row = connection.execute(
                    "SELECT * FROM dataset_derivations WHERE derivation_id = ?",
                    (existing["derivation_id"],),
                ).fetchone()
                return self._decode(row)
            connection.execute(
                """
                INSERT INTO dataset_derivations (
                    derivation_id, request_sha256, state, session_id, run_id,
                    robot_id, task_text, fps, requested_start_offset_ms,
                    requested_end_offset_ms, target_start_ns, target_end_ns,
                    state_topic_key, camera_topic_keys_json, request_json,
                    source_manifest_sha256, quality_report_sha256,
                    robot_manifest_sha256, converter_contract_version,
                    lerobot_version, lerobot_codebase_version, created_by,
                    created_at, started_at, updated_at, revision
                ) VALUES (?, ?, 'RUNNING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    prepared.derivation_id,
                    prepared.request_sha256,
                    prepared.session_id,
                    prepared.run_id,
                    prepared.robot_id,
                    prepared.task_text,
                    prepared.fps,
                    prepared.requested_start_offset_ms,
                    prepared.requested_end_offset_ms,
                    prepared.target_start_ns,
                    prepared.target_end_ns,
                    prepared.state.key,
                    json.dumps([item.key for item in prepared.cameras], separators=(",", ":")),
                    canonical_json(prepared.request).decode("utf-8"),
                    prepared.source_manifest_sha256,
                    prepared.quality_report_sha256,
                    prepared.robot_manifest_sha256,
                    DERIVATION_CONTRACT_VERSION,
                    LEROBOT_PACKAGE_VERSION,
                    LEROBOT_CODEBASE_VERSION,
                    created_by,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM dataset_derivations WHERE derivation_id = ?",
                (prepared.derivation_id,),
            ).fetchone()
            return self._decode(row)

    def complete(
        self,
        derivation_id: str,
        *,
        frame_count: int,
        output_object_key: str,
        output_size_bytes: int,
        output_sha256: str,
        manifest_object_key: str,
        manifest_size_bytes: int,
        manifest_sha256: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        timestamp = isoformat_z(occurred_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE dataset_derivations
                SET state = 'COMPLETE', frame_count = ?, episode_count = 1,
                    output_object_key = ?, output_size_bytes = ?, output_sha256 = ?,
                    manifest_object_key = ?, manifest_size_bytes = ?, manifest_sha256 = ?,
                    completed_at = ?, updated_at = ?, revision = revision + 1
                WHERE derivation_id = ? AND state = 'RUNNING'
                """,
                (
                    frame_count,
                    output_object_key,
                    output_size_bytes,
                    output_sha256,
                    manifest_object_key,
                    manifest_size_bytes,
                    manifest_sha256,
                    timestamp,
                    timestamp,
                    derivation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise DerivationConflict("only a RUNNING derivation can complete")
            row = connection.execute(
                "SELECT * FROM dataset_derivations WHERE derivation_id = ?",
                (derivation_id,),
            ).fetchone()
            return self._decode(row)

    def fail(
        self,
        derivation_id: str,
        error: str,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        message = error.strip()[:2000] or "derivation failed"
        timestamp = isoformat_z(occurred_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE dataset_derivations
                SET state = 'FAILED', error = ?, completed_at = ?, updated_at = ?,
                    revision = revision + 1
                WHERE derivation_id = ? AND state = 'RUNNING'
                """,
                (message, timestamp, timestamp, derivation_id),
            )
            if cursor.rowcount != 1:
                raise DerivationConflict("only a RUNNING derivation can fail")
            row = connection.execute(
                "SELECT * FROM dataset_derivations WHERE derivation_id = ?",
                (derivation_id,),
            ).fetchone()
            return self._decode(row)

    def get(self, derivation_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dataset_derivations WHERE derivation_id = ?",
                (derivation_id,),
            ).fetchone()
            if row is None:
                raise DerivationNotFound(f"unknown dataset derivation: {derivation_id}")
            return self._decode(row)

    def list(
        self,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("derivation limit must be between 1 and 500")
        clauses = []
        parameters: list[Any] = []
        for column, value in (
            ("session_id", session_id),
            ("run_id", run_id),
            ("state", state),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM dataset_derivations"
                + where
                + " ORDER BY created_at DESC, derivation_id LIMIT ?",
                parameters,
            ).fetchall()
            return [self._decode(row) for row in rows]


def _decode_observation(
    key: str,
    message_type: str,
    payload: bytes,
    log_time_ns: int,
) -> Observation:
    if message_type == JOINT_STATE_TYPE:
        return decode_joint_state(payload, log_time_ns=log_time_ns)
    if message_type == COMPRESSED_IMAGE_TYPE:
        try:
            decoded = decode_compressed_image(payload)
        except PlaybackInvalid as exc:
            raise DerivationError(str(exc)) from exc
        return CameraObservation(
            source_time_ns=decoded.source_time_ns,
            log_time_ns=log_time_ns,
            jpeg=decoded.data,
        )
    raise DerivationError(f"unsupported selected message type: {key} {message_type}")


def _iter_messages(prepared: PreparedDerivation):
    by_topic = {item.topic: item for item in prepared.selected_streams}
    start_hint = max(0, prepared.target_start_ns - 5_000_000_000)
    end_hint = prepared.target_end_ns + 5_000_000_000
    previous_log_time = None
    for source in prepared.source_objects:
        try:
            with source.path.open("rb") as stream:
                reader = SeekingReader(stream, record_size_limit=MAX_MCAP_RECORD_BYTES)
                for schema, channel, message in reader.iter_messages(
                    topics=list(by_topic),
                    start_time=max(source.start_time_ns, start_hint),
                    end_time=min(source.end_time_ns + 1, end_hint),
                ):
                    if previous_log_time is not None and message.log_time < previous_log_time:
                        raise DerivationError("MCAP objects overlap or regress in log time")
                    previous_log_time = message.log_time
                    contract = by_topic[channel.topic]
                    if (
                        channel.message_encoding != "cdr"
                        or schema is None
                        or schema.encoding != "ros2msg"
                        or schema.name != contract.message_type
                    ):
                        raise DerivationError(f"MCAP schema differs for selected topic: {contract.key}")
                    yield contract, _decode_observation(
                        contract.key,
                        contract.message_type,
                        message.data,
                        message.log_time,
                    )
        except DerivationError:
            raise
        except (McapError, OSError, ValueError) as exc:
            raise DerivationError("source MCAP messages cannot be read") from exc


def sample_observations(prepared: PreparedDerivation) -> Iterator[SampledFrame]:
    keys = tuple(item.key for item in prepared.selected_streams)
    previous: dict[str, Observation] = {}
    current: dict[str, Observation] = {}
    last_source_time: dict[str, int] = {}
    emitted = 0

    def target_time(index: int) -> int:
        return prepared.target_start_ns + round(index * 1_000_000_000 / prepared.fps)

    for contract, observation in _iter_messages(prepared):
        prior_time = last_source_time.get(contract.key)
        if prior_time is not None and observation.source_time_ns < prior_time:
            raise DerivationError(f"source timestamp regressed: {contract.key}")
        last_source_time[contract.key] = observation.source_time_ns
        if contract.key in current:
            previous[contract.key] = current[contract.key]
        current[contract.key] = observation

        while emitted < prepared.expected_frame_count:
            target = target_time(emitted)
            if not all(key in current and current[key].source_time_ns >= target for key in keys):
                break
            selected = {}
            for key in keys:
                candidates = [current[key]]
                if key in previous:
                    candidates.append(previous[key])
                nearest = min(
                    candidates,
                    key=lambda item: abs(item.source_time_ns - target),
                )
                skew_ns = abs(nearest.source_time_ns - target)
                if skew_ns > MAX_SAMPLE_SKEW_NS:
                    raise DerivationError(
                        f"nearest observation exceeds 50 ms at frame {emitted}: {key}"
                    )
                selected[key] = nearest
            yield SampledFrame(target_time_ns=target, observations=selected)
            emitted += 1

    if emitted != prepared.expected_frame_count:
        raise DerivationError(
            f"selected interval produced {emitted} of {prepared.expected_frame_count} frames"
        )


class DatasetWriter(Protocol):
    root: Path

    def add_frame(self, frame: SampledFrame) -> None: ...

    def finalize(self) -> None: ...


class LeRobotV3Writer:
    def __init__(self, root: Path, prepared: PreparedDerivation) -> None:
        import importlib.metadata
        import numpy as np
        from PIL import Image
        from lerobot.datasets import lerobot_dataset as dataset_module
        from lerobot.datasets import utils as dataset_utils
        from lerobot.datasets.lerobot_dataset import CODEBASE_VERSION, LeRobotDataset

        if importlib.metadata.version("lerobot") != LEROBOT_PACKAGE_VERSION:
            raise DerivationError("installed LeRobot package version differs from the frozen contract")
        if CODEBASE_VERSION != LEROBOT_CODEBASE_VERSION:
            raise DerivationError("installed LeRobot dataset format differs from the frozen contract")
        if sha256_file(Path(dataset_module.__file__))[1] != LEROBOT_DATASET_SHA256:
            raise DerivationError("installed LeRobot dataset module differs from the frozen contract")
        if sha256_file(Path(dataset_utils.__file__))[1] != LEROBOT_UTILS_SHA256:
            raise DerivationError("installed LeRobot dataset utils differ from the frozen contract")

        self.root = root
        self.prepared = prepared
        self.np = np
        self.Image = Image
        features = {
            "observation.state": {
                "dtype": "float32",
                "shape": prepared.state.shape,
                "names": list(prepared.state.names),
            }
        }
        for camera in prepared.cameras:
            features[f"observation.images.{camera.key}"] = {
                "dtype": "video",
                "shape": camera.shape,
                "names": list(camera.names),
            }
        self.dataset = LeRobotDataset.create(
            repo_id=f"sang/{prepared.derivation_id}",
            fps=prepared.fps,
            features=features,
            root=root,
            robot_type=prepared.robot_id,
            use_videos=True,
            vcodec="h264",
        )

    def add_frame(self, frame: SampledFrame) -> None:
        state_observation = frame.observations[self.prepared.state.key]
        if not isinstance(state_observation, JointObservation):
            raise DerivationError("sampled state observation has the wrong type")
        by_name = dict(zip(state_observation.names, state_observation.positions, strict=True))
        missing = [name for name in self.prepared.state.names if name not in by_name]
        if missing:
            raise DerivationError(f"JointState is missing configured joints: {missing}")
        payload: dict[str, Any] = {
            "observation.state": self.np.asarray(
                [by_name[name] for name in self.prepared.state.names],
                dtype=self.np.float32,
            ),
            "task": self.prepared.task_text,
        }
        for camera in self.prepared.cameras:
            observation = frame.observations[camera.key]
            if not isinstance(observation, CameraObservation):
                raise DerivationError(f"sampled camera observation has the wrong type: {camera.key}")
            try:
                with self.Image.open(BytesIO(observation.jpeg)) as image:
                    rgb = self.np.asarray(image.convert("RGB"))
            except Exception as exc:
                raise DerivationError(f"camera JPEG cannot be decoded: {camera.key}") from exc
            if rgb.shape != camera.shape:
                raise DerivationError(
                    f"camera shape differs from robot manifest: {camera.key} {rgb.shape}"
                )
            payload[f"observation.images.{camera.key}"] = rgb
        self.dataset.add_frame(payload)

    def finalize(self) -> None:
        self.dataset.save_episode(parallel_encoding=False)
        self.dataset.finalize()
        if "action" in self.dataset.features:
            raise DerivationError("observation-only derivation unexpectedly contains action")


def _dataset_files(root: Path) -> list[dict[str, Any]]:
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise DerivationError("LeRobot output contains a symbolic link")
        if not path.is_file() or path.name == "sang-derivation.json":
            continue
        size, digest = sha256_file(path)
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size_bytes": size,
                "sha256": digest,
            }
        )
    if not files:
        raise DerivationError("LeRobot output contains no dataset files")
    return files


def _create_deterministic_tar(root: Path, destination: Path) -> None:
    with tarfile.open(destination, "w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            if path.is_symlink():
                raise DerivationError("LeRobot output contains a symbolic link")
            relative = path.relative_to(root).as_posix()
            info = tarfile.TarInfo(relative)
            info.size = path.stat().st_size
            info.mode = 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            with path.open("rb") as stream:
                archive.addfile(info, stream)


async def _publish_file(
    object_store: FilesystemObjectStore,
    object_key: str,
    path: Path,
    *,
    size_bytes: int,
    sha256: str,
) -> None:
    async def blocks():
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    return
                yield block
        finally:
            os.close(descriptor)

    await object_store.put_stream(
        object_key,
        blocks(),
        size_bytes=size_bytes,
        sha256=sha256,
    )


def run_derivation(
    *,
    prepared: PreparedDerivation,
    catalog: DatasetDerivationCatalog,
    object_store: FilesystemObjectStore,
    work_root: Path,
    created_by: str | None = None,
    writer_factory: Callable[[Path, PreparedDerivation], DatasetWriter] = LeRobotV3Writer,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    started = catalog.begin(prepared, created_by=created_by, occurred_at=now())
    if started["state"] == "COMPLETE":
        return started
    work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f"{prepared.derivation_id}-", dir=work_root))
    dataset_root = temporary / "dataset"
    archive_path = temporary / "dataset.tar"
    try:
        writer = writer_factory(dataset_root, prepared)
        timeline = {
            "target_time_ns": [],
            "streams": {
                item.key: {"source_time_ns": [], "log_time_ns": [], "skew_ns": []}
                for item in prepared.selected_streams
            },
        }
        frame_count = 0
        for frame in sample_observations(prepared):
            writer.add_frame(frame)
            timeline["target_time_ns"].append(frame.target_time_ns)
            for key, observation in frame.observations.items():
                stream = timeline["streams"][key]
                stream["source_time_ns"].append(observation.source_time_ns)
                stream["log_time_ns"].append(observation.log_time_ns)
                stream["skew_ns"].append(observation.source_time_ns - frame.target_time_ns)
            frame_count += 1
        if frame_count != prepared.expected_frame_count:
            raise DerivationError("derived frame count differs from the fixed-rate contract")
        writer.finalize()

        files = _dataset_files(dataset_root)
        provenance = {
            "schema_version": DERIVATION_SCHEMA_VERSION,
            "derivation_id": prepared.derivation_id,
            "request_sha256": prepared.request_sha256,
            "request": prepared.request,
            "frame_count": frame_count,
            "episode_count": 1,
            "features": {
                "observation.state": prepared.state.as_dict(),
                **{
                    f"observation.images.{camera.key}": camera.as_dict()
                    for camera in prepared.cameras
                },
            },
            "action_feature": False,
            "timeline": timeline,
            "maximum_absolute_skew_ns": {
                key: max(abs(value) for value in stream["skew_ns"])
                for key, stream in timeline["streams"].items()
            },
            "dataset_files": files,
        }
        manifest_payload = canonical_json(provenance) + b"\n"
        manifest_path = dataset_root / "sang-derivation.json"
        with manifest_path.open("xb") as stream:
            stream.write(manifest_payload)
            stream.flush()
            os.fsync(stream.fileno())
        manifest_sha256 = sha256_bytes(manifest_payload)
        _create_deterministic_tar(dataset_root, archive_path)
        output_size, output_sha256 = sha256_file(archive_path)
        prefix = f"derivations/{prepared.derivation_id}"
        output_key = f"{prefix}/dataset.tar"
        manifest_key = f"{prefix}/sang-derivation.json"
        asyncio.run(
            _publish_file(
                object_store,
                output_key,
                archive_path,
                size_bytes=output_size,
                sha256=output_sha256,
            )
        )
        asyncio.run(
            object_store.put_bytes(
                manifest_key,
                manifest_payload,
                sha256=manifest_sha256,
            )
        )
        return catalog.complete(
            prepared.derivation_id,
            frame_count=frame_count,
            output_object_key=output_key,
            output_size_bytes=output_size,
            output_sha256=output_sha256,
            manifest_object_key=manifest_key,
            manifest_size_bytes=len(manifest_payload),
            manifest_sha256=manifest_sha256,
            occurred_at=now(),
        )
    except BaseException as exc:
        try:
            catalog.fail(prepared.derivation_id, str(exc), occurred_at=now())
        except DerivationError:
            pass
        raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Derive an observation-only LeRobot v3 dataset from a quality-passed MCAP session."
    )
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--object-store-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--robot-manifest", type=Path, required=True)
    parser.add_argument("--state-topic-key", required=True)
    parser.add_argument("--camera-topic-key", action="append", required=True)
    parser.add_argument("--start-offset-s", type=float, required=True)
    parser.add_argument("--end-offset-s", type=float, required=True)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--task", required=True)
    parser.add_argument("--created-by")
    options = parser.parse_args(args)
    for name in ("start_offset_s", "end_offset_s"):
        value = getattr(options, name)
        if not math.isfinite(value) or value < 0:
            parser.error(f"--{name.replace('_', '-')} must be a finite non-negative number")
    if options.end_offset_s <= options.start_offset_s:
        parser.error("--end-offset-s must be greater than --start-offset-s")
    return options


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    try:
        recordings = RecordingCatalog(options.db_path)
        objects = FilesystemObjectStore(options.object_store_root)
        prepared = prepare_derivation(
            recordings=recordings,
            object_store=objects,
            session_id=options.session_id,
            robot_manifest_path=options.robot_manifest,
            state_topic_key=options.state_topic_key,
            camera_topic_keys=options.camera_topic_key,
            start_offset_ms=round(options.start_offset_s * 1000),
            end_offset_ms=round(options.end_offset_s * 1000),
            fps=options.fps,
            task_text=options.task,
        )
        result = run_derivation(
            prepared=prepared,
            catalog=DatasetDerivationCatalog(options.db_path),
            object_store=objects,
            work_root=options.work_root,
            created_by=options.created_by,
        )
    except (DerivationError, OSError, ValueError) as exc:
        print(json.dumps({"state": "FAILED", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1:])
