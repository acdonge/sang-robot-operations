#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 4 || $# -gt 5 ]]; then
  echo "usage: SANG_EDGE_UID=<uid> SANG_EDGE_GID=<gid> $0 CENTRAL_HOST CENTRAL_IP EDGE_HOST EDGE_IP [AGENT_ID]" >&2
  exit 2
fi
if [[ -z "${SANG_EDGE_UID:-}" || -z "${SANG_EDGE_GID:-}" ]]; then
  echo "set SANG_EDGE_UID and SANG_EDGE_GID from 'id -u' and 'id -g' on the Edge laptop" >&2
  exit 2
fi
if [[ ! "${SANG_EDGE_UID}" =~ ^[0-9]+$ || ! "${SANG_EDGE_GID}" =~ ^[0-9]+$ ]]; then
  echo "SANG_EDGE_UID and SANG_EDGE_GID must be numeric" >&2
  exit 2
fi

CENTRAL_HOST="$1"
CENTRAL_IP="$2"
EDGE_HOST="$3"
EDGE_IP="$4"
AGENT_ID="${5:-workcell-soarm-01}"
AGENT_NAME="${SANG_AGENT_NAME:-SO-ARM Edge Laptop}"
ROBOT_ID="${SANG_ROBOT_ID:-bi_so101_01}"
ENABLE_OPCUA="${SANG_ENABLE_OPCUA:-true}"
IMAGE="${SANG_IMAGE:-sang-fleet:jazzy}"
CENTRAL_ADMIN_PORT_VALUE="${CENTRAL_ADMIN_PORT:-8090}"
CENTRAL_INGRESS_PORT_VALUE="${CENTRAL_INGRESS_PORT:-8443}"
EDGE_OPCUA_PORT_VALUE="${EDGE_OPCUA_PORT:-4840}"
EDGE_PREVIEW_PORT_VALUE="${EDGE_PREVIEW_PORT:-8101}"
ROS_DOMAIN_ID_VALUE="${ROS_DOMAIN_ID:-42}"
WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT="${SANG_PHASE3_RUNTIME:-${WORKSPACE}/runtime/phase3}"
SECRET_ROOT="${RUNTIME_ROOT}/pki-${AGENT_ID}"
CENTRAL_DATA="${RUNTIME_ROOT}/central"
EDGE_DATA="${RUNTIME_ROOT}/edge"
CONFIG_DIR="${RUNTIME_ROOT}/config"
UID_VALUE="$(id -u)"
GID_VALUE="$(id -g)"
EDGE_UID_VALUE="${SANG_EDGE_UID}"
EDGE_GID_VALUE="${SANG_EDGE_GID}"
EDGE_DIALOUT_GID_VALUE="${SANG_EDGE_DIALOUT_GID:-20}"
EDGE_VIDEO_GID_VALUE="${SANG_EDGE_VIDEO_GID:-44}"
PYTHONPATH_VALUE="/ws/src/sang_security:/ws/src/fleet_control_plane:/ws/src/robot_agent:/ws/src/sang_opcua_gateway"
EDGE_WORKSPACE_VALUE="${SANG_EDGE_WORKSPACE:-${WORKSPACE}}"
EDGE_DATA_VALUE="${SANG_EDGE_DATA_PATH:-${EDGE_DATA}}"
EDGE_SECRETS_VALUE="${SANG_EDGE_SECRETS_PATH:-${SECRET_ROOT}/roles/edge}"

if [[ "${ENABLE_OPCUA}" != "true" && "${ENABLE_OPCUA}" != "false" ]]; then
  echo "SANG_ENABLE_OPCUA must be true or false" >&2
  exit 2
fi

if [[ ! "${EDGE_DIALOUT_GID_VALUE}" =~ ^[0-9]+$ \
  || ! "${EDGE_VIDEO_GID_VALUE}" =~ ^[0-9]+$ ]]; then
  echo "SANG_EDGE_DIALOUT_GID and SANG_EDGE_VIDEO_GID must be numeric" >&2
  exit 2
fi

