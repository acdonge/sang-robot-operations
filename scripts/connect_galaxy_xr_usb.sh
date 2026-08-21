#!/usr/bin/env bash
set -euo pipefail

XR_PORT="${XR_PORT:-5556}"
XR_DESKTOP_PORT="${XR_DESKTOP_PORT:-15557}"

fail() {
    echo "ERROR: $1" >&2
    exit 1
}

[[ "${XR_PORT}" =~ ^[0-9]+$ ]] || fail "XR_PORT must be an integer"
(( XR_PORT >= 1 && XR_PORT <= 65535 )) || \
    fail "XR_PORT must be between 1 and 65535"
[[ "${XR_DESKTOP_PORT}" =~ ^[0-9]+$ ]] || \
    fail "XR_DESKTOP_PORT must be an integer"
(( XR_DESKTOP_PORT >= 1 && XR_DESKTOP_PORT <= 65535 )) || \
    fail "XR_DESKTOP_PORT must be between 1 and 65535"
(( XR_DESKTOP_PORT != XR_PORT )) || \
    fail "XR_DESKTOP_PORT must differ from XR_PORT"

if [[ -n "${ADB_BIN:-}" ]]; then
    ADB="${ADB_BIN}"
else
    SCRCPY_BIN="$(command -v scrcpy || true)"
    BUNDLED_ADB=""
    if [[ -n "${SCRCPY_BIN}" ]]; then
        BUNDLED_ADB="$(dirname "$(readlink -f "${SCRCPY_BIN}")")/adb"
    fi
    if [[ -n "${BUNDLED_ADB}" && -x "${BUNDLED_ADB}" ]]; then
        ADB="${BUNDLED_ADB}"
    else
        ADB="$(command -v adb || true)"
    fi
fi
[[ -x "${ADB}" ]] || fail "adb is not installed"

"${ADB}" start-server >/dev/null

if [[ -n "${ANDROID_SERIAL:-}" ]]; then
    SERIAL="${ANDROID_SERIAL}"
    STATE="$("${ADB}" -s "${SERIAL}" get-state 2>/dev/null || true)"
    [[ "${STATE}" == "device" ]] || \
        fail "ANDROID_SERIAL=${SERIAL} is not an authorized online device"
else
    mapfile -t DEVICES < <("${ADB}" devices | awk 'NR > 1 && $2 == "device" {print $1}')
    if (( ${#DEVICES[@]} == 0 )); then
        "${ADB}" devices -l >&2
        fail "no authorized Galaxy XR found; connect USB and approve the headset RSA prompt"
    fi
    if (( ${#DEVICES[@]} > 1 )); then
        "${ADB}" devices -l >&2
        fail "multiple devices found; set ANDROID_SERIAL to the Galaxy XR serial"
    fi
    SERIAL="${DEVICES[0]}"
fi

"${ADB}" -s "${SERIAL}" reverse "tcp:${XR_PORT}" "tcp:${XR_PORT}"
"${ADB}" -s "${SERIAL}" reverse \
    "tcp:${XR_DESKTOP_PORT}" "tcp:${XR_DESKTOP_PORT}"
REVERSE_LIST="$("${ADB}" -s "${SERIAL}" reverse --list)"
for PORT in "${XR_PORT}" "${XR_DESKTOP_PORT}"; do
    if ! awk -v endpoint="tcp:${PORT}" \
        '$2 == endpoint && $3 == endpoint {found = 1} END {exit !found}' \
        <<<"${REVERSE_LIST}"; then
        printf '%s\n' "${REVERSE_LIST}" >&2
        fail "adb reverse rule for tcp:${PORT} was not reported after setup"
    fi
done

MODEL="$("${ADB}" -s "${SERIAL}" shell getprop ro.product.model 2>/dev/null | tr -d '\r')"
echo "Galaxy XR USB tunnel ready: ${MODEL:-unknown model} (${SERIAL})"
echo "Telemetry: headset localhost:${XR_PORT} -> PC localhost:${XR_PORT}"
echo "Desktop: headset localhost:${XR_DESKTOP_PORT} -> PC localhost:${XR_DESKTOP_PORT}"
