#!/usr/bin/env bash
set -euo pipefail

# Isolate this smoke from any robot ROS graph already running.
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-$(( (RANDOM % 100) + 1 ))}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORK_DIR="$(mktemp -d)"
XR_PORT="${XR_PORT:-$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')}"
BRIDGE_PID=""

cleanup() {
    set +e
    if [[ -n "${BRIDGE_PID}" ]] && kill -0 "${BRIDGE_PID}" 2>/dev/null; then
        kill -TERM -- "-${BRIDGE_PID}" 2>/dev/null || kill -TERM "${BRIDGE_PID}"
        wait "${BRIDGE_PID}" 2>/dev/null
    fi
    rm -rf "${WORK_DIR}"
    set -e
}

fail() {
    local message="$1"
    echo "FAIL: ${message}" >&2
    for log_file in "${WORK_DIR}"/*.log; do
        if [[ -f "${log_file}" ]]; then
            echo "--- ${log_file} ---" >&2
            tail -n 160 "${log_file}" >&2
        fi
    done
    exit 1
}

trap cleanup EXIT
trap 'exit 130' INT TERM

[[ -f /opt/ros/jazzy/setup.bash ]] || fail "ROS 2 Jazzy is not installed"
command -v colcon >/dev/null || fail "colcon is not installed"
command -v setsid >/dev/null || fail "setsid is not installed"

set +u
source /opt/ros/jazzy/setup.bash
set -u

if ! colcon --log-base "${WORK_DIR}/log" build \
    --build-base "${WORK_DIR}/build" \
    --install-base "${WORK_DIR}/install" \
    --symlink-install \
    --packages-select xr_teleop_interfaces xr_teleop_bridge \
    --event-handlers console_direct+ \
    >"${WORK_DIR}/build.log" 2>&1; then
    fail "XR ROS packages did not build"
fi

set +u
source "${WORK_DIR}/install/setup.bash"
set -u

setsid ros2 run xr_teleop_bridge xr_teleop_bridge --ros-args \
    -p "bind_host:=127.0.0.1" \
    -p "port:=${XR_PORT}" \
    -p "publish_hz:=120.0" \
    -p "diagnostics_hz:=20.0" \
    -p "stale_timeout_ms:=250.0" \
    >"${WORK_DIR}/bridge.log" 2>&1 &
BRIDGE_PID=$!

if ! python3 "${WS_ROOT}/src/xr_teleop_bridge/test/ros_smoke_probe.py" \
    --port "${XR_PORT}" >"${WORK_DIR}/probe.log" 2>&1; then
    fail "TCP-to-ROS integration assertions failed"
fi

kill -0 "${BRIDGE_PID}" 2>/dev/null || fail "XR bridge exited unexpectedly"
grep -Fq "robot command output is disabled" "${WORK_DIR}/bridge.log" || \
    fail "bridge did not report its input-only safety mode"

sed -n '1,160p' "${WORK_DIR}/probe.log"
echo "PASS: XR TCP -> ROS frame/pose/TF/diagnostics smoke (domain ${ROS_DOMAIN_ID})"
