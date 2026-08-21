import pytest

from xr_omy_retarget.isaac_gateway import PROTOCOL, target_document
from xr_teleop_interfaces.msg import XROmyTarget


def target_message(*, enabled=True, protocol_version=None, gripper=0.5):
    message = XROmyTarget()
    message.protocol_version = (
        XROmyTarget.PROTOCOL_VERSION
        if protocol_version is None
        else protocol_version
    )
    message.sequence = 42
    message.header.stamp.sec = 3
    message.header.stamp.nanosec = 7
    message.target_link = "end_effector_link"
    message.target.header.frame_id = "link0"
    message.target.pose.position.x = 0.2
    message.target.pose.position.y = -0.1
    message.target.pose.position.z = 0.4
    message.target.pose.orientation.z = 0.70710678
    message.target.pose.orientation.w = 0.70710678
    message.gripper = gripper
    message.enabled = enabled
    message.inhibit_reason = "enabled" if enabled else "deadman_released"
    message.clutch_epoch = 3
    return message


def test_gateway_document_preserves_the_omy_cartesian_contract():
    document = target_document(target_message())

    assert document == {
        "protocol": PROTOCOL,
        "sequence": 42,
        "ros_stamp_ns": 3_000_000_007,
        "target_link": "end_effector_link",
        "target": {
            "frame_id": "link0",
            "position": [0.2, -0.1, 0.4],
            "orientation_xyzw": [0.0, 0.0, 0.70710678, 0.70710678],
        },
        "gripper": pytest.approx(0.5),
        "enabled": True,
        "inhibit_reason": "enabled",
        "clutch_epoch": 3,
    }


def test_gateway_rejects_invalid_protocol_and_gripper_values():
    with pytest.raises(ValueError, match="unsupported"):
        target_document(target_message(protocol_version=99))
    with pytest.raises(ValueError, match="gripper"):
        target_document(target_message(gripper=1.1))


def test_enabled_gateway_target_requires_a_pose_orientation():
    message = target_message()
    message.target.pose.orientation.z = 0.0
    message.target.pose.orientation.w = 0.0

    with pytest.raises(ValueError, match="enabled target"):
        target_document(message)


def test_gateway_rejects_missing_frame_and_non_unit_orientation():
    missing_frame = target_message()
    missing_frame.target.header.frame_id = ""
    with pytest.raises(ValueError, match="frame_id"):
        target_document(missing_frame)

    non_unit = target_message()
    non_unit.target.pose.orientation.z = 0.2
    non_unit.target.pose.orientation.w = 0.2
    with pytest.raises(ValueError, match="unit quaternion"):
        target_document(non_unit)
