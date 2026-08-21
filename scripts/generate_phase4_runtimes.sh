#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# -ne 1 ]]; then
  printf 'usage: %s /absolute/path/to/runtime-inventory.yaml\n' "$0" >&2
  exit 2
fi

WORKSPACE="${SANG_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
INVENTORY="$(realpath "$1")"
: "${SANG_PKI_ROOT:?set SANG_PKI_ROOT to the Central PKI directory}"
: "${SANG_CENTRAL_DATA:?set SANG_CENTRAL_DATA to the Central data directory}"
RUNTIME_ROOT="$(realpath -m "${SANG_PHASE4_RUNTIME:-${WORKSPACE}/runtime/phase4}")"
IMAGE="${SANG_IMAGE:-sang-fleet:jazzy}"
UID_VALUE="${SANG_UID:-$(id -u)}"
GID_VALUE="${SANG_GID:-$(id -g)}"

if [[ ! -f "${INVENTORY}" ]]; then
  printf 'runtime inventory not found: %s\n' "${INVENTORY}" >&2
  exit 2
fi
if [[ ! -f "${SANG_PKI_ROOT}/state.json" ]]; then
  printf 'Central PKI state not found: %s/state.json\n' "${SANG_PKI_ROOT}" >&2
  exit 2
fi
if [[ ! -f "${SANG_CENTRAL_DATA}/fleet.db" ]]; then
  printf 'Central Fleet DB not found: %s/fleet.db\n' "${SANG_CENTRAL_DATA}" >&2
  exit 2
fi

mkdir -p "${RUNTIME_ROOT}"

docker run --rm \
  --user "${UID_VALUE}:${GID_VALUE}" \
  --volume "${WORKSPACE}:/ws:ro" \
  --volume "${INVENTORY}:/inventory/runtime.yaml:ro" \
  --volume "${SANG_PKI_ROOT}:/pki" \
  --volume "${SANG_CENTRAL_DATA}:/central" \
  --volume "${RUNTIME_ROOT}:/runtime" \
  --env PYTHONPATH=/ws/src/sang_security:/ws/src/fleet_control_plane:/ws/src/robot_agent:/ws/src/sang_opcua_gateway \
  --workdir /ws \
  "${IMAGE}" \
  python3 -m fleet_control_plane.runtime_deploy \
    --inventory /inventory/runtime.yaml \
    --workspace-root /ws \
    --runtime-root /runtime \
    --pki-root /pki \
    --db-path /central/fleet.db \
    --host-workspace-root "${WORKSPACE}" \
    --host-runtime-root "${RUNTIME_ROOT}" \
    --host-pki-root "${SANG_PKI_ROOT}" \
    --image "${IMAGE}" \
    --uid "${UID_VALUE}" \
    --gid "${GID_VALUE}"
