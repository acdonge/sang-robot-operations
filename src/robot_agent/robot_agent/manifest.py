"""Minimal manifest contract for read-only ROS 2 source monitoring."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, Literal, Mapping

import yaml

from robot_agent.health import camera_format_is_valid


SUPPORTED_SCHEMA_VERSION = "0.1"
SUPPORTED_MESSAGE_TYPES = frozenset(
    {
        "sensor_msgs/msg/CompressedImage",
        "sensor_msgs/msg/JointState",
        "trajectory_msgs/msg/JointTrajectory",
    }
)
MESSAGE_TYPE_BY_KIND = {
    "state": "sensor_msgs/msg/JointState",
    "action": "trajectory_msgs/msg/JointTrajectory",
    "camera": "sensor_msgs/msg/CompressedImage",
}


class ManifestValidationError(ValueError):
    """Raised when a robot manifest cannot be monitored safely."""


@dataclass(frozen=True)
class RobotConfig:
    robot_id: str
    model: str
    real_or_sim: Literal["real", "sim"]
    host_id: str
    namespace: str
    display_name: str

    @property
    def runtime_kind(self) -> Literal["physical", "simulation"]:
        """Expose the Phase 4 identity without changing manifest schema v0.1."""
        return "physical" if self.real_or_sim == "real" else "simulation"


@dataclass(frozen=True)
class SourceConfig:
    key: str
    kind: Literal["state", "action", "camera"]
    topics: tuple[str, ...]
    message_type: str
    freshness_ms: int
    minimum_rate_hz: float | None
    required: bool
    topic_features: tuple[tuple[str, ...], ...]
    encoding: str | None

    def features_for(self, topic: str) -> tuple[str, ...]:
        try:
            index = self.topics.index(topic)
        except ValueError as exc:
            raise KeyError(f"unknown topic for source {self.key}: {topic}") from exc
        return self.topic_features[index]


@dataclass(frozen=True)
class RobotManifest:
    schema_version: str
    robot: RobotConfig
    sources: tuple[SourceConfig, ...]

    @property
    def source_keys(self) -> frozenset[str]:
        return frozenset(source.key for source in self.sources)


def _mapping(parent: Mapping[str, Any], key: str, path: str) -> Mapping[str, Any]:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise ManifestValidationError(f"{path}.{key} must be a mapping")
    return value


def _string(parent: Mapping[str, Any], key: str, path: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestValidationError(f"{path}.{key} must be a non-empty string")
    return value.strip()


def _positive_int(parent: Mapping[str, Any], key: str, path: str) -> int:
    value = parent.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ManifestValidationError(f"{path}.{key} must be a positive integer")
    return value


def _optional_positive_number(
    parent: Mapping[str, Any],
    key: str,
    path: str,
) -> float | None:
    value = parent.get(key)
    if value is None:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0.0
    ):
        raise ManifestValidationError(f"{path}.{key} must be a positive number")
    return float(value)


def _list(parent: Mapping[str, Any], key: str, path: str) -> list[Any]:
    value = parent.get(key)
    if not isinstance(value, list):
        raise ManifestValidationError(f"{path}.{key} must be a list")
    return value


def _string_list(
    parent: Mapping[str, Any], key: str, path: str, *, non_empty: bool = True
) -> tuple[str, ...]:
    values = _list(parent, key, path)
    if non_empty and not values:
        raise ManifestValidationError(f"{path}.{key} must not be empty")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ManifestValidationError(f"{path}.{key} must contain non-empty strings")
    return tuple(value.strip() for value in values)


def _source(
    raw: Any,
    path: str,
    *,
    kind: Literal["state", "action", "camera"],
    required_sources: frozenset[str],
) -> SourceConfig:
    if not isinstance(raw, Mapping):
        raise ManifestValidationError(f"{path} must be a mapping")
    key = _string(raw, "key", path)
    topics = (
        (_string(raw, "topic", path),)
        if kind == "camera"
        else _string_list(raw, "topics", path)
    )
    message_type = _string(raw, "message_type", path)
    if message_type not in SUPPORTED_MESSAGE_TYPES:
        raise ManifestValidationError(
            f"{path}.message_type {message_type!r} is not supported by robot_agent"
        )
    expected_message_type = MESSAGE_TYPE_BY_KIND[kind]
    if message_type != expected_message_type:
        raise ManifestValidationError(
            f"{path}.message_type must be {expected_message_type!r} for {kind}"
        )

    if kind == "camera":
        topic_features = ((),)
        encoding = _string(raw, "encoding", path)
        if not camera_format_is_valid(encoding):
            raise ManifestValidationError(
                f"{path}.encoding must be a supported JPEG alias"
            )
    else:
        feature_names = _string_list(raw, "feature_names", path)
        if len(feature_names) != len(set(feature_names)):
            raise ManifestValidationError(f"{path}.feature_names must be unique")
        raw_topic_features = raw.get("topic_features")
        if raw_topic_features is None:
            if len(topics) != 1:
                raise ManifestValidationError(
                    f"{path}.topic_features is required for multi-topic sources"
                )
            topic_features = (feature_names,)
        else:
            if not isinstance(raw_topic_features, Mapping):
                raise ManifestValidationError(
                    f"{path}.topic_features must be a mapping"
                )
            if set(raw_topic_features) != set(topics):
                raise ManifestValidationError(
                    f"{path}.topic_features keys must match topics"
                )
            topic_features = tuple(
                _string_list(
                    raw_topic_features,
                    topic,
                    f"{path}.topic_features",
                )
                for topic in topics
            )
            if tuple(
                feature for values in topic_features for feature in values
            ) != feature_names:
                raise ManifestValidationError(
                    f"{path}.topic_features must flatten to feature_names order"
                )
        encoding = None
    return SourceConfig(
        key=key,
        kind=kind,
        topics=topics,
        message_type=message_type,
        freshness_ms=_positive_int(raw, "freshness_ms", path),
        minimum_rate_hz=_optional_positive_number(
            raw,
            "minimum_rate_hz",
            path,
        ),
        required=key in required_sources,
        topic_features=topic_features,
        encoding=encoding,
    )


def parse_manifest(data: Any) -> RobotManifest:
    if not isinstance(data, Mapping):
        raise ManifestValidationError("manifest root must be a mapping")

    schema_version = _string(data, "schema_version", "manifest")
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise ManifestValidationError(
            f"unsupported schema_version {schema_version!r}; expected {SUPPORTED_SCHEMA_VERSION!r}"
        )

    robot_raw = _mapping(data, "robot", "manifest")
    ui_raw = data.get("ui", {})
    if not isinstance(ui_raw, Mapping):
        raise ManifestValidationError("manifest.ui must be a mapping")
    robot_id = _string(robot_raw, "robot_id", "robot")
    real_or_sim = _string(robot_raw, "real_or_sim", "robot")
    if real_or_sim not in {"real", "sim"}:
        raise ManifestValidationError("robot.real_or_sim must be 'real' or 'sim'")
    display_name = ui_raw.get("display_name", robot_id)
    if not isinstance(display_name, str) or not display_name.strip():
        raise ManifestValidationError("ui.display_name must be a non-empty string")
    robot = RobotConfig(
        robot_id=robot_id,
        model=_string(robot_raw, "model", "robot"),
        real_or_sim=real_or_sim,
        host_id=_string(robot_raw, "host_id", "robot"),
        namespace=_string(robot_raw, "namespace", "robot"),
        display_name=display_name.strip(),
    )

    monitoring_raw = _mapping(data, "monitoring", "manifest")
    required_sources = frozenset(
        _string_list(monitoring_raw, "required_sources", "monitoring")
    )
    interfaces_raw = _mapping(data, "interfaces", "manifest")
    sources = []
    for group in ("state", "action"):
        for index, raw in enumerate(_list(interfaces_raw, group, "interfaces")):
            sources.append(
                _source(
                    raw,
                    f"interfaces.{group}[{index}]",
                    kind=group,
                    required_sources=required_sources,
                )
            )
    for index, raw in enumerate(_list(interfaces_raw, "cameras", "interfaces")):
        sources.append(
            _source(
                raw,
                f"interfaces.cameras[{index}]",
                kind="camera",
                required_sources=required_sources,
            )
        )
    if not sources:
        raise ManifestValidationError("interfaces must contain at least one source")

    source_keys = [source.key for source in sources]
    if len(source_keys) != len(set(source_keys)):
        raise ManifestValidationError("interface keys must be unique")
    unknown_required = required_sources - set(source_keys)
    if unknown_required:
        raise ManifestValidationError(
            "monitoring.required_sources contains unknown sources: "
            + ", ".join(sorted(unknown_required))
        )
    return RobotManifest(schema_version, robot, tuple(sources))


def load_manifest(path: str | Path) -> RobotManifest:
    manifest_path = Path(path)
    try:
        with manifest_path.open("r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestValidationError(
            f"failed to load manifest {manifest_path}: {exc}"
        ) from exc
    return parse_manifest(data)
