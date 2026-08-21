import math

import pytest

from xr_omy_retarget.math3d import (
    head_yaw_quaternion,
    map_xr_vector,
    quaternion_from_axis_angle,
    rotate_vector,
    to_operator_vector,
)


def test_openxr_translation_maps_to_ros_rep_103_axes():
    assert map_xr_vector((0.1, 0.2, -0.3)) == pytest.approx(
        (0.3, -0.1, 0.2)
    )


def test_head_yaw_removes_calibration_heading_from_translation():
    head = quaternion_from_axis_angle((0.0, 1.0, 0.0), math.pi / 2.0)
    yaw = head_yaw_quaternion(head)
    world_head_forward = rotate_vector(head, (0.0, 0.0, -0.2))

    operator_delta = to_operator_vector(world_head_forward, yaw)

    assert operator_delta == pytest.approx((0.0, 0.0, -0.2), abs=1e-9)
    assert map_xr_vector(operator_delta) == pytest.approx(
        (0.2, 0.0, 0.0), abs=1e-9
    )


def test_head_yaw_rejects_a_vertical_forward_direction():
    looking_up = quaternion_from_axis_angle((1.0, 0.0, 0.0), math.pi / 2.0)

    with pytest.raises(ValueError, match="too vertical"):
        head_yaw_quaternion(looking_up)
