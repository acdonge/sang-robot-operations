"""Forward and inverse kinematics for the ROBOTIS OMY-F3M arm."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

import numpy as np


ARM_JOINT_NAMES = (
    "joint1",
    "joint2",
    "joint3",
    "joint4",
    "joint5",
    "joint6",
)
READY_JOINTS = np.array(
    [0.0, -1.5708, 2.6529, -1.0821, 1.5708, 0.0], dtype=float
)
READY_POSITION = np.array([0.19080657, -0.11299910, 0.39004890], dtype=float)
READY_ORIENTATION_XYZW = np.array(
    [0.0, 0.0, 0.70710678, 0.70710678], dtype=float
)

# These origins and axes are from ROBOTIS' generated OMY-F3M URDF. The final
# offset combines link6 -> end_effector_flange_link -> end_effector_link.
JOINT_ORIGINS = np.array(
    [
        [0.0, 0.0, 0.1715],
        [0.0, -0.1215, 0.0],
        [0.0, 0.0, 0.2470],
        [0.0, 0.1215, 0.2195],
        [0.0, -0.1130, 0.0],
        [0.0, 0.0, 0.1155],
    ],
    dtype=float,
)
JOINT_AXES = np.array(
    [
        [0.0, 0.0, 1.0],
        [0.0, 1.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
        [0.0, 1.0, 0.0],
    ],
    dtype=float,
)
TOOL_OFFSET = np.array([0.0, -0.2440, 0.0], dtype=float)


@dataclass(frozen=True)
class IKResult:
    joints: np.ndarray
    converged: bool
    iterations: int
    position_error_m: float
    orientation_error_rad: float
    reason: str


def _finite_vector(values: Iterable[float], size: int, name: str) -> np.ndarray:
    result = np.asarray(tuple(values), dtype=float)
    if result.shape != (size,) or not np.all(np.isfinite(result)):
        raise ValueError(f"{name} must contain {size} finite values")
    return result


def _rotation(axis: np.ndarray, angle: float) -> np.ndarray:
    x, y, z = axis
    skew = np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])
    return np.eye(3) + math.sin(angle) * skew + (1.0 - math.cos(angle)) * (
        skew @ skew
    )


def rotation_from_quaternion_xyzw(values: Iterable[float]) -> np.ndarray:
    x, y, z, w = _finite_vector(values, 4, "orientation")
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm <= 1e-12:
        raise ValueError("orientation quaternion must be non-zero")
    x, y, z, w = (x / norm, y / norm, z / norm, w / norm)
    return np.array(
        [
            [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
            [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
            [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
        ],
        dtype=float,
    )


def quaternion_xyzw_from_rotation(rotation: np.ndarray) -> np.ndarray:
    matrix = np.asarray(rotation, dtype=float)
    if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
        raise ValueError("rotation must be a finite 3x3 matrix")

    trace = float(np.trace(matrix))
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * scale
        x = (matrix[2, 1] - matrix[1, 2]) / scale
        y = (matrix[0, 2] - matrix[2, 0]) / scale
        z = (matrix[1, 0] - matrix[0, 1]) / scale
    else:
        diagonal = np.diag(matrix)
        index = int(np.argmax(diagonal))
        if index == 0:
            diagonal_term = (
                1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2]
            )
            scale = math.sqrt(max(0.0, diagonal_term)) * 2.0
            x = 0.25 * scale
            y = (matrix[0, 1] + matrix[1, 0]) / scale
            z = (matrix[0, 2] + matrix[2, 0]) / scale
            w = (matrix[2, 1] - matrix[1, 2]) / scale
        elif index == 1:
            diagonal_term = (
                1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2]
            )
            scale = math.sqrt(max(0.0, diagonal_term)) * 2.0
            x = (matrix[0, 1] + matrix[1, 0]) / scale
            y = 0.25 * scale
            z = (matrix[1, 2] + matrix[2, 1]) / scale
            w = (matrix[0, 2] - matrix[2, 0]) / scale
        else:
            diagonal_term = (
                1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1]
            )
            scale = math.sqrt(max(0.0, diagonal_term)) * 2.0
            x = (matrix[0, 2] + matrix[2, 0]) / scale
            y = (matrix[1, 2] + matrix[2, 1]) / scale
            z = 0.25 * scale
            w = (matrix[1, 0] - matrix[0, 1]) / scale

    quaternion = np.array([x, y, z, w], dtype=float)
    quaternion /= np.linalg.norm(quaternion)
    if quaternion[3] < 0.0:
        quaternion = -quaternion
    return quaternion


def rotation_vector(rotation: np.ndarray) -> np.ndarray:
    quaternion = quaternion_xyzw_from_rotation(rotation)
    vector = quaternion[:3]
    magnitude = float(np.linalg.norm(vector))
    if magnitude <= 1e-12:
        return np.zeros(3)
    angle = 2.0 * math.atan2(magnitude, float(quaternion[3]))
    return vector * (angle / magnitude)


def forward_kinematics(joints: Iterable[float]) -> tuple[np.ndarray, np.ndarray]:
    q = _finite_vector(joints, 6, "joints")
    transform = np.eye(4)
    for origin, axis, angle in zip(JOINT_ORIGINS, JOINT_AXES, q):
        transform[:3, 3] += transform[:3, :3] @ origin
        transform[:3, :3] = transform[:3, :3] @ _rotation(axis, float(angle))
    transform[:3, 3] += transform[:3, :3] @ TOOL_OFFSET
    return transform[:3, 3].copy(), transform[:3, :3].copy()


def pose_error(
    joints: Iterable[float],
    target_position: Iterable[float],
    target_orientation_xyzw: Iterable[float],
) -> tuple[np.ndarray, np.ndarray]:
    position = _finite_vector(target_position, 3, "target_position")
    target_rotation = rotation_from_quaternion_xyzw(target_orientation_xyzw)
    current_position, current_rotation = forward_kinematics(joints)
    return (
        position - current_position,
        rotation_vector(target_rotation @ current_rotation.T),
    )


def numerical_jacobian(joints: Iterable[float], epsilon: float = 1e-5) -> np.ndarray:
    q = _finite_vector(joints, 6, "joints")
    if not math.isfinite(epsilon) or epsilon <= 0.0:
        raise ValueError("epsilon must be positive and finite")
    jacobian = np.zeros((6, 6), dtype=float)
    for index in range(6):
        plus = q.copy()
        minus = q.copy()
        plus[index] += epsilon
        minus[index] -= epsilon
        plus_position, plus_rotation = forward_kinematics(plus)
        minus_position, minus_rotation = forward_kinematics(minus)
        jacobian[:3, index] = (plus_position - minus_position) / (2.0 * epsilon)
        jacobian[3:, index] = rotation_vector(
            plus_rotation @ minus_rotation.T
        ) / (2.0 * epsilon)
    return jacobian


def geometric_jacobian(joints: Iterable[float]) -> np.ndarray:
    q = _finite_vector(joints, 6, "joints")
    rotation = np.eye(3)
    position = np.zeros(3)
    joint_positions = []
    joint_axes = []
    for origin, axis, angle in zip(JOINT_ORIGINS, JOINT_AXES, q):
        position = position + rotation @ origin
        joint_positions.append(position.copy())
        joint_axes.append(rotation @ axis)
        rotation = rotation @ _rotation(axis, float(angle))
    tcp_position = position + rotation @ TOOL_OFFSET

    jacobian = np.zeros((6, 6), dtype=float)
    for index, (joint_position, joint_axis) in enumerate(
        zip(joint_positions, joint_axes)
    ):
        jacobian[:3, index] = np.cross(
            joint_axis, tcp_position - joint_position
        )
        jacobian[3:, index] = joint_axis
    return jacobian


def solve_ik(
    target_position: Iterable[float],
    target_orientation_xyzw: Iterable[float],
    seed_joints: Iterable[float],
    joint_limits: np.ndarray | None = None,
    *,
    max_iterations: int = 80,
    damping: float = 0.025,
    orientation_weight: float = 0.15,
    max_step_rad: float = 0.18,
    position_tolerance_m: float = 0.002,
    orientation_tolerance_rad: float = math.radians(2.0),
) -> IKResult:
    position = _finite_vector(target_position, 3, "target_position")
    orientation = _finite_vector(target_orientation_xyzw, 4, "target_orientation")
    target_rotation = rotation_from_quaternion_xyzw(orientation)
    q = _finite_vector(seed_joints, 6, "seed_joints").copy()
    limits = (
        np.tile(np.array([[-2.0 * math.pi, 2.0 * math.pi]]), (6, 1))
        if joint_limits is None
        else np.asarray(joint_limits, dtype=float)
    )
    if limits.shape != (6, 2) or not np.all(np.isfinite(limits)):
        raise ValueError("joint_limits must be a finite 6x2 array")
    if np.any(limits[:, 0] >= limits[:, 1]):
        raise ValueError("joint limit lower bounds must be below upper bounds")
    if max_iterations <= 0:
        raise ValueError("max_iterations must be positive")
    for value, name in (
        (damping, "damping"),
        (orientation_weight, "orientation_weight"),
        (max_step_rad, "max_step_rad"),
        (position_tolerance_m, "position_tolerance_m"),
        (orientation_tolerance_rad, "orientation_tolerance_rad"),
    ):
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"{name} must be positive and finite")

    q = np.clip(q, limits[:, 0], limits[:, 1])
    weights = np.array(
        [1.0, 1.0, 1.0, orientation_weight, orientation_weight, orientation_weight]
    )
    last_position_error = math.inf
    last_orientation_error = math.inf

    for iteration in range(1, max_iterations + 1):
        current_position, current_rotation = forward_kinematics(q)
        position_delta = position - current_position
        orientation_delta = rotation_vector(target_rotation @ current_rotation.T)
        last_position_error = float(np.linalg.norm(position_delta))
        last_orientation_error = float(np.linalg.norm(orientation_delta))
        if (
            last_position_error <= position_tolerance_m
            and last_orientation_error <= orientation_tolerance_rad
        ):
            return IKResult(
                q.copy(),
                True,
                iteration - 1,
                last_position_error,
                last_orientation_error,
                "converged",
            )

        error = np.concatenate((position_delta, orientation_delta)) * weights
        jacobian = geometric_jacobian(q) * weights[:, None]
        normal = jacobian.T @ jacobian + (damping * damping) * np.eye(6)
        try:
            step = np.linalg.solve(normal, jacobian.T @ error)
        except np.linalg.LinAlgError:
            return IKResult(
                q.copy(),
                False,
                iteration,
                last_position_error,
                last_orientation_error,
                "singular_system",
            )
        step = np.clip(step, -max_step_rad, max_step_rad)
        q = np.clip(q + step, limits[:, 0], limits[:, 1])

    return IKResult(
        q.copy(),
        False,
        max_iterations,
        last_position_error,
        last_orientation_error,
        "max_iterations",
    )
