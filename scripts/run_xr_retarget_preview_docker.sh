#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
XR_PORT="${XR_PORT:-5556}"
ISAAC_PORT="${ISAAC_PORT:-5558}"
ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-42}"
XR_TRANSLATION_SCALE="${XR_TRANSLATION_SCALE:-0.5}"
XR_DOCKER_IMAGE="${XR_DOCKER_IMAGE:-sang-fleet:jazzy}"
XR_CONTAINER_NAME="${XR_CONTAINER_NAME:-galaxy-xr-ros}"

fail() {
    echo "ERROR: $1" >&2
    exit 1
}

[[ "${XR_PORT}" =~ ^[0-9]+$ ]] || fail "XR_PORT must be an integer"
(( XR_PORT >= 1 && XR_PORT <= 65535 )) || \
    fail "XR_PORT must be between 1 and 65535"
[[ "${ISAAC_PORT}" =~ ^[0-9]+$ ]] || fail "ISAAC_PORT must be an integer"
(( ISAAC_PORT >= 1 && ISAAC_PORT <= 65535 )) || \
    fail "ISAAC_PORT must be between 1 and 65535"
[[ "${XR_PORT}" != "${ISAAC_PORT}" ]] || fail "XR_PORT and ISAAC_PORT must differ"
if ! python3 - "${XR_TRANSLATION_SCALE}" <<'PY'
import math
import sys

try:
    value = float(sys.argv[1])
except ValueError as exc:
    raise SystemExit(f"XR_TRANSLATION_SCALE must be a number: {exc}")
if not math.isfinite(value) or value <= 0.0:
    raise SystemExit("XR_TRANSLATION_SCALE must be positive and finite")
PY
then
    fail "invalid XR translation scale"
fi
command -v docker >/dev/null || fail "docker is not installed"
docker image inspect "${XR_DOCKER_IMAGE}" >/dev/null 2>&1 || \
    fail "Docker image ${XR_DOCKER_IMAGE} is not available"
if docker container inspect "${XR_CONTAINER_NAME}" >/dev/null 2>&1; then
    fail "container ${XR_CONTAINER_NAME} already exists"
fi

if ! python3 - "${XR_PORT}" "${ISAAC_PORT}" <<'PY'
import socket
import sys

for raw_port in sys.argv[1:]:
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", int(raw_port)))
        except OSError as exc:
            raise SystemExit(f"TCP port {raw_port} is unavailable: {exc}")
PY
then
    fail "stop the process already using XR/Isaac TCP ports"
fi

echo "Starting XR input on TCP ${XR_PORT} and Isaac gateway on TCP ${ISAAC_PORT}."
echo "XR translation scale: ${XR_TRANSLATION_SCALE}"
echo "No robot command publisher is included. Press Ctrl-C to stop."
exec docker run --rm --init --network host \
    --name "${XR_CONTAINER_NAME}" \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp \
    -e "ROS_DOMAIN_ID=${ROS_DOMAIN_ID}" \
    -e "XR_PORT=${XR_PORT}" \
    -e "ISAAC_PORT=${ISAAC_PORT}" \
    -e "XR_TRANSLATION_SCALE=${XR_TRANSLATION_SCALE}" \
    -v "${WS_ROOT}:/ws" \
    -w /ws \
    "${XR_DOCKER_IMAGE}" \
    bash -lc '
        set -euo pipefail
        set +u
        source /opt/ros/jazzy/setup.bash
        set -u
        colcon --log-base /tmp/xr-retarget-log build \
            --build-base /tmp/xr-retarget-build \
            --install-base /tmp/xr-retarget-install \
            --symlink-install \
            --packages-select \
                xr_teleop_interfaces xr_teleop_bridge xr_teleop_retarget \
            --event-handlers console_direct+
        set +u
        source /tmp/xr-retarget-install/setup.bash
        set -u
        exec ros2 launch xr_teleop_retarget xr_retarget_preview.launch.py \
            bind_host:=0.0.0.0 port:="${XR_PORT}" rviz:=false \
            translation_scale:="${XR_TRANSLATION_SCALE}" \
            isaac_gateway:=true \
            isaac_endpoint:=tcp://0.0.0.0:"${ISAAC_PORT}"
    '
