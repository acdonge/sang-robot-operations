#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IMAGE:-recorder-core:jazzy}"
LEROBOT_REF="${LEROBOT_REF:-v0.6.0}"
STORE_ROOT="${STORE_ROOT:-/home/sa08050/robot_store}"

docker build \
  --build-arg "LEROBOT_REF=${LEROBOT_REF}" \
  -t "${IMAGE}" \
  -f docker/Dockerfile \
  .

docker run --rm -it \
  -v "$PWD":/ws \
  -v "${STORE_ROOT}":"${STORE_ROOT}" \
  "${IMAGE}" \
  bash

