#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/docker_access.sh"
rb5_require_docker

found=false
for container in sang-rb5-official-control sang-rb5-official-simulation; do
  if docker container inspect "${container}" >/dev/null 2>&1; then
    docker stop --time 10 "${container}"
    found=true
  fi
done

if [[ "${found}" == "false" ]]; then
  zenity --info --title="RB5 control" --text="No RB5 control container is running."
fi
