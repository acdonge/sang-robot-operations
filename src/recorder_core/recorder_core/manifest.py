"""Loader and validation for the V2 robot manifest schema."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


SUPPORTED_SCHEMA_VERSION = "0.1"


class ManifestValidationError(ValueError):
    """Raised when a manifest does not conform to the supported schema."""


@dataclass(frozen=True)
class RobotConfig:
    robot_id: str
    model: str
    embodiment: str
    real_or_sim: str
    host_id: str
    namespace: str
    frames: Mapping[str, Any]
    spool_path: str
    lineage: Mapping[str, Any]


@dataclass(frozen=True)
class FeatureInterface:
    key: str
    topics: tuple[str, ...]
    message_type: str
    feature_names: tuple[str, ...]
    dtype: str
    shape: tuple[int, ...]
    unit: str
    freshness_ms: int
    semantics: str | None = None

    @property
    def dimension(self) -> int:
        dimension = 1
        for size in self.shape:
            dimension *= size
        return dimension


@dataclass(frozen=True)
class CameraInterface:
    key: str
    topic: str
    message_type: str
    encoding: str
    width: int
    height: int
    fps: int
    freshness_ms: int


@dataclass(frozen=True)
class InterfacesConfig:
    state: tuple[FeatureInterface, ...]
    action: tuple[FeatureInterface, ...]
    cameras: tuple[CameraInterface, ...]

    @property
    def source_keys(self) -> frozenset[str]:
        return frozenset(
            interface.key
            for interface in (*self.state, *self.action, *self.cameras)
        )


@dataclass(frozen=True)
class RecordingConfig:
    fps: int
    task: str
    contract: str
    rosbag: str
    required_sources: tuple[str, ...]
    staging_root: str
    dataset_root: str


@dataclass(frozen=True)
class Manifest:
    schema_version: str
    robot: RobotConfig
    interfaces: InterfacesConfig
    recording: RecordingConfig

    @property
    def state_feature_dimension(self) -> int:
        return sum(interface.dimension for interface in self.interfaces.state)


def _mapping(parent: Mapping[str, Any], key: str, path: str) -> Mapping[str, Any]:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise ManifestValidationError(f"{path}.{key} must be a mapping")
    return value


def _string(parent: Mapping[str, Any], key: str, path: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestValidationError(f"{path}.{key} must be a non-empty string")
    return value


def _positive_int(parent: Mapping[str, Any], key: str, path: str) -> int:
    value = parent.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ManifestValidationError(f"{path}.{key} must be a positive integer")
    return value


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
    return tuple(values)


def _parse_feature_interface(
    raw: Any, path: str, *, require_semantics: bool
) -> FeatureInterface:
    if not isinstance(raw, Mapping):
        raise ManifestValidationError(f"{path} must be a mapping")

    shape_values = _list(raw, "shape", path)
    if not shape_values or any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in shape_values
    ):
        raise ManifestValidationError(f"{path}.shape must contain positive integers")

    feature_names = _string_list(raw, "feature_names", path)
    shape = tuple(shape_values)
    dimension = 1
    for size in shape:
        dimension *= size
    if len(feature_names) != dimension:
        raise ManifestValidationError(
            f"{path}.feature_names has {len(feature_names)} entries; shape requires {dimension}"
        )

    semantics = _string(raw, "semantics", path) if require_semantics else raw.get("semantics")
    if semantics is not None and (not isinstance(semantics, str) or not semantics.strip()):
        raise ManifestValidationError(f"{path}.semantics must be a non-empty string")

    return FeatureInterface(
        key=_string(raw, "key", path),
        topics=_string_list(raw, "topics", path),
        message_type=_string(raw, "message_type", path),
        feature_names=feature_names,
        dtype=_string(raw, "dtype", path),
        shape=shape,
        unit=_string(raw, "unit", path),
        freshness_ms=_positive_int(raw, "freshness_ms", path),
        semantics=semantics,
    )


def _parse_camera(raw: Any, path: str) -> CameraInterface:
    if not isinstance(raw, Mapping):
        raise ManifestValidationError(f"{path} must be a mapping")
    return CameraInterface(
        key=_string(raw, "key", path),
        topic=_string(raw, "topic", path),
        message_type=_string(raw, "message_type", path),
        encoding=_string(raw, "encoding", path),
        width=_positive_int(raw, "width", path),
        height=_positive_int(raw, "height", path),
        fps=_positive_int(raw, "fps", path),
        freshness_ms=_positive_int(raw, "freshness_ms", path),
    )


def parse_manifest(data: Any) -> Manifest:
    """Validate and parse a decoded V2 manifest into dataclasses."""
    if not isinstance(data, Mapping):
        raise ManifestValidationError("manifest root must be a mapping")

    schema_version = _string(data, "schema_version", "manifest")
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise ManifestValidationError(
            f"unsupported schema_version {schema_version!r}; expected {SUPPORTED_SCHEMA_VERSION!r}"
        )

    robot_raw = _mapping(data, "robot", "manifest")
    robot = RobotConfig(
        robot_id=_string(robot_raw, "robot_id", "robot"),
        model=_string(robot_raw, "model", "robot"),
        embodiment=_string(robot_raw, "embodiment", "robot"),
        real_or_sim=_string(robot_raw, "real_or_sim", "robot"),
        host_id=_string(robot_raw, "host_id", "robot"),
        namespace=_string(robot_raw, "namespace", "robot"),
        frames=_mapping(robot_raw, "frames", "robot"),
        spool_path=_string(robot_raw, "spool_path", "robot"),
        lineage=_mapping(robot_raw, "lineage", "robot"),
    )

    interfaces_raw = _mapping(data, "interfaces", "manifest")
    state_raw = _list(interfaces_raw, "state", "interfaces")
    action_raw = _list(interfaces_raw, "action", "interfaces")
    cameras_raw = _list(interfaces_raw, "cameras", "interfaces")
    if not state_raw:
        raise ManifestValidationError("interfaces.state must not be empty")
    if not action_raw:
        raise ManifestValidationError("interfaces.action must not be empty")
    if not cameras_raw:
        raise ManifestValidationError("interfaces.cameras must contain at least one camera")

    interfaces = InterfacesConfig(
        state=tuple(
            _parse_feature_interface(raw, f"interfaces.state[{index}]", require_semantics=False)
            for index, raw in enumerate(state_raw)
        ),
        action=tuple(
            _parse_feature_interface(raw, f"interfaces.action[{index}]", require_semantics=True)
            for index, raw in enumerate(action_raw)
        ),
        cameras=tuple(
            _parse_camera(raw, f"interfaces.cameras[{index}]")
            for index, raw in enumerate(cameras_raw)
        ),
    )

    all_keys = [
        interface.key
        for interface in (*interfaces.state, *interfaces.action, *interfaces.cameras)
    ]
    if len(all_keys) != len(set(all_keys)):
        raise ManifestValidationError("interface keys must be unique")

    recording_raw = _mapping(data, "recording", "manifest")
    required_sources = _string_list(recording_raw, "required_sources", "recording")
    unknown_sources = set(required_sources) - interfaces.source_keys
    if unknown_sources:
        raise ManifestValidationError(
            "recording.required_sources contains unknown sources: "
            + ", ".join(sorted(unknown_sources))
        )
    recording = RecordingConfig(
        fps=_positive_int(recording_raw, "fps", "recording"),
        task=_string(recording_raw, "task", "recording"),
        contract=_string(recording_raw, "contract", "recording"),
        rosbag=_string(recording_raw, "rosbag", "recording"),
        required_sources=required_sources,
        staging_root=_string(recording_raw, "staging_root", "recording"),
        dataset_root=_string(recording_raw, "dataset_root", "recording"),
    )

    return Manifest(
        schema_version=schema_version,
        robot=robot,
        interfaces=interfaces,
        recording=recording,
    )


def load_manifest(path: str | Path) -> Manifest:
    """Load a YAML manifest from *path*, then validate and parse it."""
    manifest_path = Path(path)
    try:
        with manifest_path.open("r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestValidationError(
            f"failed to load manifest {manifest_path}: {exc}"
        ) from exc
    return parse_manifest(data)

