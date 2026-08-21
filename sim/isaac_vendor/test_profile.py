from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from isaac_vendor.profile import (
    DEFAULT_PROFILES_PATH,
    ProfileError,
    load_profiles,
    verify_profile_files,
)


def test_vendor_profiles_match_local_assets():
    profiles = load_profiles()

    assert set(profiles) == {"omy", "rb5"}
    assert profiles["omy"].controlled_joints == (
        "joint1",
        "joint2",
        "joint3",
        "joint4",
        "joint5",
        "joint6",
        "rh_r1_joint",
    )
    assert profiles["rb5"].controlled_joints == (
        "base",
        "shoulder",
        "elbow",
        "wrist1",
        "wrist2",
        "wrist3",
    )
    for profile in profiles.values():
        assert all(check["matches"] for check in verify_profile_files(profile))


def test_robotis_runtime_camera_contract_is_explicit():
    omy = load_profiles()["omy"]

    assert {camera.key for camera in omy.cameras} == {"cam_top", "cam_wrist"}
    assert all(not camera.required_in_scene for camera in omy.cameras)
    assert all(camera.provisioned_by == "robotis_lab_runtime" for camera in omy.cameras)
    assert {camera.mode for camera in omy.cameras} == {
        "link_mount_look_at",
        "world_look_at",
    }
    assert omy.observation.joint_state_topic == "/joint_states"
    assert omy.observation.publish_rate_hz == 30.0
    assert all(camera.width == 848 and camera.height == 480 for camera in omy.cameras)
    assert all(camera.minimum_rate_hz == 25.0 for camera in omy.cameras)


def test_rb5_vendor_scene_cameras_are_required():
    rb5 = load_profiles()["rb5"]

    assert {camera.key for camera in rb5.cameras} == {"cam_front", "cam_wrist"}
    assert all(camera.required_in_scene for camera in rb5.cameras)
    assert all(camera.mode == "existing" for camera in rb5.cameras)
    assert all(camera.width == 640 and camera.height == 480 for camera in rb5.cameras)
    assert rb5.observation.joint_state_topic == "/rbpodo/joint_states"
    assert len(rb5.known_issues) == 2


def test_profiles_match_sim_manifest_observation_contracts():
    root = Path(__file__).resolve().parents[2]
    profiles = load_profiles()
    manifests = {
        "omy": yaml.safe_load((root / "manifests/omy_sim_01.yaml").read_text()),
        "rb5": yaml.safe_load(
            (root / "manifests/rb5_850e_sim_01.yaml").read_text()
        ),
    }

    for profile_id, profile in profiles.items():
        interfaces = manifests[profile_id]["interfaces"]
        state = interfaces["state"][0]
        assert state["topics"] == [profile.observation.joint_state_topic]
        assert state["minimum_rate_hz"] == profile.observation.joint_minimum_rate_hz
        cameras = {camera["key"]: camera for camera in interfaces["cameras"]}
        assert set(cameras) == {camera.key for camera in profile.cameras}
        for camera in profile.cameras:
            manifest_camera = cameras[camera.key]
            assert manifest_camera["topic"] == camera.topic
            assert manifest_camera["width"] == camera.width
            assert manifest_camera["height"] == camera.height
            assert manifest_camera["minimum_rate_hz"] == camera.minimum_rate_hz


def test_ready_positions_must_match_controlled_joints(tmp_path: Path):
    data = yaml.safe_load(DEFAULT_PROFILES_PATH.read_text(encoding="utf-8"))
    data = deepcopy(data)
    del data["profiles"]["omy"]["joints"]["ready_positions"]["joint6"]
    config = tmp_path / "profiles.yaml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ProfileError, match="exactly match"):
        load_profiles(config)


def test_exercise_is_bounded(tmp_path: Path):
    data = yaml.safe_load(DEFAULT_PROFILES_PATH.read_text(encoding="utf-8"))
    data = deepcopy(data)
    data["profiles"]["rb5"]["joints"]["exercise"]["delta_rad"] = 0.5
    config = tmp_path / "profiles.yaml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ProfileError, match="within 0.25 rad"):
        load_profiles(config)


def test_spawned_camera_pose_contract_is_required(tmp_path: Path):
    data = yaml.safe_load(DEFAULT_PROFILES_PATH.read_text(encoding="utf-8"))
    data = deepcopy(data)
    del data["profiles"]["omy"]["cameras"][0]["target"]
    config = tmp_path / "profiles.yaml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ProfileError, match="link_mount_look_at requires"):
        load_profiles(config)
