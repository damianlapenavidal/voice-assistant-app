#!/usr/bin/env bash
set -Eeuo pipefail

# Tear down a development session against the Raspberry Pi Zero 2 W. Runs on the Mac.
# Thin wrapper -- all logic lives in stop-target.sh.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/stop-target.sh" pizero2w "$@"
