#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SOURCE_MANIFEST="${SOURCE_MANIFEST:-${WS_ROOT}/manifests/bi_so101_01.yaml}"
DROP_SOURCE="${DROP_SOURCE:-top}"
TASK="${TASK:-smoke_m4_3}"
LOG_DIR="$(mktemp -d)"
STORE_ROOT="${LOG_DIR}/store"
MANIFEST_PATH="${LOG_DIR}/manifest.yaml"

fake_pid=""
recorder_pid=""

stop_nodes() {
    set +e
    if [[ -n "${recorder_pid}" ]] && kill -0 "${recorder_pid}" 2>/dev/null; then
        kill -TERM -- "-${recorder_pid}" 2>/dev/null || kill -TERM "${recorder_pid}"
    fi
    if [[ -n "${fake_pid}" ]] && kill -0 "${fake_pid}" 2>/dev/null; then
        kill -TERM -- "-${fake_pid}" 2>/dev/null || kill -TERM "${fake_pid}"
    fi
    if [[ -n "${recorder_pid}" ]]; then
        wait "${recorder_pid}" 2>/dev/null
    fi
    if [[ -n "${fake_pid}" ]]; then
        wait "${fake_pid}" 2>/dev/null
    fi
    sleep 1
    pkill -KILL -f 'recorder_core/(fake_robot|recorder_core)' 2>/dev/null
    fake_pid=""
    recorder_pid=""
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

wait_for_log() {
    local log_path="$1"
    local pattern="$2"
    local timeout_s="$3"
    local attempts=$((timeout_s * 2))
    local index
    for ((index = 0; index < attempts; index++)); do
        if grep -Fq "${pattern}" "${log_path}" 2>/dev/null; then
            return 0
        fi
        if [[ -n "${recorder_pid}" ]] && ! kill -0 "${recorder_pid}" 2>/dev/null; then
            return 1
        fi
        sleep 0.5
    done
    return 1
}

call_trigger() {
    local service="$1"
    local output
    output="$(timeout 180 ros2 service call "${service}" std_srvs/srv/Trigger '{}')" || \
        fail "service call failed: ${service}"
    if [[ "${output}" != *"success=True"* ]]; then
        fail "service rejected ${service}: ${output}"
    fi
}

start_nodes() {
    local label="$1"
    local drop_source="$2"
    local drop_after_s="$3"
    local fake_log="${LOG_DIR}/${label}_fake.log"
    local recorder_log="${LOG_DIR}/${label}_recorder.log"
    local -a drop_args=()

    if [[ -n "${drop_source}" ]]; then
        drop_args=(-p "drop_source:=${drop_source}" -p "drop_after_s:=${drop_after_s}")
    fi
    if pgrep -f 'recorder_core/(fake_robot|recorder_core)' >/dev/null 2>&1; then
        fail "leftover node process before ${label} case"
    fi

    setsid ros2 run recorder_core fake_robot --ros-args \
        -p "manifest_path:=${MANIFEST_PATH}" \
        "${drop_args[@]}" >"${fake_log}" 2>&1 &
    fake_pid=$!
    sleep 1
    setsid ros2 run recorder_core recorder_core --ros-args \
        -p "manifest_path:=${MANIFEST_PATH}" \
        -p "task:=${TASK}" \
        -p "min_staging_free_gb:=0.01" >"${recorder_log}" 2>&1 &
    recorder_pid=$!

    wait_for_log "${recorder_log}" "HEALTH all_sources_fresh=true" 15 || \
        fail "sources did not become fresh in ${label} case"
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
# ROS setup scripts reference unset variables; disable nounset only while sourcing.
set +u
source /opt/ros/jazzy/setup.bash
set -u
colcon build --symlink-install
set +u
source "${WS_ROOT}/install/setup.bash"
set -u

start_nodes "normal" "" "0"
normal_recorder_log="${LOG_DIR}/normal_recorder.log"
call_trigger "/recorder_core/record_start"
wait_for_log "${normal_recorder_log}" "to=RECORDING" 15 || \
    fail "normal case did not enter RECORDING"
sleep 5
call_trigger "/recorder_core/record_stop"
wait_for_log "${normal_recorder_log}" "to=READY" 180 || \
    fail "normal case did not enter READY"

raw_path="$(sed -n 's/.*DATASET_READY path=//p' "${normal_recorder_log}" | tail -n 1)"
[[ -n "${raw_path}" && -d "${raw_path}" ]] || fail "promoted raw dataset is missing"
python3 - "${raw_path}/validation.json" <<'PY'
import json
from pathlib import Path
import sys

result = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if result.get("status") != "PASS":
    raise SystemExit(f"validation did not pass: {result}")
if result.get("frame_count", 0) <= 0:
    raise SystemExit(f"validation frame count is invalid: {result}")
PY
if find "${STORE_ROOT}/staging" -maxdepth 1 -name '*.partial' -print -quit | grep -q .; then
    fail "normal case left a .partial directory"
fi
stop_nodes

start_nodes "drop" "${DROP_SOURCE}" "7.0"
drop_recorder_log="${LOG_DIR}/drop_recorder.log"
call_trigger "/recorder_core/record_start"
wait_for_log "${drop_recorder_log}" "to=RECORDING" 15 || \
    fail "drop case did not enter RECORDING"
wait_for_log "${drop_recorder_log}" "to=FAULT" 20 || \
    fail "drop case did not latch FAULT"
grep -Fq "GATE_FAILURE source=${DROP_SOURCE}" "${drop_recorder_log}" || \
    fail "drop case did not report gate source ${DROP_SOURCE}"
if find "${STORE_ROOT}/staging" -maxdepth 1 -name '*.partial' -print -quit | grep -q .; then
    fail "drop case left a .partial directory"
fi

echo "PASS: M4-③/④ record, validate, promote, and gate-abort smoke checks passed"
