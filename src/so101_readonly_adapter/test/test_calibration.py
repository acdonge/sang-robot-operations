from copy import deepcopy

import pytest

from so101_readonly_adapter.calibration import (
    CalibrationError,
    MOTOR_ORDER,
    parse_calibration,
)


def calibration_data():
    return {
        name: {
            "id": index,
            "drive_mode": 0,
            "homing_offset": 0,
            "range_min": 100,
            "range_max": 1100,
        }
        for index, name in enumerate(MOTOR_ORDER, start=1)
    }


def test_normalization_matches_lerobot_ranges():
    calibration = parse_calibration(calibration_data())
    minimum = calibration.normalize({motor_id: 100 for motor_id in range(1, 7)})
    midpoint = calibration.normalize({motor_id: 600 for motor_id in range(1, 7)})
    maximum = calibration.normalize({motor_id: 1100 for motor_id in range(1, 7)})

    assert minimum == (-100.0, -100.0, -100.0, -100.0, -100.0, 0.0)
    assert midpoint == (0.0, 0.0, 0.0, 0.0, 0.0, 50.0)
    assert maximum == (100.0, 100.0, 100.0, 100.0, 100.0, 100.0)


def test_drive_mode_and_clamping_are_applied():
    data = calibration_data()
    data["shoulder_pan"]["drive_mode"] = 1
    data["gripper"]["drive_mode"] = 1
    calibration = parse_calibration(data)

    values = calibration.normalize(
        {1: 1100, 2: -500, 3: 1600, 4: 600, 5: 600, 6: 100}
    )

    assert values == (-100.0, -100.0, 100.0, 0.0, 0.0, 100.0)


def test_invalid_motor_set_and_ranges_are_rejected():
    missing = calibration_data()
    del missing["gripper"]
    with pytest.raises(CalibrationError, match="missing gripper"):
        parse_calibration(missing)

    invalid_range = deepcopy(calibration_data())
    invalid_range["elbow_flex"]["range_max"] = 100
    with pytest.raises(CalibrationError, match="range_min"):
        parse_calibration(invalid_range)
