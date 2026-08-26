"""Command-line entry point for one local MCAP capture session."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import math
import signal
import sys
import threading
from uuid import uuid4

from .profile import ProfileError, load_profile
from .recorder import run_capture
from .session import CaptureContext, SessionError
from robot_agent.clock_sync import ClockSyncSampler, recording_clock


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record one allowlisted read-only ROS 2 session to MCAP."
    )
    parser.add_argument("--profile", required=True)
    parser.add_argument("--capture-root", default="/var/lib/sang/captures")
    parser.add_argument("--session-id")
    parser.add_argument("--duration-s", type=float, default=600.0)
    parser.add_argument("--shutdown-timeout-s", type=float, default=20.0)
    parser.add_argument("--run-id")
    parser.add_argument("--task-definition-id")
    parser.add_argument("--task-definition-version", type=int)
    parser.add_argument("--clock-sync-host")
    parser.add_argument("--clock-sync-timeout-s", type=float, default=1.0)
    options = parser.parse_args(args)
    if (
        not math.isfinite(options.clock_sync_timeout_s)
        or options.clock_sync_timeout_s <= 0.0
    ):
        parser.error("clock-sync-timeout-s must be positive")
    if options.clock_sync_host is not None and (
        not options.clock_sync_host
        or len(options.clock_sync_host) > 253
        or "/" in options.clock_sync_host
        or "://" in options.clock_sync_host
    ):
        parser.error("clock-sync-host must be a host name or IP address")
    return options


def default_session_id(profile_id: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{profile_id[:72]}-{timestamp}-{uuid4().hex[:8]}"


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    stop_event = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stop_event.set()

    previous_sigint = signal.signal(signal.SIGINT, request_stop)
    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    try:
        profile = load_profile(options.profile)
        if options.clock_sync_host is not None:
            snapshot = ClockSyncSampler(
                options.clock_sync_host,
                timeout_s=options.clock_sync_timeout_s,
            ).sample(force=True)
            profile = replace(profile, clock=recording_clock(snapshot))
        context = CaptureContext(
            session_id=options.session_id or default_session_id(profile.profile_id),
            run_id=options.run_id,
            task_definition_id=options.task_definition_id,
            task_definition_version=options.task_definition_version,
        )
        manifest, manifest_path = run_capture(
            profile=profile,
            context=context,
            capture_root=options.capture_root,
            duration_s=options.duration_s,
            shutdown_timeout_s=options.shutdown_timeout_s,
            stop_event=stop_event,
        )
    except (ProfileError, SessionError) as exc:
        print(json.dumps({"state": "failed", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)

    print(
        json.dumps(
            {
                "state": manifest["state"],
                "session_id": manifest["session_id"],
                "manifest": str(manifest_path),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
