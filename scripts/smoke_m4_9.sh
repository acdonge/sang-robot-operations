#!/usr/bin/env bash
set -euo pipefail

# 다른 실행의 DDS 트래픽과 leader 명령을 섞지 않는다.
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-$(( (RANDOM % 100) + 1 ))}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SOURCE_MANIFEST="${SOURCE_MANIFEST:-${WS_ROOT}/manifests/bi_so101_01.yaml}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
ROBOT_ID="bi_so101_01"
LEADER_TOPIC="/bi_so101/leader_left/joint_trajectory_norm"
TRAIN_TIMEOUT_S="${TRAIN_TIMEOUT_S:-1800}"
LOG_DIR="$(mktemp -d)"
STORE_ROOT="${LOG_DIR}/store"
MANIFEST_PATH="${LOG_DIR}/manifest.yaml"

fake_pid=""
gateway_pid=""
echo_pid=""

stop_fake() {
    set +e
    if [[ -n "${fake_pid}" ]] && kill -0 "${fake_pid}" 2>/dev/null; then
        kill -TERM -- "-${fake_pid}" 2>/dev/null || kill -TERM "${fake_pid}"
        wait "${fake_pid}" 2>/dev/null
    fi
    fake_pid=""
    set -e
}

cleanup() {
    set +e
    if [[ -n "${echo_pid}" ]] && kill -0 "${echo_pid}" 2>/dev/null; then
        kill -TERM "${echo_pid}" 2>/dev/null
        wait "${echo_pid}" 2>/dev/null
    fi
    if [[ -n "${gateway_pid}" ]] && kill -0 "${gateway_pid}" 2>/dev/null; then
        kill -TERM -- "-${gateway_pid}" 2>/dev/null || kill -TERM "${gateway_pid}"
        wait "${gateway_pid}" 2>/dev/null
    fi
    stop_fake
    rm -rf "${LOG_DIR}"
    set -e
}

