#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
XR_PORT="${XR_PORT:-5556}"
ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-42}"
XR_DOCKER_IMAGE="${XR_DOCKER_IMAGE:-sang-fleet:jazzy}"

fail() {
    echo "ERROR: $1" >&2
    exit 1
}

[[ "${XR_PORT}" =~ ^[0-9]+$ ]] || fail "XR_PORT must be an integer"
(( XR_PORT >= 1 && XR_PORT <= 65535 )) || \
    fail "XR_PORT must be between 1 and 65535"
command -v docker >/dev/null || fail "docker is not installed"
docker image inspect "${XR_DOCKER_IMAGE}" >/dev/null 2>&1 || \
    fail "Docker image ${XR_DOCKER_IMAGE} is not available"

if ! python3 - "${XR_PORT}" <<'PY'
import socket
import sys

with socket.socket() as listener:
    try:
        listener.bind(("127.0.0.1", int(sys.argv[1])))
    except OSError as exc:
        raise SystemExit(f"TCP port {sys.argv[1]} is unavailable: {exc}")
PY
then
    fail "stop the process already using TCP ${XR_PORT}"
fi

echo "Starting input-only XR bridge on TCP ${XR_PORT}. Press Ctrl-C to stop."
exec docker run --rm --init --network host \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp \
    -e "ROS_DOMAIN_ID=${ROS_DOMAIN_ID}" \
    -e "XR_PORT=${XR_PORT}" \
    -v "${WS_ROOT}:/ws" \
    -w /ws \
    "${XR_DOCKER_IMAGE}" \
    bash -lc '
        set -euo pipefail
        set +u
        source /opt/ros/jazzy/setup.bash
        set -u
        colcon --log-base /tmp/xr-log build \
            --build-base /tmp/xr-build \
            --install-base /tmp/xr-install \
            --symlink-install \
            --packages-select xr_teleop_interfaces xr_teleop_bridge \
            --event-handlers console_direct+
        set +u
        source /tmp/xr-install/setup.bash
        set -u
        exec ros2 run xr_teleop_bridge xr_teleop_bridge --ros-args \
            -p bind_host:=0.0.0.0 \
            -p port:="${XR_PORT}"
    '
