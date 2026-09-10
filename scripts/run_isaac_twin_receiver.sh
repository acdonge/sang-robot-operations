#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV="${HOME}/miniconda3/envs/leisaac"
ROS="${ENV}/lib/python3.11/site-packages/isaacsim/exts/isaacsim.ros2.bridge/jazzy"
export ROS_DISTRO=jazzy ROS_DOMAIN_ID=98 ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp RMW_FASTRTPS_PUBLICATION_MODE=ASYNCHRONOUS
export FASTDDS_BUILTIN_TRANSPORTS=UDPv4 PYTHONNOUSERSITE=1
export LD_LIBRARY_PATH="${ROS}/lib:${LD_LIBRARY_PATH:-}"
export PYTHONPATH="${ROOT}/sim:${ROS}/rclpy"
exec "${ENV}/bin/python" -m isaac_twin.receiver "$@"
