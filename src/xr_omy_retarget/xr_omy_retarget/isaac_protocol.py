"""Validation and freshness gating for XR-to-OMY Isaac targets."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping


PROTOCOL = "xr_omy_target_v1"
BASE_FRAME = "link0"
TARGET_LINK = "end_effector_link"
WORKSPACE_MIN = (0.05, -0.45, 0.05)
WORKSPACE_MAX = (0.58, 0.40, 0.72)


@dataclass(frozen=True)
class IsaacTarget:
    sequence: int
    ros_stamp_ns: int
    position: tuple[float, float, float] | None
    orientation_xyzw: tuple[float, float, float, float] | None
    gripper: float
    enabled: bool
    inhibit_reason: str
    clutch_epoch: int


@dataclass(frozen=True)
class StreamStatus:
    command: IsaacTarget | None
    active: bool
    reason: str
    age_s: float


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _finite_values(value: Any, size: int, name: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError(f"{name} must contain {size} values")
    try:
        result = tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain numeric values") from exc
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{name} must contain only finite values")
    return result


def parse_target(document: Mapping[str, Any]) -> IsaacTarget:
    if not isinstance(document, Mapping):
        raise ValueError("target document must be an object")
    if document.get("protocol") != PROTOCOL:
        raise ValueError("unsupported protocol")
    sequence = _integer(document.get("sequence"), "sequence")
    ros_stamp_ns = _integer(document.get("ros_stamp_ns"), "ros_stamp_ns")
    clutch_epoch = _integer(document.get("clutch_epoch"), "clutch_epoch")
    enabled = document.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be boolean")
    inhibit_reason = document.get("inhibit_reason")
    if not isinstance(inhibit_reason, str) or not inhibit_reason:
        raise ValueError("inhibit_reason must be a non-empty string")
    try:
        gripper = float(document.get("gripper"))
    except (TypeError, ValueError) as exc:
        raise ValueError("gripper must be numeric") from exc
    if not math.isfinite(gripper) or not 0.0 <= gripper <= 1.0:
        raise ValueError("gripper must be finite and between 0 and 1")
    if document.get("target_link") != TARGET_LINK:
        raise ValueError(f"target_link must be {TARGET_LINK}")

    target = document.get("target")
    position = None
    orientation = None
    if target is not None:
        if not isinstance(target, Mapping):
            raise ValueError("target must be an object or null")
        if target.get("frame_id") != BASE_FRAME:
            raise ValueError(f"target frame_id must be {BASE_FRAME}")
        position_values = _finite_values(target.get("position"), 3, "position")
        orientation_values = _finite_values(
            target.get("orientation_xyzw"), 4, "orientation_xyzw"
        )
        norm = math.sqrt(sum(value * value for value in orientation_values))
        if abs(norm - 1.0) > 1e-3:
            raise ValueError("orientation_xyzw must be a unit quaternion")
        position = position_values  # type: ignore[assignment]
        orientation = tuple(value / norm for value in orientation_values)
    if enabled and target is None:
        raise ValueError("enabled target must contain a pose")
    if enabled and position is not None and not all(
        lower <= value <= upper
        for value, lower, upper in zip(
            position, WORKSPACE_MIN, WORKSPACE_MAX
        )
    ):
        raise ValueError("enabled target is outside the OMY workspace")

    return IsaacTarget(
        sequence=sequence,
        ros_stamp_ns=ros_stamp_ns,
        position=position,
        orientation_xyzw=orientation,  # type: ignore[arg-type]
        gripper=gripper,
        enabled=enabled,
        inhibit_reason=inhibit_reason,
        clutch_epoch=clutch_epoch,
    )


class TargetStream:
    def __init__(
        self, stale_timeout_s: float = 0.25, sequence_reset_after_s: float = 1.0
    ) -> None:
        if not math.isfinite(stale_timeout_s) or stale_timeout_s <= 0.0:
            raise ValueError("stale_timeout_s must be positive and finite")
        if (
            not math.isfinite(sequence_reset_after_s)
            or sequence_reset_after_s <= stale_timeout_s
        ):
            raise ValueError("sequence_reset_after_s must exceed stale_timeout_s")
        self.stale_timeout_s = stale_timeout_s
        self.sequence_reset_after_s = sequence_reset_after_s
        self._command: IsaacTarget | None = None
        self._received_s: float | None = None
        self._last_accepted_s: float | None = None
        self._invalid_reason: str | None = None

    def ingest(self, document: Mapping[str, Any], now_s: float) -> IsaacTarget:
        if not math.isfinite(now_s):
            raise ValueError("now_s must be finite")
        command = parse_target(document)
        if self._command is not None and command.sequence < self._command.sequence:
            assert self._last_accepted_s is not None
            if now_s - self._last_accepted_s <= self.sequence_reset_after_s:
                raise ValueError("target sequence must increase")
        self._command = command
        self._received_s = now_s
        self._last_accepted_s = now_s
        self._invalid_reason = None
        return command

    def invalidate(self, now_s: float, reason: str = "invalid_target") -> None:
        if not math.isfinite(now_s):
            raise ValueError("now_s must be finite")
        if not reason:
            raise ValueError("reason must not be empty")
        self._received_s = now_s
        self._invalid_reason = reason

    def status(self, now_s: float) -> StreamStatus:
        if not math.isfinite(now_s):
            raise ValueError("now_s must be finite")
        if self._received_s is None:
            return StreamStatus(None, False, "no_target", math.inf)
        age_s = max(0.0, now_s - self._received_s)
        if self._invalid_reason is not None:
            return StreamStatus(
                self._command, False, self._invalid_reason, age_s
            )
        if self._command is None:
            return StreamStatus(None, False, "no_target", age_s)
        if age_s > self.stale_timeout_s:
            return StreamStatus(self._command, False, "input_stale", age_s)
        if not self._command.enabled:
            return StreamStatus(
                self._command,
                False,
                self._command.inhibit_reason,
                age_s,
            )
        return StreamStatus(self._command, True, "enabled", age_s)
