#!/usr/bin/env bash
# Launch the local (Mac) application with the selected target's configuration.
#
# The real startup command was taken from this repo, not assumed: pyproject.toml
# declares the console script `voice-assistant = voice_assistant.main:main`, and
# README documents `python -m voice_assistant`. The module form is used here so
# the venv interpreter is explicit.

[[ -n "${_VA_APP_SH:-}" ]] && return 0
_VA_APP_SH=1

HANDSHAKE_WATCHDOG_PID=""

# Pick the interpreter: repo venv first (this repo uses venv/, not .venv/),
# then .venv/, then whatever python3 is on PATH.
app_python() {
  if [[ -x "${REPO_ROOT}/venv/bin/python" ]]; then
    printf '%s' "${REPO_ROOT}/venv/bin/python"
  elif [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    printf '%s' "${REPO_ROOT}/.venv/bin/python"
  elif have python3; then
    command -v python3
  else
    die "No Python interpreter found." \
      "Create the virtual environment first (on the Mac):" \
      "    python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
  fi
}

# Is anything already bound to the app's device port on the Mac?
_port_in_use() {
  local port="$1"
  if have lsof; then
    lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1
  else
    netstat -an 2>/dev/null | grep -q "\.${port} .*LISTEN"
  fi
}

# Count established connections to the app's device port. This is how the
# handshake is actually verified: the Pi endpoint dialling in shows up here as
# an ESTABLISHED socket. Nothing is sent and no audio device is touched.
#
# `grep -c` exits 1 when the count is 0, which -- under this launcher's
# `set -Eeuo pipefail` and inherited ERR trap -- would fire the "(failed at
# ...)" reporter on every poll until the device connects. Swallow that: a zero
# count is a normal answer, not an error. Always prints a number, always
# exits 0.
_established_count() {
  local port="$1" count
  count="$(netstat -an 2>/dev/null | grep -c "\.${port} .*ESTABLISHED" || true)"
  count="${count//[^0-9]/}"
  printf '%s' "${count:-0}"
}

# Background watchdog: if no device connects within the timeout, say so with
# something actionable. Killed by the launcher's EXIT trap.
_start_handshake_watchdog() {
  local port="$1" timeout="$2"
  (
    local waited=0
    while [[ ${waited} -lt ${timeout} ]]; do
      sleep 2
      waited=$(( waited + 2 ))
      if [[ "$(_established_count "${port}")" != "0" ]]; then
        printf '\n    %s[handshake] device connected to port %s after %ss%s\n' \
          "${C_GREEN}" "${port}" "${waited}" "${C_RESET}" >&2
        printf '    %sWatch for "session.device_ready" with target=%s below.%s\n' \
          "${C_GREEN}" "${TARGET_NAME}" "${C_RESET}" >&2
        exit 0
      fi
    done
    printf '\n    %s[handshake] no device connected within %ss.%s\n' \
      "${C_YELLOW}" "${timeout}" "${C_RESET}" >&2
    printf '    %sThe app is running and listening; the %s endpoint has not dialled in.%s\n' \
      "${C_YELLOW}" "${TARGET_NAME}" "${C_RESET}" >&2
    printf '    %sCheck the endpoint is pointed at this Mac and see its log:%s\n' \
      "${C_YELLOW}" "${C_RESET}" >&2
    printf '    %s  %s%s\n' "${C_YELLOW}" "$(service_logs_command)" "${C_RESET}" >&2
  ) &
  HANDSHAKE_WATCHDOG_PID=$!
}

stop_handshake_watchdog() {
  if [[ -n "${HANDSHAKE_WATCHDOG_PID}" ]]; then
    kill "${HANDSHAKE_WATCHDOG_PID}" >/dev/null 2>&1 || true
    wait "${HANDSHAKE_WATCHDOG_PID}" 2>/dev/null || true
    HANDSHAKE_WATCHDOG_PID=""
  fi
}

# Steps 9 and 10: start the app in the FOREGROUND so Ctrl+C stops it cleanly.
# Stopping the app never touches the remote systemd service.
start_local_app() {
  local py port mac_ip iface
  port="${TARGET_ENDPOINT_PORT}"

  log_step "Starting the local app on the Mac (target: ${TARGET_NAME})"

  if [[ "${TARGET_USE_SSH_TUNNEL}" == "1" ]]; then
    # The endpoint's ExecStart dials ws://127.0.0.1:<port> on itself; the SSH
    # -R tunnel opened earlier carries that back to this Mac. No Mac IP needed.
    mac_ip="127.0.0.1"
    log_info "This Mac : reached via SSH reverse tunnel (endpoint dials its own 127.0.0.1:${port})"
  else
    iface="$(wifi_interface 2>/dev/null || true)"
    mac_ip="${TARGET_ENDPOINT_HOST}"
    if [[ -z "${mac_ip}" && -n "${iface}" ]]; then
      mac_ip="$(mac_lan_ip "${iface}")"
    fi

    if [[ -n "${mac_ip}" ]]; then
      log_info "This Mac : ${mac_ip}:${port}  <- the ${TARGET_NAME} endpoint must dial this"
    else
      log_warn "Could not determine this Mac's IP on the Wi-Fi interface."
      log_dim "The endpoint needs it to connect. Find it with: ipconfig getifaddr ${iface:-en0}"
    fi
  fi

  if is_dry_run; then
    py="$(app_python 2>/dev/null || printf 'python3')"
    log_info "[dry-run] would export:"
    log_info "[dry-run]   VOICE_ASSISTANT_TARGET=${TARGET_NAME}"
    log_info "[dry-run]   VOICE_ASSISTANT_DEVICE_HOST=${mac_ip:-<auto>}"
    log_info "[dry-run]   VOICE_ASSISTANT_DEVICE_PORT=${port}"
    log_info "[dry-run] would run: ${py} -m voice_assistant --target ${TARGET_NAME} --port ${port}${APP_EXTRA_ARGS:+ ${APP_EXTRA_ARGS}}"
    log_info "[dry-run] the app is NOT started"
    return 0
  fi

  py="$(app_python)"

  if _port_in_use "${port}"; then
    die "Port ${port} on this Mac is already in use." \
      "Another copy of the app is probably still running." \
      "" \
      "Find it:  lsof -nP -iTCP:${port} -sTCP:LISTEN" \
      "Then stop that process, or pick a different port with" \
      "${TARGET_PREFIX}_ENDPOINT_PORT in config/targets.local.env."
  fi

  # Target selection reaches the app through its existing env-var config layer
  # (src/voice_assistant/config.py). DEVICE_PORT is the app's own key; the
  # VOICE_ASSISTANT_* names are the target-aware additions.
  export VOICE_ASSISTANT_TARGET="${TARGET_NAME}"
  export VOICE_ASSISTANT_DEVICE_HOST="${mac_ip}"
  export VOICE_ASSISTANT_DEVICE_PORT="${port}"
  export DEVICE_PORT="${port}"

  log_ok "Launching: ${py##*/} -m voice_assistant --target ${TARGET_NAME} --port ${port}"
  log_dim "Ctrl+C stops the app (and its reverse tunnel). The ${TARGET_NAME} service keeps"
  log_dim "running -- stop the full session with: ./scripts/terminate-${TARGET_NAME}.sh"

  _start_handshake_watchdog "${port}" "${TARGET_READY_TIMEOUT}"

  # Foreground, unquoted APP_EXTRA_ARGS so callers can pass "--web --log-level DEBUG".
  # shellcheck disable=SC2086
  "${py}" -m voice_assistant --target "${TARGET_NAME}" --port "${port}" ${APP_EXTRA_ARGS:-}
}

# PIDs listening on a local TCP port (macOS lsof). Prints one PID per line.
_listener_pids() {
  local port="$1"
  if have lsof; then
    lsof -nP -iTCP:"${port}" -sTCP:LISTEN -t 2>/dev/null || true
  fi
}

# Stop leftover Mac-side app listeners on the target's device port.
stop_local_app() {
  local port="${TARGET_ENDPOINT_PORT}"
  log_step "Stopping local app on port ${port}"

  if is_dry_run; then
    log_info "[dry-run] would stop listeners on TCP ${port}"
    return 0
  fi

  local pids pid cmd stopped=0
  pids="$(_listener_pids "${port}")"
  if [[ -z "${pids}" ]]; then
    log_ok "No local listener on port ${port}"
    return 0
  fi

  while IFS= read -r pid; do
    [[ -z "${pid}" ]] && continue
    cmd="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
    # Only kill processes that look like this app / its launcher Python.
    if [[ "${cmd}" != *voice_assistant* && "${cmd}" != *voice-assistant* ]]; then
      log_warn "Port ${port} held by pid ${pid}, but it is not voice_assistant -- leaving it alone"
      log_dim "  ${cmd}"
      continue
    fi
    log_info "Stopping pid ${pid}: ${cmd}"
    kill "${pid}" >/dev/null 2>&1 || true
    stopped=1
  done <<< "${pids}"

  # Give listeners a moment to exit, then SIGKILL stragglers we own.
  if [[ ${stopped} -eq 1 ]]; then
    sleep 1
    pids="$(_listener_pids "${port}")"
    while IFS= read -r pid; do
      [[ -z "${pid}" ]] && continue
      cmd="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
      if [[ "${cmd}" == *voice_assistant* || "${cmd}" == *voice-assistant* ]]; then
        kill -9 "${pid}" >/dev/null 2>&1 || true
      fi
    done <<< "${pids}"
  fi

  if _port_in_use "${port}"; then
    log_warn "Port ${port} is still in use after stop attempt"
  else
    log_ok "Local app stopped (port ${port} free)"
  fi
}

# Kill orphaned `ssh -R <port>:localhost:<port>` processes left by a crashed launcher.
stop_orphan_tunnels() {
  local port="${TARGET_ENDPOINT_PORT}"
  log_step "Stopping orphan SSH reverse tunnels for port ${port}"

  if is_dry_run; then
    log_info "[dry-run] would kill ssh -R ${port}:localhost:${port} processes"
    return 0
  fi

  local pids pid cmd killed=0
  # macOS ps: match the reverse-forward pattern used by tunnel.sh.
  pids="$(pgrep -f "ssh .* -R ${port}:localhost:${port}" 2>/dev/null || true)"
  if [[ -z "${pids}" ]]; then
    log_ok "No orphan tunnels found"
    return 0
  fi

  while IFS= read -r pid; do
    [[ -z "${pid}" ]] && continue
    cmd="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
    log_info "Stopping tunnel pid ${pid}"
    log_dim "  ${cmd}"
    kill "${pid}" >/dev/null 2>&1 || true
    killed=1
  done <<< "${pids}"

  if [[ ${killed} -eq 1 ]]; then
    sleep 0.5
    log_ok "Orphan tunnels stopped"
  fi
}
