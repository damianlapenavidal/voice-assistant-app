#!/usr/bin/env bash
# systemd control for the Pi-side endpoint, plus readiness verification.
#
# A USER service (systemctl --user) is preferred: it needs no sudo at all, which
# keeps the deployment path free of passwordless-sudo grants. A user service
# does require lingering so it survives SSH logout -- this checks for that and
# tells the user the one command to run, rather than running it silently.

[[ -n "${_VA_SERVICE_SH:-}" ]] && return 0
_VA_SERVICE_SH=1

# systemctl prefix for the configured scope.
_systemctl() {
  if [[ "${TARGET_SERVICE_SCOPE}" == "user" ]]; then
    printf 'systemctl --user'
  else
    printf 'sudo systemctl'
  fi
}

_journalctl() {
  if [[ "${TARGET_SERVICE_SCOPE}" == "user" ]]; then
    printf 'journalctl --user'
  else
    printf 'sudo journalctl'
  fi
}

# The exact command the user can run to follow logs. Also printed on failure.
service_logs_command() {
  printf "ssh %s '%s -u %s -f --no-pager'" \
    "${TARGET_SSH_HOST}" "$(_journalctl)" "${TARGET_SERVICE_NAME}"
}

service_show_logs() {
  local lines="${1:-50}"
  log_dim "Last ${lines} log lines from ${TARGET_SERVICE_NAME}:"
  ssh_run "$(_journalctl) -u $(shq "${TARGET_SERVICE_NAME}") --no-pager -n ${lines}" 2>&1 \
    | sed 's/^/    /' >&2 || log_dim "(could not read the journal)"
}

# Abort with installation instructions when the unit does not exist.
_service_require_installed() {
  local unit="${TARGET_SERVICE_NAME}.service" found
  found="$(ssh_run "$(_systemctl) list-unit-files --no-pager --no-legend $(shq "${unit}") 2>/dev/null | head -1")" || true

  [[ -n "${found}" ]] && return 0

  local scope_note install_path
  if [[ "${TARGET_SERVICE_SCOPE}" == "user" ]]; then
    scope_note="user"
    # SC2088: this string is displayed to the user as the path to type on the
    # Pi. It is never used as a path here, so the tilde must stay literal.
    # shellcheck disable=SC2088
    install_path="~/.config/systemd/user/${unit}"
  else
    scope_note="system"
    install_path="/etc/systemd/system/${unit}"
  fi

  die "The ${scope_note} service '${unit}' is not installed on ${TARGET_NAME}." \
    "Nothing was changed on the Pi." \
    "" \
    "This launcher deliberately does not invent an endpoint command. Install the" \
    "unit once, using the template in this repo as a starting point:" \
    "" \
    "    deploy/systemd/${unit}" \
    "" \
    "Copy it to ${install_path} ON THE PI, edit ExecStart to the endpoint's real" \
    "entrypoint, then:" \
    "" \
    "    ssh ${TARGET_SSH_HOST}" \
    "    $(_systemctl) daemon-reload" \
    "    $(_systemctl) enable --now ${TARGET_SERVICE_NAME}" \
    "" \
    "Full walkthrough: docs/raspberry-pi-development-workflow.md" \
    "" \
    "To run the rest of the workflow meanwhile, rerun with --skip-app or --dry-run."
}

# A user service only survives SSH logout when lingering is enabled. Report,
# do not silently enable -- it is a persistent system change.
_service_check_linger() {
  [[ "${TARGET_SERVICE_SCOPE}" == "user" ]] || return 0

  local linger
  linger="$(ssh_run "loginctl show-user \"\$(id -un)\" --property=Linger --value 2>/dev/null")" || return 0

  if [[ "${linger}" != "yes" ]]; then
    log_warn "User lingering is disabled on ${TARGET_NAME}."
    log_dim "Without it the user service stops when your SSH session ends, and does"
    log_dim "not start at boot. Enable it once, ON THE PI:"
    log_dim "  ssh ${TARGET_SSH_HOST} 'loginctl enable-linger \$(id -un)'"
  else
    log_ok "User lingering enabled (service survives SSH logout and starts at boot)"
  fi
}

