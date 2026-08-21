#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IMAGE:-sang-fleet:jazzy}"

docker build \
  -t "${IMAGE}" \
  -f docker/Dockerfile \
  .

docker run --rm -it \
  --network host \
  -v "$PWD":/ws \
  "${IMAGE}" \
  bash
