#!/usr/bin/env bash
set -euo pipefail

readonly app_dir="/home/lab1/Applications/Rainbow-RB-8.44/RB_Linux"
readonly executable="${app_dir}/RB_Linux.x86_64"
readonly state_dir="${HOME}/.local/state/rainbow-rb"
readonly log_file="${state_dir}/rainbow-rb.log"

if [[ ! -x "${executable}" ]]; then
  message="Rainbow-RB 8.44 is not installed at ${executable}"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --title="Rainbow-RB" --text="${message}"
  else
    printf '%s\n' "${message}" >&2
  fi
  exit 1
fi

mkdir -p "${state_dir}"
cd "${app_dir}"
exec "${executable}" -logFile "${log_file}"
