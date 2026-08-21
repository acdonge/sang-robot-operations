#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CAPTURE_ROOT="${1:-$(mktemp -d)/captures}"
SESSION_ID="fake-rb5-$(date -u +%Y%m%dT%H%M%SZ)-${RANDOM}"
ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-199}"
export ROS_DOMAIN_ID

set +u
source /opt/ros/jazzy/setup.bash
set -u
export PYTHONPATH="${WS_ROOT}/src/sang_mcap_recorder:${PYTHONPATH:-}"

stamp="$(date +%s)"
publisher_pids=()
cleanup() {
    for pid in "${publisher_pids[@]}"
    do
        kill "${pid}" 2>/dev/null || true
        wait "${pid}" 2>/dev/null || true
    done
}
trap cleanup EXIT INT TERM

ros2 topic pub -r 30 /rbpodo/joint_states sensor_msgs/msg/JointState \
    "{header: {stamp: {sec: ${stamp}, nanosec: 1}}, name: [joint_1], position: [0.1]}" \
    >/tmp/sang-mcap-joint-publisher.log 2>&1 &
publisher_pids+=("$!")
ros2 topic pub -r 15 \
    /rb5_lab1_eval_01/camera/welding_view/image_raw/compressed \
    sensor_msgs/msg/CompressedImage \
    "{header: {stamp: {sec: ${stamp}, nanosec: 2}}, format: jpeg, data: [255, 216, 255, 217]}" \
    >/tmp/sang-mcap-otos-publisher.log 2>&1 &
publisher_pids+=("$!")
ros2 topic pub -r 15 \
    /rb5_lab1_eval_01/camera/d455_color/image_raw/compressed \
    sensor_msgs/msg/CompressedImage \
    "{header: {stamp: {sec: ${stamp}, nanosec: 3}}, format: jpeg, data: [255, 216, 255, 217]}" \
    >/tmp/sang-mcap-d455-publisher.log 2>&1 &
publisher_pids+=("$!")

sleep 1
python3 -m sang_mcap_recorder.cli \
    --profile "${WS_ROOT}/config/rb5_lab1_capture.yaml" \
    --capture-root "${CAPTURE_ROOT}" \
    --session-id "${SESSION_ID}" \
    --duration-s 3

ros2 bag info "${CAPTURE_ROOT}/${SESSION_ID}"
python3 - "${CAPTURE_ROOT}/${SESSION_ID}" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

session = Path(sys.argv[1])
manifest = json.loads((session / "session-manifest.json").read_text())
assert manifest["state"] == "complete"
assert len(manifest["topics"]) == 3
assert all(topic["message_count"] > 0 for topic in manifest["topics"])
for entry in manifest["files"]:
    digest = hashlib.sha256((session / entry["path"]).read_bytes()).hexdigest()
    assert digest == entry["sha256"]
print(json.dumps({"verified": True, "manifest": str(session / "session-manifest.json")}))
PY
