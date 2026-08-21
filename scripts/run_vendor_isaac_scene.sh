#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ROOT="$(cd "${ROOT}/.." && pwd)"
CONDA_ENV="${ISAAC_CONDA_ENV:-${HOME}/miniconda3/envs/leisaac}"
LEISAAC_ROOT="${LEISAAC_ROOT:-${APP_ROOT}/leisaac}"

if [[ $# -lt 1 || "$1" == "-h" || "$1" == "--help" ]]; then
  echo "Usage: $0 <omy|rb5> [--gui] [--exercise] [--publish-ros --run-seconds N] [--report PATH]"
  echo "Default mode is headless load validation. Add --exercise for bounded motion."
  echo "ROS observation mode uses a dedicated Jazzy domain (default 99) and LOCALHOST discovery."
  exit 0
fi

profile="$1"
shift
if [[ "$profile" != "omy" && "$profile" != "rb5" ]]; then
  echo "Unknown profile: $profile (expected omy or rb5)" >&2
  exit 2
fi
if [[ ! -d "$CONDA_ENV" ]]; then
  echo "Isaac conda environment not found: $CONDA_ENV" >&2
  exit 2
fi
if [[ ! -d "$LEISAAC_ROOT" ]]; then
  echo "LeIsaac workspace not found: $LEISAAC_ROOT" >&2
  exit 2
fi

set +u
source "${HOME}/miniconda3/etc/profile.d/conda.sh"
conda activate "$CONDA_ENV"
set -u
export ACCEPT_EULA=Y
export OMNI_KIT_ACCEPT_EULA=YES
export PYTHONNOUSERSITE=1
export ROS_DISTRO=jazzy
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export RMW_FASTRTPS_PUBLICATION_MODE=ASYNCHRONOUS
export ROS_DOMAIN_ID="${SANG_ISAAC_ROS_DOMAIN_ID:-99}"
export ROS_AUTOMATIC_DISCOVERY_RANGE="${SANG_ISAAC_DISCOVERY_RANGE:-LOCALHOST}"
export FASTDDS_BUILTIN_TRANSPORTS="${FASTDDS_BUILTIN_TRANSPORTS:-UDPv4}"
ROS_BRIDGE_ROOT="${CONDA_ENV}/lib/python3.11/site-packages/isaacsim/exts/isaacsim.ros2.bridge"
if [[ ! -d "${ROS_BRIDGE_ROOT}/jazzy/lib" ]]; then
  echo "Isaac Sim Jazzy ROS bridge not found: ${ROS_BRIDGE_ROOT}" >&2
  exit 2
fi
export LD_LIBRARY_PATH="${ROS_BRIDGE_ROOT}/jazzy/lib:${LD_LIBRARY_PATH:-}"
export PYTHONPATH="${ROOT}/sim"

cd "$LEISAAC_ROOT"
exec python -m isaac_vendor.scene_runner \
  --profiles "${ROOT}/sim/isaac_vendor/profiles.yaml" \
  --profile "$profile" \
  "$@"
