#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 LOCAL_EDGE_ROLE EDGE_SSH REMOTE_EDGE_ROLE" >&2
  exit 2
fi

LOCAL_EDGE_ROLE="$(realpath "$1")"
EDGE_SSH="$2"
REMOTE_EDGE_ROLE="$3"
OPCUA_CRL="${LOCAL_EDGE_ROLE}/opcua/crl/opcua-crl.pem"
HTTPS_BUNDLE="${LOCAL_EDGE_ROLE}/https/trust/ca-crl.pem"

if [[ ! -f "${OPCUA_CRL}" || ! -f "${HTTPS_BUNDLE}" ]]; then
  echo "Edge role CRL material is incomplete: ${LOCAL_EDGE_ROLE}" >&2
  exit 1
fi

TEMP_DIR="$(mktemp -d)"
REMOTE_ARCHIVE=""
cleanup() {
  rm -rf "${TEMP_DIR}"
  if [[ -n "${REMOTE_ARCHIVE}" ]]; then
    ssh "${EDGE_SSH}" "rm -f '${REMOTE_ARCHIVE}'" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

sed -n '/-----BEGIN X509 CRL-----/,/-----END X509 CRL-----/p' \
  "${HTTPS_BUNDLE}" >"${TEMP_DIR}/https-crl.pem"
check_crl() {
  local path="$1"
  local next_update
  local next_epoch
  next_update="$(openssl crl -in "${path}" -nextupdate -noout | cut -d= -f2-)"
  next_epoch="$(date -d "${next_update}" +%s)"
  if (( next_epoch < $(date +%s) + 604800 )); then
    echo "CRL expires in less than seven days: ${path}" >&2
    exit 1
  fi
}
check_crl "${OPCUA_CRL}"
check_crl "${TEMP_DIR}/https-crl.pem"
if [[ "${SANG_CRL_SYNC_DRY_RUN:-0}" == "1" ]]; then
  echo "Edge CRLs are valid for at least seven days."
  exit 0
fi

tar -C "${LOCAL_EDGE_ROLE}" -cf "${TEMP_DIR}/crls.tar" \
  opcua/crl/opcua-crl.pem \
  https/trust/ca-crl.pem
REMOTE_ARCHIVE="$(
  ssh "${EDGE_SSH}" \
    'umask 077; mktemp /tmp/sang-phase3-crls.XXXXXXXXXX'
)"
if [[ ! "${REMOTE_ARCHIVE}" =~ ^/tmp/sang-phase3-crls\.[A-Za-z0-9]+$ ]]; then
  echo "Edge returned an invalid temporary archive path" >&2
  exit 1
fi
scp -q "${TEMP_DIR}/crls.tar" "${EDGE_SSH}:${REMOTE_ARCHIVE}"

printf -v REMOTE_ROLE_Q '%q' "${REMOTE_EDGE_ROLE}"
printf -v REMOTE_ARCHIVE_Q '%q' "${REMOTE_ARCHIVE}"
ssh "${EDGE_SSH}" "bash -s -- ${REMOTE_ROLE_Q} ${REMOTE_ARCHIVE_Q}" <<'REMOTE'
set -euo pipefail
role="$1"
archive="$2"
staging=$(mktemp -d "${role}/.crl-update.XXXXXX")
trap 'rm -rf "${staging}"; rm -f "${archive}"' EXIT
tar -xf "${archive}" -C "${staging}"
chmod 0644 "${staging}/opcua/crl/opcua-crl.pem" "${staging}/https/trust/ca-crl.pem"
mv "${staging}/opcua/crl/opcua-crl.pem" "${role}/opcua/crl/opcua-crl.pem"
mv "${staging}/https/trust/ca-crl.pem" "${role}/https/trust/ca-crl.pem"
REMOTE

echo "Edge CRLs updated; reconnect OPC UA clients after the Gateway reload interval."