# Step 7: restart the endpoint.
service_restart() {
  log_step "Restarting endpoint service on ${TARGET_NAME}"
  log_info "Unit  : ${TARGET_SERVICE_NAME}.service (${TARGET_SERVICE_SCOPE} scope)"

  if is_dry_run; then
    log_info "[dry-run] would run: $(_systemctl) restart ${TARGET_SERVICE_NAME}"
    log_info "[dry-run] no service is touched"
    return 0
  fi

  _service_require_installed
  _service_check_linger

  local out
  if ! out="$(ssh_run "$(_systemctl) restart $(shq "${TARGET_SERVICE_NAME}")" 2>&1)"; then
    log_error "Failed to restart '${TARGET_SERVICE_NAME}' on ${TARGET_NAME}." \
      "$(printf '%s\n' "${out}" | sed 's/^/  /')"
    service_show_logs 50
    die "Endpoint service restart failed." \
      "Follow the logs with:" \
      "    $(service_logs_command)"
  fi
  log_ok "Restart issued"
}

# Stop the endpoint service. Idempotent: inactive/not-found is success.
service_stop() {
  log_step "Stopping endpoint service on ${TARGET_NAME}"
  log_info "Unit  : ${TARGET_SERVICE_NAME}.service (${TARGET_SERVICE_SCOPE} scope)"

  if is_dry_run; then
    log_info "[dry-run] would run: $(_systemctl) stop ${TARGET_SERVICE_NAME}"
    log_info "[dry-run] no service is touched"
    return 0
  fi

  if ! ssh_is_reachable; then
    log_warn "SSH to '${TARGET_SSH_HOST}' failed -- cannot stop the remote service."
    log_dim "If the Pi is powered off, the service is already down."
    return 0
  fi

  local state out
  state="$(ssh_run "$(_systemctl) is-active $(shq "${TARGET_SERVICE_NAME}") 2>&1" || true)"
  if [[ "${state}" == "inactive" || "${state}" == "failed" || "${state}" == "unknown" ]]; then
    log_ok "Service already stopped (state: ${state})"
    return 0
  fi

  if ! out="$(ssh_run "$(_systemctl) stop $(shq "${TARGET_SERVICE_NAME}")" 2>&1)"; then
    # Unit missing is fine for terminate -- nothing to stop.
    if printf '%s\n' "${out}" | grep -qiE 'not found|could not be found|Unit .* not loaded'; then
      log_ok "Service unit not installed -- nothing to stop"
      return 0
    fi
    die "Failed to stop '${TARGET_SERVICE_NAME}' on ${TARGET_NAME}." \
      "$(printf '%s\n' "${out}" | sed 's/^/  /')" \
      "" \
      "Inspect:  ssh ${TARGET_SSH_HOST} '$(_systemctl) status ${TARGET_SERVICE_NAME} --no-pager -l'"
  fi

  state="$(ssh_run "$(_systemctl) is-active $(shq "${TARGET_SERVICE_NAME}") 2>&1" || true)"
  if [[ "${state}" == "inactive" || "${state}" == "failed" ]]; then
    log_ok "Service stopped"
  else
    log_warn "Stop issued, but state is still '${state}'"
  fi
}

# Confirm systemd considers the unit active. Necessary but not sufficient --
# the endpoint is a client that cannot connect until the app (started later)
# is listening, so the definitive proof is the HELLO handshake, verified by the
# watchdog in start_local_app. See the ordering note in start-target.sh.
service_assert_active() {
  is_dry_run && { log_info "[dry-run] would verify the unit is active"; return 0; }

  # "activating" is normal right after restart AND during RestartSec when the
  # unit is about to auto-restart (e.g. endpoint briefly failed to dial before
  # the Mac app is listening). Wait through that window rather than treating
  # the first few seconds as a hard failure.
  local state attempt
  local max_attempts="${TARGET_READY_TIMEOUT:-30}"
  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    state="$(ssh_run "$(_systemctl) is-active $(shq "${TARGET_SERVICE_NAME}") 2>&1" || true)"
    [[ "${state}" == "active" ]] && { log_ok "Service is active"; return 0; }
    if [[ "${state}" != "activating" ]]; then
      break
    fi
    sleep 1
  done

  log_error "Service '${TARGET_SERVICE_NAME}' is not active (state: ${state})."
  service_show_logs 50
  die "The endpoint service did not stay running on ${TARGET_NAME}." \
    "A unit that starts and immediately exits usually means the ExecStart" \
    "command is wrong, a Python dependency is missing on the Pi, or (with" \
    "an SSH tunnel) the endpoint is crashing on connect instead of retrying." \
    "" \
    "Inspect:  ssh ${TARGET_SSH_HOST} '$(_systemctl) status ${TARGET_SERVICE_NAME} --no-pager -l'"
}

