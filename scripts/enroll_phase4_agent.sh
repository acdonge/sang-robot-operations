#!/usr/bin/env bash
set -euo pipefail
umask 077

WORKSPACE="${SANG_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
: "${SANG_PKI_ROOT:?set SANG_PKI_ROOT}"
: "${SANG_CENTRAL_DATA:?set SANG_CENTRAL_DATA}"
: "${SANG_AGENT_ID:?set SANG_AGENT_ID}"
: "${SANG_AGENT_NAME:?set SANG_AGENT_NAME}"
: "${CENTRAL_INGRESS_URL:?set CENTRAL_INGRESS_URL}"

IMAGE="${SANG_IMAGE:-sang-fleet:jazzy}"
UID_VALUE="${SANG_UID:-$(id -u)}"
GID_VALUE="${SANG_GID:-$(id -g)}"
PYTHONPATH_VALUE="/ws/src/sang_security:/ws/src/fleet_control_plane:/ws/src/robot_agent"

PKI_COMMAND=(
  provision-agent
  --root /pki
  --agent-id "${SANG_AGENT_ID}"
)
ENROLL_RUNTIME_ARGS=()
if [[ -n "${SANG_EDGE_HOST:-}" || -n "${EDGE_OPCUA_ENDPOINT:-}" ]]; then
  if [[ -z "${SANG_EDGE_HOST:-}" || -z "${EDGE_OPCUA_ENDPOINT:-}" ]]; then
    printf 'SANG_EDGE_HOST and EDGE_OPCUA_ENDPOINT must be set together\n' >&2
    exit 2
  fi
  PKI_COMMAND=(
    provision-runtime
    --root /pki
    --agent-id "${SANG_AGENT_ID}"
    --edge-host "${SANG_EDGE_HOST}"
  )
  ENROLL_RUNTIME_ARGS=(
    --edge-opcua-endpoint "${EDGE_OPCUA_ENDPOINT}"
    --runtime-metadata "/pki/roles/agents/${SANG_AGENT_ID}/central/runtime-metadata.json"
  )
fi

docker run --rm \
  --user "${UID_VALUE}:${GID_VALUE}" \
  --volume "${WORKSPACE}:/ws:ro" \
  --volume "${SANG_PKI_ROOT}:/pki" \
  --env "PYTHONPATH=${PYTHONPATH_VALUE}" \
  --workdir /ws \
  "${IMAGE}" \
  python3 -m sang_security.pki "${PKI_COMMAND[@]}"

docker run --rm \
  --user "${UID_VALUE}:${GID_VALUE}" \
  --volume "${WORKSPACE}:/ws:ro" \
  --volume "${SANG_PKI_ROOT}:/pki:ro" \
  --volume "${SANG_CENTRAL_DATA}:/central-data" \
  --env "PYTHONPATH=${PYTHONPATH_VALUE}" \
  --workdir /ws \
  "${IMAGE}" \
  python3 -m fleet_control_plane.security_enroll \
    --db-path /central-data/fleet.db \
    --role-metadata "/pki/roles/agents/${SANG_AGENT_ID}/central/role-metadata.json" \
    --display-name "${SANG_AGENT_NAME}" \
    --central-ingress-url "${CENTRAL_INGRESS_URL}" \
    "${ENROLL_RUNTIME_ARGS[@]}"
