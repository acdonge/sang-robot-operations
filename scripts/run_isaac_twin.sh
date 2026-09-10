#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV="${HOME}/miniconda3/envs/leisaac"
export PYTHONNOUSERSITE=1
export ACCEPT_EULA=Y
export OMNI_KIT_ACCEPT_EULA=YES
export ROS_DISTRO=jazzy
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export RMW_FASTRTPS_PUBLICATION_MODE=ASYNCHRONOUS
export ROS_DOMAIN_ID=98
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
export FASTDDS_BUILTIN_TRANSPORTS=UDPv4
export LD_LIBRARY_PATH="${ENV}/lib/python3.11/site-packages/isaacsim/exts/isaacsim.ros2.bridge/jazzy/lib:${LD_LIBRARY_PATH:-}"
export PYTHONPATH="${ROOT}/sim:${ROOT}/src/fleet_control_plane"
cd "${ROOT}/../leisaac"
exec "${ENV}/bin/python" -m isaac_twin.runner "$@"
