"""Pinned, bounded RB5 pilot contract shared by runtime and ROS receiver."""

from pathlib import Path
import json
import os
from uuid import uuid4

SCENARIO_ID = "isaac-rb5-twin-pilot"
PROFILE_ID = "rb5"
ASSET_ID = "isaac-twin-rb5"
AGENT_ID = "isaac-twin-rb5-agent"
SCENE_SHA256 = "5837e8eb41c67024a4d461f07e3d63dc53bde338209fd64c6c238ceb187972ce"
RUNTIME_KIND = "isaac-sim-executor"
JOINTS = ("base", "shoulder", "elbow", "wrist1", "wrist2", "wrist3")
DOMAIN_ID = "98"
ROOT = Path("/tmp/sang-isaac-rb5-twin")
STATE_TOPIC = "/sang/twin/rb5/state"
JOINT_TOPIC = "/sang/twin/rb5/joint_states"
CAMERA_TOPIC = "/sang/twin/rb5/camera/front/compressed"
CAPABILITY = "simulate-bounded-joint@1"
OPERATIONS = {"move_offset": "move-offset", "return_ready": "return-ready"}
AUTHORITY_TTL_S = 2.0
MAX_MOTION_S = 15.0
MOTION_DURATION_S = 3.0
MAX_ERROR_RAD = 0.02
DELTA_RAD = 0.1


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(data, allow_nan=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def require_isolation() -> None:
    required = {
        "ROS_DISTRO": "jazzy", "ROS_DOMAIN_ID": DOMAIN_ID,
        "ROS_AUTOMATIC_DISCOVERY_RANGE": "LOCALHOST",
        "FASTDDS_BUILTIN_TRANSPORTS": "UDPv4",
        "RMW_FASTRTPS_PUBLICATION_MODE": "ASYNCHRONOUS",
    }
    if any(os.environ.get(key) != value for key, value in required.items()):
        raise RuntimeError("Isaac twin requires Jazzy, dedicated domain 98 and LOCALHOST UDPv4")


def identity(session_id: str) -> dict:
    return {
        "schema_version": 1, "runtime_kind": RUNTIME_KIND, "profile_id": PROFILE_ID,
        "asset_id": ASSET_ID, "scene_sha256": SCENE_SHA256,
        "session_id": session_id, "physical_output_connected": False,
    }
