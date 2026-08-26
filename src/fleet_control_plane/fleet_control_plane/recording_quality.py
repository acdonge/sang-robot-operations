"""Bounded quality analysis for immutable ROS 2 MCAP recording sessions."""

from __future__ import annotations

import argparse
import asyncio
from bisect import bisect_left
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any

from mcap.exceptions import McapError
from mcap.reader import SeekingReader
import yaml

from fleet_control_plane.recording_playback import (
    COMPRESSED_IMAGE_TYPE,
    MAX_MCAP_RECORD_BYTES,
    PlaybackInvalid,
    decode_compressed_image,
    decode_ros_header,
)
from fleet_control_plane.recording_storage import (
    FilesystemObjectStore,
    RecordingCatalog,
    RecordingConflict,
    RecordingInvalid,
)
from fleet_control_plane.status import isoformat_z


JOINT_STATE_TYPE = "sensor_msgs/msg/JointState"
REPORT_SCHEMA_VERSION = "1.0"
MAX_MESSAGES_PER_TOPIC = 2_000_000
MAX_POLICY_FILES = 256
MAX_GAP_MS = 250.0
MAX_P95_SKEW_MS = 50.0
MAX_SKEW_MS = 150.0
SAFE_ROBOT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class QualityUnavailable(RuntimeError):
    """The session cannot be analyzed with the configured quality contract."""


def _percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return float(ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction)


def _distribution_ms(values_ns: list[int]) -> dict[str, Any]:
    values = [item / 1_000_000.0 for item in values_ns]
    return {
        "count": len(values),
        "p50_ms": _percentile(values, 50),
        "p95_ms": _percentile(values, 95),
        "max_ms": max(values, default=None),
    }


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 4 or not data.startswith(b"\xff\xd8"):
        raise ValueError("JPEG start marker is missing")
    offset = 2
    start_of_frame = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while offset < len(data):
        while offset < len(data) and data[offset] != 0xFF:
            offset += 1
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0x01, *range(0xD0, 0xD9)}:
            continue
        if offset + 2 > len(data):
            break
        length = int.from_bytes(data[offset : offset + 2], "big")
        if length < 2 or offset + length > len(data):
            break
        if marker in start_of_frame:
            if length < 7:
                break
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
            if width <= 0 or height <= 0:
                break
            return width, height
        offset += length
    raise ValueError("JPEG dimensions are unavailable")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise QualityUnavailable(f"{label} must be a mapping")
    return value


