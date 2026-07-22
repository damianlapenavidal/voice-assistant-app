#!/usr/bin/env bash
set -Eeuo pipefail

# Explicit, opt-in audio diagnostics for a Raspberry Pi target.
# RUNS ON: the Mac. The ALSA/SoX commands run on the Pi over SSH.
#
# SAFETY CONTRACT
#   - Nothing here runs as part of deployment, startup, or readiness checking.
#   - --info is read-only and silent.
#   - --mic captures only; it never plays anything back.
#   - --speaker and --loopback are the ONLY modes that make sound, they must be
#     asked for by name, they start very quiet, they are finite, and they fade
#     in and out.
#   - No high-amplitude tone is ever generated.
#
# Capture and playback are testable separately and on purpose, so a fault can be
# isolated to one half of the signal chain.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck source=lib/config.sh
source "${SCRIPT_DIR}/lib/config.sh"
# shellcheck source=lib/ssh.sh
source "${SCRIPT_DIR}/lib/ssh.sh"
# shellcheck source=lib/audio.sh
source "${SCRIPT_DIR}/lib/audio.sh"

usage() {
  cat <<'EOF'
Usage: ./scripts/audio-diagnostic.sh <pi5|pizero2w> <mode> [options]

Modes:
  --info       Read-only: cards, capture/playback devices, resolved ALSA device,
               busy streams. SILENT -- makes no sound. Safe to run any time.
  --mic        Record a few seconds from the mic and report the level of each
               channel. Captures only, plays NOTHING. Proves the mic works and
               that the signal is on the expected channel.
  --speaker    Play a short, very quiet fading tone. MAKES SOUND -- see warning.
  --loopback   Record, then play the recording back. MAKES SOUND. This is the
               controlled record-then-play test: capture stops before playback
               starts (half duplex), so the mic never hears the speaker.

Options:
  --duration N   Seconds to record (default 5)
  --gain G       Override playback gain for this run (default: target's
                 configured playback gain)
  --dry-run      Print what would run; opens no audio device and makes no sound
  --help, -h     Show this help

SPEAKER WARNING
  The MAX98357A amplifier pops when the ALSA playback stream opens and again
  when it closes. This is expected and is a property of the amplifier, not a
  fault. Keep the speaker away from your ear. Playback starts very quiet.
  If you hear harsh continuous noise or distortion, stop immediately (Ctrl+C)
  and lower the gain.
EOF
}

MODE=""
TARGET_ARG=""
DURATION=5
GAIN_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --info|--mic|--speaker|--loopback)
      [[ -n "${MODE}" ]] && { usage >&2; printf '\nOnly one mode at a time.\n' >&2; exit 2; }
      MODE="${1#--}" ;;
    --duration) shift; DURATION="${1:-5}" ;;
    --gain)     shift; GAIN_OVERRIDE="${1:-}" ;;
    --dry-run)  DRY_RUN=1 ;;
    -h|--help)  usage; exit 0 ;;
    -*) usage >&2; printf '\nUnknown option: %s\n' "$1" >&2; exit 2 ;;
    *)
      [[ -n "${TARGET_ARG}" ]] && { usage >&2; printf '\nOnly one target.\n' >&2; exit 2; }
      TARGET_ARG="$1" ;;
  esac
  shift || true
done

[[ -n "${TARGET_ARG}" ]] || { usage >&2; printf '\nNo target given.\n' >&2; exit 2; }
[[ -n "${MODE}" ]] || { usage >&2; printf '\nNo mode given.\n' >&2; exit 2; }

case "${DURATION}" in
  ''|*[!0-9]*) die "--duration must be a whole number of seconds (got '${DURATION}')." ;;
esac
[[ "${DURATION}" -ge 1 && "${DURATION}" -le 30 ]] \
  || die "--duration must be between 1 and 30 seconds (got ${DURATION})."

require_macos
load_target_config "${TARGET_ARG}"

target_has_audio_config || die \
  "No verified ALSA configuration for target '${TARGET_NAME}'." \
  "The ${TARGET_PREFIX}_ALSA_CARD_ID / _CAPTURE_DEVICE / _PLAYBACK_DEVICE keys are empty" \
  "in config/targets.local.env." \
  "" \
  "For the Pi 5 these are intentionally blank -- its audio hardware has not been" \
  "inspected, and the Pi Zero 2 W's settings must not be assumed to apply." \
  "Verify on that board first:" \
  "    ssh ${TARGET_SSH_HOST} 'cat /proc/asound/cards; arecord -l; aplay -l; aplay -L'"

PLAYBACK_GAIN="${GAIN_OVERRIDE:-${TARGET_PLAYBACK_GAIN}}"
: "${PLAYBACK_GAIN:=0.2}"

REMOTE_TMP="/tmp/va-audio-diag-$$"

