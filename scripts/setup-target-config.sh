#!/usr/bin/env bash
set -Eeuo pipefail

# Create config/targets.local.env and help determine the exact saved Wi-Fi SSIDs.
# RUNS ON: the Mac.
#
# SSIDs must match exactly. This Mac has both "iPhone de Damian" and
# "iPhone de Damián" saved -- visually near-identical, different networks. The
# whole point of this helper is that you never have to guess or retype one.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck source=lib/config.sh
source "${SCRIPT_DIR}/lib/config.sh"
# shellcheck source=lib/network.sh
source "${SCRIPT_DIR}/lib/network.sh"

usage() {
  cat <<'EOF'
Usage: ./scripts/setup-target-config.sh [options]

Creates config/targets.local.env from the committed example and helps you fill
in the exact SSIDs macOS has saved. Runs on the Mac.

Options:
  --list-ssids   List saved Wi-Fi networks and the current SSID, then exit
  --force        Overwrite an existing config/targets.local.env
  --help, -h     Show this help

The local file is gitignored. It must never contain passwords or API keys.
EOF
}

LIST_ONLY=0
FORCE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --list-ssids) LIST_ONLY=1 ;;
    --force)      FORCE=1 ;;
    -h|--help)    usage; exit 0 ;;
    *) usage >&2; printf '\nUnknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
  shift
done

require_macos

IFACE="$(wifi_interface)"
CURRENT="$(current_ssid "${IFACE}")"

log_step "Wi-Fi on this Mac"
log_info "Interface    : ${IFACE}"
log_info "Current SSID : ${CURRENT:-<not associated>}"

log_step "Saved networks (exact names -- copy one verbatim)"
n=0
while IFS= read -r ssid; do
  [[ -z "${ssid}" ]] && continue
  n=$(( n + 1 ))
  if [[ "${ssid}" == "${CURRENT}" ]]; then
    printf '    %2d. %s%s%s  %s(current)%s\n' \
      "${n}" "${C_BOLD}" "${ssid}" "${C_RESET}" "${C_GREEN}" "${C_RESET}" >&2
  else
    printf '    %2d. %s\n' "${n}" "${ssid}" >&2
  fi
done < <(list_saved_ssids "${IFACE}")

if [[ ${n} -eq 0 ]]; then
  log_warn "No saved networks found on ${IFACE}."
fi

log_dim ""
log_dim "Names can differ in ways that are hard to see (underscore vs space,"
log_dim "'Damian' vs 'Damián'). Copy the exact string above into the config."

if [[ ${LIST_ONLY} -eq 1 ]]; then
  exit 0
fi

log_step "Creating config/targets.local.env"

if [[ -f "${TARGETS_LOCAL_FILE}" && ${FORCE} -eq 0 ]]; then
  log_ok "Already exists: ${TARGETS_LOCAL_FILE}"
  log_info "Edit it directly, or re-run with --force to replace it."
  log_info "Current Wi-Fi settings in that file:"
  grep -E '_WIFI_SSID=' "${TARGETS_LOCAL_FILE}" 2>/dev/null | sed 's/^/      /' >&2 || true
  exit 0
fi

[[ -f "${TARGETS_EXAMPLE_FILE}" ]] || die "Missing template: ${TARGETS_EXAMPLE_FILE}"

mkdir -p "$(dirname "${TARGETS_LOCAL_FILE}")"
cp "${TARGETS_EXAMPLE_FILE}" "${TARGETS_LOCAL_FILE}"
chmod 600 "${TARGETS_LOCAL_FILE}"

log_ok "Created ${TARGETS_LOCAL_FILE}"

# Offer to set the Pi Zero hotspot SSID to whatever is currently joined, since
# that is usually exactly the network you are on while setting this up.
if [[ -n "${CURRENT}" ]]; then
  log_info ""
  log_info "You are currently on '${CURRENT}'."
  printf '    Set PIZERO2W_WIFI_SSID to "%s"? [y/N] ' "${CURRENT}" >&2
  read -r reply || reply=""
  if [[ "${reply}" == "y" || "${reply}" == "Y" ]]; then
    # In-place edit without GNU sed -i semantics (macOS sed needs a backup arg).
    tmp="${TARGETS_LOCAL_FILE}.tmp.$$"
    awk -v ssid="${CURRENT}" '
      /^PIZERO2W_WIFI_SSID=/ { print "PIZERO2W_WIFI_SSID=\"" ssid "\""; next }
      { print }
    ' "${TARGETS_LOCAL_FILE}" >"${tmp}"
    mv "${tmp}" "${TARGETS_LOCAL_FILE}"
    chmod 600 "${TARGETS_LOCAL_FILE}"
    log_ok "PIZERO2W_WIFI_SSID set to \"${CURRENT}\""
  fi
fi

log_step "Next steps"
log_info "1. Review the file:   \$EDITOR config/targets.local.env"
log_info "2. Confirm PI5_WIFI_SSID matches a saved network listed above."
log_info "3. Check your SSH aliases resolve:"
log_info "     ssh -o BatchMode=yes voice-assistant-pi5 true"
log_info "     ssh -o BatchMode=yes voice-assistant-pizero2w true"
log_info "4. Try it without changing anything:"
log_info "     ./scripts/start-pizero2w.sh --dry-run"
log_info ""
log_info "Never add Wi-Fi passwords, GitHub tokens, or OpenAI keys to this file."
