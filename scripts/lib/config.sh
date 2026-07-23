#!/usr/bin/env bash
# Loads config/targets.local.env and resolves the PREFIX_KEY variables for the
# selected target into flat TARGET_* variables the rest of the scripts use.

[[ -n "${_VA_CONFIG_SH:-}" ]] && return 0
_VA_CONFIG_SH=1

TARGETS_LOCAL_FILE="${REPO_ROOT}/config/targets.local.env"
TARGETS_EXAMPLE_FILE="${REPO_ROOT}/config/targets.example.env"

# Targets this launcher knows about.
KNOWN_TARGETS=("pi5" "pizero2w")

is_known_target() {
  local candidate="$1" t
  for t in "${KNOWN_TARGETS[@]}"; do
    [[ "${candidate}" == "${t}" ]] && return 0
  done
  return 1
}

# Map a target name to the config-file key prefix (pizero2w -> PIZERO2W).
target_prefix() {
  printf '%s' "$1" | tr '[:lower:]' '[:upper:]'
}

load_targets_file() {
  if [[ ! -f "${TARGETS_LOCAL_FILE}" ]]; then
    die "No target configuration found at config/targets.local.env" \
      "Create it from the committed example by running:" \
      "" \
      "    ./scripts/setup-target-config.sh" \
      "" \
      "That helper copies ${TARGETS_EXAMPLE_FILE##*/} and helps you fill in the" \
      "exact saved Wi-Fi SSIDs. The local file is gitignored."
  fi

  # The file is plain KEY="value" assignments. Sourcing it is the whole point;
  # it is a local, gitignored, user-owned config file, so SC1090's "can't follow
  # non-constant source" is expected rather than a defect.
  set -a
  # shellcheck disable=SC1090
  source "${TARGETS_LOCAL_FILE}"
  set +a
}

# Read "<PREFIX>_<KEY>" via indirect expansion.
_cfg() {
  local var="${1}_${2}"
  printf '%s' "${!var-}"
}

# Fail if a required key is missing or empty, naming the exact key to add.
_require_cfg() {
  local prefix="$1" key="$2" value
  value="$(_cfg "${prefix}" "${key}")"
  if [[ -z "${value}" ]]; then
    die "Missing required setting ${prefix}_${key} for target '${TARGET_NAME}'." \
      "Add this line to config/targets.local.env:" \
      "" \
      "    ${prefix}_${key}=\"...\"" \
      "" \
      "See config/targets.example.env for the expected value."
  fi
  printf '%s' "${value}"
}

