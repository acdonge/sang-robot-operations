#!/usr/bin/env bash
set -euo pipefail

# Keep preview validation isolated from the project's live domain (42).
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-231}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORK_DIR="$(mktemp -d)"
NODE_PID=""
GATEWAY_PID=""

cleanup() {
    set +e
    if [[ -n "${NODE_PID}" ]] && kill -0 "${NODE_PID}" 2>/dev/null; then
        kill -TERM -- "-${NODE_PID}" 2>/dev/null || kill -TERM "${NODE_PID}"
        wait "${NODE_PID}" 2>/dev/null
    fi
    if [[ -n "${GATEWAY_PID}" ]] && kill -0 "${GATEWAY_PID}" 2>/dev/null; then
        kill -TERM -- "-${GATEWAY_PID}" 2>/dev/null || kill -TERM "${GATEWAY_PID}"
        wait "${GATEWAY_PID}" 2>/dev/null
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
            tail -n 180 "${log_file}" >&2
        fi
    done
    exit 1
}

trap cleanup EXIT
trap 'exit 130' INT TERM

[[ -f /opt/ros/jazzy/setup.bash ]] || fail "ROS 2 Jazzy is not installed"
command -v colcon >/dev/null || fail "colcon is not installed"
command -v setsid >/dev/null || fail "setsid is not installed"
python3 -c 'import zmq' || fail "python3-zmq is not installed"

set +u
source /opt/ros/jazzy/setup.bash
set -u

if ! colcon --log-base "${WORK_DIR}/log" build \
    --build-base "${WORK_DIR}/build" \
    --install-base "${WORK_DIR}/install" \
    --symlink-install \
    --packages-select \
        xr_teleop_interfaces xr_teleop_bridge xr_teleop_retarget \
    --event-handlers console_direct+ \
    >"${WORK_DIR}/build.log" 2>&1; then
    fail "XR retarget ROS packages did not build"
fi

set +u
source "${WORK_DIR}/install/setup.bash"
set -u

if ! python3 -m pytest -q \
    "${WS_ROOT}/src/xr_teleop_retarget/test/test_math3d.py" \
    "${WS_ROOT}/src/xr_teleop_retarget/test/test_retarget.py" \
    >"${WORK_DIR}/unit.log" 2>&1; then
    fail "XR retarget unit tests failed"
fi

setsid ros2 run xr_teleop_retarget xr_retarget_preview \
    >"${WORK_DIR}/node.log" 2>&1 &
NODE_PID=$!
setsid ros2 run xr_teleop_retarget xr_isaac_gateway \
    --ros-args -p bind_endpoint:=tcp://127.0.0.1:5558 \
    >"${WORK_DIR}/gateway.log" 2>&1 &
GATEWAY_PID=$!

if ! python3 \
    "${WS_ROOT}/src/xr_teleop_retarget/test/ros_smoke_probe.py" \
    >"${WORK_DIR}/probe.log" 2>&1; then
    fail "XR retarget ROS integration assertions failed"
fi

kill -0 "${NODE_PID}" 2>/dev/null || fail "XR retarget node exited unexpectedly"
kill -0 "${GATEWAY_PID}" 2>/dev/null || fail "XR Isaac gateway exited unexpectedly"
grep -Fq "robot command output is disabled" "${WORK_DIR}/node.log" || \
    fail "retarget node did not report preview-only safety mode"

sed -n '1,180p' "${WORK_DIR}/unit.log"
sed -n '1,220p' "${WORK_DIR}/probe.log"
echo "PASS: bimanual XR retarget/TF/Isaac gateway (domain ${ROS_DOMAIN_ID})"
