from pathlib import Path

import pytest

from sang_mcap_recorder.profile import ProfileError, load_profile


PROFILE = """
schema_version: "1.0"
profile_id: test-profile
agent_id: processing-test
robot_id: robot-test
clock:
  sync_source: test_clock
  quality: TEST_ONLY
  provenance: test timestamp provenance
topics:
  - key: joints
    name: /test/joint_states
    type: sensor_msgs/msg/JointState
    asset_id: test-robot
    calibration_ref: test-calibration
    timestamp_provenance: test header
    require_header_timestamp: true
  - key: camera
    name: /test/camera/compressed
    type: sensor_msgs/msg/CompressedImage
    asset_id: test-camera
    calibration_ref: test-camera-calibration
    timestamp_provenance: test header
    require_header_timestamp: true
"""


def write_profile(tmp_path: Path, contents: str = PROFILE) -> Path:
    path = tmp_path / "capture.yaml"
    path.write_text(contents, encoding="utf-8")
    return path


def test_load_profile_accepts_an_explicit_topic_allowlist(tmp_path):
    qos_path = tmp_path / "capture-qos.yaml"
    qos_path.write_text("/test/joint_states:\n  depth: 10\n", encoding="utf-8")
    profile_path = write_profile(
        tmp_path,
        PROFILE.replace(
            "robot_id: robot-test",
            "robot_id: robot-test\nqos_overrides_path: capture-qos.yaml",
        ),
    )
    profile = load_profile(profile_path)

    assert profile.profile_id == "test-profile"
    assert [topic.name for topic in profile.topics] == [
        "/test/joint_states",
        "/test/camera/compressed",
    ]
    assert all(topic.require_header_timestamp for topic in profile.topics)
    assert profile.qos_overrides_path == qos_path


@pytest.mark.parametrize(
    "old,new,match",
    [
        ("profile_id: test-profile", "profile_id: ../escape", "safe identifier"),
        ("name: /test/joint_states", "name: relative", "absolute ROS topic"),
        (
            "type: sensor_msgs/msg/JointState",
            "type: sensor_msgs/JointState",
            "ROS 2 message type",
        ),
    ],
)
def test_load_profile_rejects_unsafe_identifiers_and_topics(tmp_path, old, new, match):
    with pytest.raises(ProfileError, match=match):
        load_profile(write_profile(tmp_path, PROFILE.replace(old, new, 1)))


def test_load_profile_rejects_duplicate_topics_and_unknown_keys(tmp_path):
    duplicate = PROFILE.replace(
        "name: /test/camera/compressed", "name: /test/joint_states"
    )
    with pytest.raises(ProfileError, match="topic names must be unique"):
        load_profile(write_profile(tmp_path, duplicate))

    unknown = PROFILE.replace("profile_id: test-profile", "profile_id: test-profile\nmode: command")
    with pytest.raises(ProfileError, match="unsupported keys"):
        load_profile(write_profile(tmp_path, unknown))


def test_load_profile_rejects_qos_path_traversal(tmp_path):
    outside = tmp_path.parent / "outside-qos.yaml"
    outside.write_text("{}", encoding="utf-8")
    escaped = PROFILE.replace(
        "robot_id: robot-test",
        "robot_id: robot-test\nqos_overrides_path: ../outside-qos.yaml",
    )

    with pytest.raises(ProfileError, match="remain inside"):
        load_profile(write_profile(tmp_path, escaped))
