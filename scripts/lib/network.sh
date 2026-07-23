#!/usr/bin/env bash
# macOS Wi-Fi helpers. These run on the MAC only.
#
# Wi-Fi is only touched when the target is genuinely unreachable -- switching
# networks on every launch would be both slow and disruptive.

[[ -n "${_VA_NETWORK_SH:-}" ]] && return 0
_VA_NETWORK_SH=1

# Detect the Wi-Fi interface dynamically (usually en0, but not guaranteed).
wifi_interface() {
  local iface
  iface="$(networksetup -listallhardwareports 2>/dev/null \
    | awk '/Hardware Port: Wi-Fi/{getline; print $2; exit}')"
  [[ -n "${iface}" ]] || die \
    "Could not find a Wi-Fi interface via networksetup." \
    "Run: networksetup -listallhardwareports"
  printf '%s' "${iface}"
}

# Current SSID.
#
# macOS 14+ removed SSID reporting from `airport -I` (and macOS 26 removed the
# airport binary), so this parses `ipconfig getsummary`, which still exposes it.
# Prints nothing when not associated.
current_ssid() {
  local iface="$1"
  ipconfig getsummary "${iface}" 2>/dev/null \
    | awk -F' SSID : ' '/ SSID : /{print $2; exit}'
}

list_saved_ssids() {
  local iface="$1"
  networksetup -listpreferredwirelessnetworks "${iface}" 2>/dev/null | sed '1d;s/^[[:space:]]*//'
}

ssid_is_saved() {
  local iface="$1" want="$2" line
  while IFS= read -r line; do
    [[ "${line}" == "${want}" ]] && return 0
  done < <(list_saved_ssids "${iface}")
  return 1
}

# Join the target's saved network and wait for SSH to come back.
#
# Deliberately never accepts or stores a password: it relies on the network
# already being saved in the macOS Keychain. For enterprise networks like
# URI_Secure this is the only sane approach -- 802.1X credentials stay in
# Keychain where macOS manages them.
ensure_network_for_target() {
  local iface current
  iface="$(wifi_interface)"
  current="$(current_ssid "${iface}")"

  log_step "Switching Wi-Fi to '${TARGET_WIFI_SSID}'"
  log_info "Interface      : ${iface}"
  log_info "Current SSID   : ${current:-<not associated>}"

  if [[ "${current}" == "${TARGET_WIFI_SSID}" ]]; then
    log_ok "Already on '${TARGET_WIFI_SSID}' (but SSH did not respond -- see below)"
    _network_join_hint
    return 0
  fi

  if ! ssid_is_saved "${iface}" "${TARGET_WIFI_SSID}"; then
    die "'${TARGET_WIFI_SSID}' is not a saved network on this Mac." \
      "This launcher only joins networks macOS already knows (it never handles" \
      "Wi-Fi passwords). Join it once from the macOS Wi-Fi menu, then retry." \
      "" \
      "Saved networks on ${iface}:" \
      "$(list_saved_ssids "${iface}" | sed 's/^/  /')" \
      "" \
      "If the name is close but not exact, fix ${TARGET_PREFIX}_WIFI_SSID in" \
      "config/targets.local.env -- SSIDs are matched exactly."
  fi

  if ! would "join Wi-Fi network '${TARGET_WIFI_SSID}' on ${iface}"; then
    return 0
  fi

  # No password argument: macOS pulls the saved credential from Keychain.
  if ! networksetup -setairportnetwork "${iface}" "${TARGET_WIFI_SSID}" >/dev/null 2>&1; then
    log_warn "networksetup reported a failure joining '${TARGET_WIFI_SSID}'"
  fi

  # Backoff while the association and DHCP settle.
  local delay=2 attempt
  for attempt in 1 2 3 4 5; do
    sleep "${delay}"
    current="$(current_ssid "${iface}")"
    if [[ "${current}" == "${TARGET_WIFI_SSID}" ]]; then
      log_ok "Joined '${TARGET_WIFI_SSID}'"
      return 0
    fi
    log_dim "attempt ${attempt}/5: SSID is '${current:-<none>}', retrying in ${delay}s"
    delay=$(( delay < 8 ? delay * 2 : 8 ))
  done

  die "Could not join '${TARGET_WIFI_SSID}' after 5 attempts." \
    "Current SSID: ${current:-<not associated>}" \
    "$(_network_join_hint_text)"
}

_network_join_hint_text() {
  if [[ "${TARGET_NAME}" == "pizero2w" ]]; then
    printf '%s\n' \
      "" \
      "The Pi Zero 2 W normally lives on the iPhone hotspot:" \
      "  1. Enable Personal Hotspot on the iPhone (Settings > Personal Hotspot)." \
      "  2. Keep the iPhone unlocked and nearby until the Pi joins." \
      "  3. Confirm the Pi Zero 2 W connected, then retry." \
      "" \
      "Note this Mac has two similarly-named saved hotspots" \
      "('iPhone de Damian' and 'iPhone de Damián'). They are different networks;" \
      "PIZERO2W_WIFI_SSID must match the one the iPhone actually broadcasts."
  else
    printf '%s\n' \
      "" \
      "The Pi 5 normally lives on the URI Secure network:" \
      "  1. Confirm the Pi 5 is powered on and associated with '${TARGET_WIFI_SSID}'." \
      "  2. URI Secure is an enterprise (802.1X) network -- macOS must already" \
      "     hold valid credentials for it in Keychain." \
      "  3. Confirm the Mac and the Pi 5 are on the same VLAN/subnet."
  fi
}

_network_join_hint() { _network_join_hint_text >&2; }

# Best-effort: the IPv4 address the Pi should dial back to reach this Mac.
# Falls back to ifconfig, since `ipconfig getifaddr` returns nothing in some
# environments even when the interface has an address.
mac_lan_ip() {
  local iface="$1" ip
  ip="$(ipconfig getifaddr "${iface}" 2>/dev/null || true)"
  if [[ -z "${ip}" ]]; then
    ip="$(ifconfig "${iface}" 2>/dev/null \
      | awk '/inet /{print $2; exit}')"
  fi
  printf '%s' "${ip}"
}
