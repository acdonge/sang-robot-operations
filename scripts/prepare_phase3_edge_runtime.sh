#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 /absolute/path/to/edge.env" >&2
  exit 2
fi

ENV_FILE="$1"
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "edge environment file not found: ${ENV_FILE}" >&2
  exit 2
fi

read_env_value() {
  local key="$1"
  local line
  line="$(grep -m1 -E "^${key}=" "${ENV_FILE}" || true)"
  if [[ -z "${line}" ]]; then
    echo "missing ${key} in ${ENV_FILE}" >&2
    return 1
  fi
  printf '%s' "${line#*=}"
}

EDGE_DATA="$(read_env_value SANG_EDGE_DATA)"
EDGE_UID="$(read_env_value SANG_UID)"
EDGE_GID="$(read_env_value SANG_GID)"

if [[ "${EDGE_DATA}" != /* ]]; then
  echo "SANG_EDGE_DATA must be an absolute path" >&2
  exit 2
fi
if [[ ! "${EDGE_UID}" =~ ^[0-9]+$ || ! "${EDGE_GID}" =~ ^[0-9]+$ ]]; then
  echo "SANG_UID and SANG_GID must be numeric" >&2
  exit 2
fi

if [[ "$(id -u)" == "${EDGE_UID}" && "$(id -g)" == "${EDGE_GID}" ]]; then
  install -d -m 0700 "${EDGE_DATA}" "${EDGE_DATA}/captures"
elif [[ "$(id -u)" == "0" ]]; then
  install -d -m 0700 -o "${EDGE_UID}" -g "${EDGE_GID}" \
    "${EDGE_DATA}" "${EDGE_DATA}/captures"
else
  echo "run as UID:GID ${EDGE_UID}:${EDGE_GID}, or run with sudo" >&2
  exit 1
fi

ACTUAL_OWNER="$(stat -c '%u:%g' "${EDGE_DATA}/captures")"
if [[ "${ACTUAL_OWNER}" != "${EDGE_UID}:${EDGE_GID}" ]]; then
  echo "captures owner is ${ACTUAL_OWNER}; expected ${EDGE_UID}:${EDGE_GID}" >&2
  exit 1
fi

echo "prepared ${EDGE_DATA}/captures for UID:GID ${EDGE_UID}:${EDGE_GID}"
