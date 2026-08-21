#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
XR_DESKTOP_PORT="${XR_DESKTOP_PORT:-15557}"

exec python3 "${SCRIPT_DIR}/stream_galaxy_xr_desktop.py" \
    --port "${XR_DESKTOP_PORT}" "$@"
