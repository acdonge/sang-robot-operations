"""Offline retargeting of the 25 source TCP points to the pinned vendor RB5.

This is a virtual-cell geometry calculation, not a physical motion planner.
The dimensions and axes are checked against the vendor USD by the preflight.
"""

import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from .contract import unity_position_to_ros, unity_rotation_to_ros

ORIGINS = ([0, 0, .1692], [0, 0, 0], [0, 0, .425], [0, 0, .392],
           [0, -.1107, .1107], [0, 0, 0])
AXES = np.asarray([[0, 0, 1], [0, 1, 0], [0, 1, 0], [0, 1, 0], [0, 0, 1], [0, 1, 0]])


def forward(joints, base_position, base_quaternion):
    position = np.asarray(base_position, dtype=float).copy()
    rotation = Rotation.from_quat(base_quaternion)
    for origin, axis, angle in zip(ORIGINS, AXES, joints):
        position += rotation.apply(origin)
        rotation = rotation * Rotation.from_rotvec(axis*angle)
    direction = rotation.apply([0, -1, 0])
    return position + direction*.0967, direction


def retarget(data, layout):
    frame = next(f for f in layout["frames"] if f["path"] == "rb5_850e")
    position = frame["position_ros_world_m"]
    rotation = unity_rotation_to_ros(frame["rotation_unity_world_xyzw"])
    previous = np.array([0., .6, 2.1, -1.1, 1.57, 0.])
    result = []
    for point in data["points"]:
        target = np.asarray(unity_position_to_ros(point["world_target_m"]))
        def residual(q):
            tcp, direction = forward(q, position, rotation)
            return np.r_[10*(tcp-target), direction-[0, 0, -1], .001*(q-previous)]
        solution = least_squares(residual, previous, bounds=(-3.14, 3.14),
                                 max_nfev=1000, xtol=1e-12, ftol=1e-12, gtol=1e-12)
        tcp, direction = forward(solution.x, position, rotation)
        error = float(np.linalg.norm(tcp-target))
        angle = float(np.degrees(np.arccos(np.clip(direction @ [0, 0, -1], -1, 1))))
        if not solution.success or error > .002 or angle > 2.:
            raise ValueError(f"RB5 virtual path retarget failed: {error:.6f}m, {angle:.3f}deg")
        result.append({"positions_rad": solution.x.tolist(), "target_m": target.tolist(),
                       "fk_error_m": error, "tool_angle_deg": angle,
                       "duration_s": point["segment_duration_seconds"]})
        previous = solution.x
    if len(result) != 25:
        raise ValueError("Pinned Scenario 1 requires exactly 25 path points")
    return result
