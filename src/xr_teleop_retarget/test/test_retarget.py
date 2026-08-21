import math

import pytest

from xr_teleop_retarget.math3d import quaternion_from_axis_angle
from xr_teleop_retarget.retarget import (
    ControllerSample,
    RetargetConfig,
    Retargeter,
)


def sample(
    *,
    position=(0.0, 1.0, -0.4),
    orientation=(0.0, 0.0, 0.0, 1.0),
    connected=True,
    valid=True,
    tracked=True,
    squeeze=0.0,
    trigger=0.0,
    sequence=1,
):
    return ControllerSample(
        position=position,
        orientation=orientation,
        connected=connected,
        pose_valid=valid,
        pose_tracked=tracked,
        squeeze=squeeze,
        trigger=trigger,
        sequence=sequence,
    )


def test_calibration_requires_a_tracked_controller_pose():
    retargeter = Retargeter(RetargetConfig())

    with pytest.raises(ValueError, match="connected, valid, and tracked"):
        retargeter.calibrate(sample(tracked=False))


def test_uncalibrated_and_released_deadman_inhibit_preview():
    retargeter = Retargeter(RetargetConfig())
    current = sample()

    assert retargeter.evaluate(current, 0.01).inhibit_reason == "not_calibrated"

    retargeter.calibrate(current)
    result = retargeter.evaluate(current, 0.01)
    assert result.inhibit_reason == "deadman_released"
    assert not result.output_enabled


def test_calibrated_anchor_maps_to_neutral_when_deadman_is_pressed():
    config = RetargetConfig()
    retargeter = Retargeter(config)
    anchor = sample(squeeze=1.0, trigger=0.75)
    retargeter.calibrate(anchor)

    result = retargeter.evaluate(anchor, 0.01)

    assert result.output_enabled
    assert result.inhibit_reason == "enabled"
    assert result.target_position == pytest.approx(config.neutral_position)
    assert result.target_orientation == pytest.approx(
        config.neutral_orientation
    )
    assert result.gripper == pytest.approx(0.75)


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        ((0.0, 1.0, -0.45), (0.31823402, -0.00017677, 0.23437033)),
        ((-0.05, 1.0, -0.4), (0.29323402, 0.02482323, 0.23437033)),
        ((0.0, 1.05, -0.4), (0.29323402, -0.00017677, 0.25937033)),
    ],
)
def test_small_controller_motion_maps_forward_left_and_up(position, expected):
    retargeter = Retargeter(RetargetConfig())
    retargeter.calibrate(sample())

    result = retargeter.evaluate(
        sample(position=position, squeeze=1.0, sequence=2), 0.01
    )

    assert result.output_enabled
    assert result.target_position == pytest.approx(expected)


def test_translation_scale_can_be_overridden():
    retargeter = Retargeter(RetargetConfig(translation_scale=1.0))
    retargeter.calibrate(sample())

    result = retargeter.evaluate(
        sample(position=(0.0, 1.0, -0.45), squeeze=1.0), 0.01
    )

    assert result.target_position == pytest.approx(
        (0.34323402, -0.00017677, 0.23437033)
    )


def test_stale_and_invalid_tracking_are_inhibited():
    config = RetargetConfig(stale_timeout_s=0.25)
    retargeter = Retargeter(config)
    retargeter.calibrate(sample())

    assert (
        retargeter.evaluate(sample(squeeze=1.0), 0.3).inhibit_reason
        == "input_stale"
    )
    assert (
        retargeter.evaluate(sample(squeeze=1.0, tracked=False), 0.01).inhibit_reason
        == "tracking_invalid"
    )


def test_workspace_and_rotation_limits_reject_unsafe_targets():
    config = RetargetConfig(max_rotation_delta_rad=math.pi / 2.0)
    retargeter = Retargeter(config)
    retargeter.calibrate(sample())

    outside = retargeter.evaluate(
        sample(position=(0.0, 1.0, -1.0), squeeze=1.0), 0.01
    )
    assert outside.inhibit_reason == "outside_workspace"
    assert not outside.output_enabled

    large_rotation = retargeter.evaluate(
        sample(
            orientation=quaternion_from_axis_angle((0.0, 1.0, 0.0), 2.0),
            squeeze=1.0,
        ),
        0.01,
    )
    assert large_rotation.inhibit_reason == "rotation_limit"
    assert not large_rotation.output_enabled


def test_reset_returns_to_uncalibrated_state():
    retargeter = Retargeter(RetargetConfig())
    retargeter.calibrate(sample())

    retargeter.clear_calibration()

    assert not retargeter.calibrated
    assert (
        retargeter.evaluate(sample(squeeze=1.0), 0.01).inhibit_reason
        == "not_calibrated"
    )
