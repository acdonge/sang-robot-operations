from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from sang_mcap_recorder.profile import CaptureProfile, TopicSpec
from sang_mcap_recorder.session import (
    CaptureContext,
    SessionError,
    finalize_session,
    prepare_paths,
    validate_context,
    validate_metadata,
    write_failure_manifest,
)


NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def profile() -> CaptureProfile:
    return CaptureProfile(
        schema_version="1.0",
        profile_id="test-profile",
        agent_id="processing-test",
        robot_id="robot-test",
        clock={
            "sync_source": "test_clock",
            "quality": "TEST_ONLY",
            "provenance": "test clock",
        },
        topics=(
            TopicSpec(
                key="joints",
                name="/test/joint_states",
                message_type="sensor_msgs/msg/JointState",
                asset_id="test-robot",
                calibration_ref="test-calibration",
                timestamp_provenance="test header",
                require_header_timestamp=True,
            ),
            TopicSpec(
                key="camera",
                name="/test/camera/compressed",
                message_type="sensor_msgs/msg/CompressedImage",
                asset_id="test-camera",
                calibration_ref="test-camera-calibration",
                timestamp_provenance="test header",
                require_header_timestamp=True,
            ),
        ),
    )


def write_bag(staging: Path, *, camera_count: int = 4, camera_type: str = "sensor_msgs/msg/CompressedImage") -> None:
    staging.mkdir()
    (staging / "bag_0.mcap").write_bytes(b"fake-mcap-payload")
    metadata = {
        "rosbag2_bagfile_information": {
            "version": 9,
            "storage_identifier": "mcap",
            "duration": {"nanoseconds": 2_000_000_000},
            "starting_time": {"nanoseconds_since_epoch": 1_786_000_000_000_000_000},
            "message_count": 7 + camera_count,
            "topics_with_message_count": [
                {
                    "topic_metadata": {
                        "name": "/test/joint_states",
                        "type": "sensor_msgs/msg/JointState",
                    },
                    "message_count": 7,
                },
                {
                    "topic_metadata": {
                        "name": "/test/camera/compressed",
                        "type": camera_type,
                    },
                    "message_count": camera_count,
                },
            ],
            "relative_file_paths": ["bag_0.mcap"],
        }
    }
    (staging / "metadata.yaml").write_text(yaml.safe_dump(metadata), encoding="utf-8")


def header_stats(camera_count: int = 4):
    return {
        "/test/joint_states": {
            "message_count": 7,
            "valid_header_count": 7,
            "zero_header_count": 0,
            "timestamp_regressions": 0,
            "first_header_ns": 1_786_000_000_100_000_000,
            "last_header_ns": 1_786_000_001_900_000_000,
            "minimum_header_ns": 1_786_000_000_100_000_000,
            "maximum_header_ns": 1_786_000_001_900_000_000,
        },
        "/test/camera/compressed": {
            "message_count": camera_count,
            "valid_header_count": camera_count,
            "zero_header_count": 0,
            "timestamp_regressions": 0,
            "first_header_ns": 1_786_000_000_200_000_000,
            "last_header_ns": 1_786_000_001_800_000_000,
            "minimum_header_ns": 1_786_000_000_200_000_000,
            "maximum_header_ns": 1_786_000_001_800_000_000,
        },
    }


