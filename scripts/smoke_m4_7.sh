#!/usr/bin/env bash
set -euo pipefail

# 다른 컨테이너/호스트의 DDS가 도커 브리지 멀티캐스트로 새어들지 않게 도메인 격리
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-$(( (RANDOM % 100) + 1 ))}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SOURCE_MANIFEST="${SOURCE_MANIFEST:-${WS_ROOT}/manifests/bi_so101_01.yaml}"
BASE_URL="http://127.0.0.1:8080"
ROBOT_ID="bi_so101_01"
TASK="smoke_m4_7"
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
    local attempt
    for ((attempt = 0; attempt < 60; attempt++)); do
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

expect_error_status() {
    local expected="$1"
    local endpoint="$2"
    local payload="$3"
    local output="$4"
    local status
    status="$(curl -sS -o "${output}" -w '%{http_code}' \
        -H 'Content-Type: application/json' -d "${payload}" \
        "${BASE_URL}${endpoint}")"
    [[ "${status}" == "${expected}" ]] || \
        fail "expected ${expected} from ${endpoint}, got ${status}"
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
    fail "leftover recorder process before M4-⑦ case"
fi

setsid ros2 run recorder_core fake_robot --ros-args \
    -p "manifest_path:=${MANIFEST_PATH}" >"${LOG_DIR}/fake.log" 2>&1 &
fake_pid=$!
sleep 1

setsid ros2 run recorder_core recorder_gateway \
    "${MANIFEST_PATH}" --ui-dir "${WS_ROOT}/ui" >"${LOG_DIR}/gateway.log" 2>&1 &
gateway_pid=$!
wait_for_api || fail "gateway API did not become ready"

# M4-⑤와 동일한 단일 에피소드 REST 녹화 흐름으로 실제 dataset 하나를 승격한다.
curl -fsS -H 'Content-Type: application/json' \
    -d "{\"task\":\"${TASK}\"}" \
    "${BASE_URL}/api/robots/${ROBOT_ID}/record/start" >"${LOG_DIR}/start.json"
wait_for_state "RECORDING" || fail "recorder did not enter RECORDING"
sleep 4
curl --max-time 65 -fsS -X POST \
    "${BASE_URL}/api/robots/${ROBOT_ID}/record/stop" >"${LOG_DIR}/stop.json"

readarray -t dataset_values < <(python3 - "${LOG_DIR}/stop.json" <<'PY'
import json
from pathlib import Path
import sys

body = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not body.get("session_id") or not body.get("dataset_path"):
    raise SystemExit(f"stop response is incomplete: {body}")
print(body["session_id"])
print(body["dataset_path"])
print(Path(body["dataset_path"]).name)
PY
)
session_id="${dataset_values[0]}"
dataset_path="${dataset_values[1]}"
dataset_id="${dataset_values[2]}"

curl -fsS "${BASE_URL}/api/datasets" >"${LOG_DIR}/datasets.json"
python3 - "${LOG_DIR}/datasets.json" "${dataset_id}" "${dataset_path}" \
    "${session_id}" <<'PY'
import json
from pathlib import Path
import sys

datasets = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if len(datasets) != 1:
    raise SystemExit(f"expected one indexed dataset: {datasets}")
dataset = datasets[0]
expected_fields = {
    "dataset_id", "contract", "task", "domain", "path", "episodes",
    "frames", "fps", "size_mb", "created", "validation_status", "session_id",
}
if set(dataset) != expected_fields:
    raise SystemExit(f"dataset fields mismatch: {dataset}")
if dataset["dataset_id"] != sys.argv[2] or dataset["path"] != sys.argv[3]:
    raise SystemExit(f"dataset identity mismatch: {dataset}")
if dataset["session_id"] != sys.argv[4]:
    raise SystemExit(f"session lineage mismatch: {dataset}")
if dataset["contract"] != "bi_so101_v1" or dataset["task"] != "smoke_m4_7":
    raise SystemExit(f"dataset contract/task mismatch: {dataset}")
if dataset["domain"] != "real" or dataset["validation_status"] != "PASS":
    raise SystemExit(f"dataset domain/validation mismatch: {dataset}")
if dataset["episodes"] != 1 or dataset["frames"] <= 0 or dataset["fps"] != 30:
    raise SystemExit(f"dataset counts mismatch: {dataset}")
if not isinstance(dataset["size_mb"], (int, float)) or not dataset["created"]:
    raise SystemExit(f"dataset size/created mismatch: {dataset}")
PY

# 유효 dataset 요청은 token 누락으로 422까지만 검증하여 HF 네트워크를 호출하지 않는다.
expect_error_status "422" "/api/datasets/${dataset_id}/upload" \
    '{"repo_id":"smoke/never-uploaded","private":false}' \
    "${LOG_DIR}/invalid_request.json"

# 별도 FAIL fixture는 동기 preflight에서 409가 되어 업로드 스레드가 시작되지 않는다.
invalid_id="m4_7_invalid_preflight"
invalid_path="${STORE_ROOT}/datasets/raw/bi_so101_v1/preflight/real/${invalid_id}"
mkdir -p "${invalid_path}"
echo '{"status":"FAIL"}' >"${invalid_path}/validation.json"
expect_error_status "409" "/api/datasets/${invalid_id}/upload" \
    '{"repo_id":"smoke/never-uploaded","token":"not-used","private":false}' \
    "${LOG_DIR}/preflight.json"
python3 - "${LOG_DIR}/preflight.json" <<'PY'
import json
from pathlib import Path
import sys

body = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if set(body) != {"error"} or "not PASS" not in body["error"]:
    raise SystemExit(f"invalid preflight response: {body}")
PY

echo "PASS: M4-⑦ dataset index fields and upload request/preflight responses passed without HF network"
