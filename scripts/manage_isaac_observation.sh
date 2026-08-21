#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  echo "Usage: $0 <start|stop|restart|status> <omy|rb5> [run-seconds]"
  echo "Starts a bounded, read-only Isaac ROS 2 observation publisher."
}

if [[ $# -lt 2 || $# -gt 3 ]]; then
  usage >&2
  exit 2
fi

action="$1"
profile="$2"
duration_s="${3:-${SANG_ISAAC_RUN_SECONDS:-7200}}"
if [[ "$action" != "start" && "$action" != "stop" \
  && "$action" != "restart" && "$action" != "status" ]]; then
  usage >&2
  exit 2
fi
if [[ "$profile" != "omy" && "$profile" != "rb5" ]]; then
  echo "Unknown profile: $profile (expected omy or rb5)" >&2
  exit 2
fi
if [[ ! "$duration_s" =~ ^[1-9][0-9]*$ ]] || (( duration_s > 86400 )); then
  echo "run-seconds must be an integer from 1 to 86400" >&2
  exit 2
fi

runtime_root="${SANG_ISAAC_RUNTIME_ROOT:-${ROOT}/runtime/isaac_vendor}"
runtime_dir="${runtime_root}/${profile}"
unit="sang-isaac-observation-${profile}.service"
legacy_pid_file="${runtime_dir}/publisher.pid"

profile_process() {
  local command_line process_path
  for process_path in /proc/[0-9]*/cmdline; do
    [[ -r "$process_path" ]] || continue
    command_line="$(tr '\0' ' ' < "$process_path" 2>/dev/null || true)"
    if [[ "$command_line" == *"isaac_vendor.scene_runner"* \
      && "$command_line" == *"--profile ${profile} "* ]]; then
      basename "$(dirname "$process_path")"
      return 0
    fi
  done
  return 1
}

if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "A running user systemd manager is required for Isaac process ownership." >&2
  exit 1
fi
active_state="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
observed_pid="$(profile_process || true)"
if [[ -f "$legacy_pid_file" && -z "$observed_pid" ]]; then
  rm -f "$legacy_pid_file"
fi

case "$action" in
  start)
    if [[ "$active_state" == "active" || "$active_state" == "activating" ]]; then
      main_pid="$(systemctl --user show "$unit" --property=MainPID --value)"
      echo "Isaac $profile observation publisher is already running (systemd PID $main_pid)."
      exit 0
    fi
    if [[ -n "$observed_pid" ]]; then
      echo "Refusing duplicate $profile publisher; unmanaged PID $observed_pid is running." >&2
      exit 1
    fi
    if [[ "$active_state" == "failed" ]]; then
      systemctl --user reset-failed "$unit"
    fi
    mkdir -p "$runtime_dir"
    chmod 700 "$runtime_dir"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    log_path="${runtime_dir}/publisher-${stamp}.log"
    report_path="${runtime_dir}/report-${stamp}.json"
    startup_timeout_s="${SANG_ISAAC_STARTUP_TIMEOUT_S:-60}"
    if [[ ! "$startup_timeout_s" =~ ^[1-9][0-9]*$ ]] \
      || (( startup_timeout_s > 300 )); then
      echo "SANG_ISAAC_STARTUP_TIMEOUT_S must be an integer from 1 to 300" >&2
      exit 2
    fi
    printf '%s\n' "$log_path" > "${runtime_dir}/latest-log.path"
    printf '%s\n' "$report_path" > "${runtime_dir}/latest-report.path"
    chmod 600 "${runtime_dir}/latest-log.path" "${runtime_dir}/latest-report.path"
    systemd-run --user \
      --unit "$unit" \
      --collect \
      --description "SANG read-only Isaac $profile observation publisher" \
      --property Type=exec \
      --property "RuntimeMaxSec=$((duration_s + 300))" \
      --property TimeoutStopSec=30 \
      --property KillSignal=SIGTERM \
      --property "WorkingDirectory=${ROOT}" \
      --property "StandardOutput=append:${log_path}" \
      --property "StandardError=append:${log_path}" \
      --setenv "SANG_ISAAC_ROS_DOMAIN_ID=${SANG_ISAAC_ROS_DOMAIN_ID:-99}" \
      --setenv "SANG_ISAAC_DISCOVERY_RANGE=${SANG_ISAAC_DISCOVERY_RANGE:-LOCALHOST}" \
      "${ROOT}/scripts/run_vendor_isaac_scene.sh" "$profile" \
        --publish-ros \
        --run-seconds "$duration_s" \
        --report "$report_path"
    ready=false
    completed=false
    for _ in $(seq 1 "$startup_timeout_s"); do
      active_state="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
      marker_seen=false
      if grep -q '^SANG_ISAAC_OBSERVATION_READY=' "$log_path"; then
        marker_seen=true
      fi
      if [[ "$marker_seen" == true \
        && ( "$active_state" == "active" || "$active_state" == "activating" ) ]]; then
        ready=true
        break
      fi
      if [[ "$active_state" != "active" && "$active_state" != "activating" ]]; then
        if [[ "$marker_seen" == true && -f "$report_path" ]] \
          && grep -Eq '"status"[[:space:]]*:[[:space:]]*"passed"' "$report_path"; then
          completed=true
        fi
        break
      fi
      sleep 1
    done
    if [[ "$completed" == true ]]; then
      echo "Isaac $profile observation publisher completed its ${duration_s}s run."
      echo "Log: $log_path"
      echo "Report: $report_path"
      exit 0
    fi
    if [[ "$ready" != true ]]; then
      systemctl --user stop "$unit" 2>/dev/null || true
      tail -80 "$log_path" >&2 || true
      echo "Isaac $profile observation publisher did not become ready within ${startup_timeout_s}s." >&2
      exit 1
    fi
    main_pid="$(systemctl --user show "$unit" --property=MainPID --value)"
    echo "Isaac $profile observation publisher started (systemd PID $main_pid, ${duration_s}s)."
    echo "Log: $log_path"
    echo "Report: $report_path"
    ;;
  stop)
    if [[ "$active_state" != "active" && "$active_state" != "activating" ]]; then
      if [[ -n "$observed_pid" ]]; then
        echo "Refusing to stop unmanaged $profile publisher PID $observed_pid." >&2
        exit 1
      fi
      if [[ "$active_state" == "failed" ]]; then
        systemctl --user reset-failed "$unit"
      fi
      echo "Isaac $profile observation publisher is not running."
      exit 0
    fi
    systemctl --user stop "$unit"
    surviving_pid="$(profile_process || true)"
    if [[ -n "$surviving_pid" ]]; then
      echo "Managed unit stopped, but an unmanaged $profile publisher PID $surviving_pid remains." >&2
      exit 1
    fi
    echo "Isaac $profile observation publisher stopped."
    ;;
  restart)
    "$0" stop "$profile" "$duration_s"
    exec "$0" start "$profile" "$duration_s"
    ;;
  status)
    if [[ "$active_state" == "active" || "$active_state" == "activating" ]]; then
      main_pid="$(systemctl --user show "$unit" --property=MainPID --value)"
      echo "Isaac $profile observation publisher is running (systemd PID $main_pid)."
      exit 0
    fi
    if [[ -n "$observed_pid" ]]; then
      echo "Isaac $profile observation publisher is running unmanaged (PID $observed_pid)."
      exit 1
    fi
    if [[ "$active_state" == "failed" ]]; then
      echo "Isaac $profile observation publisher failed; inspect: journalctl --user -u $unit" >&2
      exit 1
    fi
    echo "Isaac $profile observation publisher is stopped."
    ;;
esac
