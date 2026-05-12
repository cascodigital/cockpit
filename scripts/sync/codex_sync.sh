#!/bin/bash
# Sync Codex CLI session logs (~/.codex/sessions/) -> Cockpit server.
# Example cron: */5 * * * * /path/to/cockpit-oss/scripts/sync/codex_sync.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && . "$SCRIPT_DIR/.env"

: "${COCKPIT_HOST:?COCKPIT_HOST not set in scripts/sync/.env}"
: "${COCKPIT_USER:?COCKPIT_USER not set}"
: "${COCKPIT_DATA_ROOT:?COCKPIT_DATA_ROOT not set}"
: "${CLIENT_NAME:?CLIENT_NAME not set}"

SRC_DIR="$HOME/.codex/sessions"
REMOTE_DIR="$COCKPIT_DATA_ROOT/codex/$CLIENT_NAME/sessions"
REMOTE="$COCKPIT_USER@$COCKPIT_HOST:$REMOTE_DIR/"

[ -d "$SRC_DIR" ] || exit 0

ssh -o BatchMode=yes -o ConnectTimeout=5 "$COCKPIT_USER@$COCKPIT_HOST" \
    "mkdir -p $REMOTE_DIR" 2>/dev/null || exit 0

rsync -a --quiet \
    --include="*/" \
    --include="*.jsonl" \
    --exclude="*" \
    "$SRC_DIR/" "$REMOTE" 2>/dev/null
