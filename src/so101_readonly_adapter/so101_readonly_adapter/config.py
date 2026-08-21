"""Physical device binding for the SO-101 observation adapters."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, Literal, Mapping

import yaml


SUPPORTED_SCHEMA_VERSION = "0.1"


class HardwareConfigError(ValueError):
    """Raised when physical adapter configuration is invalid."""


@dataclass(frozen=True)
class ArmConfig:
    key: str
    role: Literal["follower", "leader"]
    side: Literal["left", "right"]
    port: str
    calibration_path: Path
    topic: str
    publish_hz: float
    baudrate: int
    expected_model: int


@dataclass(frozen=True)
class CameraConfig:
    key: str
    device: str
    topic: str
    width: int
    height: int
    fps: float
    fourcc: str
    fixed_fps: bool = False
    jpeg_quality: int = 90
    mjpg_passthrough: bool = True


@dataclass(frozen=True)
class HardwareConfig:
    schema_version: str
    arms: tuple[ArmConfig, ...]
    cameras: tuple[CameraConfig, ...]


def _mapping(parent: Mapping[str, Any], key: str, path: str) -> Mapping[str, Any]:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise HardwareConfigError(f"{path}.{key} must be a mapping")
    return value


def _list(
    parent: Mapping[str, Any],
    key: str,
    path: str,
    *,
    non_empty: bool = True,
) -> list[Any]:
    value = parent.get(key)
    if not isinstance(value, list):
        raise HardwareConfigError(f"{path}.{key} must be a list")
    if non_empty and not value:
        raise HardwareConfigError(f"{path}.{key} must be a non-empty list")
    return value


def _string(parent: Mapping[str, Any], key: str, path: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HardwareConfigError(f"{path}.{key} must be a non-empty string")
    return value.strip()


def _integer(parent: Mapping[str, Any], key: str, path: str) -> int:
    value = parent.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise HardwareConfigError(f"{path}.{key} must be an integer")
    return value


def _positive_number(parent: Mapping[str, Any], key: str, path: str) -> float:
    value = parent.get(key)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise HardwareConfigError(f"{path}.{key} must be a positive number")
    return float(value)


def _optional_boolean(
    parent: Mapping[str, Any], key: str, path: str, *, default: bool
) -> bool:
    value = parent.get(key, default)
    if not isinstance(value, bool):
        raise HardwareConfigError(f"{path}.{key} must be a boolean")
    return value


def _optional_integer_range(
    parent: Mapping[str, Any],
    key: str,
    path: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = parent.get(key, default)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or value > maximum
    ):
        raise HardwareConfigError(
            f"{path}.{key} must be an integer between {minimum} and {maximum}"
        )
    return value


def _resolve(path: str, base: Path) -> Path:
    value = Path(path).expanduser()
    return value if value.is_absolute() else base / value


def _arm(raw: Any, path: str, base: Path) -> ArmConfig:
    if not isinstance(raw, Mapping):
        raise HardwareConfigError(f"{path} must be a mapping")
    role = _string(raw, "role", path)
    if role not in {"follower", "leader"}:
        raise HardwareConfigError(f"{path}.role must be follower or leader")
    side = _string(raw, "side", path)
    if side not in {"left", "right"}:
        raise HardwareConfigError(f"{path}.side must be left or right")
    baudrate = _integer(raw, "baudrate", path)
    expected_model = _integer(raw, "expected_model", path)
    if baudrate <= 0 or expected_model <= 0:
        raise HardwareConfigError(
            f"{path}.baudrate and expected_model must be positive"
        )
    return ArmConfig(
        key=_string(raw, "key", path),
        role=role,
        side=side,
        port=_string(raw, "port", path),
        calibration_path=_resolve(_string(raw, "calibration", path), base),
        topic=_string(raw, "topic", path),
        publish_hz=_positive_number(raw, "publish_hz", path),
        baudrate=baudrate,
        expected_model=expected_model,
    )


def _camera(raw: Any, path: str) -> CameraConfig:
    if not isinstance(raw, Mapping):
        raise HardwareConfigError(f"{path} must be a mapping")
    width = _integer(raw, "width", path)
    height = _integer(raw, "height", path)
    fourcc = _string(raw, "fourcc", path)
    if width <= 0 or height <= 0:
        raise HardwareConfigError(f"{path}.width and height must be positive")
    if len(fourcc) != 4:
        raise HardwareConfigError(f"{path}.fourcc must contain exactly 4 characters")
    if fourcc not in {"MJPG", "YUYV"}:
        raise HardwareConfigError(f"{path}.fourcc must be MJPG or YUYV")
    return CameraConfig(
        key=_string(raw, "key", path),
        device=_string(raw, "device", path),
        topic=_string(raw, "topic", path),
        width=width,
        height=height,
        fps=_positive_number(raw, "fps", path),
        fourcc=fourcc,
        fixed_fps=_optional_boolean(raw, "fixed_fps", path, default=False),
        jpeg_quality=_optional_integer_range(
            raw,
            "jpeg_quality",
            path,
            default=90,
            minimum=1,
            maximum=100,
        ),
        mjpg_passthrough=_optional_boolean(
            raw,
            "mjpg_passthrough",
            path,
            default=True,
        ),
    )


def parse_hardware_config(data: Any, *, base: Path = Path(".")) -> HardwareConfig:
    if not isinstance(data, Mapping):
        raise HardwareConfigError("hardware config root must be a mapping")
    schema_version = _string(data, "schema_version", "hardware")
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise HardwareConfigError(
            f"unsupported schema_version {schema_version!r}; "
            f"expected {SUPPORTED_SCHEMA_VERSION!r}"
        )
    arms = tuple(
        _arm(raw, f"hardware.arms[{index}]", base)
        for index, raw in enumerate(
            _list(data, "arms", "hardware", non_empty=False)
        )
    )
    cameras = tuple(
        _camera(raw, f"hardware.cameras[{index}]")
        for index, raw in enumerate(_list(data, "cameras", "hardware"))
    )

    arm_keys = [arm.key for arm in arms]
    camera_keys = [camera.key for camera in cameras]
    topics = [arm.topic for arm in arms] + [camera.topic for camera in cameras]
    ports = [arm.port for arm in arms]
    devices = [camera.device for camera in cameras]
    for values, label in (
        (arm_keys, "arm keys"),
        (camera_keys, "camera keys"),
        (topics, "topics"),
        (ports, "arm ports"),
        (devices, "camera devices"),
    ):
        if len(values) != len(set(values)):
            raise HardwareConfigError(f"hardware {label} must be unique")
    role_sides = [(arm.role, arm.side) for arm in arms]
    if len(role_sides) != len(set(role_sides)):
        raise HardwareConfigError("hardware arm role/side pairs must be unique")
    return HardwareConfig(schema_version, arms, cameras)


def load_hardware_config(path: str | Path) -> HardwareConfig:
    config_path = Path(path).expanduser()
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise HardwareConfigError(
            f"failed to load hardware config {config_path}: {exc}"
        ) from exc
    return parse_hardware_config(data, base=config_path.parent)
