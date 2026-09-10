#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV="${HOME}/miniconda3/envs/leisaac"
ROS="${ENV}/lib/python3.11/site-packages/isaacsim/exts/isaacsim.ros2.bridge/jazzy"
if [[ "${IITP_RB5_SIM_TO_REAL:-0}" != 0 || "${IITP_OMY_SIM_TO_REAL:-0}" != 0 ]]; then
  echo "Isaac Scenario 1 receiver refuses vendor-output arm flags" >&2
  exit 1
fi
export IITP_RB5_SIM_TO_REAL=0 IITP_OMY_SIM_TO_REAL=0
export ROS_DISTRO=jazzy ROS_DOMAIN_ID=98 ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
export ROS_LOCALHOST_ONLY=1
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp RMW_FASTRTPS_PUBLICATION_MODE=ASYNCHRONOUS
export FASTDDS_BUILTIN_TRANSPORTS=UDPv4 PYTHONNOUSERSITE=1
unset ROS_STATIC_PEERS ROS_DISCOVERY_SERVER FASTRTPS_DEFAULT_PROFILES_FILE FASTDDS_DEFAULT_PROFILES_FILE
export LD_LIBRARY_PATH="${ROS}/lib:${LD_LIBRARY_PATH:-}"
export PYTHONPATH="${ROOT}/sim:${ROS}/rclpy"
# The receiver contract fixes /tmp/sang-isaac-scenario1; no root override is exposed.
exec "${ENV}/bin/python" -m isaac_scenario1.receiver "$@"