cleanup() {
  local rc=$?
  if [[ "${MODE}" != "info" ]] && ! is_dry_run; then
    ssh_run "rm -rf $(shq "${REMOTE_TMP}")" >/dev/null 2>&1 || true
  fi
  return ${rc}
}
trap cleanup EXIT
trap 'log_warn "Interrupted -- stopping."; exit 130' INT TERM

log_step "Audio diagnostic: ${MODE} on ${TARGET_NAME}"
log_info "Card     : ${TARGET_ALSA_CARD_ID}"
log_info "Capture  : ${TARGET_CAPTURE_DEVICE}"
log_info "Playback : ${TARGET_PLAYBACK_DEVICE}"

if ! is_dry_run; then
  ssh_is_reachable || die \
    "Cannot reach '${TARGET_SSH_HOST}' over SSH." \
    "Bring the target up first:  ./scripts/start-${TARGET_NAME}.sh --skip-app"
fi

# --------------------------------------------------------------------------
case "${MODE}" in

info)
  log_step "Read-only device information (silent)"
  if is_dry_run; then
    log_info "[dry-run] would read /proc/asound/cards, arecord -l, aplay -l, aplay -L"
    log_info "[dry-run] no audio device is opened"
    exit 0
  fi
  ssh_run "
    echo '--- /proc/asound/cards ---'; cat /proc/asound/cards
    echo; echo '--- arecord -l ---';    arecord -l
    echo; echo '--- aplay -l ---';      aplay -l
    echo; echo '--- device strings for this card ---'
    aplay -L 2>/dev/null | grep -E 'CARD=$(shq "${TARGET_ALSA_CARD_ID}")' || true
    echo; echo '--- current index of card $(shq "${TARGET_ALSA_CARD_ID}") ---'
    awk -v c=$(shq "${TARGET_ALSA_CARD_ID}") '\$2 == \"[\"c\"]:\" { print \"index \" \$1 }' /proc/asound/cards
    echo; echo '--- running PCM streams ---'
    grep -l '^state: RUNNING' /proc/asound/card*/pcm*/sub*/status 2>/dev/null || echo '(none running)'
  " 2>&1 | sed 's/^/    /' >&2
  log_ok "Done. Nothing was played."
  log_dim "Note the index above is informational -- the service addresses the card"
  log_dim "by ID (${TARGET_ALSA_CARD_ID}), because indexes shift when other cards appear."
  ;;

mic)
  log_step "Microphone capture test (${DURATION}s) -- captures only, plays nothing"
  log_info "Speak normally, 5-10 cm from the microphone."
  log_dim "The speaker stays silent for the whole test."

  if is_dry_run; then
    log_info "[dry-run] would run on the Pi:"
    log_info "[dry-run]   arecord -D ${TARGET_CAPTURE_DEVICE} -f ${TARGET_CAPTURE_FORMAT} \\"
    log_info "[dry-run]     -c ${TARGET_CAPTURE_CHANNELS} -r ${TARGET_SAMPLE_RATE} -d ${DURATION}"
    log_info "[dry-run] no audio device is opened"
    exit 0
  fi

  log_info "Recording starts in 2s..."
  sleep 2
  ssh_run "
    set -e
    mkdir -p $(shq "${REMOTE_TMP}")
    cd $(shq "${REMOTE_TMP}")
    arecord -D $(shq "${TARGET_CAPTURE_DEVICE}") \
      -f $(shq "${TARGET_CAPTURE_FORMAT}") \
      -c $(shq "${TARGET_CAPTURE_CHANNELS}") \
      -r $(shq "${TARGET_SAMPLE_RATE}") \
      -d ${DURATION} -t wav raw.wav 2>&1 | grep -vi 'recording' || true
    echo '--- per-channel levels ---'
    ch=1
    while [ \$ch -le $(shq "${TARGET_CAPTURE_CHANNELS}") ]; do
      printf 'channel %s: ' \"\$ch\"
      sox raw.wav -n remix \$ch stat 2>&1 | awk -F: '/Maximum amplitude|RMS +amplitude/ {gsub(/^ +| +$/,\"\",\$2); printf \"%s=%s  \", \$1, \$2}'
      echo
      ch=\$((ch + 1))
    done
  " 2>&1 | sed 's/^/    /' >&2

  log_ok "Capture finished. Nothing was played back."
  log_dim "Expected on the Pi Zero 2 W: channel 1 (left) carries your voice;"
  log_dim "channel 2 (right) is near-silent. That is the ICS-43434 wiring, not a fault."
  log_dim "If BOTH channels are near-silent, see the 'Silent recording' section in"
  log_dim "docs/raspberry-pi-development-workflow.md."
  ;;