class QualityPolicyResolver:
    """Resolve one checked-in Robot Manifest and freeze its observation thresholds."""

    def __init__(self, root: str | Path) -> None:
        unresolved = Path(root).expanduser()
        if unresolved.is_symlink():
            raise QualityUnavailable("quality policy root may not be a symbolic link")
        self.root = unresolved.resolve(strict=True)
        if not self.root.is_dir():
            raise QualityUnavailable("quality policy root is not a directory")

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise QualityUnavailable("Robot Manifest is not a safe regular file")
        try:
            return _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), "manifest")
        except (OSError, yaml.YAMLError) as exc:
            raise QualityUnavailable(f"cannot read Robot Manifest: {exc}") from exc

    def _path(self, robot_id: str) -> tuple[Path, dict[str, Any]]:
        if SAFE_ROBOT_ID.fullmatch(robot_id) is None:
            raise QualityUnavailable("robot_id is not safe for policy lookup")
        direct = self.root / f"{robot_id}.yaml"
        if direct.is_file() and not direct.is_symlink():
            return direct, self._load(direct)
        for index, path in enumerate(sorted(self.root.glob("*.yaml"))):
            if index >= MAX_POLICY_FILES:
                break
            payload = self._load(path)
            robot = payload.get("robot")
            if isinstance(robot, dict) and robot.get("robot_id") == robot_id:
                return path, payload
        raise QualityUnavailable(f"no Robot Manifest matches {robot_id}")

    def resolve(self, robot_id: str) -> dict[str, Any]:
        path, payload = self._path(robot_id)
        robot = _mapping(payload.get("robot"), "manifest.robot")
        interfaces = _mapping(payload.get("interfaces"), "manifest.interfaces")
        monitoring = _mapping(payload.get("monitoring", {}), "manifest.monitoring")
        required_keys = set(monitoring.get("required_sources", []))
        streams = []
        for kind, entries in (
            ("state", interfaces.get("state", [])),
            ("camera", interfaces.get("cameras", [])),
        ):
            if not isinstance(entries, list):
                raise QualityUnavailable(f"manifest.interfaces.{kind} must be a list")
            for entry in entries:
                item = _mapping(entry, f"manifest.interfaces.{kind}[]")
                key = item.get("key")
                topics = item.get("topics") if kind == "state" else [item.get("topic")]
                if not isinstance(key, str) or not isinstance(topics, list) or not topics:
                    raise QualityUnavailable("Robot Manifest stream identity is invalid")
                for topic in topics:
                    if not isinstance(topic, str) or not topic.startswith("/"):
                        raise QualityUnavailable("Robot Manifest topic is invalid")
                    streams.append(
                        {
                            "key": key,
                            "kind": kind,
                            "topic": topic,
                            "required": key in required_keys,
                            "minimum_rate_hz": item.get("minimum_rate_hz"),
                            "width": item.get("width") if kind == "camera" else None,
                            "height": item.get("height") if kind == "camera" else None,
                        }
                    )
        policy = {
            "robot_id": robot.get("robot_id"),
            "model": robot.get("model"),
            "manifest": path.name,
            "streams": streams,
            "thresholds": {
                "max_gap_ms": MAX_GAP_MS,
                "max_p95_camera_joint_skew_ms": MAX_P95_SKEW_MS,
                "max_camera_joint_skew_ms": MAX_SKEW_MS,
            },
        }
        canonical = json.dumps(policy, separators=(",", ":"), sort_keys=True).encode()
        policy["sha256"] = hashlib.sha256(canonical).hexdigest()
        return policy


def _check(
    target: list[dict[str, Any]],
    name: str,
    passed: bool,
    *,
    severity: str,
    actual: Any,
    limit: Any,
) -> None:
    target.append(
        {
            "name": name,
            "passed": bool(passed),
            "severity": severity,
            "actual": actual,
            "limit": limit,
        }
    )


def _state(checks: list[dict[str, Any]]) -> str:
    failures = [item for item in checks if not item["passed"]]
    if any(item["severity"] == "fail" for item in failures):
        return "FAIL"
    if failures:
        return "WARNING"
    return "PASS"


def _nearest_skew(joint_stamps: list[int], camera_stamps: list[int]) -> dict[str, Any]:
    cameras = sorted(camera_stamps)
    if not cameras:
        return {**_distribution_ms([]), "eligible": 0, "excluded_outside_window": 0}
    skews = []
    excluded = 0
    eligible = 0
    for stamp in joint_stamps:
        eligible += 1
        if stamp < cameras[0] or stamp > cameras[-1]:
            excluded += 1
            continue
        index = bisect_left(cameras, stamp)
        candidates = []
        if index < len(cameras):
            candidates.append(cameras[index])
        if index > 0:
            candidates.append(cameras[index - 1])
        skews.append(min(abs(candidate - stamp) for candidate in candidates))
    return {
        **_distribution_ms(skews),
        "eligible": eligible,
        "excluded_outside_window": excluded,
    }


def _clock_check(clock: dict[str, Any]) -> dict[str, Any]:
    quality = str(clock.get("quality", "UNKNOWN")).upper()
    if quality in {"LOCKED", "SYNCHRONIZED"}:
        severity = "fail"
        passed = True
    elif quality in {"UNSYNCHRONIZED", "UNSYNCED"}:
        severity = "fail"
        passed = False
    else:
        severity = "warning"
        passed = False
    return {
        "name": "capture_clock.synchronized",
        "passed": passed,
        "severity": severity,
        "actual": quality,
        "limit": "LOCKED",
    }


def _natural_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)]


