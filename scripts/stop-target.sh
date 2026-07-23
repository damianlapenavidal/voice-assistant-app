#!/usr/bin/env bash
set -Eeuo pipefail

# Tear down a development session against one Raspberry Pi target.
#
# RUNS ON: the Mac. Remote actions go over SSH.
#
# Complements start-target.sh: Ctrl+C on the launcher stops the Mac app and
# reverse tunnel, but leaves the Pi systemd unit running. This script stops:
#   1. The Pi endpoint service
#   2. Any leftover Mac voice_assistant listener on the target port
#   3. Orphaned ssh -R tunnels for that port
#
# Idempotent: safe to re-run when things are already stopped.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck source=lib/config.sh
source "${SCRIPT_DIR}/lib/config.sh"
# shellcheck source=lib/ssh.sh
source "${SCRIPT_DIR}/lib/ssh.sh"
# shellcheck source=lib/service.sh
source "${SCRIPT_DIR}/lib/service.sh"
# shellcheck source=lib/app.sh
source "${SCRIPT_DIR}/lib/app.sh"

# SC2034: DRY_RUN consumed by lib/common.sh helpers.
# shellcheck disable=SC2034
DRY_RUN="${DRY_RUN:-0}"

usage() {
  cat <<'EOF'
Usage: ./scripts/stop-target.sh <pi5|pizero2w> [options]

Stops a development session for one Raspberry Pi. Runs on the Mac.

Stops (in order):
  1. The remote endpoint systemd service
  2. Any local voice_assistant listener on the target device port
  3. Orphaned SSH reverse tunnels for that port

Options:
  --dry-run    Print what would happen; change nothing
  --help, -h   Show this help

Convenience wrappers:
  ./scripts/terminate-pi5.sh
  ./scripts/terminate-pizero2w.sh
EOF
}

TARGET_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) usage >&2; printf '\nUnknown option: %s\n' "$1" >&2; exit 2 ;;
    *)
      if [[ -n "${TARGET_ARG}" ]]; then
        usage >&2
        printf '\nOnly one target may be given (got "%s" and "%s").\n' \
          "${TARGET_ARG}" "$1" >&2
        exit 2
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

require_macos
load_target_config "${TARGET_ARG}"

if is_dry_run; then
  log_step "DRY RUN -- nothing will be changed"
fi

print_target_config

service_stop
stop_local_app
stop_orphan_tunnels

log_ok "Session for '${TARGET_NAME}' terminated."
if ! is_dry_run; then
  log_dim "If a start-* terminal is still open, Ctrl+C it -- the app should already be gone."
fi