if [[ ! "${ROS_DOMAIN_ID_VALUE}" =~ ^(0|[1-9][0-9]{0,2})$ ]] \
  || (( 10#${ROS_DOMAIN_ID_VALUE} > 232 )); then
  echo "ROS_DOMAIN_ID must be an integer between 0 and 232" >&2
  exit 2
fi

if [[ -e "${SECRET_ROOT}" ]]; then
  if [[ ! -f "${SECRET_ROOT}/state.json" ]]; then
    echo "refusing PKI directory without recoverable state: ${SECRET_ROOT}" >&2
    exit 1
  fi
  echo "resuming verified PKI bootstrap: ${SECRET_ROOT}" >&2
fi
if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
  echo "missing image ${IMAGE}; build it with:" >&2
  echo "  docker build -t ${IMAGE} -f docker/Dockerfile ." >&2
  exit 1
fi

mkdir -p "${RUNTIME_ROOT}" "${CENTRAL_DATA}" "${EDGE_DATA}" "${CONFIG_DIR}"
chmod 700 "${RUNTIME_ROOT}" "${CENTRAL_DATA}" "${EDGE_DATA}" "${CONFIG_DIR}"

ENROLL_COMMAND=(
  python3 -m fleet_control_plane.security_enroll
  --db-path /data/fleet.db
  --role-metadata /run/secrets/sang/role-metadata.json
  --display-name "${AGENT_NAME}"
  --central-ingress-url "https://${CENTRAL_IP}:${CENTRAL_INGRESS_PORT_VALUE}"
)
if [[ "${ENABLE_OPCUA}" == "true" ]]; then
  ENROLL_COMMAND+=(
    --edge-opcua-endpoint
    "opc.tcp://${EDGE_IP}:${EDGE_OPCUA_PORT_VALUE}/sang/"
  )
fi

docker run --rm \
  --user "${UID_VALUE}:${GID_VALUE}" \
  -e "PYTHONPATH=${PYTHONPATH_VALUE}" \
  -v "${WORKSPACE}:/ws:ro" \
  -v "${RUNTIME_ROOT}:/runtime" \
  "${IMAGE}" \
  python3 -m sang_security.pki bootstrap-lab \
  --root "/runtime/pki-${AGENT_ID}" \
  --central-host "${CENTRAL_HOST}" \
  --central-ip "${CENTRAL_IP}" \
  --edge-host "${EDGE_HOST}" \
  --edge-ip "${EDGE_IP}" \
  --agent-id "${AGENT_ID}"

docker run --rm \
  --user "${UID_VALUE}:${GID_VALUE}" \
  -e "PYTHONPATH=${PYTHONPATH_VALUE}" \
  -v "${WORKSPACE}:/ws:ro" \
  -v "${CENTRAL_DATA}:/data" \
  -v "${SECRET_ROOT}/roles/central:/run/secrets/sang:ro" \
  "${IMAGE}" \
  "${ENROLL_COMMAND[@]}"

cat >"${CONFIG_DIR}/central.env" <<EOF
SANG_IMAGE=${IMAGE}
SANG_UID=${UID_VALUE}
SANG_GID=${GID_VALUE}
SANG_WORKSPACE=${WORKSPACE}
SANG_CENTRAL_DATA=${CENTRAL_DATA}
SANG_CENTRAL_SECRETS=${SECRET_ROOT}/roles/central
SANG_AGENT_ID=${AGENT_ID}
SANG_ROBOT_ID=${ROBOT_ID}
CENTRAL_ADMIN_PORT=${CENTRAL_ADMIN_PORT_VALUE}
CENTRAL_INGRESS_PORT=${CENTRAL_INGRESS_PORT_VALUE}
SHADOW_DURATION_S=60
SHADOW_FINAL_DURATION_S=7200
EOF

cat >"${CONFIG_DIR}/edge.env" <<EOF
SANG_IMAGE=${IMAGE}
SANG_UID=${EDGE_UID_VALUE}
SANG_GID=${EDGE_GID_VALUE}
SANG_DIALOUT_GID=${EDGE_DIALOUT_GID_VALUE}
SANG_VIDEO_GID=${EDGE_VIDEO_GID_VALUE}
SANG_WORKSPACE=${EDGE_WORKSPACE_VALUE}
SANG_EDGE_DATA=${EDGE_DATA_VALUE}
SANG_EDGE_SECRETS=${EDGE_SECRETS_VALUE}
SANG_AGENT_ID=${AGENT_ID}
SANG_AGENT_NAME=${AGENT_NAME}
LOCAL_HEALTH_KEY_ID=${AGENT_ID}-health-01
CENTRAL_INGRESS_URL=https://${CENTRAL_IP}:${CENTRAL_INGRESS_PORT_VALUE}
EDGE_PREVIEW_PORT=${EDGE_PREVIEW_PORT_VALUE}
ROS_DOMAIN_ID=${ROS_DOMAIN_ID_VALUE}
ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
EOF
if [[ "${ENABLE_OPCUA}" == "true" ]]; then
  echo "EDGE_OPCUA_ENDPOINT=opc.tcp://${EDGE_IP}:${EDGE_OPCUA_PORT_VALUE}/sang/" >>"${CONFIG_DIR}/edge.env"
fi
chmod 600 "${CONFIG_DIR}/central.env" "${CONFIG_DIR}/edge.env"

echo "Phase 3 bootstrap complete"
echo "Central env: ${CONFIG_DIR}/central.env"
echo "Edge env:    ${CONFIG_DIR}/edge.env"
echo "Copy only ${SECRET_ROOT}/roles/edge and edge.env to the Edge laptop."