class RecordingQualityService:
    def __init__(
        self,
        catalog: RecordingCatalog,
        object_store: FilesystemObjectStore,
        policy_root: str | Path,
    ) -> None:
        self.catalog = catalog
        self.object_store = object_store
        self.policies = QualityPolicyResolver(policy_root)

    def _scan(self, session: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], bool]:
        topics = {item["name"]: item for item in session["topics"]}
        samples = {
            name: {
                "log_source": [],
                "jpeg_sizes": [],
                "dimensions": Counter(),
                "decode_errors": 0,
                "observed_count": 0,
            }
            for name in topics
        }
        truncated = False
        artifacts = sorted(
            (
                item
                for item in session["artifacts"]
                if item["state"] == "COMPLETE" and item["path"].endswith(".mcap")
            ),
            key=lambda item: _natural_key(item["path"]),
        )
        if not artifacts:
            raise QualityUnavailable("recording session has no complete MCAP object")
        for item in artifacts:
            artifact = self.catalog.artifact(session["session_id"], item["path"])
            path = self.object_store.open_path(
                artifact["object_key"],
                size_bytes=artifact["expected_size_bytes"],
                sha256=artifact["expected_sha256"],
            )
            try:
                with path.open("rb") as stream:
                    reader = SeekingReader(stream, record_size_limit=MAX_MCAP_RECORD_BYTES)
                    for schema, channel, message in reader.iter_messages(topics=list(topics)):
                        del schema
                        target = samples[channel.topic]
                        target["observed_count"] += 1
                        if len(target["log_source"]) >= MAX_MESSAGES_PER_TOPIC:
                            truncated = True
                            continue
                        topic = topics[channel.topic]
                        try:
                            if topic["type"] == COMPRESSED_IMAGE_TYPE:
                                decoded = decode_compressed_image(message.data)
                                target["jpeg_sizes"].append(len(decoded.data))
                                target["dimensions"][jpeg_dimensions(decoded.data)] += 1
                                source_time_ns = decoded.source_time_ns
                            else:
                                source_time_ns = decode_ros_header(message.data).source_time_ns
                        except (PlaybackInvalid, ValueError):
                            target["decode_errors"] += 1
                            continue
                        target["log_source"].append((message.log_time, source_time_ns))
            except (McapError, OSError, ValueError) as exc:
                raise QualityUnavailable("recording MCAP cannot be scanned") from exc
        return samples, truncated

    @staticmethod
    def _stream_report(
        topic: dict[str, Any],
        policy: dict[str, Any] | None,
        sample: dict[str, Any],
        *,
        joint_stamps: list[int],
    ) -> dict[str, Any]:
        ordered = sorted(sample["log_source"], key=lambda item: item[0])
        source_stamps = [source for _, source in ordered if source > 0]
        source_gaps = [
            current - previous
            for previous, current in zip(source_stamps, source_stamps[1:])
        ]
        positive_gaps = [gap for gap in source_gaps if gap >= 0]
        elapsed_ns = source_stamps[-1] - source_stamps[0] if len(source_stamps) > 1 else 0
        rate_hz = (
            (len(source_stamps) - 1) * 1_000_000_000 / elapsed_ns
            if elapsed_ns > 0
            else 0.0
        )
        dimensions = sample["dimensions"]
        primary_dimensions = dimensions.most_common(1)[0][0] if dimensions else None
        jpeg_sizes = [float(item) for item in sample["jpeg_sizes"]]
        skew = (
            _nearest_skew(joint_stamps, source_stamps)
            if topic["type"] == COMPRESSED_IMAGE_TYPE
            else None
        )
        configured = {
            "required": False if policy is None else bool(policy["required"]),
            "minimum_rate_hz": None if policy is None else policy["minimum_rate_hz"],
            "width": None if policy is None else policy["width"],
            "height": None if policy is None else policy["height"],
        }
        measured = {
            "message_count": sample["observed_count"],
            "valid_source_timestamp_count": len(source_stamps),
            "rate_hz": rate_hz,
            "source_gap": _distribution_ms(positive_gaps),
            "gaps_over_250_ms": sum(gap > MAX_GAP_MS * 1_000_000 for gap in positive_gaps),
            "timestamp_regressions": sum(gap < 0 for gap in source_gaps),
            "decode_errors": sample["decode_errors"],
            "width": None if primary_dimensions is None else primary_dimensions[0],
            "height": None if primary_dimensions is None else primary_dimensions[1],
            "dimension_variants": [
                {"width": width, "height": height, "count": count}
                for (width, height), count in dimensions.most_common()
            ],
            "jpeg_bytes": {
                "count": len(jpeg_sizes),
                "average": None if not jpeg_sizes else sum(jpeg_sizes) / len(jpeg_sizes),
                "p95": _percentile(jpeg_sizes, 95),
                "maximum": max(jpeg_sizes, default=None),
            },
            "nearest_camera_joint_skew": skew,
        }
        checks: list[dict[str, Any]] = []
        required_severity = "fail" if configured["required"] else "warning"
        _check(
            checks,
            "message_count.matches_manifest",
            measured["message_count"] == topic["message_count"],
            severity=required_severity,
            actual=measured["message_count"],
            limit=topic["message_count"],
        )
        _check(
            checks,
            "source_timestamp.decode",
            measured["decode_errors"] == 0,
            severity=required_severity,
            actual=measured["decode_errors"],
            limit=0,
        )
        _check(
            checks,
            "source_timestamp.regressions",
            measured["timestamp_regressions"] == 0,
            severity=required_severity,
            actual=measured["timestamp_regressions"],
            limit=0,
        )
        minimum_rate = configured["minimum_rate_hz"]
        if minimum_rate is None:
            _check(
                checks,
                "policy.available",
                False,
                severity="warning",
                actual=None,
                limit="Robot Manifest stream policy",
            )
        else:
            _check(
                checks,
                "rate.minimum",
                rate_hz >= float(minimum_rate),
                severity=required_severity,
                actual=rate_hz,
                limit=minimum_rate,
            )
            maximum_gap = measured["source_gap"]["max_ms"]
            _check(
                checks,
                "source_gap.maximum",
                maximum_gap is not None and maximum_gap <= MAX_GAP_MS,
                severity=required_severity,
                actual=maximum_gap,
                limit=MAX_GAP_MS,
            )
        if topic["type"] == COMPRESSED_IMAGE_TYPE:
            expected_dimensions = (configured["width"], configured["height"])
            actual_dimensions = (measured["width"], measured["height"])
            _check(
                checks,
                "image.dimensions",
                None not in expected_dimensions and actual_dimensions == expected_dimensions,
                severity=required_severity,
                actual={"width": measured["width"], "height": measured["height"]},
                limit={"width": configured["width"], "height": configured["height"]},
            )
            _check(
                checks,
                "image.dimension_stability",
                len(measured["dimension_variants"]) == 1,
                severity=required_severity,
                actual=len(measured["dimension_variants"]),
                limit=1,
            )
            _check(
                checks,
                "camera_joint_skew.p95",
                skew["p95_ms"] is not None and skew["p95_ms"] <= MAX_P95_SKEW_MS,
                severity=required_severity,
                actual=skew["p95_ms"],
                limit=MAX_P95_SKEW_MS,
            )
            _check(
                checks,
                "camera_joint_skew.maximum",
                skew["max_ms"] is not None and skew["max_ms"] <= MAX_SKEW_MS,
                severity=required_severity,
                actual=skew["max_ms"],
                limit=MAX_SKEW_MS,
            )
        return {
            "key": topic["key"],
            "topic": topic["name"],
            "message_type": topic["type"],
            "asset_id": topic["asset_id"],
            "kind": "camera" if topic["type"] == COMPRESSED_IMAGE_TYPE else "state",
            "state": _state(checks),
            "configured": configured,
            "measured": measured,
            "checks": checks,
        }

    def analyze_report(
        self,
        session_id: str,
        *,
        analyzed_at: datetime | None = None,
    ) -> dict[str, Any]:
        session = self.catalog.get_session(session_id)
        if session["state"] != "COMPLETE":
            raise RecordingConflict("quality analysis requires a complete session")
        manifest = self.catalog.manifest(session_id)
        policy = self.policies.resolve(session["robot_id"])
        policy_by_topic = {item["topic"]: item for item in policy["streams"]}
        samples, truncated = self._scan(session)
        joint_topic = next(
            (
                topic
                for topic in session["topics"]
                if topic["type"] == JOINT_STATE_TYPE
            ),
            None,
        )
        joint_stamps = []
        if joint_topic is not None:
            joint_stamps = [
                source
                for _, source in sorted(
                    samples[joint_topic["name"]]["log_source"],
                    key=lambda item: item[0],
                )
                if source > 0
            ]
        streams = [
            self._stream_report(
                topic,
                policy_by_topic.get(topic["name"]),
                samples[topic["name"]],
                joint_stamps=joint_stamps,
            )
            for topic in session["topics"]
        ]
        recorded_topics = {item["name"] for item in session["topics"]}
        for expected in policy["streams"]:
            if not expected["required"] or expected["topic"] in recorded_topics:
                continue
            streams.append(
                {
                    "key": expected["key"],
                    "topic": expected["topic"],
                    "message_type": None,
                    "asset_id": None,
                    "kind": expected["kind"],
                    "state": "FAIL",
                    "configured": expected,
                    "measured": {"message_count": 0},
                    "checks": [
                        {
                            "name": "required_stream.recorded",
                            "passed": False,
                            "severity": "fail",
                            "actual": False,
                            "limit": True,
                        }
                    ],
                }
            )
        checks = [_clock_check(manifest.get("clock", {}))]
        lost_messages = int(manifest.get("recorder_diagnostics", {}).get("transport_messages_lost", 0))
        _check(
            checks,
            "recorder.transport_messages_lost",
            lost_messages == 0,
            severity="fail",
            actual=lost_messages,
            limit=0,
        )
        _check(
            checks,
            "analysis.within_bound",
            not truncated,
            severity="warning",
            actual=truncated,
            limit=False,
        )
        all_checks = [*checks, *(item for stream in streams for item in stream["checks"])]
        state = _state(all_checks)
        failure_counts = {
            "fail": sum(
                not item["passed"] and item["severity"] == "fail"
                for item in all_checks
            ),
            "warning": sum(
                not item["passed"] and item["severity"] == "warning"
                for item in all_checks
            ),
        }
        observed = analyzed_at or datetime.now(timezone.utc)
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise RecordingInvalid("quality analyzed_at must include a timezone")
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "session_id": session_id,
            "manifest_sha256": session["manifest_sha256"],
            "storage_state": session["state"],
            "state": state,
            "analyzed_at": isoformat_z(observed),
            "policy": policy,
            "clock": manifest.get("clock", {}),
            "analysis_limits": {
                "max_messages_per_topic": MAX_MESSAGES_PER_TOPIC,
                "truncated": truncated,
            },
            "summary": {
                "stream_count": len(streams),
                "pass_stream_count": sum(item["state"] == "PASS" for item in streams),
                "warning_stream_count": sum(item["state"] == "WARNING" for item in streams),
                "fail_stream_count": sum(item["state"] == "FAIL" for item in streams),
                "failed_check_count": failure_counts["fail"],
                "warning_check_count": failure_counts["warning"],
            },
            "checks": checks,
            "streams": streams,
            "safety": {
                "read_only_analysis": True,
                "robot_command_interfaces": False,
                "vendor_command_channel": False,
            },
        }

    def analyze(self, session_id: str) -> dict[str, Any]:
        existing = self.catalog.quality_report(session_id)
        if existing is not None:
            return existing
        report = self.analyze_report(session_id)
        payload = (
            json.dumps(report, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
            + "\n"
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        object_key = self.object_store.quality_key(session_id, digest)
        asyncio.run(self.object_store.put_bytes(object_key, payload, sha256=digest))
        return self.catalog.save_quality_report(
            report,
            object_key=object_key,
            report_sha256=digest,
            report_size_bytes=len(payload),
        )


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze one immutable recording session")
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--object-store-root", type=Path, required=True)
    parser.add_argument("--policy-root", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    try:
        result = RecordingQualityService(
            RecordingCatalog(options.db_path),
            FilesystemObjectStore(options.object_store_root),
            options.policy_root,
        ).analyze(options.session_id)
    except (OSError, RecordingConflict, RecordingInvalid, QualityUnavailable) as exc:
        raise SystemExit(f"recording quality analysis failed: {exc}") from exc
    print(json.dumps({"session_id": options.session_id, "state": result["state"]}))


if __name__ == "__main__":
    main(sys.argv[1:])