fail() {
    local message="$1"
    echo "FAIL: ${message}" >&2
    for log in "${LOG_DIR}"/*.log; do
        if [[ -f "${log}" ]]; then
            echo "--- ${log} ---" >&2
            tail -n 160 "${log}" >&2
        fi
    done
    exit 1
}

wait_for_api() {
    local attempt
    for ((attempt = 0; attempt < 120; attempt++)); do
        if curl -fsS "${BASE_URL}/api/robots" >"${LOG_DIR}/robots.json"; then
            return 0
        fi
        if [[ -n "${gateway_pid}" ]] && ! kill -0 "${gateway_pid}" 2>/dev/null; then
            return 1
        fi
        sleep 0.5
    done
    return 1
}

wait_for_recorder_state() {
    local expected="$1"
    local attempt
    for ((attempt = 0; attempt < 120; attempt++)); do
        if curl -fsS "${BASE_URL}/api/robots/${ROBOT_ID}/health" \
            >"${LOG_DIR}/health.json"; then
            if python3 - "${LOG_DIR}/health.json" "${expected}" <<'PY'
import json
from pathlib import Path
import sys

status = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
raise SystemExit(0 if status.get("fsm_state") == sys.argv[2] else 1)
PY
            then
                return 0
            fi
        fi
        sleep 0.5
    done
    return 1
}

start_fake() {
    local label="$1"
    shift
    setsid ros2 run recorder_core fake_robot --ros-args \
        -p "manifest_path:=${MANIFEST_PATH}" "$@" \
        >"${LOG_DIR}/${label}_fake.log" 2>&1 &
    fake_pid=$!
    sleep 1
}

trap cleanup EXIT
trap 'exit 130' INT TERM

command -v lerobot-train >/dev/null || fail "lerobot-train is not installed"
python3 - <<'PY' || fail "LeRobot training/inference dependencies are incomplete"
import accelerate
import torch
import wandb
from lerobot.configs import PreTrainedConfig
from lerobot.policies import get_policy_class, make_pre_post_processors
PY

mkdir -p "${STORE_ROOT}"
python3 - "${SOURCE_MANIFEST}" "${MANIFEST_PATH}" "${STORE_ROOT}" <<'PY'
from pathlib import Path
import sys
import yaml

source, output, store = map(Path, sys.argv[1:])
data = yaml.safe_load(source.read_text(encoding="utf-8"))
data["recording"]["staging_root"] = str(store / "staging")
data["recording"]["dataset_root"] = str(store / "datasets" / "raw")
output.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY

cd "${WS_ROOT}"
set +u
source /opt/ros/jazzy/setup.bash
set -u
colcon build --symlink-install
set +u
source "${WS_ROOT}/install/setup.bash"
set -u

if pgrep -f 'recorder_core/(fake_robot|recorder_gateway)' >/dev/null 2>&1; then
    fail "leftover recorder process before M4-⑨ case"
fi

start_fake "record"
setsid ros2 run recorder_core recorder_gateway \
    "${MANIFEST_PATH}" --ui-dir "${WS_ROOT}/ui" \
    >"${LOG_DIR}/gateway.log" 2>&1 &
gateway_pid=$!
wait_for_api || fail "gateway API did not become ready"

# 한 episode dataset을 같은 isolated store에 생성·검증·승격한다.
curl -fsS -H 'Content-Type: application/json' \
    -d '{"task":"smoke_m4_9"}' \
    "${BASE_URL}/api/robots/${ROBOT_ID}/record/start" \
    >"${LOG_DIR}/record_start.json"
wait_for_recorder_state "RECORDING" || fail "recorder did not enter RECORDING"
sleep 3
curl --max-time 180 -fsS -X POST \
    "${BASE_URL}/api/robots/${ROBOT_ID}/record/stop" \
    >"${LOG_DIR}/record_stop.json"

dataset_id="$(python3 - "${LOG_DIR}/record_stop.json" <<'PY'
import json
from pathlib import Path
import sys

body = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
path = body.get("dataset_path")
if not path:
    raise SystemExit(f"record stop omitted dataset_path: {body}")
print(Path(path).name)
PY
)" || fail "dataset creation failed"

# 공식 CLI로 작은 CPU ACT를 20 step 학습하고 DONE까지 기다린다.
export BASE_URL DATASET_ID="${dataset_id}" TRAIN_TIMEOUT_S LOG_DIR
timeout "${TRAIN_TIMEOUT_S}" python3 - <<'PY' || fail "20-step training failed"
import json
import os
from pathlib import Path
import time
from urllib.request import Request, urlopen

base = os.environ["BASE_URL"]
payload = json.dumps(
    {"dataset_id": os.environ["DATASET_ID"], "policy_type": "act", "steps": 20}
).encode("utf-8")
request = Request(
    f"{base}/api/train",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urlopen(request, timeout=30) as response:
    run_id = json.loads(response.read())["run_id"]
Path(os.environ["LOG_DIR"], "run_id").write_text(run_id, encoding="utf-8")

deadline = time.monotonic() + float(os.environ["TRAIN_TIMEOUT_S"])
while time.monotonic() < deadline:
    with urlopen(f"{base}/api/runs", timeout=30) as response:
        runs = json.loads(response.read())
    run = next(item for item in runs if item.get("run_id") == run_id)
    if run.get("state") == "DONE":
        break
    if run.get("state") == "ERROR":
        raise RuntimeError(f"training entered ERROR: {run}")
    time.sleep(1)
else:
    raise TimeoutError("training did not reach DONE")

with urlopen(f"{base}/api/policies", timeout=30) as response:
    policies = json.loads(response.read())
policy = next((item for item in policies if item.get("run_id") == run_id), None)
if policy is None or not Path(policy["checkpoint_path"]).is_dir():
    raise RuntimeError(f"DONE run is absent from policies: {policies}")
PY

run_id="$(<"${LOG_DIR}/run_id")"

# Synthetic action publish를 제거하고 observation만 유지한다. 이후 leader echo는
# inference executor가 보낸 명령만 관측한다.
stop_fake
start_fake "inference" -p "drop_source:=action" -p "drop_after_s:=0.0"
sleep 2

timeout 120 ros2 topic echo "${LEADER_TOPIC}" \
    trajectory_msgs/msg/JointTrajectory --once \
    >"${LOG_DIR}/leader_echo.log" 2>&1 &
echo_pid=$!

curl -fsS -H 'Content-Type: application/json' \
    -d "{\"run_id\":\"${run_id}\",\"robot_id\":\"${ROBOT_ID}\"}" \
    "${BASE_URL}/api/inference/start" >"${LOG_DIR}/inference_start.json"

python3 - "${LOG_DIR}/inference_start.json" <<'PY' || fail "unsafe start response"
import json
from pathlib import Path
import sys

body = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if body.get("state") != "RUNNING":
    raise SystemExit(f"inference did not start: {body}")
warning = body.get("warning", "")
if "fake_robot/sim only" not in warning or "M7" not in warning or "deadman" not in warning:
    raise SystemExit(f"safety warning is incomplete: {body}")
PY

if ! wait "${echo_pid}"; then
    echo_pid=""
    fail "leader topic did not receive an inference command"
fi
echo_pid=""
grep -Fq "joint_names:" "${LOG_DIR}/leader_echo.log" || \
    fail "leader echo did not contain JointTrajectory fields"
grep -Fq "positions:" "${LOG_DIR}/leader_echo.log" || \
    fail "leader echo did not contain action positions"

curl -fsS -X POST "${BASE_URL}/api/inference/stop" \
    >"${LOG_DIR}/inference_stop.json"
python3 - "${LOG_DIR}/inference_stop.json" <<'PY' || fail "inference stop failed"
import json
from pathlib import Path
import sys

body = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if body.get("state") != "STOPPED":
    raise SystemExit(f"inference did not stop: {body}")
PY

if timeout 2 ros2 topic echo "${LEADER_TOPIC}" \
    trajectory_msgs/msg/JointTrajectory --once \
    >"${LOG_DIR}/after_stop_echo.log" 2>&1; then
    fail "leader command publication continued after stop"
fi

echo "PASS: M4-⑨ dataset, 20-step ACT, closed-loop leader command, and stop verified"
