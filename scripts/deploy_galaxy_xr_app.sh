#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_DIR="${GALAXY_XR_APP_DIR:-${WS_ROOT}/../oculus_reader/app}"
PACKAGE="com.sa08050.galaxyxr.teleop"
ACTIVITY="${PACKAGE}.MainActivity"
XR_PORT="${XR_PORT:-5556}"
XR_DESKTOP_PORT="${XR_DESKTOP_PORT:-15557}"

fail() {
    echo "ERROR: $1" >&2
    exit 1
}

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

[[ -x "${APP_DIR}/gradlew" ]] || \
    fail "Galaxy XR app was not found at ${APP_DIR}"

ADB_BIN="${ADB}" \
XR_PORT="${XR_PORT}" \
XR_DESKTOP_PORT="${XR_DESKTOP_PORT}" \
    "${SCRIPT_DIR}/connect_galaxy_xr_usb.sh"

if [[ -n "${ANDROID_SERIAL:-}" ]]; then
    SERIAL="${ANDROID_SERIAL}"
else
    mapfile -t DEVICES < <("${ADB}" devices | awk 'NR > 1 && $2 == "device" {print $1}')
    (( ${#DEVICES[@]} == 1 )) || fail "expected exactly one authorized Android device"
    SERIAL="${DEVICES[0]}"
fi

echo "Building Galaxy XR sender APK..."
(
    cd "${APP_DIR}"
    ./gradlew assembleDebug --no-daemon
)

APK="${APP_DIR}/build/outputs/apk/debug/GalaxyXRTeleop-debug.apk"
[[ -f "${APK}" ]] || fail "Gradle did not produce ${APK}"

"${ADB}" -s "${SERIAL}" install --no-streaming -r "${APK}"
"${ADB}" -s "${SERIAL}" shell setprop debug.xr_teleop.host 127.0.0.1
"${ADB}" -s "${SERIAL}" shell setprop debug.xr_teleop.port "${XR_PORT}"
"${ADB}" -s "${SERIAL}" shell setprop debug.xr_desktop.host 127.0.0.1
"${ADB}" -s "${SERIAL}" shell setprop debug.xr_desktop.port "${XR_DESKTOP_PORT}"
if ! "${ADB}" -s "${SERIAL}" shell pm grant \
    "${PACKAGE}" android.permission.HAND_TRACKING; then
    echo "WARNING: approve the hand-tracking permission prompt in the headset." >&2
fi
"${ADB}" -s "${SERIAL}" shell am force-stop "${PACKAGE}"
"${ADB}" -s "${SERIAL}" shell am start -W -n "${PACKAGE}/${ACTIVITY}"

echo "Galaxy XR sender launched. The bridge terminal should change to 'XR input fresh'."
echo "Desktop stream: ${SCRIPT_DIR}/run_galaxy_xr_desktop_stream.sh"
echo "Device logs: adb -s ${SERIAL} logcat -s GalaxyXRTeleop GalaxyXRInputViz GalaxyXRDesktop"
