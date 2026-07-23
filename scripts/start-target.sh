#!/usr/bin/env bash
set -Eeuo pipefail

# Launch a full development session against one Raspberry Pi target.
#
# RUNS ON: the Mac. Everything it does on a Pi goes over SSH.
#
# Workflow:
#   1. Load and validate the target configuration
#   2. Try SSH first -- only switch Wi-Fi if the target is unreachable
#   3. Switch the Mac's Wi-Fi to the target's saved network (no passwords)
#   4. Verify SSH and report the remote system
#   5. Update the remote repo with git pull --ff-only (never destructive)
#   6. Silent, read-only ALSA preflight (never makes sound)
#   7. Restart the endpoint systemd service
#   8. Wait for the endpoint to become ready
#   9. Start the local app with the selected target
#  10. Verify the handshake (the endpoint dialling in)
#
# Only one target at a time: the Mac has one active Wi-Fi connection.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck source=lib/config.sh
source "${SCRIPT_DIR}/lib/config.sh"
# shellcheck source=lib/network.sh
source "${SCRIPT_DIR}/lib/network.sh"
# shellcheck source=lib/ssh.sh
source "${SCRIPT_DIR}/lib/ssh.sh"
# shellcheck source=lib/tunnel.sh
source "${SCRIPT_DIR}/lib/tunnel.sh"
# shellcheck source=lib/deploy.sh
source "${SCRIPT_DIR}/lib/deploy.sh"
# shellcheck source=lib/audio.sh
source "${SCRIPT_DIR}/lib/audio.sh"
# shellcheck source=lib/service.sh
source "${SCRIPT_DIR}/lib/service.sh"
# shellcheck source=lib/app.sh
source "${SCRIPT_DIR}/lib/app.sh"

SKIP_WIFI=0
SKIP_PULL=0
SKIP_APP=0
SKIP_AUDIO_CHECK=0
FOLLOW_LOGS=0
# SC2034: DRY_RUN and APP_EXTRA_ARGS are consumed by lib/common.sh and
# lib/app.sh, which shellcheck analyses as separate units.
# shellcheck disable=SC2034
DRY_RUN="${DRY_RUN:-0}"
# shellcheck disable=SC2034
APP_EXTRA_ARGS="${APP_EXTRA_ARGS:-}"

usage() {
  cat <<'EOF'
Usage: ./scripts/start-target.sh <pi5|pizero2w> [options]

Launches a development session against one Raspberry Pi. Runs on the Mac.

Options:
  --skip-wifi         Never change the Mac's Wi-Fi, even if the target is unreachable
  --skip-pull         Do not update the git repository on the Pi
  --skip-app          Do everything except start the local app on the Mac
  --skip-audio-check  Skip the (silent, read-only) ALSA preflight
  --logs              Follow the endpoint's journal instead of starting the app
  --dry-run           Print what would happen and change nothing anywhere
  --help, -h          Show this help

Anything after `--` is passed to the app, e.g.:
  ./scripts/start-target.sh pizero2w -- --web --log-level DEBUG

Convenience wrappers:
  ./scripts/start-pi5.sh [options]
  ./scripts/start-pizero2w.sh [options]

A --dry-run never switches Wi-Fi, never touches a repository, never restarts a
service, never starts the app, and never opens an audio device.
EOF
}

TARGET_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-wifi)        SKIP_WIFI=1 ;;
    --skip-pull)        SKIP_PULL=1 ;;
    --skip-app)         SKIP_APP=1 ;;
    --skip-audio-check) SKIP_AUDIO_CHECK=1 ;;
    --logs)             FOLLOW_LOGS=1 ;;
    --dry-run)          DRY_RUN=1 ;;
    -h|--help)          usage; exit 0 ;;
    --) shift; APP_EXTRA_ARGS="${APP_EXTRA_ARGS:+${APP_EXTRA_ARGS} }$*"; break ;;
    -*) usage >&2; printf '\nUnknown option: %s\n' "$1" >&2; exit 2 ;;
    *)
      if [[ -n "${TARGET_ARG}" ]]; then
        usage >&2; printf '\nOnly one target may be given (got "%s" and "%s").\n' \
          "${TARGET_ARG}" "$1" >&2; exit 2
      fi
      TARGET_ARG="$1"
      ;;
  esac
  shift || true
done

if [[ -z "${TARGET_ARG}" ]]; then
  usage >&2
  printf '\nNo target given.\n' >&2
  exit 2
fi

cleanup() {
  local rc=$?
  stop_handshake_watchdog
  stop_reverse_tunnel
  return ${rc}
}
trap cleanup EXIT
trap 'log_warn "Interrupted."; exit 130' INT TERM

on_error() {
  local rc=$? line=$1
  [[ ${rc} -eq 0 ]] && return 0
  log_dim "(failed at ${BASH_SOURCE[1]:-script}:${line}, exit ${rc})"
  return ${rc}
}
trap 'on_error ${LINENO}' ERR

require_macos

# --- Step 1: configuration -------------------------------------------------
load_target_config "${TARGET_ARG}"

if is_dry_run; then
  log_step "DRY RUN -- nothing will be changed"
  log_dim "No Wi-Fi switch, no git update, no service restart, no app start,"
  log_dim "no audio device opened, no sound produced."
fi

print_target_config

