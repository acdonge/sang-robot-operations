"""Pure retarget state machine used by the ROS node and unit tests."""

from __future__ import annotations

from dataclasses import dataclass
import math

from xr_teleop_retarget.math3d import (
    Quaternion,
    Vector3,
    add,
    map_xr_vector,
    quaternion,
    relative_target_orientation,
    scale,
    subtract,
    vector3,
)


@dataclass(frozen=True)
class RetargetConfig:
    neutral_position: Vector3 = (0.29323402, -0.00017677, 0.23437033)
    neutral_orientation: Quaternion = (
        0.01721363,
        -0.70689953,
        -0.01720645,
        0.70689510,
    )
    translation_scale: float = 0.5
    workspace_min: Vector3 = (0.10, -0.20, 0.05)
    workspace_max: Vector3 = (0.40, 0.20, 0.35)
    max_rotation_delta_rad: float = math.pi / 2.0
    deadman_threshold: float = 0.5
    stale_timeout_s: float = 0.25

    def __post_init__(self) -> None:
        object.__setattr__(self, "neutral_position", vector3(self.neutral_position))
        object.__setattr__(
            self, "neutral_orientation", quaternion(self.neutral_orientation)
        )
        object.__setattr__(self, "workspace_min", vector3(self.workspace_min))
        object.__setattr__(self, "workspace_max", vector3(self.workspace_max))
        for lower, upper in zip(self.workspace_min, self.workspace_max):
            if lower >= upper:
                raise ValueError("workspace_min must be below workspace_max")
        for name, value in (
            ("translation_scale", self.translation_scale),
            ("max_rotation_delta_rad", self.max_rotation_delta_rad),
            ("stale_timeout_s", self.stale_timeout_s),
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be positive and finite")
        if not 0.0 <= self.deadman_threshold <= 1.0:
            raise ValueError("deadman_threshold must be between 0 and 1")


@dataclass(frozen=True)
class ControllerSample:
    position: Vector3
    orientation: Quaternion
    connected: bool
    pose_valid: bool
    pose_tracked: bool
    squeeze: float
    trigger: float
    sequence: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "position", vector3(self.position))
        object.__setattr__(self, "orientation", quaternion(self.orientation))
        for name, value in (("squeeze", self.squeeze), ("trigger", self.trigger)):
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be finite and between 0 and 1")

    @property
    def tracking_valid(self) -> bool:
        return self.connected and self.pose_valid and self.pose_tracked


@dataclass(frozen=True)
class RetargetResult:
    calibrated: bool
    fresh: bool
    tracking_valid: bool
    deadman_pressed: bool
    within_workspace: bool
    rotation_within_limit: bool
    output_enabled: bool
    inhibit_reason: str
    target_position: Vector3 | None
    target_orientation: Quaternion | None
    rotation_delta_rad: float
    gripper: float
    sequence: int


class Retargeter:
    def __init__(self, config: RetargetConfig) -> None:
        self.config = config
        self._anchor: ControllerSample | None = None

    @property
    def calibrated(self) -> bool:
        return self._anchor is not None

    def calibrate(self, sample: ControllerSample) -> None:
        if not sample.tracking_valid:
            raise ValueError("controller pose must be connected, valid, and tracked")
        self._anchor = sample

    def clear_calibration(self) -> None:
        self._anchor = None

    def evaluate(
        self, sample: ControllerSample | None, input_age_s: float
    ) -> RetargetResult:
        fresh = (
            sample is not None
            and math.isfinite(input_age_s)
            and 0.0 <= input_age_s <= self.config.stale_timeout_s
        )
        tracking_valid = sample.tracking_valid if sample is not None else False
        deadman_pressed = (
            sample is not None
            and sample.squeeze >= self.config.deadman_threshold
        )
        target_position = None
        target_orientation = None
        rotation_delta_rad = 0.0
        within_workspace = False
        rotation_within_limit = False

        if self._anchor is not None and sample is not None and tracking_valid:
            xr_delta = subtract(sample.position, self._anchor.position)
            target_position = add(
                self.config.neutral_position,
                scale(map_xr_vector(xr_delta), self.config.translation_scale),
            )
            target_orientation, rotation_delta_rad = relative_target_orientation(
                self._anchor.orientation,
                sample.orientation,
                self.config.neutral_orientation,
            )
            within_workspace = all(
                lower <= value <= upper
                for value, lower, upper in zip(
                    target_position,
                    self.config.workspace_min,
                    self.config.workspace_max,
                )
            )
            rotation_within_limit = (
                rotation_delta_rad <= self.config.max_rotation_delta_rad
            )

        conditions = (
            (self.calibrated, "not_calibrated"),
            (fresh, "input_stale"),
            (tracking_valid, "tracking_invalid"),
            (deadman_pressed, "deadman_released"),
            (within_workspace, "outside_workspace"),
            (rotation_within_limit, "rotation_limit"),
        )
        inhibit_reason = "enabled"
        for passed, reason in conditions:
            if not passed:
                inhibit_reason = reason
                break

        return RetargetResult(
            calibrated=self.calibrated,
            fresh=fresh,
            tracking_valid=tracking_valid,
            deadman_pressed=deadman_pressed,
            within_workspace=within_workspace,
            rotation_within_limit=rotation_within_limit,
            output_enabled=inhibit_reason == "enabled",
            inhibit_reason=inhibit_reason,
            target_position=target_position,
            target_orientation=target_orientation,
            rotation_delta_rad=rotation_delta_rad,
            gripper=sample.trigger if sample is not None else 0.0,
            sequence=sample.sequence if sample is not None else 0,
        )
