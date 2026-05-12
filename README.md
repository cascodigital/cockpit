# Cockpit

A self-hosted forensic UI for your AI chat sessions. Indexes [Claude
Code](https://docs.claude.com/en/docs/claude-code), [Codex
CLI](https://github.com/openai/codex), and [Gemini
CLI](https://github.com/google-gemini/gemini-cli) logs across all your machines
into one searchable, audited dashboard.

If you keep losing the conversation where you actually figured out the answer:
this is for you.

## Why

CLI assistants scatter `.jsonl` files across `~/.claude/`, `~/.codex/`,
`~/.gemini/` on every machine you use. There's no global search, no recall, no
"what did I work on last Thursday" view. Cockpit fixes that by:

- **Centralizing** all sessions on one server you control.
- **Indexing** them with BM25 + (optional) Gemini embeddings for hybrid search.
- **Auditing** the day's work via LLM into a structured JSON dashboard —
  headline, narrative, behavioral patterns, focus score, category heatmap.
- **Distilling** a memory profile from recurring themes across sessions.

The auditor's voice is intentionally sarcastic (it plays a fictional Elder AI
called Skippy). Swap the prompt if that's not your thing — it's in
`app/daily_auditor.py`.

## Screenshots

> *(Drop screenshots into `docs/screenshots/` and link them here.)*

## Architecture

```
[ client machines ]               [ cockpit server ]
                                  ┌─────────────────────┐
~/.claude/projects/  ──────────►  │ data/claude/        │
~/.codex/sessions/   ── cron ─►   │ data/codex/         │  ─► cockpit.py (Python)
~/.gemini/tmp/...    ──────────►  │ data/gemini/        │     │
                                  │                     │     ├─ index worker (BM25)
                                  │ data/daily_audit.   │ ◄───┤  every 150s
                                  │ json, search_index, │     │
                                  │ memory_profile.json │     ├─ /api/search
                                  └─────────────────────┘     ├─ /api/memory/*
                                                              └─ web UI (port 8000)
```

Sync is push-based: each client cron-rsyncs its session directory to the
server. The server-side container only reads — it never SSHes back out.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the long version.

## Quick start

### 1. Run the server

```bash
git clone https://github.com/<you>/cockpit.git
cd cockpit
cp .env.example .env
# Edit .env — at minimum, set DEEPSEEK_API_KEY *or* GEMINI_API_KEY.
docker compose up -d
```

Open `http://<your-server>:8000`. You'll see an empty UI until sessions
arrive.

### 2. Sync sessions from each client

On every machine where you use Claude / Codex / Gemini CLIs:

```bash
git clone https://github.com/<you>/cockpit.git
cd cockpit/scripts/sync
cp .env.example .env
# Edit .env — set COCKPIT_HOST / COCKPIT_USER / CLIENT_NAME

ssh-copy-id $COCKPIT_USER@$COCKPIT_HOST   # passwordless SSH

crontab -e
# Add the scripts you need (skip the ones you don't use):
*/5 * * * * /path/to/cockpit/scripts/sync/claude_sync.sh
*/5 * * * * /path/to/cockpit/scripts/sync/codex_sync.sh
*/5 * * * * /path/to/cockpit/scripts/sync/gemini_sync.sh
```

Sessions appear in the UI within ~3 minutes (cron interval + index loop).

## Configuration

All config is env-driven. See [`.env.example`](.env.example) for the full list.
TL;DR:

| Variable | Default | What it does |
|---|---|---|
| `PORT` | `8000` | HTTP port for the UI/API |
| `DEEPSEEK_API_KEY` | — | LLM provider for daily/weekly audits (primary) |
| `GEMINI_API_KEY` | — | LLM provider (fallback) + embeddings for semantic search |
| `TZ` | `UTC` | Affects daily audit date boundaries |
| `MEMORY_KEYWORDS` | (empty) | Comma-separated keywords to filter chats for the memory distiller. Empty = use all recent chats |

You need at least one of `DEEPSEEK_API_KEY` / `GEMINI_API_KEY`. Without
`GEMINI_API_KEY`, semantic search is disabled but BM25 keyword search still
works.

See [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) for the long version,
including custom skill taxonomies and category vocabularies.

## API

- `GET /` — Web UI
- `GET /api/chats` — All indexed sessions (metadata)
- `GET /api/chat/<uid>` — Full messages of one session
- `POST /api/search` — Hybrid BM25 + semantic search. Body: `{"query": "..."}`
- `GET /api/search/status` — Index health
- `GET /api/memory/daily` — Rolling 14-day audit history
- `GET /api/memory/weekly` — On-demand weekly digest (regenerates per call)
- `GET /api/memory/profile` — Long-term memory distilled by `memory_distiller.py`
- `POST /api/skill_log` — Tag the next session with a skill/agent
  (`{"skill":"coding","agent":"claude","ts":"<iso>"}`)

## Customizing the persona

The daily/weekly auditors and the memory distiller share a deliberately
sarcastic persona ("Skippy the Magnificent" — a reference to *Expeditionary
Force*). Some users find this energizing; some find it grating. To switch:

1. Open `app/daily_auditor.py` (or `weekly_digest.py` / `memory_distiller.py`).
2. Find the `prompt_text = (...)` block.
3. Replace the TONE RULES section with whatever voice you want. Keep the JSON
   FORMAT section intact — that's what the UI parses.

## Limitations and trade-offs

- **No auth.** The UI is wide-open on whatever port you bind. Put it behind a
  reverse proxy + basic auth if it's not on a private LAN.
- **One user.** No multi-tenant separation. Sessions from all clients land in
  the same index.
- **LLM cost.** One daily audit + one weekly digest per day, plus
  ~1 embedding per new chat. With DeepSeek as primary this stays well under
  $1/month for typical use.
- **Codex JSONL parser is best-effort.** Codex's log format has changed
  occasionally; if a new format ships, the parser in `app/cockpit.py`
  (`convert_codex_log`) may need an update.

## Contributing

Issues and PRs welcome. Anything that makes the indexer more robust,
generalizes the parsers, or adds a new chat source (other CLIs, Cursor, etc.)
is in scope. UI rewrites are out of scope unless they keep the single-file
Python deployability.

## License

MIT — see [`LICENSE`](LICENSE).