speaker)
  log_step "Speaker test -- THIS MAKES SOUND"
  log_warn "Keep the speaker away from your ear."
  log_warn "The amplifier pops when the stream opens and closes. That is expected."
  log_info "A short, quiet, fading tone will play at gain ${PLAYBACK_GAIN}."
  log_info "Press Ctrl+C immediately if you hear harsh or distorted noise."

  if is_dry_run; then
    log_info "[dry-run] would synthesise a quiet 3s fading tone and play it once"
    log_info "[dry-run] no audio device is opened and NO SOUND is produced"
    exit 0
  fi

  printf '    Continue and play sound? [y/N] ' >&2
  read -r reply || reply=""
  [[ "${reply}" == "y" || "${reply}" == "Y" ]] || { log_info "Cancelled. Nothing played."; exit 0; }

  # A quiet 440 Hz tone, faded in and out, written to a file first and played
  # through ONE aplay invocation -- one stream open, one stream close, so the
  # amplifier pops once at each end rather than repeatedly.
  ssh_run "
    set -e
    mkdir -p $(shq "${REMOTE_TMP}")
    cd $(shq "${REMOTE_TMP}")
    sox -n -r $(shq "${TARGET_SAMPLE_RATE}") -b 16 -c $(shq "${TARGET_PLAYBACK_CHANNELS}") tone.wav \
      synth 3 sine 440 fade t 0.5 3 0.5 vol $(shq "${PLAYBACK_GAIN}")
    aplay -D $(shq "${TARGET_PLAYBACK_DEVICE}") -q tone.wav
  " 2>&1 | sed 's/^/    /' >&2

  log_ok "Playback finished."
  log_dim "Heard nothing but two pops? The stream opened and closed, so the device"
  log_dim "works but the level is too low or the amp is muted -- retry with a"
  log_dim "higher gain, e.g. --gain 0.5. Raise it gradually."
  ;;

loopback)
  log_step "Record-then-play test -- THIS MAKES SOUND"
  log_warn "Half duplex on purpose: recording finishes BEFORE playback starts,"
  log_warn "so the microphone never hears the speaker and cannot feed back."
  log_warn "The amplifier will pop when playback opens and closes."
  log_info "Record ${DURATION}s, then play it back at gain ${PLAYBACK_GAIN}."

  if is_dry_run; then
    log_info "[dry-run] would record ${DURATION}s, take the ${TARGET_MIC_CHANNEL} channel,"
    log_info "[dry-run] duplicate to ${TARGET_PLAYBACK_CHANNELS} channels, then play once"
    log_info "[dry-run] no audio device is opened and NO SOUND is produced"
    exit 0
  fi

  printf '    Continue? Recording is silent; playback makes sound. [y/N] ' >&2
  read -r reply || reply=""
  [[ "${reply}" == "y" || "${reply}" == "Y" ]] || { log_info "Cancelled."; exit 0; }

  # Mic channel -> index for `sox remix` (left is the first slot).
  MIC_IDX=1
  [[ "${TARGET_MIC_CHANNEL}" == "right" ]] && MIC_IDX=2

  log_info "Recording now -- speak."
  ssh_run "
    set -e
    mkdir -p $(shq "${REMOTE_TMP}")
    cd $(shq "${REMOTE_TMP}")
    arecord -D $(shq "${TARGET_CAPTURE_DEVICE}") \
      -f $(shq "${TARGET_CAPTURE_FORMAT}") \
      -c $(shq "${TARGET_CAPTURE_CHANNELS}") \
      -r $(shq "${TARGET_SAMPLE_RATE}") \
      -d ${DURATION} -t wav raw.wav 2>&1 | grep -vi 'recording' || true
    echo 'capture done'
  " 2>&1 | sed 's/^/    /' >&2

  log_ok "Capture finished. Preparing playback (mic is now idle)."
  log_warn "Sound is about to play."
  sleep 1

  # Take the mic channel, band-limit, normalize THIS OFFLINE TEST FILE only
  # (offline normalization is fine here; realtime audio must not be normalized
  # per chunk), duplicate to the playback channel count, apply gain, play once.
  ssh_run "
    set -e
    cd $(shq "${REMOTE_TMP}")
    sox raw.wav base.wav remix ${MIC_IDX} highpass 100 lowpass 8000 gain -n -3
    sox base.wav -r $(shq "${TARGET_SAMPLE_RATE}") -b 16 -c $(shq "${TARGET_PLAYBACK_CHANNELS}") play.wav \
      remix $(for _ in $(seq 1 "${TARGET_PLAYBACK_CHANNELS}"); do printf '1 '; done) \
      vol $(shq "${PLAYBACK_GAIN}")
    aplay -D $(shq "${TARGET_PLAYBACK_DEVICE}") -q play.wav
  " 2>&1 | sed 's/^/    /' >&2

  log_ok "Playback finished."
  log_dim "Heard your voice? Capture, channel selection, and playback all work."
  log_dim "Too quiet or too loud? Adjust ${TARGET_PREFIX}_PLAYBACK_GAIN in"
  log_dim "config/targets.local.env (currently ${TARGET_PLAYBACK_GAIN})."
  ;;
esac
