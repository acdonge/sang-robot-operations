"""Dependency-free vector and quaternion helpers for OMY retargeting."""

from __future__ import annotations

import math
from typing import Iterable


Vector3 = tuple[float, float, float]
Quaternion = tuple[float, float, float, float]

# OpenXR: +X right, +Y up, -Z forward. ROS REP-103: +X forward,
# +Y left, +Z up.
XR_TO_ROS_QUATERNION: Quaternion = (0.5, -0.5, -0.5, 0.5)


def vector3(values: Iterable[float]) -> Vector3:
    result = tuple(float(value) for value in values)
    if len(result) != 3 or not all(math.isfinite(value) for value in result):
        raise ValueError("vector must contain three finite values")
    return result  # type: ignore[return-value]


def quaternion(values: Iterable[float]) -> Quaternion:
    result = tuple(float(value) for value in values)
    if len(result) != 4 or not all(math.isfinite(value) for value in result):
        raise ValueError("quaternion must contain four finite values")
    magnitude = math.sqrt(sum(value * value for value in result))
    if magnitude <= 1e-12:
        raise ValueError("quaternion magnitude must be non-zero")
    normalized = tuple(value / magnitude for value in result)
    if normalized[3] < 0.0:
        normalized = tuple(-value for value in normalized)
    return normalized  # type: ignore[return-value]


def add(left: Vector3, right: Vector3) -> Vector3:
    return tuple(a + b for a, b in zip(left, right))  # type: ignore[return-value]


def subtract(left: Vector3, right: Vector3) -> Vector3:
    return tuple(a - b for a, b in zip(left, right))  # type: ignore[return-value]


def scale(values: Vector3, factor: float) -> Vector3:
    if not math.isfinite(factor) or factor <= 0.0:
        raise ValueError("scale factor must be positive and finite")
    return tuple(value * factor for value in values)  # type: ignore[return-value]


def map_xr_vector(values: Iterable[float]) -> Vector3:
    x, y, z = vector3(values)
    return (-z, -x, y)


def quaternion_conjugate(value: Quaternion) -> Quaternion:
    x, y, z, w = quaternion(value)
    return (-x, -y, -z, w)


def quaternion_multiply(left: Quaternion, right: Quaternion) -> Quaternion:
    lx, ly, lz, lw = quaternion(left)
    rx, ry, rz, rw = quaternion(right)
    return quaternion(
        (
            lw * rx + lx * rw + ly * rz - lz * ry,
            lw * ry - lx * rz + ly * rw + lz * rx,
            lw * rz + lx * ry - ly * rx + lz * rw,
            lw * rw - lx * rx - ly * ry - lz * rz,
        )
    )


def rotate_vector(rotation: Quaternion, values: Iterable[float]) -> Vector3:
    x, y, z, w = quaternion(rotation)
    vx, vy, vz = vector3(values)
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return (
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    )


def quaternion_from_axis_angle(axis: Vector3, angle: float) -> Quaternion:
    if not math.isfinite(angle):
        raise ValueError("angle must be finite")
    length = math.sqrt(sum(value * value for value in axis))
    if length <= 1e-12:
        raise ValueError("axis magnitude must be non-zero")
    half_angle = angle / 2.0
    factor = math.sin(half_angle) / length
    return quaternion(
        (axis[0] * factor, axis[1] * factor, axis[2] * factor, math.cos(half_angle))
    )


def head_yaw_quaternion(head_orientation: Quaternion) -> Quaternion:
    forward = rotate_vector(head_orientation, (0.0, 0.0, -1.0))
    horizontal_length = math.hypot(forward[0], forward[2])
    if horizontal_length <= 1e-6:
        raise ValueError("head forward direction is too vertical to calibrate")
    yaw = math.atan2(-forward[0], -forward[2])
    return quaternion_from_axis_angle((0.0, 1.0, 0.0), yaw)


def to_operator_vector(
    world_delta_xr: Vector3, operator_yaw_xr: Quaternion
) -> Vector3:
    return rotate_vector(quaternion_conjugate(operator_yaw_xr), world_delta_xr)


def map_xr_quaternion(value: Quaternion) -> Quaternion:
    basis_inverse = quaternion_conjugate(XR_TO_ROS_QUATERNION)
    return quaternion_multiply(
        quaternion_multiply(XR_TO_ROS_QUATERNION, quaternion(value)),
        basis_inverse,
    )


def operator_relative_orientation(
    anchor_xr: Quaternion,
    current_xr: Quaternion,
    operator_yaw_xr: Quaternion,
    target_anchor_robot: Quaternion,
) -> tuple[Quaternion, float]:
    delta_world_xr = quaternion_multiply(
        quaternion(current_xr), quaternion_conjugate(anchor_xr)
    )
    yaw_inverse = quaternion_conjugate(operator_yaw_xr)
    delta_operator_xr = quaternion_multiply(
        quaternion_multiply(yaw_inverse, delta_world_xr), operator_yaw_xr
    )
    delta_robot = map_xr_quaternion(delta_operator_xr)
    target = quaternion_multiply(delta_robot, target_anchor_robot)
    return target, quaternion_angle(delta_robot)


def quaternion_angle(value: Quaternion) -> float:
    normalized = quaternion(value)
    return 2.0 * math.acos(min(1.0, max(-1.0, abs(normalized[3]))))
