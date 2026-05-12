#!/bin/bash
# Sync Claude Code session logs (~/.claude/projects/) -> Cockpit server.
# Run via cron on each client machine.
# Example cron: */5 * * * * /path/to/cockpit-oss/scripts/sync/claude_sync.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
[ -f "$SCRIPT_DIR/.env" ] && . "$SCRIPT_DIR/.env"

: "${COCKPIT_HOST:?COCKPIT_HOST not set in scripts/sync/.env}"
: "${COCKPIT_USER:?COCKPIT_USER not set}"
: "${COCKPIT_DATA_ROOT:?COCKPIT_DATA_ROOT not set}"
: "${CLIENT_NAME:?CLIENT_NAME not set}"

SRC_DIR="$HOME/.claude/projects"
REMOTE_DIR="$COCKPIT_DATA_ROOT/claude/$CLIENT_NAME"
REMOTE="$COCKPIT_USER@$COCKPIT_HOST:$REMOTE_DIR/"

[ -d "$SRC_DIR" ] || exit 0

ssh -o BatchMode=yes -o ConnectTimeout=5 "$COCKPIT_USER@$COCKPIT_HOST" \
    "mkdir -p $REMOTE_DIR" 2>/dev/null || exit 0

rsync -a --quiet "$SRC_DIR/" "$REMOTE" 2>/dev/null
