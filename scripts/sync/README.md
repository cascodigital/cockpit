# Sync scripts

Push Claude Code, Codex CLI, and Gemini CLI session logs from your client
machines to the Cockpit server.

## Setup (per client)

1. Copy this folder somewhere on the client (or just clone the repo).
2. `cp .env.example .env` and fill in the four variables.
3. Make sure SSH key auth works:
   `ssh-copy-id $COCKPIT_USER@$COCKPIT_HOST`
4. Drop a cron entry:

```cron
*/5 * * * * /path/to/cockpit-oss/scripts/sync/claude_sync.sh
*/5 * * * * /path/to/cockpit-oss/scripts/sync/codex_sync.sh
*/5 * * * * /path/to/cockpit-oss/scripts/sync/gemini_sync.sh
```

Skip any sync script you don't need (e.g. no Codex installed -> drop that line).

## What each script does

- **claude_sync.sh** — rsyncs `~/.claude/projects/` to
  `$COCKPIT_DATA_ROOT/claude/$CLIENT_NAME/` on the server.
- **codex_sync.sh** — rsyncs `*.jsonl` files from `~/.codex/sessions/` to
  `$COCKPIT_DATA_ROOT/codex/$CLIENT_NAME/sessions/`.
- **gemini_sync.sh** — converts Gemini's `.jsonl` chat logs into the structured
  `.json` shape Cockpit expects (locally, under
  `~/.gemini/tmp/*/chats_converted/`), then rsyncs to
  `$COCKPIT_DATA_ROOT/gemini/$CLIENT_NAME/chats/`.

## Windows / WSL

On WSL these scripts work as-is — call them from cron inside WSL, not from
Windows Task Scheduler.

If you need true Windows (no WSL), the closest equivalent is a PowerShell
scheduled task that calls `scp`/`rsync` (via cygwin or WSL interop). Not
included here; the bash scripts are the reference implementation.

## Server-side note

The server (the box running `docker-compose up`) doesn't need any sync script
of its own — Cockpit reads directly from the `data/` volume mounted into the
container. If the server *itself* also runs Claude/Codex/Gemini and you want
those logs indexed, drop the appropriate sync scripts on it too with
`CLIENT_NAME=server` (or whatever) — they just write to a local path on the
same machine.
