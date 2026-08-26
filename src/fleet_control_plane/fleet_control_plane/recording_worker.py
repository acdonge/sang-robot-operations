"""Pull and execute one fixed-profile read-only capture job at the Edge."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import math
from pathlib import Path
import re
import shutil
import signal
import ssl
import subprocess
import sys
import threading
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from fleet_control_plane.capture_jobs import CaptureWorkerPoll
from fleet_control_plane.recording_upload import (
    create_client_context,
    read_manifest,
    validate_central_url,
)
from sang_mcap_recorder.profile import CaptureProfile, ProfileError, load_profile


MAX_CONTROL_RESPONSE_BYTES = 1024 * 1024
SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


class RecordingWorkerError(RuntimeError):
    """The fixed-profile worker cannot safely execute or reconcile a job."""


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True), flush=True)


def request_control_json(
    url: str,
    *,
    context: ssl.SSLContext,
    payload: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=encoded,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Content-Length": str(len(encoded)),
            "User-Agent": "sang-recording-worker/1",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_s, context=context) as response:
            body = response.read(MAX_CONTROL_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        detail = exc.read(16 * 1024).decode("utf-8", errors="replace")
        raise RecordingWorkerError(
            f"Central capture control returned HTTP {exc.code}: {detail}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RecordingWorkerError(f"Central capture control failed: {exc}") from exc
    if len(body) > MAX_CONTROL_RESPONSE_BYTES:
        raise RecordingWorkerError("Central capture response exceeds 1 MiB")
    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RecordingWorkerError("Central returned invalid capture JSON") from exc
    if not isinstance(result, dict):
        raise RecordingWorkerError("Central returned an invalid capture response")
    return result


def profile_asset_ids(profile: CaptureProfile) -> list[str]:
    return sorted({topic.asset_id for topic in profile.topics})


def validate_job(
    job: dict[str, Any],
    *,
    poll: CaptureWorkerPoll,
    agent_id: str,
) -> None:
    expected = {
        "worker_id": poll.worker_id,
        "agent_id": agent_id,
        "robot_id": poll.robot_id,
        "profile_id": poll.profile_id,
    }
    for key, value in expected.items():
        if value is not None and job.get(key) != value:
            raise RecordingWorkerError(f"capture job {key} differs from the fixed profile")
    identifiers = {
        "job_id": job.get("job_id"),
        "session_id": job.get("session_id"),
        "run_id": job.get("run_id"),
    }
    task = job.get("task_definition")
    if isinstance(task, dict):
        identifiers["task_definition.id"] = task.get("id")
    for label, value in identifiers.items():
        if not isinstance(value, str) or re.fullmatch(SAFE_ID_PATTERN, value) is None:
            raise RecordingWorkerError(f"capture job {label} is not a safe identifier")
    if job.get("session_id") != job.get("job_id"):
        raise RecordingWorkerError("capture job session_id must equal its job_id")
    if sorted(job.get("asset_ids", [])) != sorted(poll.asset_ids):
        raise RecordingWorkerError("capture job assets differ from the fixed profile")
    task = job.get("task_definition")
    if not isinstance(task, dict) or not task.get("id") or not isinstance(
        task.get("version"), int
    ):
        raise RecordingWorkerError("capture job is missing its Task Definition")
    if not isinstance(job.get("run_id"), str) or not job["run_id"]:
        raise RecordingWorkerError("capture job is missing its Task Run")
    if not isinstance(job.get("duration_s"), int) or not 5 <= job["duration_s"] <= 28800:
        raise RecordingWorkerError("capture job duration is outside the accepted bound")
    if not isinstance(job.get("lease_id"), str) or not job["lease_id"]:
        raise RecordingWorkerError("capture job does not contain a worker lease")


def recorder_command(
    *,
    profile_path: Path,
    capture_root: Path,
    job: dict[str, Any],
    clock_sync_host: str | None = None,
) -> list[str]:
    task = job["task_definition"]
    command = [
        sys.executable,
        "-m",
        "sang_mcap_recorder.cli",
        "--profile",
        str(profile_path),
        "--capture-root",
        str(capture_root),
        "--session-id",
        job["session_id"],
        "--duration-s",
        str(job["duration_s"]),
        "--run-id",
        job["run_id"],
        "--task-definition-id",
        task["id"],
        "--task-definition-version",
        str(task["version"]),
    ]
    if clock_sync_host is not None:
        command.extend(["--clock-sync-host", clock_sync_host])
    return command


class RecordingWorker:
    def __init__(
        self,
        *,
        central_url: str,
        context: ssl.SSLContext,
        agent_id: str,
        poll: CaptureWorkerPoll,
        profile_path: Path,
        capture_root: Path,
        request_timeout_s: float,
        reserve_mib: float,
        estimated_mib_per_s: float,
        clock_sync_host: str | None = None,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    ) -> None:
        self.central_url = central_url
        self.context = context
        self.agent_id = agent_id
        self.poll_body = poll
        self.profile_path = profile_path
        self.capture_root = capture_root
        self.request_timeout_s = request_timeout_s
        self.reserve_mib = reserve_mib
        self.estimated_mib_per_s = estimated_mib_per_s
        self.clock_sync_host = clock_sync_host
        self._popen = popen
        self._disk_usage = disk_usage
        self.process: subprocess.Popen | None = None
        self.active_job: dict[str, Any] | None = None
        self.stop_sent = False

    def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return request_control_json(
            f"{self.central_url}{path}",
            context=self.context,
            payload=payload,
            timeout_s=self.request_timeout_s,
        )

    def poll(self) -> dict[str, Any]:
        return self._request(
            "/api/v1/capture-workers/poll",
            self.poll_body.model_dump(mode="json"),
        )

    def report(
        self,
        job: dict[str, Any],
        state: str,
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "worker_id": self.poll_body.worker_id,
            "lease_id": job["lease_id"],
            "state": state,
        }
        if error is not None:
            payload["error"] = error[:2048]
        return self._request(
            f"/api/v1/capture-jobs/{quote(job['job_id'], safe='')}/status",
            payload,
        )

    def _paths(self, job: dict[str, Any]) -> tuple[Path, Path]:
        session_id = job["session_id"]
        return (
            self.capture_root / f"{session_id}.partial",
            self.capture_root / session_id,
        )

    def _validate_final(self, job: dict[str, Any], final: Path) -> None:
        manifest = read_manifest(final)
        expected = (
            job["session_id"],
            self.poll_body.profile_id,
            self.poll_body.robot_id,
            job["run_id"],
            job["task_definition"]["id"],
            job["task_definition"]["version"],
        )
        task = manifest.task_definition
        actual = (
            manifest.session_id,
            manifest.profile_id,
            manifest.robot_id,
            manifest.run_id,
            None if task is None else task.id,
            None if task is None else task.version,
        )
        if actual != expected or manifest.agent_id != self.agent_id:
            raise RecordingWorkerError(
                "finalized session identity differs from its Capture Job"
            )

    def _ensure_capacity(self, duration_s: int) -> None:
        free_bytes = self._disk_usage(self.capture_root).free
        required_mib = self.reserve_mib + self.estimated_mib_per_s * duration_s
        if free_bytes < required_mib * 1024 * 1024:
            raise RecordingWorkerError(
                "insufficient Edge storage for the requested bounded capture"
            )

    def _start(self, job: dict[str, Any]) -> None:
        self._ensure_capacity(job["duration_s"])
        command = recorder_command(
            profile_path=self.profile_path,
            capture_root=self.capture_root,
            job=job,
            clock_sync_host=self.clock_sync_host,
        )
        self.process = self._popen(command)
        self.active_job = job
        self.stop_sent = False
        self.report(job, "RECORDING")
        emit(
            {
                "event": "capture_started",
                "job_id": job["job_id"],
                "session_id": job["session_id"],
                "run_id": job["run_id"],
                "duration_s": job["duration_s"],
            }
        )

    def _failed(self, job: dict[str, Any], message: str) -> None:
        self.report(job, "FAILED", error=message)
        emit({"event": "capture_failed", "job_id": job["job_id"], "error": message})

    def _reconcile(self, job: dict[str, Any]) -> None:
        validate_job(job, poll=self.poll_body, agent_id=self.agent_id)
        partial, final = self._paths(job)
        if final.exists():
            try:
                self._validate_final(job, final)
            except (OSError, ValueError, RecordingWorkerError) as exc:
                self._failed(job, f"invalid finalized capture retained: {exc}")
                return
            self.report(job, "UPLOADING")
            return
        if partial.exists():
            self._failed(
                job,
                "recording worker restarted; partial session was preserved for inspection",
            )
            return
        if job["state"] == "UPLOADING":
            return
        if job.get("stop_requested_at") is not None:
            self._failed(job, "capture was stopped before the recorder started")
            return
        if job["state"] != "CLAIMED":
            self._failed(job, "recording process is absent and no session evidence exists")
            return
        try:
            self._start(job)
        except OSError as exc:
            self.process = None
            self.active_job = None
            self._failed(job, str(exc))
        except RecordingWorkerError as exc:
            if self.process is not None:
                raise
            self.active_job = None
            self._failed(job, str(exc))

    def _monitor(self, polled_job: dict[str, Any] | None) -> None:
        job = self.active_job
        process = self.process
        if job is None or process is None:
            raise RecordingWorkerError("recording worker process state is inconsistent")
        if polled_job is not None:
            validate_job(
                polled_job,
                poll=self.poll_body,
                agent_id=self.agent_id,
            )
            if polled_job["job_id"] != job["job_id"]:
                raise RecordingWorkerError("Central changed jobs while a capture is active")
            job = polled_job
            self.active_job = job
        if job["state"] == "CLAIMED":
            self.report(job, "RECORDING")
        if job.get("stop_requested_at") is not None and not self.stop_sent:
            process.send_signal(signal.SIGINT)
            self.stop_sent = True
            self.report(job, "FINALIZING")
        return_code = process.poll()
        if return_code is None:
            return
        partial, final = self._paths(job)
        self.process = None
        self.active_job = None
        self.stop_sent = False
        if final.exists():
            try:
                self._validate_final(job, final)
            except (OSError, ValueError, RecordingWorkerError) as exc:
                self._failed(job, f"invalid finalized capture retained: {exc}")
                return
            self.report(job, "UPLOADING")
            emit(
                {
                    "event": "capture_finalized",
                    "job_id": job["job_id"],
                    "session_id": job["session_id"],
                }
            )
            return
        retained = "partial session retained" if partial.exists() else "no session evidence"
        self._failed(job, f"recorder exited with code {return_code}; {retained}")

    def tick(self) -> None:
        response = self.poll()
        job = response.get("job")
        if job is not None and not isinstance(job, dict):
            raise RecordingWorkerError("Central returned an invalid capture job")
        if self.process is not None:
            self._monitor(job)
        elif job is not None:
            self._reconcile(job)

    def shutdown(self, timeout_s: float = 30.0) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Execute Central Capture Jobs using one fixed read-only profile"
    )
    parser.add_argument("--central-url", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--capture-root", type=Path, required=True)
    parser.add_argument("--ca-bundle", type=Path, required=True)
    parser.add_argument("--client-certificate", type=Path, required=True)
    parser.add_argument("--client-private-key", type=Path, required=True)
    parser.add_argument("--poll-interval-s", type=float, default=3.0)
    parser.add_argument("--request-timeout-s", type=float, default=10.0)
    parser.add_argument("--reserve-mib", type=float, default=5120.0)
    parser.add_argument("--estimated-mib-per-s", type=float, default=12.0)
    parser.add_argument("--clock-sync-host")
    options = parser.parse_args(args)
    try:
        options.central_url = validate_central_url(options.central_url)
    except ValueError as exc:
        parser.error(str(exc))
    if urlsplit(options.central_url).scheme != "https":
        parser.error("remote capture control requires HTTPS")
    for label, value in (
        ("poll-interval-s", options.poll_interval_s),
        ("request-timeout-s", options.request_timeout_s),
        ("estimated-mib-per-s", options.estimated_mib_per_s),
    ):
        if not math.isfinite(value) or value <= 0.0:
            parser.error(f"{label} must be positive")
    if not math.isfinite(options.reserve_mib) or options.reserve_mib < 0.0:
        parser.error("reserve-mib must be non-negative")
    return options


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    try:
        profile = load_profile(options.profile)
        if profile.agent_id != options.agent_id:
            raise ValueError("fixed profile agent_id differs from --agent-id")
        options.capture_root.mkdir(parents=True, exist_ok=True)
        if options.capture_root.is_symlink() or not options.capture_root.is_dir():
            raise ValueError("capture-root must be a regular non-symlink directory")
        context = create_client_context(
            ca_bundle=options.ca_bundle,
            client_certificate=options.client_certificate,
            client_private_key=options.client_private_key,
        )
        poll = CaptureWorkerPoll(
            worker_id=options.worker_id,
            profile_id=profile.profile_id,
            robot_id=profile.robot_id,
            asset_ids=profile_asset_ids(profile),
        )
        worker = RecordingWorker(
            central_url=options.central_url,
            context=context,
            agent_id=options.agent_id,
            poll=poll,
            profile_path=options.profile.resolve(),
            capture_root=options.capture_root.resolve(),
            request_timeout_s=options.request_timeout_s,
            reserve_mib=options.reserve_mib,
            estimated_mib_per_s=options.estimated_mib_per_s,
            clock_sync_host=options.clock_sync_host,
        )
    except (OSError, ssl.SSLError, ProfileError, ValueError) as exc:
        raise SystemExit(f"invalid recording worker configuration: {exc}") from exc

    stop = threading.Event()

    def request_stop(signum, frame) -> None:
        del signum, frame
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        while not stop.is_set():
            try:
                worker.tick()
            except RecordingWorkerError as exc:
                emit({"event": "capture_worker_poll_failed", "error": str(exc)})
            if stop.wait(options.poll_interval_s):
                break
    finally:
        worker.shutdown()


if __name__ == "__main__":
    main(sys.argv[1:])
