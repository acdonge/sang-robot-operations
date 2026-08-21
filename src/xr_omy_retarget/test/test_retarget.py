import math

import pytest

from xr_omy_retarget.math3d import quaternion_from_axis_angle
from xr_omy_retarget.retarget import ControllerSample, RetargetConfig, Retargeter


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


def test_calibration_requires_a_tracked_controller():
    retargeter = Retargeter(RetargetConfig())

    with pytest.raises(ValueError, match="connected, valid, and tracked"):
        retargeter.calibrate(sample(tracked=False))


def test_held_squeeze_at_calibration_starts_at_official_ready_tcp():
    config = RetargetConfig()
    retargeter = Retargeter(config)
    anchor = sample(squeeze=1.0, trigger=0.75)
    retargeter.calibrate(anchor)

    result = retargeter.evaluate(anchor, 0.01)

    assert result.output_enabled
    assert result.target_position == pytest.approx(config.neutral_position)
    assert result.target_orientation == pytest.approx(config.neutral_orientation)
    assert result.gripper == pytest.approx(0.75)
    assert not result.clutch_reanchored


def test_translation_uses_fixed_xr_local_axes():
    config = RetargetConfig()
    retargeter = Retargeter(config)
    anchor = sample(position=(0.0, 0.0, 0.0), squeeze=1.0)
    retargeter.calibrate(anchor)

    result = retargeter.evaluate(
        sample(position=(0.0, 0.0, -0.1), squeeze=1.0, sequence=2), 0.01
    )

    assert result.target_position == pytest.approx(
        (
            config.neutral_position[0] + 0.1,
            config.neutral_position[1],
            config.neutral_position[2],
        ),
        abs=1e-8,
    )


def test_translation_only_mode_ignores_controller_rotation():
    config = RetargetConfig(track_orientation=False)
    retargeter = Retargeter(config)
    anchor = sample(squeeze=1.0)
    retargeter.calibrate(anchor)

    result = retargeter.evaluate(
        sample(
            orientation=quaternion_from_axis_angle((1.0, 0.0, 0.0), 1.0),
            squeeze=1.0,
            sequence=2,
        ),
        0.01,
    )

    assert result.output_enabled
    assert result.target_orientation == pytest.approx(config.neutral_orientation)
    assert result.rotation_delta_rad == 0.0


def test_release_reposition_and_regrasp_does_not_jump_target():
    config = RetargetConfig()
    retargeter = Retargeter(config)
    retargeter.calibrate(sample())

    first_grasp = retargeter.evaluate(
        sample(position=(0.2, 1.0, -0.6), squeeze=1.0, sequence=2), 0.01
    )
    assert first_grasp.clutch_reanchored
    assert first_grasp.target_position == pytest.approx(config.neutral_position)

    moved = retargeter.evaluate(
        sample(position=(0.2, 1.0, -0.7), squeeze=1.0, sequence=3), 0.01
    )
    assert moved.output_enabled
    expected = moved.target_position

    released = retargeter.evaluate(
        sample(position=(-0.3, 1.3, -0.2), squeeze=0.0, sequence=4), 0.01
    )
    assert released.inhibit_reason == "deadman_released"

    regrasped = retargeter.evaluate(
        sample(position=(-0.3, 1.3, -0.2), squeeze=1.0, sequence=5), 0.01
    )
    assert regrasped.output_enabled
    assert regrasped.clutch_reanchored
    assert regrasped.clutch_reanchors == 2
    assert regrasped.target_position == pytest.approx(expected)


def test_stale_tracking_forces_safe_reanchor_on_recovery():
    retargeter = Retargeter(RetargetConfig())
    anchor = sample(squeeze=1.0)
    retargeter.calibrate(anchor)
    accepted = retargeter.evaluate(
        sample(position=(0.0, 1.0, -0.5), squeeze=1.0), 0.01
    )
    assert accepted.output_enabled

    stale = retargeter.evaluate(
        sample(position=(0.3, 1.0, -0.1), squeeze=1.0), 0.3
    )
    assert stale.inhibit_reason == "input_stale"

    recovered = retargeter.evaluate(
        sample(position=(0.3, 1.0, -0.1), squeeze=1.0), 0.01
    )
    assert recovered.output_enabled
    assert recovered.clutch_reanchored
    assert recovered.target_position == pytest.approx(accepted.target_position)


def test_workspace_rotation_and_tracking_gates_inhibit_output():
    config = RetargetConfig(
        track_orientation=True, max_rotation_delta_rad=math.pi / 2.0
    )
    retargeter = Retargeter(config)
    anchor = sample(squeeze=1.0)
    retargeter.calibrate(anchor)

    outside = retargeter.evaluate(
        sample(position=(0.0, 1.0, -2.0), squeeze=1.0), 0.01
    )
    assert outside.inhibit_reason == "outside_workspace"

    large_rotation = retargeter.evaluate(
        sample(
            orientation=quaternion_from_axis_angle((0.0, 1.0, 0.0), 2.0),
            squeeze=1.0,
        ),
        0.01,
    )
    assert large_rotation.inhibit_reason == "rotation_limit"

    invalid = retargeter.evaluate(sample(tracked=False, squeeze=1.0), 0.01)
    assert invalid.inhibit_reason == "tracking_invalid"


def test_reset_returns_to_uncalibrated_state():
    retargeter = Retargeter(RetargetConfig())
    retargeter.calibrate(sample())

    retargeter.clear_calibration()

    result = retargeter.evaluate(sample(squeeze=1.0), 0.01)
    assert not retargeter.calibrated
    assert result.inhibit_reason == "not_calibrated"
