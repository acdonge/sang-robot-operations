from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from robot_agent.manifest import ManifestValidationError, load_manifest, parse_manifest


MANIFEST_ROOT = Path(__file__).parents[3] / "manifests"
SO101_MANIFEST_PATH = MANIFEST_ROOT / "bi_so101_01.yaml"
OMY_MANIFEST_PATH = MANIFEST_ROOT / "omy_01.yaml"
RB5_MANIFEST_PATH = MANIFEST_ROOT / "rb5_850e_01.yaml"


def manifest_data():
    return yaml.safe_load(SO101_MANIFEST_PATH.read_text(encoding="utf-8"))


def test_so101_manifest_loads_for_monitoring():
    manifest = load_manifest(SO101_MANIFEST_PATH)

    assert manifest.robot.robot_id == "bi_so101_01"
    assert manifest.robot.real_or_sim == "real"
    assert manifest.robot.runtime_kind == "physical"
    assert len(manifest.sources) == 7
    assert sum(source.required for source in manifest.sources) == 6
    action = next(source for source in manifest.sources if source.key == "action")
    assert action.kind == "action"
    assert not action.required
    assert action.minimum_rate_hz == 18.0
    assert action.features_for(action.topics[0]) == (
        "left_shoulder_pan",
        "left_shoulder_lift",
        "left_elbow_flex",
        "left_wrist_flex",
        "left_wrist_roll",
        "left_gripper",
    )
    assert [source.kind for source in manifest.sources].count("camera") == 5
    assert all(
        source.minimum_rate_hz == 25.0
        for source in manifest.sources
        if source.kind == "camera"
    )


def test_omy_manifest_matches_robotis_monitoring_topics():
    manifest = load_manifest(OMY_MANIFEST_PATH)

    assert manifest.robot.robot_id == "omy_01"
    assert manifest.robot.model == "omy_f3m"
    assert manifest.robot.runtime_kind == "physical"
    assert len(manifest.sources) == 4
    assert {source.key for source in manifest.sources if source.required} == {
        "observation.state",
        "cam_top",
        "cam_wrist",
    }
    assert next(source for source in manifest.sources if source.kind == "state").topics == (
        "/joint_states",
    )
    action = next(source for source in manifest.sources if source.kind == "action")
    assert action.topics == ("/leader/joint_trajectory",)
    assert not action.required
    cameras = {
        source.key: source for source in manifest.sources if source.kind == "camera"
    }
    assert cameras["cam_top"].topics == (
        "/camera/cam_top/color/image_raw/compressed",
    )
    assert cameras["cam_wrist"].topics == (
        "/camera/cam_wrist/color/image_rect_raw/compressed",
    )
    assert all(source.minimum_rate_hz is None for source in manifest.sources)


def test_rb5_manifest_uses_native_rbpodo_joint_state_contract():
    manifest = load_manifest(RB5_MANIFEST_PATH)
    raw = yaml.safe_load(RB5_MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest.robot.robot_id == "rb5_850e_01"
    assert manifest.robot.model == "rb5_850e"
    assert raw["adapter"] == {
        "package": "rb5_readonly_adapter",
        "executable": "state_reader",
        "kind": "readonly_vendor_data_adapter",
        "config_ref": "10.0.2.7:5001",
        "version": "0.1.0",
    }
    assert len(manifest.sources) == 2
    state = next(source for source in manifest.sources if source.kind == "state")
    assert state.key == "observation.state"
    assert state.kind == "state"
    assert state.topics == ("/rbpodo/joint_states",)
    assert state.minimum_rate_hz == 20.0
    assert state.required
    assert raw["interfaces"]["state"][0]["feature_names"] == [
        "base",
        "shoulder",
        "elbow",
        "wrist1",
        "wrist2",
        "wrist3",
    ]
    assert raw["interfaces"]["action"] == []
    camera = next(source for source in manifest.sources if source.kind == "camera")
    assert camera.key == "cam_wrist"
    assert camera.topics == (
        "/rbpodo/camera/wrist/color/image_rect_raw/compressed",
    )
    assert camera.minimum_rate_hz == 8.0
    assert camera.required
    assert raw["capabilities"]["camera"] is True
    assert raw["ui"]["camera_order"] == ["cam_wrist"]


def test_unknown_required_source_is_rejected():
    data = manifest_data()
    data["monitoring"]["required_sources"].append("missing")

    with pytest.raises(ManifestValidationError, match="missing"):
        parse_manifest(data)


def test_duplicate_source_key_is_rejected():
    data = manifest_data()
    duplicate = deepcopy(data["interfaces"]["cameras"][0])
    duplicate["key"] = data["interfaces"]["state"][0]["key"]
    data["interfaces"]["cameras"].append(duplicate)

    with pytest.raises(ManifestValidationError, match="unique"):
        parse_manifest(data)


def test_non_positive_minimum_rate_is_rejected():
    data = manifest_data()
    data["interfaces"]["cameras"][0]["minimum_rate_hz"] = 0

    with pytest.raises(ManifestValidationError, match="positive number"):
        parse_manifest(data)


def test_unknown_real_or_sim_value_is_rejected():
    data = manifest_data()
    data["robot"]["real_or_sim"] = "hardware"

    with pytest.raises(ManifestValidationError, match="real_or_sim"):
        parse_manifest(data)


def test_multi_topic_joint_source_requires_explicit_feature_partition():
    data = manifest_data()
    del data["interfaces"]["state"][0]["topic_features"]

    with pytest.raises(ManifestValidationError, match="topic_features is required"):
        parse_manifest(data)


def test_topic_feature_partition_must_preserve_flat_feature_order():
    data = manifest_data()
    topic = data["interfaces"]["state"][0]["topics"][0]
    data["interfaces"]["state"][0]["topic_features"][topic].reverse()

    with pytest.raises(ManifestValidationError, match="feature_names order"):
        parse_manifest(data)


def test_camera_encoding_and_kind_message_type_are_validated():
    data = manifest_data()
    data["interfaces"]["cameras"][0]["encoding"] = "png"
    with pytest.raises(ManifestValidationError, match="JPEG alias"):
        parse_manifest(data)

    data = manifest_data()
    data["interfaces"]["state"][0]["message_type"] = (
        "trajectory_msgs/msg/JointTrajectory"
    )
    with pytest.raises(ManifestValidationError, match="for state"):
        parse_manifest(data)
