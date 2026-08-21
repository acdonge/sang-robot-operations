from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from so101_readonly_adapter.config import (
    HardwareConfigError,
    load_hardware_config,
    parse_hardware_config,
)


CONFIG_PATH = Path(__file__).parents[3] / "config" / "bi_so101_hardware.yaml"


def config_data():
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def test_lab_hardware_config_binds_four_arms_and_five_cameras():
    config = load_hardware_config(CONFIG_PATH)

    assert [arm.key for arm in config.arms] == [
        "follower_left",
        "follower_right",
        "leader_left",
        "leader_right",
    ]
    assert len(config.cameras) == 5
    assert config.arms[0].calibration_path == (
        CONFIG_PATH.parent / "calibration" / "bi_so101_follower_left.json"
    )
    assert config.cameras[0].device.endswith(
        "usb-0:4.1:1.0-video-index0"
    )
    assert all(camera.fixed_fps for camera in config.cameras)


def test_duplicate_device_and_unsupported_fourcc_are_rejected():
    duplicate = config_data()
    duplicate["cameras"][1]["device"] = duplicate["cameras"][0]["device"]
    with pytest.raises(HardwareConfigError, match="camera devices"):
        parse_hardware_config(duplicate)

    invalid = deepcopy(config_data())
    invalid["cameras"][0]["fourcc"] = "H264"
    with pytest.raises(HardwareConfigError, match="must be MJPG or YUYV"):
        parse_hardware_config(invalid)


def test_camera_only_config_accepts_yuyv_jpeg_encoding():
    data = config_data()
    data["arms"] = []
    data["cameras"] = [deepcopy(data["cameras"][0])]
    data["cameras"][0]["fourcc"] = "YUYV"
    data["cameras"][0]["jpeg_quality"] = 85

    config = parse_hardware_config(data)

    assert config.arms == ()
    assert config.cameras[0].fourcc == "YUYV"
    assert config.cameras[0].jpeg_quality == 85
