#!/usr/bin/env bash
# Compatibility entrypoint for existing operators.
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
BOOTSTRAP="${SCRIPT_DIR}/reactor-bootstrap.sh"

if [[ ! -f "$BOOTSTRAP" ]]; then
    printf 'Missing %s. Use the pinned GitHub Release bootstrap command from README.md.\n' "$BOOTSTRAP" >&2
    exit 1
fi

printf 'setup_pi.sh is now a compatibility wrapper for the production-only bootstrap.\n'
exec bash "$BOOTSTRAP" "$@"
