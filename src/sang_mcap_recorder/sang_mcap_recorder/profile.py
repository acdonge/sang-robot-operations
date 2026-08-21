"""Validated capture-profile loading."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

import yaml


IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MESSAGE_TYPE_PATTERN = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*/msg/[A-Za-z][A-Za-z0-9_]*$"
)
ALLOWED_TOP_LEVEL_KEYS = {
    "schema_version",
    "profile_id",
    "agent_id",
    "robot_id",
    "clock",
    "qos_overrides_path",
    "topics",
}
ALLOWED_TOPIC_KEYS = {
    "key",
    "name",
    "type",
    "asset_id",
    "calibration_ref",
    "timestamp_provenance",
    "require_header_timestamp",
}


class ProfileError(ValueError):
    """Raised when a capture profile violates the checked-in contract."""


@dataclass(frozen=True)
class TopicSpec:
    key: str
    name: str
    message_type: str
    asset_id: str
    calibration_ref: str
    timestamp_provenance: str
    require_header_timestamp: bool


@dataclass(frozen=True)
class CaptureProfile:
    schema_version: str
    profile_id: str
    agent_id: str
    robot_id: str
    clock: dict[str, Any]
    topics: tuple[TopicSpec, ...]
    qos_overrides_path: Path | None = None


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProfileError(f"{label} must be a mapping")
    return value


def _text(mapping: dict[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"{label}.{key} must be a non-empty string")
    return value.strip()


def _identifier(mapping: dict[str, Any], key: str, label: str) -> str:
    value = _text(mapping, key, label)
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise ProfileError(f"{label}.{key} is not a safe identifier")
    return value


def _validate_topic(raw: Any, index: int) -> TopicSpec:
    label = f"topics[{index}]"
    topic = _mapping(raw, label)
    unknown = set(topic) - ALLOWED_TOPIC_KEYS
    if unknown:
        raise ProfileError(f"{label} has unsupported keys: {sorted(unknown)}")

    key = _identifier(topic, "key", label)
    name = _text(topic, "name", label)
    if not name.startswith("/") or any(character.isspace() for character in name):
        raise ProfileError(f"{label}.name must be an absolute ROS topic")
    message_type = _text(topic, "type", label)
    if not MESSAGE_TYPE_PATTERN.fullmatch(message_type):
        raise ProfileError(f"{label}.type must be a ROS 2 message type")
    require_header = topic.get("require_header_timestamp", True)
    if not isinstance(require_header, bool):
        raise ProfileError(f"{label}.require_header_timestamp must be boolean")

    return TopicSpec(
        key=key,
        name=name,
        message_type=message_type,
        asset_id=_identifier(topic, "asset_id", label),
        calibration_ref=_text(topic, "calibration_ref", label),
        timestamp_provenance=_text(topic, "timestamp_provenance", label),
        require_header_timestamp=require_header,
    )


def load_profile(path: str | Path) -> CaptureProfile:
    profile_path = Path(path)
    try:
        raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileError(f"cannot read capture profile {profile_path}: {exc}") from exc

    data = _mapping(raw, "profile")
    unknown = set(data) - ALLOWED_TOP_LEVEL_KEYS
    if unknown:
        raise ProfileError(f"profile has unsupported keys: {sorted(unknown)}")
    if _text(data, "schema_version", "profile") != "1.0":
        raise ProfileError("profile.schema_version must be 1.0")

    raw_clock = _mapping(data.get("clock"), "profile.clock")
    required_clock_keys = {"sync_source", "quality", "provenance"}
    missing_clock = required_clock_keys - set(raw_clock)
    if missing_clock:
        raise ProfileError(
            f"profile.clock is missing required keys: {sorted(missing_clock)}"
        )
    clock = dict(raw_clock)
    for key in required_clock_keys:
        if not isinstance(clock[key], str) or not clock[key].strip():
            raise ProfileError(f"profile.clock.{key} must be a non-empty string")

    raw_topics = data.get("topics")
    if not isinstance(raw_topics, list) or not raw_topics:
        raise ProfileError("profile.topics must be a non-empty list")
    topics = tuple(
        _validate_topic(raw_topic, index)
        for index, raw_topic in enumerate(raw_topics)
    )
    keys = [topic.key for topic in topics]
    names = [topic.name for topic in topics]
    if len(keys) != len(set(keys)):
        raise ProfileError("profile topic keys must be unique")
    if len(names) != len(set(names)):
        raise ProfileError("profile topic names must be unique")

    qos_overrides_path = None
    raw_qos_path = data.get("qos_overrides_path")
    if raw_qos_path is not None:
        if not isinstance(raw_qos_path, str) or not raw_qos_path.strip():
            raise ProfileError("profile.qos_overrides_path must be a non-empty string")
        unresolved = profile_path.resolve().parent / raw_qos_path
        if unresolved.is_symlink():
            raise ProfileError("profile.qos_overrides_path may not be a symbolic link")
        candidate = unresolved.resolve()
        try:
            candidate.relative_to(profile_path.resolve().parent)
        except ValueError as exc:
            raise ProfileError(
                "profile.qos_overrides_path must remain inside the profile directory"
            ) from exc
        if not candidate.is_file():
            raise ProfileError(
                f"profile.qos_overrides_path is not a regular file: {candidate}"
            )
        qos_overrides_path = candidate

    return CaptureProfile(
        schema_version="1.0",
        profile_id=_identifier(data, "profile_id", "profile"),
        agent_id=_identifier(data, "agent_id", "profile"),
        robot_id=_identifier(data, "robot_id", "profile"),
        clock=clock,
        topics=topics,
        qos_overrides_path=qos_overrides_path,
    )
