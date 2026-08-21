#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/docker_access.sh"
rb5_require_docker

IMAGE="${RB5_CONTROL_IMAGE:-sang-rb5-control:humble}"
CONTAINER="${RB5_CONTROL_CONTAINER:-sang-rb5-official-control}"
ROBOT_IP="${RB5_ROBOT_IP:-10.0.2.7}"
ROS_DOMAIN_ID="${RB5_CONTROL_ROS_DOMAIN_ID:-52}"

if docker container inspect "${CONTAINER}" >/dev/null 2>&1; then
  zenity --error --title="RB5 control" \
    --text="RB5 control container already exists. Use the Stop shortcut first."
  exit 1
fi

if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
  zenity --error --title="RB5 control" \
    --text="${IMAGE} is not installed."
  exit 1
fi

WARNING=$'ATTENDED LAB USE ONLY\n\nStarting this tool changes the RB5 controller to Real mode. This SANG build patches the upstream 100% startup speed bar to 1%.\n\nConfirm all items before continuing:\n- Workcell is clear and guarded\n- E-stop is within reach and tested\n- No welding arc or wire feed is enabled\n- Only one operator controls the robot\n- First motion will be small and slow\n\nThe official driver is marked active-development and not for production use.'
if ! zenity --question --title="RB5-850E safety confirmation" \
  --width=560 --ok-label="I confirm - open control UI" \
  --cancel-label="Cancel" --text="${WARNING}"; then
  exit 0
fi

python3 - "${ROBOT_IP}" <<'PY'
import socket
import sys

host = sys.argv[1]
for port in (5000, 5001):
    with socket.create_connection((host, port), timeout=2.0):
        pass
print(f"RB5 controller reachable at {host}:5000/5001")
PY

DISPLAY_VALUE="${DISPLAY:-:0}"
XAUTHORITY_VALUE="${XAUTHORITY:-/run/user/$(id -u)/gdm/Xauthority}"
XAUTHORITY_ARGS=()
if [[ -f "${XAUTHORITY_VALUE}" ]]; then
  XAUTHORITY_ARGS=(-e "XAUTHORITY=${XAUTHORITY_VALUE}" -v "${XAUTHORITY_VALUE}:${XAUTHORITY_VALUE}:ro")
fi
DEVICE_ARGS=()
if [[ -d /dev/dri ]]; then
  DEVICE_ARGS=(--device /dev/dri:/dev/dri)
fi
GROUP_ARGS=()
RENDER_GID="$(getent group render | cut -d: -f3 || true)"
if [[ -n "${RENDER_GID}" ]]; then
  GROUP_ARGS=(--group-add "${RENDER_GID}")
fi

docker run --rm --name "${CONTAINER}" \
  --network host \
  --ipc host \
  --user "$(id -u):$(rb5_login_gid)" \
  -e HOME=/tmp \
  -e "DISPLAY=${DISPLAY_VALUE}" \
  -e "ROS_DOMAIN_ID=${ROS_DOMAIN_ID}" \
  -e ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST \
  -e QT_X11_NO_MITSHM=1 \
  "${XAUTHORITY_ARGS[@]}" \
  "${GROUP_ARGS[@]}" \
  -v /tmp/.X11-unix:/tmp/.X11-unix:rw \
  "${DEVICE_ARGS[@]}" \
  "${IMAGE}" \
  ros2 launch rbpodo_moveit_config moveit.launch.py \
    model_id:=rb5_850e \
    use_fake_hardware:=false \
    cb_simulation:=false \
    robot_ip:="${ROBOT_IP}"
