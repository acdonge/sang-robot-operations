"""SO-101 calibration loading and LeRobot-compatible normalization."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping


MOTOR_ORDER = (
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
)


class CalibrationError(ValueError):
    """Raised when an SO-101 calibration file is unsafe to use."""


@dataclass(frozen=True)
class MotorCalibration:
    name: str
    motor_id: int
    drive_mode: int
    homing_offset: int
    range_min: int
    range_max: int

    def normalize(self, raw_position: int) -> float:
        bounded = min(self.range_max, max(self.range_min, raw_position))
        ratio = (bounded - self.range_min) / (self.range_max - self.range_min)
        if self.name == "gripper":
            value = ratio * 100.0
            return 100.0 - value if self.drive_mode else value
        value = ratio * 200.0 - 100.0
        return -value if self.drive_mode else value


@dataclass(frozen=True)
class ArmCalibration:
    motors: tuple[MotorCalibration, ...]

    @property
    def motor_ids(self) -> tuple[int, ...]:
        return tuple(motor.motor_id for motor in self.motors)

    @property
    def motor_names(self) -> tuple[str, ...]:
        return tuple(motor.name for motor in self.motors)

    def normalize(self, raw_positions: Mapping[int, int]) -> tuple[float, ...]:
        missing = set(self.motor_ids) - set(raw_positions)
        if missing:
            raise CalibrationError(
                "position sample is missing motor IDs: "
                + ", ".join(str(value) for value in sorted(missing))
            )
        return tuple(
            motor.normalize(int(raw_positions[motor.motor_id]))
            for motor in self.motors
        )


def _integer(parent: Mapping[str, Any], key: str, path: str) -> int:
    value = parent.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise CalibrationError(f"{path}.{key} must be an integer")
    return value


def parse_calibration(data: Any) -> ArmCalibration:
    if not isinstance(data, Mapping):
        raise CalibrationError("calibration root must be a mapping")
    actual_names = set(data)
    expected_names = set(MOTOR_ORDER)
    if actual_names != expected_names:
        missing = expected_names - actual_names
        extra = actual_names - expected_names
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if extra:
            details.append("unknown " + ", ".join(sorted(extra)))
        raise CalibrationError("calibration motors mismatch: " + "; ".join(details))

    motors = []
    for name in MOTOR_ORDER:
        raw = data[name]
        path = f"calibration.{name}"
        if not isinstance(raw, Mapping):
            raise CalibrationError(f"{path} must be a mapping")
        motor_id = _integer(raw, "id", path)
        drive_mode = _integer(raw, "drive_mode", path)
        homing_offset = _integer(raw, "homing_offset", path)
        range_min = _integer(raw, "range_min", path)
        range_max = _integer(raw, "range_max", path)
        if not 1 <= motor_id <= 253:
            raise CalibrationError(f"{path}.id must be between 1 and 253")
        if drive_mode not in (0, 1):
            raise CalibrationError(f"{path}.drive_mode must be 0 or 1")
        if not 0 <= range_min < range_max <= 4095:
            raise CalibrationError(
                f"{path} requires 0 <= range_min < range_max <= 4095"
            )
        motors.append(
            MotorCalibration(
                name=name,
                motor_id=motor_id,
                drive_mode=drive_mode,
                homing_offset=homing_offset,
                range_min=range_min,
                range_max=range_max,
            )
        )
    ids = [motor.motor_id for motor in motors]
    if len(ids) != len(set(ids)):
        raise CalibrationError("calibration motor IDs must be unique")
    return ArmCalibration(tuple(motors))


def load_calibration(path: str | Path) -> ArmCalibration:
    calibration_path = Path(path)
    try:
        data = json.loads(calibration_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationError(
            f"failed to load calibration {calibration_path}: {exc}"
        ) from exc
    return parse_calibration(data)