# Retained for reference / manual use, but NOT called in the default launch
# flow -- see the ordering note in start-target.sh. Because the endpoint dials
# the app and the app starts last, the endpoint legitimately fails to connect
# (and may restart) until the app is listening; treating those restarts as a
# crash-loop would abort every launch before the app ever starts. The endpoint's
# own reconnect backoff bridges that gap instead, and the HELLO handshake
# (verified in app.sh) is the real readiness signal.
service_wait_ready() {
  if is_dry_run; then
    log_step "Endpoint readiness"
    log_info "[dry-run] would poll the unit for up to ${TARGET_READY_TIMEOUT}s"
    log_info "[dry-run] no audio device is opened and no sound is produced"
    return 0
  fi

  log_step "Waiting for the endpoint to become ready (timeout ${TARGET_READY_TIMEOUT}s)"
  log_dim "This check is silent -- it never makes the speaker produce sound."

  local restarts_start restarts_now state elapsed=0 interval=2
  restarts_start="$(ssh_run "$(_systemctl) show $(shq "${TARGET_SERVICE_NAME}") --property=NRestarts --value 2>/dev/null" || echo 0)"
  : "${restarts_start:=0}"

  while [[ ${elapsed} -lt ${TARGET_READY_TIMEOUT} ]]; do
    state="$(ssh_run "$(_systemctl) is-active $(shq "${TARGET_SERVICE_NAME}") 2>&1" || true)"
    restarts_now="$(ssh_run "$(_systemctl) show $(shq "${TARGET_SERVICE_NAME}") --property=NRestarts --value 2>/dev/null" || echo 0)"
    : "${restarts_now:=0}"

    if [[ "${state}" == "failed" ]]; then
      log_error "Service '${TARGET_SERVICE_NAME}' entered the failed state."
      service_show_logs 50
      die "Endpoint failed to start on ${TARGET_NAME}." \
        "Follow the logs with:" \
        "    $(service_logs_command)"
    fi

    # A climbing restart counter usually means crash-loop. With an SSH reverse
    # tunnel the endpoint may still be dialling before the Mac app listens;
    # allow activating/auto-restart samples without treating them as failure
    # until we have seen a sustained active window (below). Only fail fast on
    # restart storms when the unit is not even managing to stay active.
    if [[ "${restarts_now}" -gt "${restarts_start}" && "${state}" == "active" ]]; then
      # Process is up; a prior restart during this wait is fine.
      :
    elif [[ "${restarts_now}" -gt $((restarts_start + 3)) && "${state}" != "active" ]]; then
      log_error "Service '${TARGET_SERVICE_NAME}' is restarting repeatedly" \
        "(NRestarts went ${restarts_start} -> ${restarts_now})."
      service_show_logs 50
      die "The endpoint is crash-looping on ${TARGET_NAME}." \
        "It is 'active' only because systemd keeps respawning it." \
        "The log above shows why it exits." \
        "" \
        "Follow the logs with:" \
        "    $(service_logs_command)"
    fi

    # Stayed up, no new restarts in this sample window, for two consecutive samples.
    if [[ "${state}" == "active" && ${elapsed} -ge ${interval} ]]; then
      log_ok "Endpoint is up and stable (no restarts in ${elapsed}s)"
      log_dim "Definitive proof is the HELLO handshake, verified once the app starts."
      return 0
    fi

    sleep "${interval}"
    elapsed=$(( elapsed + interval ))
  done

  log_error "Endpoint did not stabilise within ${TARGET_READY_TIMEOUT}s (state: ${state:-unknown})."
  service_show_logs 50
  die "Endpoint readiness timed out on ${TARGET_NAME}." \
    "Raise ${TARGET_PREFIX}_READY_TIMEOUT in config/targets.local.env if the Pi" \
    "Zero 2 W simply needs longer to boot the endpoint." \
    "" \
    "Follow the logs with:" \
    "    $(service_logs_command)"
}
