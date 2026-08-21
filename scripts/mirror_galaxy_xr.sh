#!/usr/bin/env bash
set -euo pipefail

WINDOW_TITLE="Galaxy XR Mirror"
MAX_SIZE="${XR_MIRROR_MAX_SIZE:-1920}"
MAX_FPS="${XR_MIRROR_MAX_FPS:-30}"

show_error() {
    local message="$1"
    if [[ "${XR_MIRROR_GUI:-0}" == "1" ]]; then
        if command -v zenity >/dev/null; then
            zenity --error --title="${WINDOW_TITLE}" --text="${message}" || true
        elif command -v notify-send >/dev/null; then
            notify-send --urgency=critical "${WINDOW_TITLE}" "${message}" || true
        fi
    fi
}

fail() {
    echo "ERROR: $1" >&2
    show_error "$1"
    exit 1
}

[[ "${MAX_SIZE}" =~ ^[1-9][0-9]*$ ]] || fail "XR_MIRROR_MAX_SIZE must be a positive integer."
[[ "${MAX_FPS}" =~ ^[1-9][0-9]*$ ]] || fail "XR_MIRROR_MAX_FPS must be a positive integer."
command -v flock >/dev/null || fail "flock is not installed."

SCRCPY_BIN="${SCRCPY_BIN:-$(command -v scrcpy || true)}"
if [[ -z "${SCRCPY_BIN}" ]]; then
    SCRCPY_BIN="${HOME}/.local/bin/scrcpy"
fi
[[ -x "${SCRCPY_BIN}" ]] || fail "scrcpy is not installed in PATH or ~/.local/bin."

if [[ -n "${ADB_BIN:-}" ]]; then
    ADB="${ADB_BIN}"
else
    BUNDLED_ADB="$(dirname "$(readlink -f "${SCRCPY_BIN}")")/adb"
    if [[ -x "${BUNDLED_ADB}" ]]; then
        ADB="${BUNDLED_ADB}"
    else
        ADB="$(command -v adb || true)"
    fi
fi
[[ -x "${ADB}" ]] || fail "ADB is not installed."

LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/galaxy-xr-mirror-${UID}.lock"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    if [[ "${XR_MIRROR_GUI:-0}" == "1" ]] && command -v notify-send >/dev/null; then
        notify-send "${WINDOW_TITLE}" "The mirror window is already running." || true
    fi
    exit 0
fi

"${ADB}" start-server >/dev/null 9>&-

find_galaxy_xr_devices() {
    local serial model
    GALAXY_DEVICES=()
    mapfile -t ONLINE_DEVICES < <(
        "${ADB}" devices 9>&- |
            awk 'NR > 1 && $2 == "device" {print $1}'
    )
    for serial in "${ONLINE_DEVICES[@]}"; do
        model="$(
            "${ADB}" -s "${serial}" shell getprop ro.product.model \
                2>/dev/null 9>&- | tr -d '\r'
        )"
        if [[ "${model}" == "SM-I610" ]]; then
            GALAXY_DEVICES+=("${serial}")
        fi
    done
}

connect_galaxy_xr_wifi() {
    local endpoint
    MDNS_OUTPUT="$("${ADB}" mdns services 2>/dev/null 9>&- || true)"
    mapfile -t WIFI_ENDPOINTS < <(
        awk '$2 == "_adb-tls-connect._tcp" {print $3}' <<<"${MDNS_OUTPUT}"
    )
    for endpoint in "${WIFI_ENDPOINTS[@]}"; do
        "${ADB}" connect "${endpoint}" >/dev/null 2>&1 9>&- || true
    done
}

if [[ -n "${ANDROID_SERIAL:-}" ]]; then
    SERIAL="${ANDROID_SERIAL}"
    STATE="$("${ADB}" -s "${SERIAL}" get-state 2>/dev/null 9>&- || true)"
    [[ "${STATE}" == "device" ]] || fail "Galaxy XR ${SERIAL} is not authorized and online."
else
    find_galaxy_xr_devices
    if (( ${#GALAXY_DEVICES[@]} == 0 )); then
        connect_galaxy_xr_wifi
        find_galaxy_xr_devices
    fi
    if (( ${#GALAXY_DEVICES[@]} == 0 )); then
        ADB_DEVICES="$("${ADB}" devices 9>&-)"
        if awk 'NR > 1 && $2 == "unauthorized" {found = 1} END {exit !found}' <<<"${ADB_DEVICES}"; then
            fail "Galaxy XR was found but ADB is not authorized. Approve this PC in the headset."
        fi
        if (( ${#WIFI_ENDPOINTS[@]} > 0 )); then
            fail "Galaxy XR Wi-Fi debugging was found but could not connect. Pair or approve this PC in Wireless debugging."
        fi
        fail "Galaxy XR is not connected. Turn on Wireless debugging on the same Wi-Fi, or connect a data USB-C cable."
    fi
    if (( ${#GALAXY_DEVICES[@]} > 1 )); then
        SERIAL=""
        for candidate in "${GALAXY_DEVICES[@]}"; do
            if [[ "${candidate}" != *:* ]]; then
                SERIAL="${candidate}"
                break
            fi
        done
        [[ -n "${SERIAL}" ]] || \
            fail "Multiple Galaxy XR Wi-Fi devices are connected. Set ANDROID_SERIAL to choose one."
    else
        SERIAL="${GALAXY_DEVICES[0]}"
    fi
fi

MODEL="$("${ADB}" -s "${SERIAL}" shell getprop ro.product.model 2>/dev/null 9>&- | tr -d '\r')"
if [[ "${SERIAL}" == *:* ]]; then
    CONNECTION="Wi-Fi"
else
    CONNECTION="USB"
fi
echo "Opening ${WINDOW_TITLE} for ${MODEL:-Android device} over ${CONNECTION} (${SERIAL})"

if ! ADB="${ADB}" "${SCRCPY_BIN}" \
    --serial "${SERIAL}" \
    --max-size "${MAX_SIZE}" \
    --max-fps "${MAX_FPS}" \
    --no-audio \
    --keep-active \
    --window-title="${WINDOW_TITLE}" 9>&-; then
    fail "The mirror stopped unexpectedly. Check the USB/Wi-Fi ADB connection and authorization."
fi