# --- Steps 2 and 3: reachability before Wi-Fi ------------------------------
log_step "Checking whether ${TARGET_NAME} is already reachable"
if ssh_is_reachable; then
  log_ok "SSH to '${TARGET_SSH_HOST}' already works -- leaving Wi-Fi alone"
elif [[ ${SKIP_WIFI} -eq 1 ]]; then
  log_warn "Target unreachable, but --skip-wifi was given; continuing anyway"
else
  log_info "Not reachable yet -- the Mac may be on the wrong network"
  ensure_network_for_target

  if ! is_dry_run; then
    # Retry SSH with backoff after joining; DHCP and mDNS need a moment.
    delay=2
    reachable=0
    for attempt in 1 2 3 4 5; do
      if ssh_is_reachable; then reachable=1; break; fi
      log_dim "SSH attempt ${attempt}/5 failed, retrying in ${delay}s"
      sleep "${delay}"
      delay=$(( delay < 8 ? delay * 2 : 8 ))
    done
    if [[ ${reachable} -eq 0 ]]; then
      # SC2088: the tilde below is literal text in a message telling the user
      # which file to edit; it is not a path this script resolves.
      # shellcheck disable=SC2088
      die "On '${TARGET_WIFI_SSID}', but '${TARGET_SSH_HOST}' is still unreachable." \
        "$(_network_join_hint_text)" \
        "" \
        "If the Pi's address changed, prefer an mDNS name over a fixed IP in" \
        "~/.ssh/config (hotspot DHCP leases move around):" \
        "    Host ${TARGET_SSH_HOST}" \
        "        HostName ${TARGET_SSH_HOST}.local" \
        "        User damianlapenavidal"
    fi
    log_ok "SSH reachable"
  fi
fi

# --- Step 4: verify SSH ----------------------------------------------------
if is_dry_run && ! ssh_is_reachable; then
  log_step "Verifying SSH to ${TARGET_SSH_HOST}"
  log_info "[dry-run] target not reachable right now; skipping remote inspection"
  DRY_RUN_OFFLINE=1
else
  verify_ssh
  DRY_RUN_OFFLINE=0
fi

# --- SSH reverse tunnel (opt-in per target) --------------------------------
if [[ "${DRY_RUN_OFFLINE}" != "1" ]]; then
  start_reverse_tunnel
fi

# --- Step 5: deploy --------------------------------------------------------
if [[ ${SKIP_PULL} -eq 1 ]]; then
  log_step "Updating repository on ${TARGET_NAME}"
  log_dim "Skipped (--skip-pull)"
elif [[ "${DRY_RUN_OFFLINE}" == "1" ]]; then
  log_step "Updating repository on ${TARGET_NAME}"
  log_info "[dry-run] would git fetch + checkout ${TARGET_BRANCH} + pull --ff-only in ${TARGET_REMOTE_REPO}"
else
  deploy_to_target
fi

# --- Step 6: audio preflight ----------------------------------------------
if [[ ${SKIP_AUDIO_CHECK} -eq 1 ]]; then
  log_step "Audio preflight"
  log_dim "Skipped (--skip-audio-check)"
elif [[ "${DRY_RUN_OFFLINE}" == "1" ]]; then
  log_step "Audio preflight"
  log_info "[dry-run] would run read-only ALSA checks; no audio device opened"
else
  audio_preflight
fi

# --- Step 7: endpoint ------------------------------------------------------
# NOTE ON ORDERING: the endpoint is a CLIENT that dials the app (the server),
# and the app is started last (below). So the endpoint cannot connect -- and a
# --logs-free launch cannot "wait for it to be ready" -- until the app is up.
# The endpoint handles exactly this with its own reconnect backoff
# (MAX_RECONNECT_ATTEMPTS in zero2w_client.py): it retries while the app boots
# and connects on a later attempt. We therefore only confirm the unit launched
# (service_assert_active); the definitive readiness signal is the HELLO
# handshake, verified by the watchdog in start_local_app once the app listens.
if [[ "${DRY_RUN_OFFLINE}" == "1" ]]; then
  log_step "Restarting endpoint service on ${TARGET_NAME}"
  log_info "[dry-run] would run: $(_systemctl) restart ${TARGET_SERVICE_NAME}"
else
  service_restart
  service_assert_active
fi

# --- --logs mode -----------------------------------------------------------
if [[ ${FOLLOW_LOGS} -eq 1 ]]; then
  log_step "Following the endpoint journal on ${TARGET_NAME}"
  if is_dry_run; then
    log_info "[dry-run] would run: $(service_logs_command)"
    exit 0
  fi
  log_dim "Ctrl+C to stop. The service keeps running."
  exec ssh -o BatchMode=yes -o ConnectTimeout="${SSH_CONNECT_TIMEOUT}" \
    "${TARGET_SSH_HOST}" "$(_journalctl) -u $(shq "${TARGET_SERVICE_NAME}") -f --no-pager"
fi

# --- Steps 9 and 10: local app --------------------------------------------
if [[ ${SKIP_APP} -eq 1 ]]; then
  log_step "Starting the local app"
  log_dim "Skipped (--skip-app)"
  log_ok "Target '${TARGET_NAME}' is prepared."
  log_info "Start the app yourself with:"
  log_info "  VOICE_ASSISTANT_TARGET=${TARGET_NAME} ./venv/bin/python -m voice_assistant --target ${TARGET_NAME}"
  exit 0
fi

start_local_app
