#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RB5_DOCKER_IMAGE="${RB5_DOCKER_IMAGE:-sang-fleet:jazzy}"
RB5_ROBOT_IP="${RB5_ROBOT_IP:-10.0.2.7}"
RB5_DATA_PORT="${RB5_DATA_PORT:-5001}"
RB5_HOST_CIDR="${RB5_HOST_CIDR:-10.0.2.100/24}"
RB5_MANAGEMENT_GATEWAY="${RB5_MANAGEMENT_GATEWAY:-192.168.0.1}"
RB5_PREVIEW_PORT="${RB5_PREVIEW_PORT:-8103}"
SANG_CONTROL_PLANE_URL="${SANG_CONTROL_PLANE_URL:-http://127.0.0.1:18091}"
ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-30}"
RB5_NET_CONTAINER="${RB5_NET_CONTAINER:-sang-rb5-net}"
RB5_ADAPTER_CONTAINER="${RB5_ADAPTER_CONTAINER:-sang-rb5-adapter}"
RB5_AGENT_CONTAINER="${RB5_AGENT_CONTAINER:-sang-rb5-agent}"

fail() {
    echo "ERROR: $1" >&2
    exit 1
}

command -v docker >/dev/null || fail "docker is not installed"
command -v ip >/dev/null || fail "iproute2 is not installed on the host"
command -v ping >/dev/null || fail "ping is not installed on the host"
docker image inspect "${RB5_DOCKER_IMAGE}" >/dev/null 2>&1 || \
    fail "Docker image ${RB5_DOCKER_IMAGE} is not available"

RB5_INTERFACE="${RB5_INTERFACE:-$(
    ip route get "${RB5_MANAGEMENT_GATEWAY}" 2>/dev/null |
        awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}'
)}"
[[ -n "${RB5_INTERFACE}" ]] || fail "could not find the RB5 management interface"
ip link show "${RB5_INTERFACE}" >/dev/null 2>&1 || \
    fail "network interface ${RB5_INTERFACE} does not exist"

python3 - "${RB5_HOST_CIDR}" "${RB5_ROBOT_IP}" "${RB5_DATA_PORT}" <<'PY'
import ipaddress
import sys

host = ipaddress.ip_interface(sys.argv[1])
robot = ipaddress.ip_address(sys.argv[2])
port = int(sys.argv[3])
if robot not in host.network:
    raise SystemExit(f"{robot} is not in management subnet {host.network}")
if not 1 <= port <= 65535:
    raise SystemExit("RB5_DATA_PORT must be between 1 and 65535")
PY

for container in \
    "${RB5_NET_CONTAINER}" "${RB5_ADAPTER_CONTAINER}" "${RB5_AGENT_CONTAINER}"
do
    if docker container inspect "${container}" >/dev/null 2>&1
    then
        fail "container ${container} already exists; run scripts/stop_rb5_readonly.sh first"
    fi
done

docker run -d --init --restart unless-stopped --network host \
    --cap-add NET_ADMIN \
    --name "${RB5_NET_CONTAINER}" \
    -e "RB5_INTERFACE=${RB5_INTERFACE}" \
    -e "RB5_HOST_CIDR=${RB5_HOST_CIDR}" \
    "${RB5_DOCKER_IMAGE}" \
    bash -lc '
        set -euo pipefail
        cleanup() {
            ip address del "${RB5_HOST_CIDR}" dev "${RB5_INTERFACE}" \
                2>/dev/null || true
        }
        trap cleanup EXIT INT TERM
        ip address replace "${RB5_HOST_CIDR}" dev "${RB5_INTERFACE}"
        while sleep 3600; do :; done
    ' >/dev/null

reachable=false
for _attempt in $(seq 1 10)
do
    if ping -c 1 -W 1 "${RB5_ROBOT_IP}" >/dev/null 2>&1
    then
        reachable=true
        break
    fi
    sleep 0.5
done
if [[ "${reachable}" != "true" ]]
then
    docker rm -f "${RB5_NET_CONTAINER}" >/dev/null 2>&1 || true
    fail "RB5 ${RB5_ROBOT_IP} did not answer on ${RB5_INTERFACE}"
fi

docker run -d --init --restart unless-stopped --network host --ipc host \
    --name "${RB5_ADAPTER_CONTAINER}" \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp \
    -e "ROS_DOMAIN_ID=${ROS_DOMAIN_ID}" \
    -e RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
    -e "RB5_ROBOT_IP=${RB5_ROBOT_IP}" \
    -e "RB5_DATA_PORT=${RB5_DATA_PORT}" \
    -v "${WS_ROOT}:/ws:ro" \
    "${RB5_DOCKER_IMAGE}" \
    bash -lc '
        set -euo pipefail
        set +u
        source /opt/ros/jazzy/setup.bash
        set -u
        export PYTHONPATH=/ws/src/rb5_readonly_adapter:${PYTHONPATH:-}
        exec python3 -m rb5_readonly_adapter.node \
            --robot-ip "${RB5_ROBOT_IP}" \
            --data-port "${RB5_DATA_PORT}" \
            --topic /rbpodo/joint_states \
            --publish-hz 30
    ' >/dev/null

docker run -d --init --restart unless-stopped --network host --ipc host \
    --name "${RB5_AGENT_CONTAINER}" \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp \
    -e "ROS_DOMAIN_ID=${ROS_DOMAIN_ID}" \
    -e RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
    -e "RB5_PREVIEW_PORT=${RB5_PREVIEW_PORT}" \
    -e "SANG_CONTROL_PLANE_URL=${SANG_CONTROL_PLANE_URL}" \
    -v "${WS_ROOT}:/ws:ro" \
    "${RB5_DOCKER_IMAGE}" \
    bash -lc '
        set -euo pipefail
        set +u
        source /opt/ros/jazzy/setup.bash
        set -u
        export PYTHONPATH=/ws/src/sang_security:/ws/src/fleet_control_plane:/ws/src/robot_agent:${PYTHONPATH:-}
        exec python3 -m robot_agent.node \
            --manifest /ws/manifests/rb5_850e_01.yaml \
            --agent-id workcell-rb5-01 \
            --agent-name "RB5-850E Management" \
            --control-plane-url "${SANG_CONTROL_PLANE_URL}" \
            --interval-s 1 \
            --timeout-s 2 \
            --startup-grace-s 3 \
            --preview-host 127.0.0.1 \
            --preview-port "${RB5_PREVIEW_PORT}" \
            --preview-public-base-url "http://127.0.0.1:${RB5_PREVIEW_PORT}"
    ' >/dev/null

echo "RB5 read-only monitoring started."
echo "  Robot: ${RB5_ROBOT_IP}:${RB5_DATA_PORT}"
echo "  Host:  ${RB5_HOST_CIDR} on ${RB5_INTERFACE}"
echo "  Topic: /rbpodo/joint_states (ROS_DOMAIN_ID=${ROS_DOMAIN_ID})"
echo "  Camera: /rbpodo/camera/wrist/color/image_rect_raw/compressed"
