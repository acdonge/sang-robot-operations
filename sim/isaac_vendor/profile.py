"""Load and validate vendor Isaac Sim scene profiles without starting Isaac Sim."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
from typing import Any, Mapping

import yaml


SCHEMA_VERSION = "0.2"
DEFAULT_PROFILES_PATH = Path(__file__).with_name("profiles.yaml")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ProfileError(ValueError):
    """Raised when a simulation profile is unsafe or inconsistent."""


@dataclass(frozen=True)
class Artifact:
    role: str
    path: Path
    sha256: str


@dataclass(frozen=True)
class ActuatorGroup:
    name: str
    joints: tuple[str, ...]
    drive_type: str
    stiffness: float
    damping: float
    max_force: float
    max_velocity_rad_s: float


@dataclass(frozen=True)
class ObservationContract:
    joint_state_topic: str
    joint_state_frame_id: str
    publish_rate_hz: float
    joint_minimum_rate_hz: float
    jpeg_quality: int


@dataclass(frozen=True)
class CameraContract:
    key: str
    topic: str
    frame_id: str
    prim_path: str
    required_in_scene: bool
    provisioned_by: str
    mode: str
    width: int
    height: int
    minimum_rate_hz: float
    focal_length: float | None
    focus_distance: float | None
    horizontal_aperture: float | None
    clipping_range: tuple[float, float] | None
    position: tuple[float, float, float] | None
    target: tuple[float, float, float] | None
    translation: tuple[float, float, float] | None
    forward_offset_m: float | None


@dataclass(frozen=True)
class Exercise:
    joint: str
    delta_rad: float
    minimum_motion_rad: float
    maximum_error_rad: float
    settle_steps: int
    exercise_steps: int


@dataclass(frozen=True)
class VendorProfile:
    profile_id: str
    manufacturer: str
    model: str
    source_url: str
    source_revision: str
    license: str
    known_issues: tuple[str, ...]
    scene_path: Path
    scene_sha256: str
    artifacts: tuple[Artifact, ...]
    articulation_prim_path: str
    joint_prim_root: str
    disabled_prims: tuple[str, ...]
    required_prims: tuple[str, ...]
    controlled_joints: tuple[str, ...]
    ready_positions: tuple[float, ...]
    actuator_groups: tuple[ActuatorGroup, ...]
    observation: ObservationContract
    cameras: tuple[CameraContract, ...]
    exercise: Exercise


def _mapping(parent: Mapping[str, Any], key: str, path: str) -> Mapping[str, Any]:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise ProfileError(f"{path}.{key} must be a mapping")
    return value


def _list(parent: Mapping[str, Any], key: str, path: str) -> list[Any]:
    value = parent.get(key)
    if not isinstance(value, list):
        raise ProfileError(f"{path}.{key} must be a list")
    return value


def _string(parent: Mapping[str, Any], key: str, path: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"{path}.{key} must be a non-empty string")
    return value.strip()


def _string_list(parent: Mapping[str, Any], key: str, path: str) -> tuple[str, ...]:
    values = _list(parent, key, path)
    if not values or any(not isinstance(value, str) or not value.strip() for value in values):
        raise ProfileError(f"{path}.{key} must contain non-empty strings")
    result = tuple(value.strip() for value in values)
    if len(result) != len(set(result)):
        raise ProfileError(f"{path}.{key} must contain unique values")
    return result


def _number(parent: Mapping[str, Any], key: str, path: str) -> float:
    value = parent.get(key)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise ProfileError(f"{path}.{key} must be a finite number")
    return float(value)


def _positive_number(parent: Mapping[str, Any], key: str, path: str) -> float:
    value = _number(parent, key, path)
    if value <= 0.0:
        raise ProfileError(f"{path}.{key} must be positive")
    return value


def _positive_int(parent: Mapping[str, Any], key: str, path: str) -> int:
    value = parent.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ProfileError(f"{path}.{key} must be a positive integer")
    return value


def _optional_positive_number(
    parent: Mapping[str, Any], key: str, path: str
) -> float | None:
    if parent.get(key) is None:
        return None
    return _positive_number(parent, key, path)


def _vector3(
    parent: Mapping[str, Any], key: str, path: str
) -> tuple[float, float, float]:
    values = _list(parent, key, path)
    if len(values) != 3:
        raise ProfileError(f"{path}.{key} must contain three numbers")
    return tuple(
        _number({"value": value}, "value", f"{path}.{key}[{index}]")
        for index, value in enumerate(values)
    )


def _optional_vector3(
    parent: Mapping[str, Any], key: str, path: str
) -> tuple[float, float, float] | None:
    if parent.get(key) is None:
        return None
    return _vector3(parent, key, path)


def _topic(parent: Mapping[str, Any], key: str, path: str) -> str:
    value = _string(parent, key, path)
    if not value.startswith("/") or "//" in value or value.endswith("/"):
        raise ProfileError(f"{path}.{key} must be an absolute ROS topic")
    return value


def _sha256(parent: Mapping[str, Any], key: str, path: str) -> str:
    value = _string(parent, key, path).lower()
    if not SHA256_PATTERN.fullmatch(value):
        raise ProfileError(f"{path}.{key} must be a lowercase SHA-256 digest")
    return value


def _prim_path(parent: Mapping[str, Any], key: str, path: str) -> str:
    value = _string(parent, key, path)
    if not value.startswith("/") or "//" in value:
        raise ProfileError(f"{path}.{key} must be an absolute USD prim path")
    return value.rstrip("/")


def _resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()


def _parse_profile(
    profile_id: str,
    raw: Mapping[str, Any],
    *,
    base_dir: Path,
) -> VendorProfile:
    path = f"profiles.{profile_id}"
    provenance = _mapping(raw, "provenance", path)
    scene = _mapping(raw, "scene", path)
    joints = _mapping(raw, "joints", path)
    observation_raw = _mapping(raw, "observation", path)
    exercise_raw = _mapping(joints, "exercise", f"{path}.joints")
    controlled = _string_list(joints, "controlled", f"{path}.joints")

    ready_raw = _mapping(joints, "ready_positions", f"{path}.joints")
    if set(ready_raw) != set(controlled):
        raise ProfileError(
            f"{path}.joints.ready_positions keys must exactly match controlled joints"
        )
    ready_positions = tuple(
        _number(ready_raw, name, f"{path}.joints.ready_positions")
        for name in controlled
    )

    artifact_items = []
    for index, item in enumerate(_list(raw, "artifacts", path)):
        item_path = f"{path}.artifacts[{index}]"
        if not isinstance(item, Mapping):
            raise ProfileError(f"{item_path} must be a mapping")
        artifact_items.append(
            Artifact(
                role=_string(item, "role", item_path),
                path=_resolve_path(_string(item, "path", item_path), base_dir),
                sha256=_sha256(item, "sha256", item_path),
            )
        )
    if len({artifact.role for artifact in artifact_items}) != len(artifact_items):
        raise ProfileError(f"{path}.artifacts roles must be unique")

    actuator_groups = []
    for index, item in enumerate(_list(joints, "actuators", f"{path}.joints")):
        item_path = f"{path}.joints.actuators[{index}]"
        if not isinstance(item, Mapping):
            raise ProfileError(f"{item_path} must be a mapping")
        drive_type = _string(item, "drive_type", item_path)
        if drive_type not in {"force", "acceleration"}:
            raise ProfileError(f"{item_path}.drive_type must be force or acceleration")
        actuator_groups.append(
            ActuatorGroup(
                name=_string(item, "name", item_path),
                joints=_string_list(item, "joints", item_path),
                drive_type=drive_type,
                stiffness=_positive_number(item, "stiffness", item_path),
                damping=_number(item, "damping", item_path),
                max_force=_positive_number(item, "max_force", item_path),
                max_velocity_rad_s=_positive_number(
                    item, "max_velocity_rad_s", item_path
                ),
            )
        )
    actuator_joints = {
        joint for group in actuator_groups for joint in group.joints
    }
    if not set(controlled).issubset(actuator_joints):
        raise ProfileError(f"{path}.joints.actuators must cover every controlled joint")

    jpeg_quality = _positive_int(
        observation_raw, "jpeg_quality", f"{path}.observation"
    )
    if jpeg_quality > 100:
        raise ProfileError(f"{path}.observation.jpeg_quality must not exceed 100")
    observation = ObservationContract(
        joint_state_topic=_topic(
            observation_raw, "joint_state_topic", f"{path}.observation"
        ),
        joint_state_frame_id=_string(
            observation_raw, "joint_state_frame_id", f"{path}.observation"
        ),
        publish_rate_hz=_positive_number(
            observation_raw, "publish_rate_hz", f"{path}.observation"
        ),
        joint_minimum_rate_hz=_positive_number(
            observation_raw, "joint_minimum_rate_hz", f"{path}.observation"
        ),
        jpeg_quality=jpeg_quality,
    )
    if observation.joint_minimum_rate_hz > observation.publish_rate_hz:
        raise ProfileError(
            f"{path}.observation.joint_minimum_rate_hz exceeds publish_rate_hz"
        )

    cameras = []
    for index, item in enumerate(_list(raw, "cameras", path)):
        item_path = f"{path}.cameras[{index}]"
        if not isinstance(item, Mapping):
            raise ProfileError(f"{item_path} must be a mapping")
        required = item.get("required_in_scene")
        if not isinstance(required, bool):
            raise ProfileError(f"{item_path}.required_in_scene must be a boolean")
        mode = _string(item, "mode", item_path)
        if mode not in {"existing", "world_look_at", "link_mount_look_at"}:
            raise ProfileError(
                f"{item_path}.mode must be existing, world_look_at, or link_mount_look_at"
            )
        position = _optional_vector3(item, "position", item_path)
        target = _optional_vector3(item, "target", item_path)
        translation = _optional_vector3(item, "translation", item_path)
        forward_offset_m = _optional_positive_number(
            item, "forward_offset_m", item_path
        )
        if mode == "existing":
            if not required or any(
                value is not None
                for value in (position, target, translation, forward_offset_m)
            ):
                raise ProfileError(
                    f"{item_path} existing cameras must be required and omit pose fields"
                )
        elif mode == "world_look_at":
            if required or position is None or target is None or any(
                value is not None for value in (translation, forward_offset_m)
            ):
                raise ProfileError(
                    f"{item_path} world_look_at requires only position and target"
                )
        elif (
            required
            or translation is None
            or target is None
            or forward_offset_m is None
            or position is not None
        ):
            raise ProfileError(
                f"{item_path} link_mount_look_at requires translation, target, and forward_offset_m"
            )
        clipping_raw = item.get("clipping_range")
        clipping_range = None
        if clipping_raw is not None:
            if not isinstance(clipping_raw, list) or len(clipping_raw) != 2:
                raise ProfileError(
                    f"{item_path}.clipping_range must contain near and far distances"
                )
            clipping_range = tuple(
                _positive_number(
                    {"value": value}, "value", f"{item_path}.clipping_range[{offset}]"
                )
                for offset, value in enumerate(clipping_raw)
            )
            if clipping_range[0] >= clipping_range[1]:
                raise ProfileError(
                    f"{item_path}.clipping_range near distance must be below far distance"
                )
        cameras.append(
            CameraContract(
                key=_string(item, "key", item_path),
                topic=_topic(item, "topic", item_path),
                frame_id=_string(item, "frame_id", item_path),
                prim_path=_prim_path(item, "prim_path", item_path),
                required_in_scene=required,
                provisioned_by=_string(item, "provisioned_by", item_path),
                mode=mode,
                width=_positive_int(item, "width", item_path),
                height=_positive_int(item, "height", item_path),
                minimum_rate_hz=_positive_number(
                    item, "minimum_rate_hz", item_path
                ),
                focal_length=_optional_positive_number(
                    item, "focal_length", item_path
                ),
                focus_distance=_optional_positive_number(
                    item, "focus_distance", item_path
                ),
                horizontal_aperture=_optional_positive_number(
                    item, "horizontal_aperture", item_path
                ),
                clipping_range=clipping_range,
                position=position,
                target=target,
                translation=translation,
                forward_offset_m=forward_offset_m,
            )
        )
    if len({camera.key for camera in cameras}) != len(cameras):
        raise ProfileError(f"{path}.cameras keys must be unique")
    camera_topics = {camera.topic for camera in cameras}
    if len(camera_topics) != len(cameras):
        raise ProfileError(f"{path}.cameras topics must be unique")
    if observation.joint_state_topic in camera_topics:
        raise ProfileError(f"{path} observation topics must be unique")
    if any(camera.minimum_rate_hz > observation.publish_rate_hz for camera in cameras):
        raise ProfileError(f"{path}.cameras minimum_rate_hz exceeds publish_rate_hz")

    exercise = Exercise(
        joint=_string(exercise_raw, "joint", f"{path}.joints.exercise"),
        delta_rad=_number(exercise_raw, "delta_rad", f"{path}.joints.exercise"),
        minimum_motion_rad=_positive_number(
            exercise_raw, "minimum_motion_rad", f"{path}.joints.exercise"
        ),
        maximum_error_rad=_positive_number(
            exercise_raw, "maximum_error_rad", f"{path}.joints.exercise"
        ),
        settle_steps=_positive_int(
            exercise_raw, "settle_steps", f"{path}.joints.exercise"
        ),
        exercise_steps=_positive_int(
            exercise_raw, "exercise_steps", f"{path}.joints.exercise"
        ),
    )
    if exercise.joint not in controlled:
        raise ProfileError(f"{path}.joints.exercise.joint must be controlled")
    if not 0.0 < abs(exercise.delta_rad) <= 0.25:
        raise ProfileError(f"{path}.joints.exercise.delta_rad must be within 0.25 rad")
    if exercise.minimum_motion_rad > abs(exercise.delta_rad):
        raise ProfileError(
            f"{path}.joints.exercise.minimum_motion_rad exceeds delta_rad"
        )

    required_prims = tuple(
        _prim_path({"value": value}, "value", f"{path}.scene.required_prims")
        for value in _string_list(scene, "required_prims", f"{path}.scene")
    )
    disabled_raw = scene.get("disabled_prims", [])
    if not isinstance(disabled_raw, list) or any(
        not isinstance(value, str) or not value.startswith("/")
        for value in disabled_raw
    ):
        raise ProfileError(f"{path}.scene.disabled_prims must contain prim paths")
    known_issues_raw = raw.get("known_issues", [])
    if not isinstance(known_issues_raw, list) or any(
        not isinstance(value, str) or not value.strip()
        for value in known_issues_raw
    ):
        raise ProfileError(f"{path}.known_issues must contain non-empty strings")

    return VendorProfile(
        profile_id=profile_id,
        manufacturer=_string(raw, "manufacturer", path),
        model=_string(raw, "model", path),
        source_url=_string(provenance, "source_url", f"{path}.provenance"),
        source_revision=_string(
            provenance, "source_revision", f"{path}.provenance"
        ),
        license=_string(provenance, "license", f"{path}.provenance"),
        known_issues=tuple(value.strip() for value in known_issues_raw),
        scene_path=_resolve_path(_string(scene, "path", f"{path}.scene"), base_dir),
        scene_sha256=_sha256(scene, "sha256", f"{path}.scene"),
        artifacts=tuple(artifact_items),
        articulation_prim_path=_prim_path(
            scene, "articulation_prim_path", f"{path}.scene"
        ),
        joint_prim_root=_prim_path(scene, "joint_prim_root", f"{path}.scene"),
        disabled_prims=tuple(value.rstrip("/") for value in disabled_raw),
        required_prims=required_prims,
        controlled_joints=controlled,
        ready_positions=ready_positions,
        actuator_groups=tuple(actuator_groups),
        observation=observation,
        cameras=tuple(cameras),
        exercise=exercise,
    )


def load_profiles(path: str | Path = DEFAULT_PROFILES_PATH) -> dict[str, VendorProfile]:
    config_path = Path(path).resolve()
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileError(f"failed to load profiles {config_path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ProfileError("profile root must be a mapping")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ProfileError(
            f"unsupported schema_version {data.get('schema_version')!r}; "
            f"expected {SCHEMA_VERSION!r}"
        )
    raw_profiles = _mapping(data, "profiles", "root")
    if not raw_profiles:
        raise ProfileError("root.profiles must not be empty")
    profiles = {}
    for profile_id, raw in raw_profiles.items():
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise ProfileError("profile IDs must be non-empty strings")
        if not isinstance(raw, Mapping):
            raise ProfileError(f"profiles.{profile_id} must be a mapping")
        profiles[profile_id] = _parse_profile(
            profile_id,
            raw,
            base_dir=config_path.parent,
        )
    return profiles


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_profile_files(profile: VendorProfile) -> list[dict[str, Any]]:
    checks = [
        ("scene", profile.scene_path, profile.scene_sha256),
        *((artifact.role, artifact.path, artifact.sha256) for artifact in profile.artifacts),
    ]
    results = []
    for role, path, expected in checks:
        exists = path.is_file()
        actual = file_sha256(path) if exists else None
        results.append(
            {
                "role": role,
                "path": str(path),
                "exists": exists,
                "expected_sha256": expected,
                "actual_sha256": actual,
                "matches": exists and actual == expected,
            }
        )
    return results


def require_profile_files(profile: VendorProfile) -> list[dict[str, Any]]:
    checks = verify_profile_files(profile)
    failures = [check for check in checks if not check["matches"]]
    if failures:
        roles = ", ".join(check["role"] for check in failures)
        raise ProfileError(f"{profile.profile_id} vendor file checks failed: {roles}")
    return checks
