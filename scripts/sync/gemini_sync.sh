#!/bin/bash
# Sync Gemini CLI session logs (~/.gemini/tmp/) -> Cockpit server.
# Converts .jsonl into the structured .json shape Cockpit indexes.
# Example cron: */5 * * * * /path/to/cockpit-oss/scripts/sync/gemini_sync.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && . "$SCRIPT_DIR/.env"

: "${COCKPIT_HOST:?COCKPIT_HOST not set in scripts/sync/.env}"
: "${COCKPIT_USER:?COCKPIT_USER not set}"
: "${COCKPIT_DATA_ROOT:?COCKPIT_DATA_ROOT not set}"
: "${CLIENT_NAME:?CLIENT_NAME not set}"

# Gemini stores chats under ~/.gemini/tmp/<username>/chats — find first match.
SRC_DIR=$(find "$HOME/.gemini/tmp" -maxdepth 2 -type d -name chats 2>/dev/null | head -1)
[ -d "$SRC_DIR" ] || exit 0

CONVERTED_DIR="${SRC_DIR}_converted"
REMOTE_DIR="$COCKPIT_DATA_ROOT/gemini/$CLIENT_NAME/chats"
REMOTE="$COCKPIT_USER@$COCKPIT_HOST:$REMOTE_DIR/"

mkdir -p "$CONVERTED_DIR"

python3 - "$SRC_DIR" "$CONVERTED_DIR" <<'PYEOF'
import json, os, glob, sys

src, dst = sys.argv[1], sys.argv[2]

for jsonl_path in glob.glob(os.path.join(src, "*.jsonl")):
    fname = os.path.basename(jsonl_path).replace(".jsonl", ".json")
    out_path = os.path.join(dst, fname)

    src_mtime = os.path.getmtime(jsonl_path)
    if os.path.exists(out_path) and os.path.getmtime(out_path) >= src_mtime:
        continue

    session_meta = {}
    messages = []

    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except:
                continue

            if "$set" in obj:
                continue
            if "sessionId" in obj and "kind" in obj:
                session_meta = obj
                continue

            msg_type = obj.get("type", "")
            if msg_type not in ("user", "model", "assistant", "gemini", "tool", "info"):
                continue

            content = obj.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                ).strip()

            if msg_type == "model":
                msg_type = "assistant"

            messages.append({
                "type": msg_type,
                "content": content,
                "timestamp": obj.get("timestamp", ""),
            })

    if not messages and not session_meta:
        continue

    output = {
        "sessionId": session_meta.get("sessionId", fname.replace(".json", "")),
        "startTime": session_meta.get("startTime", ""),
        "lastUpdated": session_meta.get("lastUpdated", ""),
        "messages": messages,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
PYEOF

if ls "$CONVERTED_DIR"/*.json &>/dev/null; then
    ssh -o BatchMode=yes -o ConnectTimeout=5 "$COCKPIT_USER@$COCKPIT_HOST" \
        "mkdir -p $REMOTE_DIR" 2>/dev/null
    rsync -a --quiet "$CONVERTED_DIR/" "$REMOTE" 2>/dev/null
fi
