#!/usr/bin/env bash
# Shared helpers: logging, dry-run plumbing, repo root discovery.
# Sourced by scripts/start-target.sh and the other lib/*.sh files.
# Not executable on its own.

# Guard against double-sourcing.
[[ -n "${_VA_COMMON_SH:-}" ]] && return 0
_VA_COMMON_SH=1

# Repo root, derived from this file's location so every script works from any
# working directory. lib/ -> scripts/ -> repo root.
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
# SC2034: consumed by the sourcing scripts, which shellcheck analyses separately.
# shellcheck disable=SC2034
readonly REPO_ROOT

# Populated by parse_common_args / the launcher.
DRY_RUN="${DRY_RUN:-0}"

# --- Colours ---------------------------------------------------------------
# Only colourize a real terminal, so piped output and CI logs stay clean.
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'
else
  C_RESET=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_DIM=""; C_BOLD=""
fi

# --- Logging ---------------------------------------------------------------
# Everything except explicitly-requested output goes to stderr, so a caller can
# capture a helper's stdout value without swallowing the progress messages.

log_step()  { printf '%s\n%s==> %s%s\n' "" "${C_BLUE}${C_BOLD}" "$*" "${C_RESET}" >&2; }
log_info()  { printf '    %s\n' "$*" >&2; }
log_ok()    { printf '    %s%s%s\n' "${C_GREEN}" "$*" "${C_RESET}" >&2; }
log_warn()  { printf '    %swarning: %s%s\n' "${C_YELLOW}" "$*" "${C_RESET}" >&2; }
log_dim()   { printf '    %s%s%s\n' "${C_DIM}" "$*" "${C_RESET}" >&2; }

# log_error prints a multi-line, actionable message. First arg is the summary;
# each remaining arg becomes an indented follow-up line telling the user what
# to actually do about it.
log_error() {
  printf '\n%serror: %s%s\n' "${C_RED}${C_BOLD}" "$1" "${C_RESET}" >&2
  shift
  local line
  for line in "$@"; do
    printf '       %s\n' "${line}" >&2
  done
  printf '\n' >&2
}

die() { log_error "$@"; exit 1; }

# --- Dry run ---------------------------------------------------------------

is_dry_run() { [[ "${DRY_RUN}" == "1" ]]; }

# Announce a mutating action. In dry-run mode this is ALL that happens.
# Usage: if would "restart the service"; then <do it>; fi
would() {
  if is_dry_run; then
    printf '    %s[dry-run] would %s%s\n' "${C_YELLOW}" "$*" "${C_RESET}" >&2
    return 1
  fi
  return 0
}

# --- Misc ------------------------------------------------------------------

have() { command -v "$1" >/dev/null 2>&1; }

require_macos() {
  [[ "$(uname -s)" == "Darwin" ]] || die \
    "This launcher runs on the Mac, not on a Raspberry Pi." \
    "Detected: $(uname -s)"
}

# Quote a string for safe interpolation into a remote shell command.
# Wraps in single quotes and escapes embedded single quotes.
shq() {
  local s=${1//\'/\'\\\'\'}
  printf "'%s'" "${s}"
}