def test_finalize_session_renames_partial_only_after_validation_and_hashes_files(tmp_path):
    context = CaptureContext(session_id="session-001", run_id="run-001")
    paths = prepare_paths(tmp_path, context)
    write_bag(paths.staging)
    (paths.staging / "recorder.log").write_text("messages lost: 0\n", encoding="utf-8")

    manifest, manifest_path = finalize_session(
        paths=paths,
        profile=profile(),
        context=context,
        started_at=NOW,
        finished_at=NOW,
        header_inspector=lambda _path: header_stats(),
        extra_artifact_names=("recorder.log",),
        recorder_diagnostics={"transport_messages_lost": 0},
    )

    assert not paths.staging.exists()
    assert paths.final.is_dir()
    assert manifest_path == paths.final / "session-manifest.json"
    assert manifest["state"] == "complete"
    assert manifest["run_id"] == "run-001"
    assert manifest["recorder_diagnostics"]["transport_messages_lost"] == 0
    assert manifest["source_time_range"]["start_ns"] == 1_786_000_000_100_000_000
    assert manifest["source_time_range"]["end_ns"] == 1_786_000_001_900_000_000
    for entry in manifest["files"]:
        payload = (paths.final / entry["path"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()


def test_finalize_session_rejects_empty_wrong_type_and_invalid_timestamps(tmp_path):
    context = CaptureContext(session_id="session-empty")
    paths = prepare_paths(tmp_path, context)
    write_bag(paths.staging, camera_count=0)
    with pytest.raises(SessionError, match="recorded no messages"):
        finalize_session(
            paths=paths,
            profile=profile(),
            context=context,
            started_at=NOW,
            finished_at=NOW,
            header_inspector=lambda _path: header_stats(0),
        )

    wrong_root = tmp_path / "wrong"
    wrong_paths = prepare_paths(wrong_root, CaptureContext(session_id="session-wrong"))
    write_bag(wrong_paths.staging, camera_type="sensor_msgs/msg/Image")
    with pytest.raises(SessionError, match="expected sensor_msgs/msg/CompressedImage"):
        validate_metadata(wrong_paths.staging, profile())

    timestamp_root = tmp_path / "timestamp"
    timestamp_context = CaptureContext(session_id="session-zero")
    timestamp_paths = prepare_paths(timestamp_root, timestamp_context)
    write_bag(timestamp_paths.staging)
    invalid_stats = header_stats()
    invalid_stats["/test/camera/compressed"]["zero_header_count"] = 1
    with pytest.raises(SessionError, match="zero source header timestamps"):
        finalize_session(
            paths=timestamp_paths,
            profile=profile(),
            context=timestamp_context,
            started_at=NOW,
            finished_at=NOW,
            header_inspector=lambda _path: invalid_stats,
        )


def test_metadata_rejects_artifact_path_traversal(tmp_path):
    context = CaptureContext(session_id="session-traversal")
    paths = prepare_paths(tmp_path, context)
    write_bag(paths.staging)
    metadata_path = paths.staging / "metadata.yaml"
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    metadata["rosbag2_bagfile_information"]["relative_file_paths"] = ["../outside.mcap"]
    metadata_path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
    (tmp_path / "outside.mcap").write_bytes(b"outside")

    with pytest.raises(SessionError, match="escapes session"):
        validate_metadata(paths.staging, profile())


def test_metadata_rejects_symbolic_link_artifacts(tmp_path):
    context = CaptureContext(session_id="session-symlink")
    paths = prepare_paths(tmp_path, context)
    write_bag(paths.staging)
    metadata_path = paths.staging / "metadata.yaml"
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    metadata["rosbag2_bagfile_information"]["relative_file_paths"] = [
        "linked.mcap"
    ]
    metadata_path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
    (paths.staging / "linked.mcap").symlink_to(paths.staging / "bag_0.mcap")

    with pytest.raises(SessionError, match="symbolic link"):
        validate_metadata(paths.staging, profile())


def test_failed_capture_retains_partial_directory_and_failure_manifest(tmp_path):
    context = CaptureContext(session_id="session-failed")
    paths = prepare_paths(tmp_path, context)

    manifest_path = write_failure_manifest(
        paths=paths,
        profile=profile(),
        context=context,
        started_at=NOW,
        finished_at=NOW,
        reason="topic recorded no messages",
        recorder_diagnostics={"transport_messages_lost": 3},
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert paths.staging.is_dir()
    assert not paths.final.exists()
    assert payload["state"] == "failed"
    assert payload["failure"]["reason"] == "topic recorded no messages"
    assert payload["recorder_diagnostics"]["transport_messages_lost"] == 3


def test_session_identity_and_task_version_are_validated(tmp_path):
    with pytest.raises(SessionError, match="safe identifier"):
        prepare_paths(tmp_path, CaptureContext(session_id="../escape"))
    with pytest.raises(SessionError, match="provided together"):
        validate_context(
            CaptureContext(session_id="safe", task_definition_id="task-only")
        )