# Resolve the selected target into TARGET_* globals.
#
# Required keys fail loudly. Audio keys are OPTIONAL on purpose: the Pi Zero 2 W
# has verified ALSA settings, the Pi 5's hardware has not been inspected, and
# forcing the Pi Zero's settings onto the Pi 5 would be wrong.
load_target_config() {
  TARGET_NAME="$1"

  if ! is_known_target "${TARGET_NAME}"; then
    die "Unknown target '${TARGET_NAME}'." \
      "Known targets: ${KNOWN_TARGETS[*]}" \
      "" \
      "Usage: ./scripts/start-target.sh <${KNOWN_TARGETS[0]}|${KNOWN_TARGETS[1]}> [options]"
  fi

  load_targets_file

  local p
  p="$(target_prefix "${TARGET_NAME}")"
  # SC2034: the TARGET_* globals below are read by the other lib/*.sh files and
  # by the launcher, which shellcheck analyses as separate units.
  # shellcheck disable=SC2034
  TARGET_PREFIX="${p}"

  # --- Required ---
  TARGET_WIFI_SSID="$(_require_cfg "${p}" WIFI_SSID)"
  TARGET_SSH_HOST="$(_require_cfg "${p}" SSH_HOST)"
  TARGET_REMOTE_REPO="$(_require_cfg "${p}" REMOTE_REPO)"
  TARGET_BRANCH="$(_require_cfg "${p}" BRANCH)"
  TARGET_SERVICE_NAME="$(_require_cfg "${p}" SERVICE_NAME)"

  # --- Optional, with defaults ---
  TARGET_SERVICE_SCOPE="$(_cfg "${p}" SERVICE_SCOPE)"; : "${TARGET_SERVICE_SCOPE:=user}"
  # shellcheck disable=SC2034  # read by lib/app.sh
  TARGET_ENDPOINT_HOST="$(_cfg "${p}" ENDPOINT_HOST)"
  TARGET_ENDPOINT_PORT="$(_cfg "${p}" ENDPOINT_PORT)"; : "${TARGET_ENDPOINT_PORT:=8765}"
  TARGET_PROTOCOL="$(_cfg "${p}" PROTOCOL)"; : "${TARGET_PROTOCOL:=websocket}"
  TARGET_READY_TIMEOUT="$(_cfg "${p}" READY_TIMEOUT)"; : "${TARGET_READY_TIMEOUT:=30}"
  # "1" -> carry the WebSocket connection over an SSH -R tunnel instead of
  # having the Pi dial the Mac's LAN IP. See scripts/lib/tunnel.sh for why.
  TARGET_USE_SSH_TUNNEL="$(_cfg "${p}" USE_SSH_TUNNEL)"; : "${TARGET_USE_SSH_TUNNEL:=0}"

  # --- Audio (optional; empty means "not verified for this board") ---
  TARGET_ALSA_CARD_ID="$(_cfg "${p}" ALSA_CARD_ID)"
  TARGET_CAPTURE_DEVICE="$(_cfg "${p}" CAPTURE_DEVICE)"
  TARGET_PLAYBACK_DEVICE="$(_cfg "${p}" PLAYBACK_DEVICE)"
  TARGET_SAMPLE_RATE="$(_cfg "${p}" SAMPLE_RATE)"
  TARGET_CAPTURE_FORMAT="$(_cfg "${p}" CAPTURE_FORMAT)"
  TARGET_CAPTURE_CHANNELS="$(_cfg "${p}" CAPTURE_CHANNELS)"
  TARGET_MIC_CHANNEL="$(_cfg "${p}" MIC_CHANNEL)"
  TARGET_PLAYBACK_CHANNELS="$(_cfg "${p}" PLAYBACK_CHANNELS)"
  TARGET_INPUT_GAIN="$(_cfg "${p}" INPUT_GAIN)"
  TARGET_PLAYBACK_GAIN="$(_cfg "${p}" PLAYBACK_GAIN)"

  case "${TARGET_SERVICE_SCOPE}" in
    user|system) ;;
    *) die "${p}_SERVICE_SCOPE must be \"user\" or \"system\" (got \"${TARGET_SERVICE_SCOPE}\")." ;;
  esac

  if [[ "${TARGET_WIFI_SSID}" == "<"*">" ]]; then
    die "${p}_WIFI_SSID is still the placeholder ${TARGET_WIFI_SSID}." \
      "Set the exact SSID as saved on macOS. To list saved networks:" \
      "" \
      "    ./scripts/setup-target-config.sh --list-ssids"
  fi
}

# True when this target has a verified ALSA capture+playback configuration.
target_has_audio_config() {
  [[ -n "${TARGET_ALSA_CARD_ID}" && -n "${TARGET_CAPTURE_DEVICE}" && -n "${TARGET_PLAYBACK_DEVICE}" ]]
}

print_target_config() {
  log_step "Target: ${TARGET_NAME}"
  log_info "Wi-Fi SSID     : ${TARGET_WIFI_SSID}"
  log_info "SSH alias      : ${TARGET_SSH_HOST}"
  log_info "Remote repo    : ${TARGET_REMOTE_REPO}"
  log_info "Branch         : ${TARGET_BRANCH}"
  log_info "Service        : ${TARGET_SERVICE_NAME} (${TARGET_SERVICE_SCOPE} scope)"
  log_info "Protocol       : ${TARGET_PROTOCOL} on port ${TARGET_ENDPOINT_PORT}"
  if [[ "${TARGET_USE_SSH_TUNNEL}" == "1" ]]; then
    log_info "Connection     : SSH reverse tunnel (this Mac's IP is not reachable over ${TARGET_NAME}'s network)"
  fi
  if target_has_audio_config; then
    log_info "ALSA card      : ${TARGET_ALSA_CARD_ID}"
    log_info "Capture        : ${TARGET_CAPTURE_DEVICE}"
    log_info "                 ${TARGET_SAMPLE_RATE} Hz, ${TARGET_CAPTURE_FORMAT}, ${TARGET_CAPTURE_CHANNELS}ch, mic=${TARGET_MIC_CHANNEL}"
    log_info "Playback       : ${TARGET_PLAYBACK_DEVICE} (${TARGET_PLAYBACK_CHANNELS}ch)"
    log_info "Gain           : input=${TARGET_INPUT_GAIN} playback=${TARGET_PLAYBACK_GAIN}"
  else
    log_dim "ALSA           : not configured for this target (audio preflight will be skipped)"
  fi
}
