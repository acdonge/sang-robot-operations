#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MANIFEST_PATH="${MANIFEST_PATH:-${WS_ROOT}/manifests/bi_so101_01.yaml}"
DROP_SOURCE="${DROP_SOURCE:-top}"
RUN_SECONDS="${RUN_SECONDS:-8}"
LOG_DIR="$(mktemp -d)"

fake_pid=""
recorder_pid=""

stop_nodes() {
    set +e
    # ros2 run은 래퍼 프로세스라 단일 kill로는 자식 노드가 살아남는다(좀비 publish).
    # setsid로 띄운 프로세스 그룹 전체에 TERM을 보낸다.
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
    recorder_pid=""
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
            tail -n 80 "${log}" >&2
        fi
    done
    exit 1
}

trap cleanup EXIT
trap 'exit 130' INT TERM

cd "${WS_ROOT}"
# ROS setup 스크립트는 set -u와 충돌(AMENT_TRACE_SETUP_FILES 미정의) — 소싱 중에만 해제
set +u
source /opt/ros/jazzy/setup.bash
set -u
colcon build --symlink-install
set +u
source "${WS_ROOT}/install/setup.bash"
set -u

mapfile -t required_sources < <(
    python3 -c 'import sys, yaml; print(*yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["recording"]["required_sources"], sep="\n")' "${MANIFEST_PATH}"
)

run_case() {
    local label="$1"
    local drop_source="$2"
    local fake_log="${LOG_DIR}/${label}_fake.log"
    local recorder_log="${LOG_DIR}/${label}_recorder.log"
    local -a drop_args=()

    if [[ -n "${drop_source}" ]]; then
        drop_args=(-p "drop_source:=${drop_source}" -p "drop_after_s:=5.0")
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
        -p "manifest_path:=${MANIFEST_PATH}" >"${recorder_log}" 2>&1 &
    recorder_pid=$!

    sleep "${RUN_SECONDS}"
    if ! kill -0 "${fake_pid}" 2>/dev/null; then
        fail "fake_robot exited early in ${label} case"
    fi
    if ! kill -0 "${recorder_pid}" 2>/dev/null; then
        fail "recorder_core exited early in ${label} case"
    fi

    local health_line
    health_line="$(grep -F "HEALTH all_sources_fresh=true" "${recorder_log}" | tail -n 1)" || \
        fail "no all-sources-fresh health log in ${label} case"
    for source in "${required_sources[@]}"; do
        if [[ "${health_line}" != *"${source}("* ]]; then
            fail "fresh health log omitted source ${source} in ${label} case"
        fi
    done

    if [[ -n "${drop_source}" ]]; then
        grep -Fq "GATE_FAILURE source=${drop_source} " "${recorder_log}" || \
            fail "gate did not report dropped source ${drop_source}"
        grep -Eq "HEALTH .*gate_fail=[1-9][0-9]* .*last_failure_source=${drop_source}" \
            "${recorder_log}" || fail "gate failure count did not increase for ${drop_source}"
    fi

    stop_nodes
}

run_case "fresh" ""
run_case "drop" "${DROP_SOURCE}"

echo "PASS: M4-② fresh-source and dropped-source smoke checks passed"
exit 0
