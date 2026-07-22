#!/usr/bin/env bash
# SSH helpers. Every connection is non-interactive (BatchMode) with a finite
# timeout, so a wrong network or a missing key fails fast instead of hanging on
# a password prompt.

[[ -n "${_VA_SSH_SH:-}" ]] && return 0
_VA_SSH_SH=1

SSH_CONNECT_TIMEOUT="${SSH_CONNECT_TIMEOUT:-6}"

_ssh_opts() {
  printf '%s\n' \
    -o BatchMode=yes \
    -o ConnectTimeout="${SSH_CONNECT_TIMEOUT}" \
    -o StrictHostKeyChecking=accept-new \
    -o LogLevel=ERROR
}

# Run a command on the target. Read-only by convention; callers gate mutating
# commands behind `would`.
#
# SC2029: callers build the remote command deliberately, interpolating local
# values through shq() so they are already quoted for the remote shell. Local
# expansion at call time is the intended behaviour, not an oversight.
# shellcheck disable=SC2029
ssh_run() {
  local opts=()
  while IFS= read -r o; do opts+=("$o"); done < <(_ssh_opts)
  ssh "${opts[@]}" "${TARGET_SSH_HOST}" "$@"
}

# Quiet reachability probe. Returns non-zero instead of printing errors, so it
# can be used in an `if`.
ssh_is_reachable() {
  local opts=()
  while IFS= read -r o; do opts+=("$o"); done < <(_ssh_opts)
  ssh "${opts[@]}" "${TARGET_SSH_HOST}" true >/dev/null 2>&1
}

# Verify SSH and print the remote system identity (Step 4).
verify_ssh() {
  log_step "Verifying SSH to ${TARGET_SSH_HOST}"

  local info
  # SC2016: single quotes are required here -- these expressions must expand on
  # the Pi, not on the Mac.
  # shellcheck disable=SC2016
  if ! info="$(ssh_run 'printf "%s\n%s\n%s\n%s\n" "$(hostname)" "$(. /etc/os-release 2>/dev/null && printf "%s" "$PRETTY_NAME")" "$(uname -r)" "$(uname -m)"' 2>&1)"; then
    die "SSH to '${TARGET_SSH_HOST}' failed." \
      "${info}" \
      "" \
      "Check, in order:" \
      "  1. Is the Pi powered on and on the '${TARGET_WIFI_SSID}' network?" \
      "  2. Does the alias resolve?    ssh -v ${TARGET_SSH_HOST}" \
      "  3. Is your key authorized?    ssh-copy-id ${TARGET_SSH_HOST}" \
      "  4. Is the alias in ~/.ssh/config with the right User and HostName?" \
      "" \
      "This launcher never uses passwords -- passwordless SSH must work."
  fi

  local hostname_r os_r kernel_r arch_r
  { read -r hostname_r; read -r os_r; read -r kernel_r; read -r arch_r; } <<<"${info}"

  log_ok "Connected to ${hostname_r}"
  log_info "OS         : ${os_r:-unknown}"
  log_info "Kernel     : ${kernel_r}"
  log_info "Arch       : ${arch_r}"
  log_info "Repo path  : ${TARGET_REMOTE_REPO}"
  log_info "Target     : ${TARGET_NAME}"
}
