#!/usr/bin/env bash

rb5_show_error() {
  local message="$1"
  if command -v zenity >/dev/null 2>&1 && [[ -n "${DISPLAY:-}" ]]; then
    zenity --error --title="RB5 control" --text="${message}"
  else
    printf 'RB5 control: %s\n' "${message}" >&2
  fi
}

rb5_require_docker() {
  if docker info >/dev/null 2>&1; then
    return
  fi

  if [[ "${RB5_DOCKER_GROUP_REEXEC:-0}" != "1" ]] \
    && getent group docker | cut -d: -f4 | tr ',' '\n' \
      | grep -Fxq "$(id -un)"; then
    exec sg docker -c \
      "RB5_DOCKER_GROUP_REEXEC=1 $(printf '%q' "$0")"
  fi

  rb5_show_error \
    "Cannot access Docker. Log out and back in once, then retry."
  exit 1
}

rb5_login_gid() {
  getent passwd "$(id -u)" | cut -d: -f4
}
