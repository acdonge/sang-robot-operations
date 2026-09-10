#!/usr/bin/env python3
"""Launch only the separate observer, pinned to fresh ROS-receiver metadata."""

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time

ROOT = Path("/tmp/sang-isaac-rb5-twin/observer")
PLAYER = Path(__file__).resolve().parent / "Builds/Linux/IsaacRb5Observer.x86_64"
JOINTS = {"base", "shoulder", "elbow", "wrist1", "wrist2", "wrist3"}
IDENTITY = {
    "schema_version": 1,
    "runtime_kind": "isaac-sim-executor",
    "profile_id": "rb5",
    "asset_id": "isaac-twin-rb5",
    "scene_sha256": "5837e8eb41c67024a4d461f07e3d63dc53bde338209fd64c6c238ceb187972ce",
    "physical_output_connected": False,
}
FIELDS = set(IDENTITY) | {
    "session_id", "epoch", "sequence", "sim_time_ns", "received_at_unix_ms",
    "joint_names", "joint_positions_rad",
}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate telemetry field")
        result[key] = value
    return result


def validate_snapshot(snapshot, now_ms):
    if not isinstance(snapshot, dict) or set(snapshot) != FIELDS:
        raise ValueError("Unexpected telemetry schema")
    if any(type(snapshot[key]) is not type(value) or snapshot[key] != value for key, value in IDENTITY.items()):
        raise ValueError("Telemetry is not the pinned disconnected Isaac RB5 source")
    if not isinstance(snapshot["session_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", snapshot["session_id"]):
        raise ValueError("Invalid telemetry session")
    for key in ("epoch", "sequence", "sim_time_ns", "received_at_unix_ms"):
        if type(snapshot[key]) is not int or not 0 <= snapshot[key] <= 2**63 - 1:
            raise ValueError("Invalid telemetry counter or timestamp")
    if not -100 <= now_ms - snapshot["received_at_unix_ms"] <= 500 or snapshot["received_at_unix_ms"] == 0:
        raise ValueError("Telemetry must be fresh (500 ms maximum age)")
    names, values = snapshot["joint_names"], snapshot["joint_positions_rad"]
    if not isinstance(names, list) or len(names) != 6 or not all(isinstance(name, str) for name in names) or set(names) != JOINTS:
        raise ValueError("Exactly the six RB5 joint names are required")
    if not isinstance(values, list) or len(values) != 6 or any(
        type(value) not in (int, float) or abs(value) > 3.14 or not math.isfinite(value) for value in values
    ):
        raise ValueError("Invalid measured joint values")
    return snapshot["session_id"], snapshot["epoch"]


def read_pin():
    private_root = ROOT.parent.lstat()
    if not stat.S_ISDIR(private_root.st_mode) or private_root.st_uid != os.getuid() or private_root.st_mode & 0o077:
        raise ValueError("Receiver root must be a private directory owned by this user")
    info = ROOT.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o002:
        raise ValueError("Observer directory must be user-owned and not world-writable")
    descriptor = os.open(ROOT / "telemetry.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        # Group bits are harmless below the validated owner-only receiver root.
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o002:
            raise ValueError("Telemetry must be a regular, user-owned, non-world-writable file")
        data = stream.read(8193)
    if not 0 < len(data) <= 8192:
        raise ValueError("Invalid telemetry file size")
    snapshot = json.loads(data, object_pairs_hook=unique_object)
    return validate_snapshot(snapshot, time.time_ns() // 1_000_000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=int, default=240, help="Bounded observation duration, 1..3600 (default: 240)")
    parser.add_argument("--check-only", action="store_true", help="Validate current telemetry without launching a Player")
    args = parser.parse_args()
    if not 1 <= args.duration_seconds <= 3600:
        parser.error("duration must be 1..3600 seconds")
    session, epoch = read_pin()
    print(json.dumps({"session_id": session, "epoch": epoch, "auto_rebind": False, "build_role": "observer"}), flush=True)
    if args.check_only:
        return 0
    if not PLAYER.is_file():
        raise ValueError("Observer Player is not built; run the documented Editor build first")
    # The lock belongs only to this observer, never to Isaac, ROS, or a legacy Player.
    descriptor = os.open(ROOT / "observer-player.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        environment = os.environ.copy()
        environment.update(SANG_TWIN_SESSION_ID=session, SANG_TWIN_EPOCH=str(epoch))
        command = [str(PLAYER), "-screen-fullscreen", "0", "-screen-width", "960", "-screen-height", "720",
                   "-logFile", str(ROOT / "observer-player.log")]
        child = subprocess.Popen(command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        try:
            return child.wait(timeout=args.duration_seconds)
        except subprocess.TimeoutExpired:
            print("Bounded observer duration completed; stopping only this observer.", flush=True)
            return 0
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        sys.exit("Observer launch refused: " + str(error))
    except KeyboardInterrupt:
        sys.exit(130)
