#!/usr/bin/env bash
set -euo pipefail

# 다른 컨테이너/호스트의 DDS가 도커 브리지 멀티캐스트로 새어들지 않게 도메인 격리
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-$(( (RANDOM % 100) + 1 ))}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SOURCE_MANIFEST="${SOURCE_MANIFEST:-${WS_ROOT}/manifests/bi_so101_01.yaml}"
BASE_URL="http://127.0.0.1:8080"
ROBOT_ID="bi_so101_01"
LOG_DIR="$(mktemp -d)"
STORE_ROOT="${LOG_DIR}/store"
MANIFEST_PATH="${LOG_DIR}/manifest.yaml"

fake_pid=""
gateway_pid=""

stop_nodes() {
    set +e
    if [[ -n "${gateway_pid}" ]] && kill -0 "${gateway_pid}" 2>/dev/null; then
        kill -TERM -- "-${gateway_pid}" 2>/dev/null || kill -TERM "${gateway_pid}"
    fi
    if [[ -n "${fake_pid}" ]] && kill -0 "${fake_pid}" 2>/dev/null; then
        kill -TERM -- "-${fake_pid}" 2>/dev/null || kill -TERM "${fake_pid}"
    fi
    if [[ -n "${gateway_pid}" ]]; then
        wait "${gateway_pid}" 2>/dev/null
    fi
    if [[ -n "${fake_pid}" ]]; then
        wait "${fake_pid}" 2>/dev/null
    fi
    sleep 1
    pkill -KILL -f 'recorder_core/(fake_robot|recorder_gateway)' 2>/dev/null
    gateway_pid=""
    fake_pid=""
    set -e
}

cleanup() {
    stop_nodes
    rm -rf "${LOG_DIR}"
}

