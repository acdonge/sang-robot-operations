import math

import pytest

from xr_teleop_retarget.math3d import (
    map_xr_quaternion,
    map_xr_vector,
    quaternion_from_axis_angle,
    relative_target_orientation,
)


def assert_quaternion_equivalent(actual, expected):
    direct = sum((left - right) ** 2 for left, right in zip(actual, expected))
    negated = sum((left + right) ** 2 for left, right in zip(actual, expected))
    assert min(direct, negated) < 1e-12


@pytest.mark.parametrize(
    ("xr_axis", "ros_axis"),
    [
        ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0)),
        ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        ((0.0, 0.0, -1.0), (1.0, 0.0, 0.0)),
    ],
)
def test_openxr_axes_map_to_ros_rep_103(xr_axis, ros_axis):
    assert map_xr_vector(xr_axis) == pytest.approx(ros_axis)


def test_xr_y_rotation_maps_to_ros_z_rotation():
    xr_rotation = quaternion_from_axis_angle((0.0, 1.0, 0.0), math.pi / 2.0)
    expected = quaternion_from_axis_angle((0.0, 0.0, 1.0), math.pi / 2.0)

    assert_quaternion_equivalent(map_xr_quaternion(xr_rotation), expected)


def test_calibration_orientation_is_identity_at_anchor():
    anchor = quaternion_from_axis_angle((1.0, 2.0, 3.0), 0.7)
    neutral = quaternion_from_axis_angle((0.0, 1.0, 0.0), 0.4)

    target, angle = relative_target_orientation(anchor, anchor, neutral)

    assert_quaternion_equivalent(target, neutral)
    assert angle == pytest.approx(0.0)


def test_relative_orientation_preserves_expected_robot_axis():
    anchor = (0.0, 0.0, 0.0, 1.0)
    current = quaternion_from_axis_angle((0.0, 1.0, 0.0), 0.3)

    target, angle = relative_target_orientation(
        anchor, current, (0.0, 0.0, 0.0, 1.0)
    )

    expected = quaternion_from_axis_angle((0.0, 0.0, 1.0), 0.3)
    assert_quaternion_equivalent(target, expected)
    assert angle == pytest.approx(0.3)
