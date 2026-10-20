#!/usr/bin/env bash
set -euo pipefail

# Backward-compatible headless entrypoint. Runtime selection, wiring,
# validation, rollback, and health checks all use the root .env file.
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec "$SCRIPT_DIR/scripts/apply_profile.sh"
