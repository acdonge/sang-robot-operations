"""Versioned newline-delimited JSON contract used between XR and the gateway."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Mapping, Sequence


SCHEMA = "xr_frame_v1"
PROTOCOL_VERSION = 1
MAX_UINT32 = (1 << 32) - 1
MAX_UINT64 = (1 << 64) - 1


class ProtocolError(ValueError):
    """Raised when one wire frame does not satisfy the XR contract."""


@dataclass(frozen=True)
class PoseData:
    position: tuple[float, float, float]
    orientation: tuple[float, float, float, float]
    valid: bool
    tracked: bool


@dataclass(frozen=True)
class ControllerData:
    connected: bool
    grip: PoseData
    aim: PoseData
    linear_velocity: tuple[float, float, float]
    angular_velocity: tuple[float, float, float]
    trigger: float
    squeeze: float
    thumbstick: tuple[float, float]
    buttons: int


@dataclass(frozen=True)
class XRFrameData:
    sequence: int
    device_time_ns: int
    source: str
    frame_id: str
    head: PoseData
    left: ControllerData
    right: ControllerData


def decode_frame(payload: str | bytes) -> XRFrameData:
    """Decode and validate one JSON object without its newline delimiter."""
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("frame is not valid UTF-8") from exc
    if not isinstance(payload, str):
        raise ProtocolError("frame must be text or bytes")

    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"frame is not valid JSON: {exc.msg}") from exc

    root = _mapping(raw, "frame")
    schema = root.get("schema")
    if schema != SCHEMA:
        raise ProtocolError(f"schema must be {SCHEMA!r}, got {schema!r}")

    sequence = _integer(root.get("sequence"), "sequence", 0, MAX_UINT32)
    device_time_ns = _integer(
        root.get("device_time_ns"), "device_time_ns", 0, MAX_UINT64
    )
    source = _text(root.get("source"), "source", max_length=64)
    frame_id = _frame_id(root.get("frame_id"))

    return XRFrameData(
        sequence=sequence,
        device_time_ns=device_time_ns,
        source=source,
        frame_id=frame_id,
        head=_pose(root.get("head"), "head"),
        left=_controller(root.get("left"), "left"),
        right=_controller(root.get("right"), "right"),
    )


def encode_frame(frame: XRFrameData) -> bytes:
    """Encode one validated frame as compact NDJSON bytes."""
    document = {
        "schema": SCHEMA,
        "sequence": frame.sequence,
        "device_time_ns": frame.device_time_ns,
        "source": frame.source,
        "frame_id": frame.frame_id,
        "head": _pose_document(frame.head),
        "left": _controller_document(frame.left),
        "right": _controller_document(frame.right),
    }
    # Re-validate generated data so mock/test producers cannot bypass the contract.
    compact = json.dumps(document, separators=(",", ":"), allow_nan=False)
    decode_frame(compact)
    return compact.encode("utf-8") + b"\n"


def _controller(value: Any, path: str) -> ControllerData:
    item = _mapping(value, path)
    grip = _pose(item.get("grip"), f"{path}.grip")
    aim_value = item.get("aim", item.get("grip"))
    aim = _pose(aim_value, f"{path}.aim")
    return ControllerData(
        connected=_boolean(item.get("connected"), f"{path}.connected"),
        grip=grip,
        aim=aim,
        linear_velocity=_vector(
            item.get("linear_velocity", (0.0, 0.0, 0.0)),
            3,
            f"{path}.linear_velocity",
        ),
        angular_velocity=_vector(
            item.get("angular_velocity", (0.0, 0.0, 0.0)),
            3,
            f"{path}.angular_velocity",
        ),
        trigger=_number(item.get("trigger", 0.0), f"{path}.trigger", 0.0, 1.0),
        squeeze=_number(item.get("squeeze", 0.0), f"{path}.squeeze", 0.0, 1.0),
        thumbstick=_vector(
            item.get("thumbstick", (0.0, 0.0)),
            2,
            f"{path}.thumbstick",
            minimum=-1.0,
            maximum=1.0,
        ),
        buttons=_integer(item.get("buttons", 0), f"{path}.buttons", 0, MAX_UINT32),
    )


def _pose(value: Any, path: str) -> PoseData:
    item = _mapping(value, path)
    valid = _boolean(item.get("valid"), f"{path}.valid")
    tracked = _boolean(item.get("tracked"), f"{path}.tracked")
    if tracked and not valid:
        raise ProtocolError(f"{path}.tracked cannot be true when valid is false")

    orientation = _vector(item.get("orientation"), 4, f"{path}.orientation")
    norm = math.sqrt(sum(component * component for component in orientation))
    if norm < 1e-6:
        raise ProtocolError(f"{path}.orientation quaternion has zero magnitude")
    normalized = tuple(component / norm for component in orientation)

    return PoseData(
        position=_vector(item.get("position"), 3, f"{path}.position"),
        orientation=(normalized[0], normalized[1], normalized[2], normalized[3]),
        valid=valid,
        tracked=tracked,
    )


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{path} must be an object")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ProtocolError(f"{path} must be a boolean")
    return value


def _integer(value: Any, path: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{path} must be an integer")
    if not minimum <= value <= maximum:
        raise ProtocolError(f"{path} must be between {minimum} and {maximum}")
    return value


def _number(
    value: Any,
    path: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"{path} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ProtocolError(f"{path} must be finite")
    if minimum is not None and result < minimum:
        raise ProtocolError(f"{path} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise ProtocolError(f"{path} must be at most {maximum}")
    return result


def _vector(
    value: Any,
    length: int,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ProtocolError(f"{path} must be an array of length {length}")
    if len(value) != length:
        raise ProtocolError(f"{path} must contain exactly {length} values")
    return tuple(
        _number(component, f"{path}[{index}]", minimum, maximum)
        for index, component in enumerate(value)
    )


def _text(value: Any, path: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ProtocolError(f"{path} must be non-empty text up to {max_length} characters")
    if any(ord(character) < 32 for character in value):
        raise ProtocolError(f"{path} contains a control character")
    return value


def _frame_id(value: Any) -> str:
    frame_id = _text(value, "frame_id", max_length=128)
    if frame_id.startswith("/") or any(character.isspace() for character in frame_id):
        raise ProtocolError("frame_id must be relative and contain no whitespace")
    return frame_id


def _pose_document(pose: PoseData) -> dict[str, Any]:
    return {
        "position": list(pose.position),
        "orientation": list(pose.orientation),
        "valid": pose.valid,
        "tracked": pose.tracked,
    }


def _controller_document(controller: ControllerData) -> dict[str, Any]:
    return {
        "connected": controller.connected,
        "grip": _pose_document(controller.grip),
        "aim": _pose_document(controller.aim),
        "linear_velocity": list(controller.linear_velocity),
        "angular_velocity": list(controller.angular_velocity),
        "trigger": controller.trigger,
        "squeeze": controller.squeeze,
        "thumbstick": list(controller.thumbstick),
        "buttons": controller.buttons,
    }
