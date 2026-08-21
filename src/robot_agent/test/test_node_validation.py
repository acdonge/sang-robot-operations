from pathlib import Path

from sensor_msgs.msg import CompressedImage, JointState
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint

from robot_agent.manifest import load_manifest
from robot_agent.node import RobotAgentNode, sample_is_valid, source_timestamp_s


MANIFEST_PATH = Path(__file__).parents[3] / "manifests" / "bi_so101_01.yaml"


def sources():
    manifest = load_manifest(MANIFEST_PATH)
    return {source.key: source for source in manifest.sources}


def test_state_sample_uses_explicit_per_topic_feature_contract():
    state = sources()["observation.state"]
    topic = state.topics[0]
    message = JointState()
    message.name = list(state.features_for(topic))
    message.position = [0.0] * len(message.name)

    assert sample_is_valid(state, topic, message)
    message.name.reverse()
    assert not sample_is_valid(state, topic, message)


def test_action_requires_exactly_one_finite_trajectory_point():
    action = sources()["action"]
    topic = action.topics[0]
    message = JointTrajectory()
    message.joint_names = list(action.features_for(topic))

    assert not sample_is_valid(action, topic, message)
    point = JointTrajectoryPoint()
    point.positions = [0.0] * len(message.joint_names)
    message.points = [point]
    assert sample_is_valid(action, topic, message)
    message.points[0].positions[0] = float("nan")
    assert not sample_is_valid(action, topic, message)


def test_camera_sample_accepts_only_the_frozen_jpeg_aliases():
    camera = sources()["top"]
    message = CompressedImage()
    message.format = "mjpg"
    assert sample_is_valid(camera, camera.topics[0], message)

    message.format = "rgb8; jpeg compressed bgr8"
    assert sample_is_valid(camera, camera.topics[0], message)

    message.format = "png"
    assert not sample_is_valid(camera, camera.topics[0], message)


def test_ros_header_timestamp_is_exposed_for_deterministic_rate_windows():
    message = JointState()
    assert source_timestamp_s(message) is None

    message.header.stamp.sec = 100
    message.header.stamp.nanosec = 250_000_000
    assert source_timestamp_s(message) == 100.25


def test_distributed_agent_can_keep_preview_local_without_advertising_it():
    class Monitor:
        @staticmethod
        def robot_snapshot(*, now_monotonic, include_runtime_kind):
            assert now_monotonic == 1.0
            assert include_runtime_kind is False
            return {
                "robot_id": "robot-1",
                "sources": [{"key": "wrist", "kind": "camera"}],
            }

    class Preview:
        @staticmethod
        def stream_url(robot_id, source_key):
            return f"http://127.0.0.1:8101/{robot_id}/{source_key}.mjpg"

    node = object.__new__(RobotAgentNode)
    node._monitors = [Monitor()]
    node._preview = Preview()
    node._advertise_preview = False
    node._heartbeat_schema_version = 1

    snapshot = node._robot_snapshots(1.0)[0]

    assert "preview_url" not in snapshot["sources"][0]
    assert node._preview is not None
