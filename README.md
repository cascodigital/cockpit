<div align="center">

# Cockpit — Forensic UI for AI Chat Sessions

**Self-hosted search, audit, and memory UI for local AI assistant sessions.**

![Status](https://img.shields.io/badge/Status-Active-16A34A?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-2563EB?style=flat-square)
![Casco Digital](https://img.shields.io/badge/Casco-Digital-111827?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=flat-square&logo=docker&logoColor=white)
![DeepSeek](https://img.shields.io/badge/DeepSeek-API-4D6BFE?style=flat-square)
![Gemini](https://img.shields.io/badge/Gemini-Embeddings-8E75B2?style=flat-square&logo=googlegemini&logoColor=white)

</div>

---

Self-hosted forensic UI that indexes [Claude Code](https://docs.claude.com/en/docs/claude-code), [Codex CLI](https://github.com/openai/codex), and [Gemini CLI](https://github.com/google-gemini/gemini-cli) sessions across every machine you use — plus optional web-app captures. Hybrid search (BM25 + embeddings), natural-language questions answered from your own history, daily LLM-generated audits, and a distilled memory profile — all in one dashboard you control.

![Sidebar](docs/screenshots/01-sidebar-chat.png)

## Why

CLI assistants scatter `.jsonl` files across `~/.claude/`, `~/.codex/`, `~/.gemini/` on every machine. There's no global search, no recall, no "what did I work on last Thursday" view. Cockpit fixes that.

## What you can do with it

- **Ask your own history a question.** *"Did we ever fix that office smart plug? How did it end?"* — `Ctrl+Enter` sends the question to the Panopticon, which answers from your distilled journal with the dates that back each claim.
- **Find the buried conversation.** Deep search fuses chunk-level BM25 with embeddings, so a topic discussed at minute 40 of a long mixed session still surfaces — and matching journal bullets render above the chat hits, so you often get the *answer*, not just where it lives.
- **Read a daily audit of yourself.** Every night an LLM turns the day's sessions into an ops brief: headline, narrative, workstreams with advanced/blocked status, focus score, and a category heatmap across two weeks.
- **Feed your agents long-term memory.** The turtle button shows the exact two-layer context (permanent core + rolling 30-day window) you can inject into your assistant's boot prompt; the memory profile distills recurring patterns and open homework from coaching-style sessions.

## Features

| Feature | Description |
|---------|-------------|
| **Hybrid Search** | Chunk-level BM25 (accent-insensitive) + optional Gemini embeddings, fused via Reciprocal Rank Fusion, with optional LLM reranking |
| **Ask the Panopticon** | Natural-language Q&A over your journal with cited dates (`/api/ask`, Ctrl+Enter in the UI). Works with any OpenAI-compatible endpoint, including a local Ollama |
| **Journal Integration** | Optional distilled-notes vault (`BRAIN_DATA`): deep search returns matching bullets, `/api/journal` serves the files. No API involved |
| **Daily Ops Brief** | LLM-generated structured JSON: headline, narrative, workstreams (advanced/maintained/blocked/noise), focus score — with anti-repetition control fed by recent audits |
| **Weekly Digest** | Cross-cutting analysis over the last 7 daily audits |
| **Memory Profile** | Long-term distillation of recurring themes, blockers, and open threads |
| **Injected Memory View** | Two-layer assistant context (permanent core + recent window) rendered in one scroll |
| **Category Heatmap** | Visual breakdown of session topics across the last 14 days |
| **Per-source Badges** | Color-coded sessions by AI (Claude/Gemini/Codex/web) and host (WIN/LNX/DKR/WEB) |
| **Custom Voice** | Opinionated auditor persona for the daily/weekly summaries — fully swappable in the prompt |

## Screenshots

| Ask the Panopticon | Daily Audit Dashboard |
|:---:|:---:|
| ![Ask](docs/screenshots/02-ask-panopticon.png) | ![Daily Audit](docs/screenshots/03-daily-audit.png) |

| Memory Profile | Injected Memory |
|:---:|:---:|
| ![Memory](docs/screenshots/04-memory-profile.png) | ![Injected Memory](docs/screenshots/05-injected-memory.png) |

## Architecture

```
[ client machines ]               [ cockpit server ]
                                  +---------------------+
~/.claude/projects/  ----rsync-->  | data/claude/        |
~/.codex/sessions/   ----cron --->  | data/codex/         |  --> cockpit.py
~/.gemini/tmp/...    ----rsync-->  | data/gemini/        |        |
web captures (opt.)  ---------->  | data/*_site/        |        +-- index worker (BM25)
journal vault (opt.) ---------->  | data/brain/journal/ | <------+   every 150s
                                  |                     |        |
                                  | daily_audit.json    |        +-- /api/search
                                  | search_index.json   |        +-- /api/ask
                                  | memory_profile.json |        +-- /api/memory/*
                                  +---------------------+        +-- web UI :8000
```

Sync is push-based: each client cron-rsyncs its session directory to the server. The server only reads — it never SSHes back out.

## Structure

```
cockpit/
├── app/
│   ├── cockpit.py              # HTTP server + index worker + UI
│   ├── daily_auditor.py        # Daily LLM audit pipeline
│   ├── weekly_digest.py        # 7-day pattern analysis
│   └── memory_distiller.py     # Long-term profile builder
├── scripts/
│   ├── sync/                   # rsync scripts for each client
│   │   ├── claude_sync.sh
│   │   ├── codex_sync.sh
│   │   └── gemini_sync.sh
│   └── demo/
│       ├── seed-data.py        # Generates fake data for demos/screenshots
│       └── mock-llm.py         # OpenAI-compatible stub for keyless ask/rerank testing
├── docs/
│   ├── ARCHITECTURE.md
│   ├── CONFIGURATION.md
│   └── screenshots/
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

## Quick Start

### 1. Run the server

```bash
git clone <repo-url> cockpit
cd cockpit
cp .env.example .env
# Edit .env — set at least one of DEEPSEEK_API_KEY or GEMINI_API_KEY
docker compose up -d
```

Open `http://localhost:8000`. UI will be empty until sessions arrive.

### 2. Try it with fake data

```bash
python scripts/demo/seed-data.py
docker compose restart cockpit
```

30 fictional sessions + 12-day audit history populate the UI. Useful for screenshots, demos, or evaluating before wiring up real syncs.

### 3. Sync from your machines

On each machine where you use Claude / Codex / Gemini CLIs:

```bash
cd scripts/sync
cp .env.example .env
# Edit .env — set COCKPIT_HOST / COCKPIT_USER / CLIENT_NAME
ssh-copy-id $COCKPIT_USER@$COCKPIT_HOST
crontab -e
```

Add the scripts you need (skip the ones you don't use):

```cron
*/5 * * * * /path/to/cockpit/scripts/sync/claude_sync.sh
*/5 * * * * /path/to/cockpit/scripts/sync/codex_sync.sh
*/5 * * * * /path/to/cockpit/scripts/sync/gemini_sync.sh
```

Sessions appear in the UI within ~3 minutes.

## Configuration

All config via env vars. See [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) for the full reference.

| Variable | Default | Purpose |
|----------|---------|---------|
| `PORT` | `8000` | HTTP listen port |
| `DEEPSEEK_API_KEY` | — | Primary LLM for audits (cheap, JSON-mode reliable) |
| `GEMINI_API_KEY` | — | Fallback LLM + embeddings for semantic search |
| `OPENAI_API_KEY` | — | Optional: enables LLM reranking + Ask the Panopticon |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible server (Ollama, LM Studio, vLLM). Local servers need no key |
| `RERANK_MODEL` | `gpt-4o-mini` | Model used for reranking |
| `RERANK_TOP` | `30` | How many top candidates to rerank per query |
| `ASK_MODEL` | `gpt-4o-mini` | Model that answers `/api/ask` questions |
| `ASK_CHAR_BUDGET` | `90000` | Journal context cap for `/api/ask` (~27k tokens) |
| `ASK_RECENT_DAYS` | `5` | Newest journal days always included in `/api/ask` context |
| `BRAIN_DATA` | `/app/data/brain` | Optional distilled-notes vault (`journal/YYYY-MM-DD.md`) |
| `MEMORY_SKILL` | (empty) | Skill name whose sessions feed the memory profile; empty disables it |
| `TZ` | `UTC` | Affects daily audit date boundaries |
| `INDEX_INTERVAL` | `150` | Seconds between filesystem index scans |

**No API key at all?** Everything file-based still works: indexing, BM25 search, journal panel, the UI. `GEMINI_API_KEY` adds semantic embeddings, `DEEPSEEK_API_KEY`/`GEMINI_API_KEY` add the nightly audits, and an OpenAI-compatible endpoint (cloud key **or** local server via `OPENAI_BASE_URL`) adds reranking and Ask the Panopticon. Each feature degrades independently.

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web UI |
| `/api/chats` | GET | All indexed sessions (metadata) |
| `/api/chat/<uid>` | GET | Full messages of one session |
| `/api/search` | POST | Hybrid search. Body: `{"query": "..."}` → `{results, journal}` |
| `/api/ask` | POST | Ask the journal. Body: `{"question": "..."}` → `{answer, sources, model}` |
| `/api/journal` | GET | Raw journal files from `BRAIN_DATA`, newest first |
| `/api/search/status` | GET | Index health |
| `/api/memory/daily` | GET | Rolling 14-day audit history |
| `/api/memory/weekly` | GET | On-demand weekly digest (regenerates per call) |
| `/api/memory/profile` | GET | Long-term distilled memory |
| `/api/memory/core` | GET | Permanent injected-memory layer (`ai_config/user-core.md`) |
| `/api/memory/memoria` | GET | Recent injected-memory layer (`ai_config/user-memoria.md`) |
| `/api/skill_log` | POST | Tag the next session with a skill |

## Stack

- **Python 3.11** stdlib HTTP server (no Flask/FastAPI dependency)
- **rank-bm25** + **numpy** for chunk-level keyword search
- **Google Gemini** embeddings (optional) for semantic search
- **Any OpenAI-compatible endpoint** (optional — OpenAI, Ollama, LM Studio, vLLM) for reranking and `/api/ask`
- **DeepSeek** (primary) + **Gemini** (fallback) for daily audits and memory distillation
- **Docker Compose** deployment
- **Bash + rsync** for client-side syncing (no agent on clients)

## Customizing the Persona

The auditor and distillers ship with a strong default voice. To change it:

1. Open `app/daily_auditor.py` (or `weekly_digest.py` / `memory_distiller.py`)
2. Find the `prompt_text = (...)` block
3. Replace the TONE RULES section. Keep the JSON FORMAT block intact — the UI parses it.

A neutral replacement is suggested in [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md).

## Limitations

- **No auth.** UI is open on whatever port you bind. Put behind a reverse proxy + basic auth if exposed beyond LAN.
- **Single tenant.** Sessions from all clients land in the same index.
- **LLM cost.** ~1 audit + ~1 weekly digest + ~1 embedding per new chat per day. With DeepSeek as primary, stays well under USD $1/month for typical use.
- **Codex parser is best-effort.** Codex JSONL format has shifted occasionally; the parser may need updates if the format changes.

## License

MIT — see [`LICENSE`](LICENSE).
