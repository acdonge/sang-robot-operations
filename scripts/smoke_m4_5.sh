#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SOURCE_MANIFEST="${SOURCE_MANIFEST:-${WS_ROOT}/manifests/bi_so101_01.yaml}"
TASK="${TASK:-smoke_m4_5}"
BASE_URL="http://127.0.0.1:8080"
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
        if curl -fsS "${BASE_URL}/api/robots/bi_so101_01/health" \
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

wait_for_fresh_health() {
    local attempt
    for ((attempt = 0; attempt < 40; attempt++)); do
        if curl -fsS "${BASE_URL}/api/robots/bi_so101_01/health" \
            >"${LOG_DIR}/health.json"; then
            if python3 - "${LOG_DIR}/health.json" <<'PY'
import json
from pathlib import Path
import sys

health = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
sources = health.get("sources", {})
raise SystemExit(0 if len(sources) == 7 and all(v.get("fresh") for v in sources.values()) else 1)
PY
            then
                return 0
            fi
        fi
        sleep 0.5
    done
    return 1
}

expect_409() {
    local endpoint="$1"
    local payload="{}"
    local status
    if (($# >= 2)); then
        payload="$2"
    fi
    status="$(curl -sS -o "${LOG_DIR}/conflict.json" -w '%{http_code}' \
        -H 'Content-Type: application/json' -d "${payload}" \
        "${BASE_URL}${endpoint}")"
    [[ "${status}" == "409" ]] || fail "expected 409 from ${endpoint}, got ${status}"
    python3 - "${LOG_DIR}/conflict.json" <<'PY'
import json
from pathlib import Path
import sys

body = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if set(body) != {"error"} or not body["error"]:
    raise SystemExit(f"invalid conflict body: {body}")
PY
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
    fail "leftover recorder process before smoke case"
fi

setsid ros2 run recorder_core fake_robot --ros-args \
    -p "manifest_path:=${MANIFEST_PATH}" >"${LOG_DIR}/fake.log" 2>&1 &
fake_pid=$!
sleep 1

setsid ros2 run recorder_core recorder_gateway \
    "${MANIFEST_PATH}" --ui-dir "${WS_ROOT}/ui" >"${LOG_DIR}/gateway.log" 2>&1 &
gateway_pid=$!

wait_for_api || fail "gateway API did not become ready"
python3 - "${LOG_DIR}/robots.json" <<'PY'
import json
from pathlib import Path
import sys

robots = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if len(robots) != 1 or robots[0].get("robot_id") != "bi_so101_01":
    raise SystemExit(f"unexpected robots response: {robots}")
if len(robots[0].get("cameras", [])) != 5:
    raise SystemExit(f"expected five cameras: {robots}")
PY
wait_for_fresh_health || fail "all sources did not become fresh"

set +e
curl --max-time 2 -sS -D "${LOG_DIR}/mjpeg.headers" \
    "${BASE_URL}/api/robots/bi_so101_01/cameras/top/stream" \
    -o "${LOG_DIR}/mjpeg.body"
mjpeg_status=$?
set -e
[[ "${mjpeg_status}" == "0" || "${mjpeg_status}" == "28" ]] || \
    fail "MJPEG curl failed with ${mjpeg_status}"
grep -aiFq 'boundary=frame' "${LOG_DIR}/mjpeg.headers" || \
    fail "MJPEG content type omitted boundary"
grep -aFq -- '--frame' "${LOG_DIR}/mjpeg.body" || \
    fail "MJPEG body omitted boundary bytes"

curl -fsS -H 'Content-Type: application/json' \
    -d "{\"task\":\"${TASK}\"}" \
    "${BASE_URL}/api/robots/bi_so101_01/record/start" \
    >"${LOG_DIR}/start.json"
python3 - "${LOG_DIR}/start.json" <<'PY'
import json
from pathlib import Path
import sys

body = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not body.get("session_id"):
    raise SystemExit(f"start omitted session_id: {body}")
PY
expect_409 "/api/robots/bi_so101_01/record/start" "{\"task\":\"${TASK}\"}"
wait_for_state "RECORDING" 15 || fail "recorder did not enter RECORDING"
sleep 4

curl --max-time 65 -fsS -X POST \
    "${BASE_URL}/api/robots/bi_so101_01/record/stop" >"${LOG_DIR}/stop.json"
dataset_path="$(python3 - "${LOG_DIR}/stop.json" <<'PY'
import json
from pathlib import Path
import sys

body = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not body.get("session_id") or not body.get("dataset_path"):
    raise SystemExit(f"stop response is incomplete: {body}")
print(body["dataset_path"])
PY
)"
[[ -d "${dataset_path}" ]] || fail "dataset_path does not exist: ${dataset_path}"
python3 - "${dataset_path}/validation.json" <<'PY'
import json
from pathlib import Path
import sys

result = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if result.get("status") != "PASS":
    raise SystemExit(f"validation did not pass: {result}")
PY
expect_409 "/api/robots/bi_so101_01/record/stop"

curl -fsS -H 'Content-Type: application/json' \
    -d '{"task":"smoke_abort"}' \
    "${BASE_URL}/api/robots/bi_so101_01/record/start" \
    >"${LOG_DIR}/abort_start.json"
wait_for_state "RECORDING" 15 || fail "abort case did not enter RECORDING"
curl -fsS -X POST "${BASE_URL}/api/robots/bi_so101_01/record/abort" \
    >"${LOG_DIR}/abort.json"
grep -Fq '"aborted":true' "${LOG_DIR}/abort.json" || fail "abort response was not true"
wait_for_state "FAULT" 15 || fail "abort case did not reach FAULT"
curl -fsS -X POST "${BASE_URL}/api/robots/bi_so101_01/record/abort" \
    >"${LOG_DIR}/reset.json"
grep -Fq '"aborted":true' "${LOG_DIR}/reset.json" || fail "FAULT reset response was not true"
wait_for_state "IDLE" 5 || fail "FAULT abort endpoint did not reset to IDLE"

echo "PASS: M4-⑤ REST, health, record, validation, abort, 409, and MJPEG smoke checks passed"
