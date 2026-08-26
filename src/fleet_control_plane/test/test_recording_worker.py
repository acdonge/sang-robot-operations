from pathlib import Path
import signal
import ssl
from types import SimpleNamespace

import pytest

from fleet_control_plane.capture_jobs import CaptureWorkerPoll
from fleet_control_plane.recording_worker import (
    RecordingWorker,
    RecordingWorkerError,
    recorder_command,
    validate_job,
)


AGENT_ID = "processing-lab1"
WORKER_ID = "processing-lab1-rb5-capture"
PROFILE_ID = "rb5-lab1-observation-v2"
ROBOT_ID = "rb5-lab1-eval-01"
ASSET_IDS = ["rb5-lab1", "rb5-lab1-d455"]


class FakeProcess:
    def __init__(self):
        self.return_code = None
        self.signals = []
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.return_code

    def send_signal(self, value):
        self.signals.append(value)

    def wait(self, timeout=None):
        del timeout
        if self.return_code is None:
            self.return_code = 0
        return self.return_code

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def poll_contract():
    return CaptureWorkerPoll(
        worker_id=WORKER_ID,
        profile_id=PROFILE_ID,
        robot_id=ROBOT_ID,
        asset_ids=ASSET_IDS,
    )


def job(state="CLAIMED"):
    return {
        "job_id": "capture-0123456789abcdef0123456789abcdef",
        "session_id": "capture-0123456789abcdef0123456789abcdef",
        "run_id": "rb5-capture-001",
        "worker_id": WORKER_ID,
        "agent_id": AGENT_ID,
        "robot_id": ROBOT_ID,
        "profile_id": PROFILE_ID,
        "asset_ids": ASSET_IDS,
        "duration_s": 60,
        "state": state,
        "lease_id": "0123456789abcdef0123456789abcdef",
        "stop_requested_at": None,
        "task_definition": {"id": "rb5-observe", "version": 1},
    }


def make_worker(tmp_path, *, popen):
    capture_root = tmp_path / "captures"
    capture_root.mkdir()
    profile_path = tmp_path / "rb5-profile.yaml"
    profile_path.write_text("fixed profile", encoding="utf-8")
    return RecordingWorker(
        central_url="https://127.0.0.1:8443",
        context=ssl.create_default_context(),
        agent_id=AGENT_ID,
        poll=poll_contract(),
        profile_path=profile_path,
        capture_root=capture_root,
        request_timeout_s=2.0,
        reserve_mib=0.0,
        estimated_mib_per_s=1.0,
        popen=popen,
        disk_usage=lambda path: SimpleNamespace(free=10 * 1024**3),
    )


def test_command_is_fixed_to_read_only_recorder_and_task_provenance(tmp_path):
    current = job()
    command = recorder_command(
        profile_path=tmp_path / "fixed.yaml",
        capture_root=tmp_path / "captures",
        job=current,
    )
    assert command[1:3] == ["-m", "sang_mcap_recorder.cli"]
    assert command[command.index("--profile") + 1] == str(tmp_path / "fixed.yaml")
    assert command[command.index("--session-id") + 1] == current["job_id"]
    assert command[command.index("--run-id") + 1] == current["run_id"]
    assert command[command.index("--task-definition-id") + 1] == "rb5-observe"
    assert "ros2" not in command
    assert "service" not in command
    assert "action" not in command
    assert "topic" not in command

    changed_agent = {**current, "agent_id": "different-agent"}
    with pytest.raises(RecordingWorkerError, match="agent_id differs"):
        validate_job(changed_agent, poll=poll_contract(), agent_id=AGENT_ID)
    unsafe_run = {**current, "run_id": "--publish-command"}
    with pytest.raises(RecordingWorkerError, match="safe identifier"):
        validate_job(unsafe_run, poll=poll_contract(), agent_id=AGENT_ID)


def test_worker_starts_stops_and_hands_finalized_session_to_uploader(
    tmp_path,
    monkeypatch,
):
    process = FakeProcess()
    commands = []

    def popen(command):
        commands.append(command)
        return process

    worker = make_worker(tmp_path, popen=popen)
    central_job = job()
    reports = []

    def request(path, payload):
        if path == "/api/v1/capture-workers/poll":
            return {"job": dict(central_job)}
        reports.append(dict(payload))
        central_job["state"] = payload["state"]
        return {"state": payload["state"]}

    monkeypatch.setattr(worker, "_request", request)
    worker.tick()
    assert len(commands) == 1
    assert worker.process is process
    assert [item["state"] for item in reports] == ["RECORDING"]

    central_job["stop_requested_at"] = "2026-08-24T16:00:10.000Z"
    worker.tick()
    assert process.signals == [signal.SIGINT]
    assert [item["state"] for item in reports] == ["RECORDING", "FINALIZING"]

    final = worker.capture_root / central_job["session_id"]
    final.mkdir()
    monkeypatch.setattr(worker, "_validate_final", lambda current, path: None)
    process.return_code = 0
    worker.tick()
    assert worker.process is None
    assert reports[-1]["state"] == "UPLOADING"


def test_restart_preserves_partial_and_reports_failure_without_duplicate(
    tmp_path,
    monkeypatch,
):
    worker = make_worker(
        tmp_path,
        popen=lambda command: pytest.fail(f"must not restart recorder: {command}"),
    )
    central_job = job("RECORDING")
    partial = worker.capture_root / f"{central_job['session_id']}.partial"
    partial.mkdir()
    reports = []

    def request(path, payload):
        if path == "/api/v1/capture-workers/poll":
            return {"job": dict(central_job)}
        reports.append(dict(payload))
        return {"state": payload["state"]}

    monkeypatch.setattr(worker, "_request", request)
    worker.tick()
    assert partial.is_dir()
    assert reports == [
        {
            "worker_id": WORKER_ID,
            "lease_id": central_job["lease_id"],
            "state": "FAILED",
            "error": (
                "recording worker restarted; partial session was preserved for inspection"
            ),
        }
    ]


def test_lost_start_report_does_not_orphan_or_duplicate_the_recorder(
    tmp_path,
    monkeypatch,
):
    process = FakeProcess()
    starts = []
    worker = make_worker(
        tmp_path,
        popen=lambda command: starts.append(command) or process,
    )
    central_job = job()
    report_attempts = 0

    def request(path, payload):
        nonlocal report_attempts
        if path == "/api/v1/capture-workers/poll":
            return {"job": dict(central_job)}
        report_attempts += 1
        if report_attempts == 1:
            raise RecordingWorkerError("simulated lost response")
        central_job["state"] = payload["state"]
        return {"state": payload["state"]}

    monkeypatch.setattr(worker, "_request", request)
    with pytest.raises(RecordingWorkerError, match="lost response"):
        worker.tick()
    assert worker.process is process
    assert len(starts) == 1

    worker.tick()
    assert worker.process is process
    assert len(starts) == 1
    assert central_job["state"] == "RECORDING"
