#!/usr/bin/env bash
# ALSA preflight. Runs read-only checks ON THE PI over SSH.
#
# SAFETY: nothing here ever produces sound. No test tones, no playback, no
# `speaker-test`. It only reads /proc/asound, lists devices, and asks ALSA
# whether a configuration *would* work. Deployment and readiness checks must
# never make the speaker emit anything -- see scripts/audio-diagnostic.sh for
# the explicit, opt-in tests.

[[ -n "${_VA_AUDIO_SH:-}" ]] && return 0
_VA_AUDIO_SH=1

# Print the diagnostics the user needs when audio is broken.
audio_dump_diagnostics() {
  log_dim "Collecting ALSA diagnostics from ${TARGET_NAME}..."
  ssh_run '
    echo "--- /proc/asound/cards ---"; cat /proc/asound/cards 2>&1
    echo "--- arecord -l ---";        arecord -l 2>&1
    echo "--- aplay -l ---";          aplay -l 2>&1
    echo "--- processes holding /dev/snd ---"
    if command -v fuser >/dev/null 2>&1; then fuser -v /dev/snd/* 2>&1 || echo "(none)"; else echo "(fuser not installed)"; fi
    echo "--- kernel audio messages ---"
    (dmesg 2>/dev/null || sudo -n dmesg 2>/dev/null || echo "(dmesg needs privileges: run: ssh '"${TARGET_SSH_HOST}"' sudo dmesg)") \
      | grep -Ei "google|voicehat|i2s|alsa|snd|audio" | tail -40
  ' 2>&1 | sed 's/^/    /' >&2 || true
}

# Step 6: verify the card, devices, access, and rate WITHOUT making sound.
audio_preflight() {
  if ! target_has_audio_config; then
    log_step "Audio preflight"
    log_dim "Skipped: no verified ALSA configuration for target '${TARGET_NAME}'."
    log_dim "The Pi 5's audio hardware has not been inspected, so no settings are"
    log_dim "assumed. Fill in the ${TARGET_PREFIX}_* ALSA keys once verified on that board."
    return 0
  fi

  log_step "Audio preflight on ${TARGET_NAME} (read-only, silent)"

  if is_dry_run; then
    log_info "[dry-run] would check card '${TARGET_ALSA_CARD_ID}' and query capture/playback"
    log_info "[dry-run] no audio device is opened"
    return 0
  fi

  local card="${TARGET_ALSA_CARD_ID}"
  local out rc=0

  # Single round-trip; each check prints a KEY=value line.
  out="$(ssh_run "
    card=$(shq "${card}")
    capture_dev=$(shq "${TARGET_CAPTURE_DEVICE}")
    playback_dev=$(shq "${TARGET_PLAYBACK_DEVICE}")
    rate=$(shq "${TARGET_SAMPLE_RATE}")
    fmt=$(shq "${TARGET_CAPTURE_FORMAT}")
    ch=$(shq "${TARGET_CAPTURE_CHANNELS}")

    # Resolve the card ID to its CURRENT numeric index, for logging only --
    # the service always addresses the card by ID, never by this number.
    idx=\$(awk -v c=\"\$card\" '\$2 == \"[\"c\"]:\" { print \$1 }' /proc/asound/cards | head -1)
    if [ -z \"\$idx\" ]; then
      idx=\$(sed -n \"s/^ *\\([0-9]\\+\\) \\[\$card *\\].*/\\1/p\" /proc/asound/cards | head -1)
    fi
    if [ -n \"\$idx\" ]; then echo \"CARD_OK=\$idx\"; else echo \"CARD_MISSING=1\"; fi

    # Match on the resolved index. In 'arecord -l' the card ID appears bare
    # after 'card N:' while the BRACKETS hold the card's long name, so grepping
    # for \"[\$card]\" here would never match (it does in /proc/asound/cards).
    if [ -n \"\$idx\" ] && arecord -l 2>/dev/null | grep -q \"^card \$idx:\"; then
      echo CAPTURE_LISTED=1
    else
      echo CAPTURE_LISTED=0
    fi
    if [ -n \"\$idx\" ] && aplay -l 2>/dev/null | grep -q \"^card \$idx:\"; then
      echo PLAYBACK_LISTED=1
    else
      echo PLAYBACK_LISTED=0
    fi

    # Device-node access as the service user.
    if [ -r /dev/snd/controlC\${idx:-0} ] && [ -w /dev/snd/controlC\${idx:-0} ]; then
      echo ACCESS=1
    else
      echo ACCESS=0
    fi
    echo \"GROUPS=\$(id -nG)\"

    # Does the exact capture configuration open at the requested rate?
    # --duration=0 + --test-position would still open the device; instead ask
    # ALSA to dump the hw params it WOULD use. This opens the capture device
    # briefly but never the speaker, and produces no sound.
    if arecord -D \"\$capture_dev\" -f \"\$fmt\" -c \"\$ch\" -r \"\$rate\" --dump-hw-params -d 1 /dev/null 2>&1 | grep -q 'RATE'; then
      echo CAPTURE_CONFIG=1
    else
      echo CAPTURE_CONFIG=0
    fi

    # Playback capability is checked WITHOUT opening the speaker: read the
    # card's own PCM capability file. Opening playback would pop.
    if [ -r \"/proc/asound/card\${idx:-0}/pcm0p/sub0/hw_params\" ] || [ -e \"/dev/snd/pcmC\${idx:-0}D0p\" ]; then
      echo PLAYBACK_NODE=1
    else
      echo PLAYBACK_NODE=0
    fi

    # Is anything already holding the PCM devices exclusively?
    busy=\$(cat /proc/asound/card\${idx:-0}/pcm*/sub*/status 2>/dev/null | grep -c '^state: RUNNING' || true)
    echo \"BUSY=\${busy:-0}\"
  " 2>&1)" || rc=$?

  if [[ ${rc} -ne 0 ]]; then
    log_warn "Audio preflight could not run over SSH"
    printf '%s\n' "${out}" | sed 's/^/    /' >&2
    audio_dump_diagnostics
    die "Audio preflight failed on ${TARGET_NAME}."
  fi

  # Parse the KEY=value lines.
  local card_idx="" card_missing=0 capture_listed=0 playback_listed=0
  local access=0 groups="" capture_config=0 playback_node=0 busy=0 line
  while IFS= read -r line; do
    case "${line}" in
      CARD_OK=*)         card_idx="${line#CARD_OK=}" ;;
      CARD_MISSING=*)    card_missing=1 ;;
      CAPTURE_LISTED=*)  capture_listed="${line#CAPTURE_LISTED=}" ;;
      PLAYBACK_LISTED=*) playback_listed="${line#PLAYBACK_LISTED=}" ;;
      ACCESS=*)          access="${line#ACCESS=}" ;;
      GROUPS=*)          groups="${line#GROUPS=}" ;;
      CAPTURE_CONFIG=*)  capture_config="${line#CAPTURE_CONFIG=}" ;;
      PLAYBACK_NODE=*)   playback_node="${line#PLAYBACK_NODE=}" ;;
      BUSY=*)            busy="${line#BUSY=}" ;;
    esac
  done <<<"${out}"

  if [[ ${card_missing} -eq 1 || -z "${card_idx}" ]]; then
    audio_dump_diagnostics
    die "ALSA card '${card}' was not found on ${TARGET_NAME}." \
      "Expected the googlevoicehat overlay to provide it." \
      "" \
      "Check the boot config ON THE PI:" \
      "    ssh ${TARGET_SSH_HOST} 'grep -E \"dtoverlay|dtparam=audio\" /boot/firmware/config.txt'" \
      "" \
      "It should contain:  dtoverlay=googlevoicehat-soundcard" \
      "A missing card after a config change usually means a reboot is pending," \
      "or the I2S wiring (BCLK GPIO18 / LRCLK GPIO19) is disconnected."
  fi
  log_ok "Card '${card}' present (currently index ${card_idx} -- addressed by ID, not index)"

  [[ "${capture_listed}" == "1" ]] \
    || { audio_dump_diagnostics; die "Card '${card}' exposes no capture device on ${TARGET_NAME}."; }
  [[ "${playback_listed}" == "1" ]] \
    || { audio_dump_diagnostics; die "Card '${card}' exposes no playback device on ${TARGET_NAME}."; }
  log_ok "Capture and playback devices both listed"

  if [[ "${access}" != "1" ]]; then
    audio_dump_diagnostics
    die "The service user cannot access the ALSA device nodes on ${TARGET_NAME}." \
      "Current groups: ${groups}" \
      "" \
      "Add the user to the audio group ON THE PI, then log out and back in:" \
      "    ssh ${TARGET_SSH_HOST} 'sudo usermod -aG audio \$USER'"
  fi
  log_ok "Device nodes accessible (groups: ${groups})"

  if [[ "${capture_config}" == "1" ]]; then
    log_ok "Capture supports ${TARGET_SAMPLE_RATE} Hz / ${TARGET_CAPTURE_FORMAT} / ${TARGET_CAPTURE_CHANNELS}ch"
  else
    log_warn "Could not confirm ${TARGET_SAMPLE_RATE} Hz ${TARGET_CAPTURE_FORMAT} ${TARGET_CAPTURE_CHANNELS}ch on ${TARGET_CAPTURE_DEVICE}"
    log_dim "The device may be busy, or the plug plugin may be resampling."
    log_dim "Inspect with: ./scripts/audio-diagnostic.sh ${TARGET_NAME} --info"
  fi

  if [[ "${playback_node}" == "1" ]]; then
    log_ok "Playback device node present (not opened -- opening it pops the amplifier)"
  else
    log_warn "Playback PCM node not found; playback may fail at runtime"
  fi

  if [[ "${busy}" != "0" ]]; then
    log_warn "${busy} PCM substream(s) currently RUNNING on card '${card}'"
    log_dim "Another process may hold the device. Identify it with:"
    log_dim "  ssh ${TARGET_SSH_HOST} 'fuser -v /dev/snd/*'"
  fi

  log_info "Mic channel    : ${TARGET_MIC_CHANNEL} (of ${TARGET_CAPTURE_CHANNELS} captured; the other slot is silent by design)"
  log_info "Input gain     : ${TARGET_INPUT_GAIN}"
  log_info "Playback gain  : ${TARGET_PLAYBACK_GAIN}"
}
