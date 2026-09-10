"""Fixed local Scenario 1 identity and measured observation contract."""

from pathlib import Path
import hashlib
import json

from isaac_twin.contract import atomic_json, require_isolation

ROOT = Path("/tmp/sang-isaac-scenario1")
HERE = Path(__file__).resolve().parent
SCENARIO_ID = "isaac-scenario1"
PROFILE_ID = "scenario1"
STATE_TOPIC = "/sang/scenario1/state"
CAMERA_TOPIC = "/sang/scenario1/camera/front/compressed"
JOINTS = {
    "omy": ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "rh_r1_joint"),
    "rb5": ("base", "shoulder", "elbow", "wrist1", "wrist2", "wrist3"),
    "carrier": (), "plate": (),
}
STEPS = ("mobile_delivery", "omy_pre_pick_place", "rb5_waam", "omy_post_pick_place", "mobile_return")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_manifest():
    path = HERE / "manifest.json"
    data = json.loads(path.read_text())
    for row in data["sources"]:
        source = (HERE / row["path"]).resolve()
        if sha256(source) != row["sha256"]:
            raise ValueError(f"Scenario 1 source changed: {source.name}")
    return data, sha256(path)


def unity_position_to_ros(value):
    x, y, z = value
    return [z, -x, y]


def unity_rotation_to_ros(value):
    x, y, z, w = value
    return [-z, x, -y, w]
