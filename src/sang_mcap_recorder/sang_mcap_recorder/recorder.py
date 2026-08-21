"""Lifecycle wrapper around the ROS 2 MCAP recorder."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Callable

from .profile import CaptureProfile
from .session import (
    CaptureContext,
    SessionError,
    SessionPaths,
    finalize_session,
    prepare_paths,
    write_failure_manifest,
)


LOST_MESSAGES_PATTERN = re.compile(
    r"Number of messages lost(?: on the transport layer)?:\s*(\d+)"
)


def reported_lost_messages(line: str) -> int:
    match = LOST_MESSAGES_PATTERN.search(line)
    return int(match.group(1)) if match else 0


def validate_recorder_diagnostics(diagnostics: dict) -> None:
    if diagnostics.get("output_monitor_error"):
        raise SessionError(
            f"recorder output monitor failed: {diagnostics['output_monitor_error']}"
        )
    if diagnostics["transport_messages_lost"] > 0:
        raise SessionError(
            "ros2 bag record reported "
            f"{diagnostics['transport_messages_lost']} lost transport messages"
        )


def _monitor_output(stream, log_path: Path, diagnostics: dict) -> None:
    try:
        with log_path.open("x", encoding="utf-8") as log:
            for line in iter(stream.readline, ""):
                log.write(line)
                log.flush()
                sys.stdout.write(line)
                sys.stdout.flush()
                diagnostics["transport_messages_lost"] += reported_lost_messages(line)
    except Exception as exc:
        diagnostics["output_monitor_error"] = str(exc)


def _preserve_recorder_log(
    *,
    monitor_thread: threading.Thread | None,
    temporary_log: Path,
    paths: SessionPaths,
) -> None:
    if monitor_thread is not None:
        monitor_thread.join(timeout=5.0)
        if monitor_thread.is_alive():
            raise SessionError("recorder output monitor did not stop")
    if temporary_log.exists():
        paths.staging.mkdir(parents=True, exist_ok=True)
        os.replace(temporary_log, paths.staging / "recorder.log")


def build_rosbag_command(
    *, profile: CaptureProfile, context: CaptureContext, paths: SessionPaths
) -> list[str]:
    custom_data = [
        f"session_id={context.session_id}",
        f"profile_id={profile.profile_id}",
        f"agent_id={profile.agent_id}",
        f"robot_id={profile.robot_id}",
    ]
    if context.run_id is not None:
        custom_data.append(f"run_id={context.run_id}")
    command = [
        "ros2",
        "bag",
        "record",
        "--storage",
        "mcap",
        "--storage-preset-profile",
        "none",
        "--output",
        str(paths.staging),
        "--disable-keyboard-controls",
        "--node-name",
        "sang_mcap_recorder",
    ]
    if profile.qos_overrides_path is not None:
        command.extend(
            ["--qos-profile-overrides-path", str(profile.qos_overrides_path)]
        )
    command.extend(
        [
            "--custom-data",
            *custom_data,
            "--topics",
            *(topic.name for topic in profile.topics),
        ]
    )
    return command


def _stop_process(process: subprocess.Popen, timeout_s: float) -> int:
    if process.poll() is not None:
        return int(process.returncode)
    try:
        os.killpg(process.pid, signal.SIGINT)
        return int(process.wait(timeout=timeout_s))
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            return int(process.wait(timeout=5.0))
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            try:
                return int(process.wait(timeout=5.0))
            except subprocess.TimeoutExpired as exc:
                raise SessionError(
                    "ros2 bag record did not terminate after SIGKILL"
                ) from exc


def run_capture(
    *,
    profile: CaptureProfile,
    context: CaptureContext,
    capture_root: str | Path,
    duration_s: float,
    shutdown_timeout_s: float = 20.0,
    stop_event: threading.Event | None = None,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> tuple[dict, Path]:
    if duration_s < 0:
        raise SessionError("duration_s must be zero or greater")
    if shutdown_timeout_s <= 0:
        raise SessionError("shutdown_timeout_s must be greater than zero")
    paths = prepare_paths(capture_root, context)
    temporary_log = paths.root / f".{context.session_id}.recorder.log.partial"
    if temporary_log.exists():
        raise SessionError(f"temporary recorder log already exists: {temporary_log}")
    event = stop_event or threading.Event()
    started_at = datetime.now(timezone.utc)
    process: subprocess.Popen | None = None
    monitor_thread: threading.Thread | None = None
    diagnostics = {"transport_messages_lost": 0}
    try:
        process = popen(
            build_rosbag_command(profile=profile, context=context, paths=paths),
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if process.stdout is None:
            raise SessionError("ros2 bag record output stream is unavailable")
        monitor_thread = threading.Thread(
            target=_monitor_output,
            args=(process.stdout, temporary_log, diagnostics),
            name="sang-mcap-recorder-output",
            daemon=True,
        )
        monitor_thread.start()
        deadline = time.monotonic() + duration_s if duration_s > 0 else None
        exited_early = False
        while not event.is_set():
            return_code = process.poll()
            if return_code is not None:
                exited_early = deadline is None or time.monotonic() < deadline
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            event.wait(0.1)

        return_code = _stop_process(process, shutdown_timeout_s)
        _preserve_recorder_log(
            monitor_thread=monitor_thread,
            temporary_log=temporary_log,
            paths=paths,
        )
        if return_code != 0:
            raise SessionError(f"ros2 bag record exited with status {return_code}")
        validate_recorder_diagnostics(diagnostics)
        if exited_early and not event.is_set():
            raise SessionError("ros2 bag record exited before the requested duration")
        return finalize_session(
            paths=paths,
            profile=profile,
            context=context,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            extra_artifact_names=("recorder.log",),
            recorder_diagnostics=diagnostics,
        )
    except Exception as exc:
        failure_reason = str(exc)
        if process is not None and process.poll() is None:
            try:
                _stop_process(process, shutdown_timeout_s)
            except Exception as stop_exc:
                diagnostics["stop_error"] = str(stop_exc)
        try:
            _preserve_recorder_log(
                monitor_thread=monitor_thread,
                temporary_log=temporary_log,
                paths=paths,
            )
        except Exception as log_exc:
            diagnostics["log_preservation_error"] = str(log_exc)
        try:
            write_failure_manifest(
                paths=paths,
                profile=profile,
                context=context,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                reason=failure_reason,
                recorder_diagnostics=diagnostics,
            )
        except Exception as manifest_exc:
            raise SessionError(
                f"capture failed ({exc}); failure manifest also failed ({manifest_exc})"
            ) from exc
        if isinstance(exc, SessionError):
            raise
        raise SessionError(str(exc)) from exc
