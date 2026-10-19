#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ENV_FILE="$SCRIPT_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "Missing $ENV_FILE; nothing was stopped." >&2
    exit 1
fi

cd "$SCRIPT_DIR"
env -u COMPOSE_FILE -u COMPOSE_ENV_FILES -u COMPOSE_PROJECT_NAME \
    COMPOSE_PROFILES=simulation,hardware \
    docker compose --env-file "$ENV_FILE" down --remove-orphans
