import pytest

from xr_omy_retarget.isaac_protocol import TargetStream, parse_target


def target_document(sequence=1, enabled=True):
    return {
        "protocol": "xr_omy_target_v1",
        "sequence": sequence,
        "ros_stamp_ns": 123,
        "target_link": "end_effector_link",
        "target": {
            "frame_id": "link0",
            "position": [0.2, -0.1, 0.4],
            "orientation_xyzw": [0.0, 0.0, 0.70710678, 0.70710678],
        },
        "gripper": 0.4,
        "enabled": enabled,
        "inhibit_reason": "enabled" if enabled else "deadman_released",
        "clutch_epoch": 2,
    }


def test_stream_accepts_fresh_enabled_target_then_marks_it_stale():
    stream = TargetStream(stale_timeout_s=0.25)
    stream.ingest(target_document(), now_s=10.0)

    assert stream.status(10.2).active
    status = stream.status(10.251)
    assert not status.active
    assert status.reason == "input_stale"


def test_disabled_target_preserves_inhibit_reason():
    stream = TargetStream()
    stream.ingest(target_document(enabled=False), now_s=1.0)

    status = stream.status(1.01)
    assert not status.active
    assert status.reason == "deadman_released"


def test_stream_rejects_replayed_sequence_until_session_timeout():
    stream = TargetStream(stale_timeout_s=0.25, sequence_reset_after_s=1.0)
    stream.ingest(target_document(sequence=20), now_s=1.0)

    with pytest.raises(ValueError, match="increase"):
        stream.ingest(target_document(sequence=19), now_s=1.5)

    assert stream.ingest(target_document(sequence=1), now_s=2.1).sequence == 1


def test_stream_accepts_duplicate_sequence_as_a_heartbeat():
    stream = TargetStream(stale_timeout_s=0.25)
    stream.ingest(target_document(sequence=20), now_s=1.0)

    duplicate = stream.ingest(target_document(sequence=20), now_s=1.2)

    assert duplicate.sequence == 20
    assert stream.status(1.4).active


def test_rejected_frames_do_not_prevent_new_sequence_epoch():
    stream = TargetStream(stale_timeout_s=0.25, sequence_reset_after_s=1.0)
    stream.ingest(target_document(sequence=20), now_s=1.0)

    for now_s in (1.5, 1.9):
        with pytest.raises(ValueError, match="increase"):
            stream.ingest(target_document(sequence=1), now_s=now_s)
        stream.invalidate(now_s)

    assert stream.ingest(target_document(sequence=1), now_s=2.1).sequence == 1
    assert stream.status(2.1).reason == "enabled"


def test_parser_rejects_wrong_robot_frame_or_non_unit_quaternion():
    wrong_frame = target_document()
    wrong_frame["target"]["frame_id"] = "world"
    with pytest.raises(ValueError, match="frame_id"):
        parse_target(wrong_frame)

    wrong_quaternion = target_document()
    wrong_quaternion["target"]["orientation_xyzw"] = [0.0, 0.0, 0.0, 2.0]
    with pytest.raises(ValueError, match="unit quaternion"):
        parse_target(wrong_quaternion)


def test_parser_rejects_non_object_or_missing_gripper_cleanly():
    with pytest.raises(ValueError, match="object"):
        parse_target([])

    missing_gripper = target_document()
    del missing_gripper["gripper"]
    with pytest.raises(ValueError, match="gripper"):
        parse_target(missing_gripper)


def test_enabled_target_must_remain_inside_omy_workspace():
    outside = target_document()
    outside["target"]["position"] = [0.9, 0.0, 0.4]

    with pytest.raises(ValueError, match="workspace"):
        parse_target(outside)

    outside["enabled"] = False
    outside["inhibit_reason"] = "outside_workspace"
    assert not parse_target(outside).enabled


def test_malformed_packet_inhibits_the_previous_valid_target_immediately():
    stream = TargetStream(stale_timeout_s=0.25)
    stream.ingest(target_document(), now_s=10.0)
    assert stream.status(10.01).active

    stream.invalidate(10.02)

    status = stream.status(10.021)
    assert not status.active
    assert status.reason == "invalid_target"
