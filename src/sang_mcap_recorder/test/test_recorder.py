import io
import json
from pathlib import Path
import threading

import pytest

from sang_mcap_recorder import recorder
from sang_mcap_recorder.profile import CaptureProfile, TopicSpec
from sang_mcap_recorder.recorder import (
    build_rosbag_command,
    reported_lost_messages,
    run_capture,
    validate_recorder_diagnostics,
)
from sang_mcap_recorder.session import CaptureContext, SessionError, SessionPaths


def capture_profile() -> CaptureProfile:
    return CaptureProfile(
        schema_version="1.0",
        profile_id="test-profile",
        agent_id="processing-test",
        robot_id="robot-test",
        clock={"sync_source": "test", "quality": "TEST", "provenance": "test"},
        topics=(
            TopicSpec(
                key="joints",
                name="/test/joints",
                message_type="sensor_msgs/msg/JointState",
                asset_id="robot-test",
                calibration_ref="cal-test",
                timestamp_provenance="header",
                require_header_timestamp=True,
            ),
            TopicSpec(
                key="camera",
                name="/test/camera/compressed",
                message_type="sensor_msgs/msg/CompressedImage",
                asset_id="camera-test",
                calibration_ref="cal-test",
                timestamp_provenance="header",
                require_header_timestamp=True,
            ),
        ),
    )


class FinishedProcess:
    def __init__(self, returncode: int, output: str):
        self.returncode = returncode
        self.stdout = io.StringIO(output)
        self.pid = 999_999

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


def test_rosbag_command_is_mcap_and_contains_only_allowlisted_topics(tmp_path):
    profile = capture_profile()
    context = CaptureContext(session_id="session-001")
    paths = SessionPaths(
        root=tmp_path,
        staging=tmp_path / "session-001.partial",
        final=tmp_path / "session-001",
    )

    command = build_rosbag_command(profile=profile, context=context, paths=paths)

    assert command[:3] == ["ros2", "bag", "record"]
    assert command[command.index("--storage") + 1] == "mcap"
    assert command[command.index("--output") + 1].endswith("session-001.partial")
    assert command[command.index("--topics") + 1 :] == [
        "/test/joints",
        "/test/camera/compressed",
    ]
    for unsafe_selector in ("--all", "--all-topics", "--regex"):
        assert unsafe_selector not in command


def test_recorder_source_has_no_robot_or_vendor_command_channel_calls():
    source_root = Path(__file__).parents[1] / "sang_mcap_recorder"
    source = "\n".join(path.read_text() for path in source_root.glob("*.py"))

    for forbidden in (
        "Cobot(",
        "set_operation_mode",
        "set_speed_bar",
        "move_j",
        "move_l",
        "send_action",
        "publish(",
    ):
        assert forbidden not in source


def test_rosbag_loss_warning_is_parsed_and_other_output_is_ignored():
    fixture = (
        Path(__file__).parent / "fixtures" / "rosbag2_jazzy_transport_loss.log"
    ).read_text(encoding="utf-8")
    assert reported_lost_messages(fixture) == 14
    assert reported_lost_messages("[INFO] Recording stopped") == 0


def test_any_reported_transport_loss_blocks_completion():
    validate_recorder_diagnostics({"transport_messages_lost": 0})
    with pytest.raises(SessionError, match="14 lost transport messages"):
        validate_recorder_diagnostics({"transport_messages_lost": 14})


@pytest.mark.parametrize(
    "returncode,output,error",
    [
        (2, "recorder startup failed\n", "exited with status 2"),
        (0, "recording stopped early\n", "exited before the requested duration"),
        (
            0,
            "[WARN] Number of messages lost on the transport layer: 14\n",
            "14 lost transport messages",
        ),
    ],
)
def test_failed_process_retains_log_and_failure_manifest(
    tmp_path, returncode, output, error
):
    context = CaptureContext(session_id=f"failed-{returncode}-{len(output)}")

    with pytest.raises(SessionError, match=error):
        run_capture(
            profile=capture_profile(),
            context=context,
            capture_root=tmp_path,
            duration_s=10,
            popen=lambda *_args, **_kwargs: FinishedProcess(returncode, output),
        )

    partial = tmp_path / f"{context.session_id}.partial"
    manifest = json.loads((partial / "session-manifest.json").read_text())
    assert manifest["state"] == "failed"
    assert error.split()[0] in manifest["failure"]["reason"]
    assert (partial / "recorder.log").read_text() == output


def test_stop_event_can_finalize_after_graceful_process_exit(tmp_path, monkeypatch):
    context = CaptureContext(session_id="operator-stop")
    expected_manifest = tmp_path / "operator-stop" / "session-manifest.json"
    stop_event = threading.Event()
    stop_event.set()

    def fake_finalize_session(**kwargs):
        assert (kwargs["paths"].staging / "recorder.log").is_file()
        return {"state": "complete"}, expected_manifest

    monkeypatch.setattr(recorder, "finalize_session", fake_finalize_session)
    manifest, path = run_capture(
        profile=capture_profile(),
        context=context,
        capture_root=tmp_path,
        duration_s=0,
        stop_event=stop_event,
        popen=lambda *_args, **_kwargs: FinishedProcess(0, "recording stopped\n"),
    )

    assert manifest == {"state": "complete"}
    assert path == expected_manifest