fail() {
    local message="$1"
    echo "FAIL: ${message}" >&2
    for log in "${LOG_DIR}"/*.log; do
        if [[ -f "${log}" ]]; then
            echo "--- ${log} ---" >&2
            tail -n 120 "${log}" >&2
        fi
    done
    exit 1
}

wait_for_api() {
    local attempt
    for ((attempt = 0; attempt < 60; attempt++)); do
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

wait_for_state() {
    local expected="$1"
    local timeout_s="$2"
    local attempts=$((timeout_s * 2))
    local attempt
    for ((attempt = 0; attempt < attempts; attempt++)); do
        if curl -fsS "${BASE_URL}/api/robots/${ROBOT_ID}/health" \
            >"${LOG_DIR}/health.json"; then
            if python3 - "${LOG_DIR}/health.json" "${expected}" <<'PY'
import json
from pathlib import Path
import sys

health = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
raise SystemExit(0 if health.get("fsm_state") == sys.argv[2] else 1)
PY
            then
                return 0
            fi
        fi
        sleep 0.5
    done
    return 1
}

wait_for_episode_index() {
    local expected="$1"
    local timeout_s="$2"
    local attempts=$((timeout_s * 2))
    local attempt
    for ((attempt = 0; attempt < attempts; attempt++)); do
        if curl -fsS "${BASE_URL}/api/robots/${ROBOT_ID}/health" \
            >"${LOG_DIR}/health.json"; then
            if python3 - "${LOG_DIR}/health.json" "${expected}" <<'PY'
import json
from pathlib import Path
import sys

status = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
ready = status.get("fsm_state") == "RESETTING" and status.get("episode_index") == int(sys.argv[2])
raise SystemExit(0 if ready else 1)
PY
            then
                return 0
            fi
        fi
        sleep 0.5
    done
    return 1
}

post() {
    local endpoint="$1"
    local output="$2"
    curl -fsS -X POST "${BASE_URL}/api/robots/${ROBOT_ID}/record/${endpoint}" \
        >"${output}"
}

trap cleanup EXIT
trap 'exit 130' INT TERM

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
    fail "leftover recorder process before M4-⑥ case"
fi

setsid ros2 run recorder_core fake_robot --ros-args \
    -p "manifest_path:=${MANIFEST_PATH}" >"${LOG_DIR}/fake.log" 2>&1 &
fake_pid=$!
sleep 1

setsid ros2 run recorder_core recorder_gateway \
    "${MANIFEST_PATH}" --ui-dir "${WS_ROOT}/ui" >"${LOG_DIR}/gateway.log" 2>&1 &
gateway_pid=$!

wait_for_api || fail "gateway API did not become ready"

curl -fsS -H 'Content-Type: application/json' \
    -d '{"task":"smoke_m4_6","task_instructions":["pick the red box","pick the blue box"],"episode_time_s":0,"reset_time_s":30,"num_episodes":2,"rosbag":false}' \
    "${BASE_URL}/api/robots/${ROBOT_ID}/record/start" >"${LOG_DIR}/start.json"
session_id="$(python3 - "${LOG_DIR}/start.json" <<'PY'
import json
from pathlib import Path
import sys

body = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not body.get("session_id"):
    raise SystemExit(f"start omitted session_id: {body}")
print(body["session_id"])
PY
)"

wait_for_state "RECORDING" 15 || fail "initial episode did not enter RECORDING"
sleep 2
post "retry" "${LOG_DIR}/retry.json"
wait_for_state "RESETTING" 10 || fail "retry did not enter RESETTING"
post "continue" "${LOG_DIR}/retry_continue.json"
wait_for_state "RECORDING" 15 || fail "retry did not return to RECORDING"

sleep 2
post "save_next" "${LOG_DIR}/save_next.json"
wait_for_episode_index "1" 60 || fail "save_next did not persist episode 0"
python3 - "${LOG_DIR}/health.json" <<'PY'
import json
from pathlib import Path
import sys

status = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if status.get("episode_index") != 1:
    raise SystemExit(f"episode index did not advance: {status}")
if status.get("current_instruction") != "pick the blue box":
    raise SystemExit(f"instruction did not rotate: {status}")
if set(status.get("system", {})) != {"cpu_pct", "ram_free_gb", "disk_free_gb"}:
    raise SystemExit(f"system status is incomplete: {status}")
PY
post "continue" "${LOG_DIR}/save_continue.json"
wait_for_state "RECORDING" 15 || fail "second episode did not enter RECORDING"

sleep 2
curl --max-time 180 -fsS -X POST \
    "${BASE_URL}/api/robots/${ROBOT_ID}/record/finish" >"${LOG_DIR}/finish.json"
dataset_path="$(python3 - "${LOG_DIR}/finish.json" <<'PY'
import json
from pathlib import Path
import sys

body = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not body.get("dataset_path"):
    raise SystemExit(f"finish omitted dataset_path: {body}")
print(body["dataset_path"])
PY
)"

python3 - "${dataset_path}" "${STORE_ROOT}/sessions/${session_id}/session.json" <<'PY'
import json
from pathlib import Path
import sys

dataset = Path(sys.argv[1])
session_path = Path(sys.argv[2])
validation = json.loads((dataset / "validation.json").read_text(encoding="utf-8"))
if validation.get("status") != "PASS" or validation.get("episode_count") != 2:
    raise SystemExit(f"two-episode validation failed: {validation}")

info = json.loads((dataset / "meta" / "info.json").read_text(encoding="utf-8"))
if info.get("total_episodes") != 2:
    raise SystemExit(f"metadata episode count mismatch: {info}")
# v3 레이아웃: tasks는 meta/tasks.parquet (task 문자열이 index)
import pandas as pd
tasks = set(pd.read_parquet(dataset / "meta" / "tasks.parquet").index.astype(str))
expected_tasks = {"pick the red box", "pick the blue box"}
if tasks != expected_tasks:
    raise SystemExit(f"metadata tasks mismatch: got {tasks}, expected {expected_tasks}")

session = json.loads(session_path.read_text(encoding="utf-8"))
outcomes = [(episode["index"], episode["result"]) for episode in session["episodes"]]
if outcomes != [(0, "RETRIED"), (0, "SAVED"), (1, "SAVED")]:
    raise SystemExit(f"session episode outcomes mismatch: {outcomes}")
if session.get("result") != "PASS":
    raise SystemExit(f"session result is not PASS: {session}")
PY

stop_nodes
SOURCE_MANIFEST="${SOURCE_MANIFEST}" "${SCRIPT_DIR}/smoke_m4_3.sh"
SOURCE_MANIFEST="${SOURCE_MANIFEST}" "${SCRIPT_DIR}/smoke_m4_5.sh"

echo "PASS: M4-⑥ two-episode retry/instruction/session/status flow and M4-③/⑤ regressions passed"
