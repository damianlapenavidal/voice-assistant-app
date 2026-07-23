#!/usr/bin/env bash
# SSH reverse-tunnel fallback for targets whose network cannot carry a routable
# IPv4 back to this Mac (e.g. an iPhone Personal Hotspot running IPv6-only
# NAT64/464XLAT -- `ipconfig getifaddr` reports the 192.0.0.2 CLAT placeholder
# instead of a real LAN address, and mDNS does not resolve Mac -> Pi either).
#
# SSH itself already connects reliably regardless of the hotspot's addressing
# mode, so `ssh -R` carries the WebSocket connection instead of asking the Pi
# to dial a Mac IP: the Pi's endpoint always targets ws://127.0.0.1:<port>,
# and the reverse tunnel forwards that to the Mac's real listening app.

[[ -n "${_VA_TUNNEL_SH:-}" ]] && return 0
_VA_TUNNEL_SH=1

TUNNEL_PID=""

# Step: open `ssh -R <port>:localhost:<port>` in the background, left running
# for the lifetime of the launcher. No-op unless the target opts in.
start_reverse_tunnel() {
  [[ "${TARGET_USE_SSH_TUNNEL}" == "1" ]] || return 0

  log_step "Opening SSH reverse tunnel (${TARGET_NAME} network cannot carry a routable IPv4 back to this Mac)"

  if is_dry_run; then
    log_info "[dry-run] would run: ssh -N -R ${TARGET_ENDPOINT_PORT}:localhost:${TARGET_ENDPOINT_PORT} ${TARGET_SSH_HOST}"
    return 0
  fi

  local opts=()
  while IFS= read -r o; do opts+=("$o"); done < <(_ssh_opts)

  # Until the local app actually starts listening (the last step of this
  # launcher), every connection the Pi's endpoint attempts through this
  # tunnel gets a normal, expected "connect_to ... failed" from ssh. Log that
  # to its own file instead of the main output, where it reads as an error.
  TUNNEL_LOG="${TMPDIR:-/tmp}/voice-assistant-tunnel-${TARGET_NAME}.log"
  : > "${TUNNEL_LOG}"

  ssh "${opts[@]}" -o ExitOnForwardFailure=yes -N \
    -R "${TARGET_ENDPOINT_PORT}:localhost:${TARGET_ENDPOINT_PORT}" \
    "${TARGET_SSH_HOST}" >"${TUNNEL_LOG}" 2>&1 &
  TUNNEL_PID=$!

  # ExitOnForwardFailure makes ssh exit immediately if the remote port is
  # already bound (e.g. a tunnel from a previous crashed run); give it a beat
  # to fail fast rather than declaring success on a doomed process.
  sleep 1
  if ! kill -0 "${TUNNEL_PID}" 2>/dev/null; then
    TUNNEL_PID=""
    die "SSH reverse tunnel failed to start." \
      "$(sed 's/^/  /' "${TUNNEL_LOG}" 2>/dev/null)" \
      "" \
      "The remote port ${TARGET_ENDPOINT_PORT} on ${TARGET_NAME} may already be bound" \
      "by a tunnel from a previous run that did not exit cleanly. Check:" \
      "    ssh ${TARGET_SSH_HOST} 'lsof -i :${TARGET_ENDPOINT_PORT}' || ssh ${TARGET_SSH_HOST} 'ss -ltnp | grep ${TARGET_ENDPOINT_PORT}'"
  fi

  log_ok "Tunnel open (pid ${TUNNEL_PID}): ${TARGET_NAME}:${TARGET_ENDPOINT_PORT} -> this Mac:${TARGET_ENDPOINT_PORT}"
  log_dim "(connection-refused noise is expected here until the local app starts listening; log: ${TUNNEL_LOG})"
}

stop_reverse_tunnel() {
  if [[ -n "${TUNNEL_PID}" ]]; then
    kill "${TUNNEL_PID}" >/dev/null 2>&1 || true
    wait "${TUNNEL_PID}" 2>/dev/null || true
    TUNNEL_PID=""
  fi
}
