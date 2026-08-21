import math

import numpy as np
import pytest

from xr_omy_retarget.omy_kinematics import (
    READY_JOINTS,
    READY_ORIENTATION_XYZW,
    READY_POSITION,
    forward_kinematics,
    geometric_jacobian,
    numerical_jacobian,
    quaternion_xyzw_from_rotation,
    rotation_from_quaternion_xyzw,
    solve_ik,
)


def test_ready_pose_matches_robotis_model_fk():
    position, rotation = forward_kinematics(READY_JOINTS)

    assert position == pytest.approx(READY_POSITION, abs=1e-8)
    assert quaternion_xyzw_from_rotation(rotation) == pytest.approx(
        READY_ORIENTATION_XYZW, abs=2e-6
    )


def test_ik_solves_small_six_dof_motion_from_ready_pose():
    target_position = READY_POSITION + np.array([0.025, 0.008, 0.015])
    rotation_delta = rotation_from_quaternion_xyzw(
        [math.sin(math.radians(4.0) / 2.0), 0.0, 0.0, math.cos(math.radians(4.0) / 2.0)]
    )
    ready_rotation = rotation_from_quaternion_xyzw(READY_ORIENTATION_XYZW)
    target_orientation = quaternion_xyzw_from_rotation(rotation_delta @ ready_rotation)

    result = solve_ik(target_position, target_orientation, READY_JOINTS)

    assert result.converged, result
    assert result.position_error_m <= 0.002
    assert result.orientation_error_rad <= math.radians(2.0)


def test_geometric_jacobian_matches_central_difference():
    assert geometric_jacobian(READY_JOINTS) == pytest.approx(
        numerical_jacobian(READY_JOINTS), abs=2e-6
    )


def test_ik_rejects_nonfinite_target():
    with pytest.raises(ValueError, match="finite"):
        solve_ik([math.nan, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0], READY_JOINTS)
