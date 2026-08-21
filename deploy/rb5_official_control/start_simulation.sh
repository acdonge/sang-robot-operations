#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/docker_access.sh"
rb5_require_docker

IMAGE="${RB5_CONTROL_IMAGE:-sang-rb5-control:humble}"
CONTAINER="${RB5_SIM_CONTAINER:-sang-rb5-official-simulation}"
ROS_DOMAIN_ID="${RB5_CONTROL_ROS_DOMAIN_ID:-52}"
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
    use_fake_hardware:=true \
    cb_simulation:=true
