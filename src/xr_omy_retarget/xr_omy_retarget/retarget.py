"""Pure OMY retargeting state machine shared by ROS and unit tests."""

from __future__ import annotations

from dataclasses import dataclass
import math

from xr_omy_retarget.math3d import (
    Quaternion,
    Vector3,
    add,
    map_xr_vector,
    operator_relative_orientation,
    quaternion,
    scale,
    subtract,
    vector3,
)


@dataclass(frozen=True)
class RetargetConfig:
    # FK of the official OMY F3M ready joint pose at end_effector_link.
    neutral_position: Vector3 = (0.19080657, -0.11299910, 0.39004890)
    neutral_orientation: Quaternion = (0.0, 0.0, 0.70710678, 0.70710678)
    translation_scale: float = 1.0
    track_orientation: bool = False
    workspace_min: Vector3 = (0.05, -0.45, 0.05)
    workspace_max: Vector3 = (0.58, 0.40, 0.72)
    max_rotation_delta_rad: float = 2.09439510239
    deadman_threshold: float = 0.5
    stale_timeout_s: float = 0.25

    def __post_init__(self) -> None:
        object.__setattr__(self, "neutral_position", vector3(self.neutral_position))
        object.__setattr__(
            self, "neutral_orientation", quaternion(self.neutral_orientation)
        )
        object.__setattr__(self, "workspace_min", vector3(self.workspace_min))
        object.__setattr__(self, "workspace_max", vector3(self.workspace_max))
        if not isinstance(self.track_orientation, bool):
            raise ValueError("track_orientation must be boolean")
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
    clutch_reanchored: bool
    clutch_reanchors: int


class Retargeter:
    def __init__(self, config: RetargetConfig) -> None:
        self.config = config
        self._source_anchor: ControllerSample | None = None
        self._target_anchor_position = config.neutral_position
        self._target_anchor_orientation = config.neutral_orientation
        self._last_target_position = config.neutral_position
        self._last_target_orientation = config.neutral_orientation
        self._was_deadman_pressed = False
        self._clutch_reanchors = 0

    @property
    def calibrated(self) -> bool:
        return self._source_anchor is not None

    @property
    def clutch_reanchors(self) -> int:
        return self._clutch_reanchors

    def calibrate(self, sample: ControllerSample) -> None:
        if not sample.tracking_valid:
            raise ValueError("controller pose must be connected, valid, and tracked")
        self._source_anchor = sample
        self._target_anchor_position = self.config.neutral_position
        self._target_anchor_orientation = self.config.neutral_orientation
        self._last_target_position = self.config.neutral_position
        self._last_target_orientation = self.config.neutral_orientation
        self._was_deadman_pressed = (
            sample.squeeze >= self.config.deadman_threshold
        )
        self._clutch_reanchors = 0

    def clear_calibration(self) -> None:
        self._source_anchor = None
        self._target_anchor_position = self.config.neutral_position
        self._target_anchor_orientation = self.config.neutral_orientation
        self._last_target_position = self.config.neutral_position
        self._last_target_orientation = self.config.neutral_orientation
        self._was_deadman_pressed = False
        self._clutch_reanchors = 0

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
        valid_for_clutch = self.calibrated and fresh and tracking_valid
        clutch_reanchored = False

        if valid_for_clutch and deadman_pressed and not self._was_deadman_pressed:
            self._source_anchor = sample
            self._target_anchor_position = self._last_target_position
            self._target_anchor_orientation = self._last_target_orientation
            self._clutch_reanchors += 1
            clutch_reanchored = True

        target_position = None
        target_orientation = None
        rotation_delta_rad = 0.0
        within_workspace = False
        rotation_within_limit = False
        if (
            self.calibrated
            and sample is not None
            and tracking_valid
            and self._source_anchor is not None
        ):
            delta_xr = subtract(sample.position, self._source_anchor.position)
            target_position = add(
                self._target_anchor_position,
                scale(map_xr_vector(delta_xr), self.config.translation_scale),
            )
            if self.config.track_orientation:
                target_orientation, rotation_delta_rad = (
                    operator_relative_orientation(
                        self._source_anchor.orientation,
                        sample.orientation,
                        (0.0, 0.0, 0.0, 1.0),
                        self._target_anchor_orientation,
                    )
                )
            else:
                target_orientation = self._target_anchor_orientation
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

        output_enabled = inhibit_reason == "enabled"
        if output_enabled:
            assert target_position is not None
            assert target_orientation is not None
            self._last_target_position = target_position
            self._last_target_orientation = target_orientation

        self._was_deadman_pressed = bool(valid_for_clutch and deadman_pressed)
        return RetargetResult(
            calibrated=self.calibrated,
            fresh=fresh,
            tracking_valid=tracking_valid,
            deadman_pressed=deadman_pressed,
            within_workspace=within_workspace,
            rotation_within_limit=rotation_within_limit,
            output_enabled=output_enabled,
            inhibit_reason=inhibit_reason,
            target_position=target_position,
            target_orientation=target_orientation,
            rotation_delta_rad=rotation_delta_rad,
            gripper=sample.trigger if sample is not None else 0.0,
            sequence=sample.sequence if sample is not None else 0,
            clutch_reanchored=clutch_reanchored,
            clutch_reanchors=self._clutch_reanchors,
        )
