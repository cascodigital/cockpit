# Configuration

All runtime knobs are environment variables. The full set:

## Server (read by `app/cockpit.py`)

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8000` | HTTP listen port |
| `TZ` | `UTC` | Timezone for daily-audit date boundaries |
| `GEMINI_DATA` | `/app/data/gemini` | Where the index worker globs for Gemini `.json` files |
| `CLAUDE_DATA` | `/app/data/claude` | Where to find raw Claude `.jsonl` logs |
| `CLAUDE_CONVERTED` | `/app/data/claude_converted` | Where `sync_claude()` writes normalized `.json` |
| `CODEX_DATA` | `/app/data/codex` | Where to find Codex `.jsonl` logs |
| `CHATGPT_SITE_DATA` | `/app/data/chatgpt_site` | Web-captured ChatGPT sessions (`machine=WEB`) |
| `GEMINI_SITE_DATA` | `/app/data/gemini_site` | Web-captured Gemini sessions (`machine=WEB`) |
| `CLAUDE_SITE_DATA` | `/app/data/claude_site` | Web-captured Claude sessions (`machine=WEB`) |
| `BRAIN_DATA` | `/app/data/brain` | Optional distilled-notes vault (see Journal below) |
| `INDEX_INTERVAL` | `150` | Seconds between filesystem index scans |
| `DEEPSEEK_API_KEY` | — | Primary LLM for audits/digests. Cheap, JSON-mode reliable |
| `GEMINI_API_KEY` | — | Fallback LLM, AND the embeddings provider for semantic search |
| `OPENAI_API_KEY` | — | Optional. Enables LLM reranking + `/api/ask` |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible server. Setting it (even without a key) enables rerank/ask — e.g. Ollama `http://host:11434/v1` |
| `RERANK_MODEL` | `gpt-4o-mini` | Model used for reranking |
| `RERANK_TOP` | `30` | Number of top candidates reranked per query |
| `ASK_MODEL` | `gpt-4o-mini` | Model answering `/api/ask` |
| `ASK_CHAR_BUDGET` | `90000` | Max journal characters sent as `/api/ask` context |
| `ASK_RECENT_DAYS` | `5` | Newest journal days always included in the context |

Everything degrades independently: with no keys at all you keep indexing, BM25
search, the journal panel and the whole UI. `GEMINI_API_KEY` adds embeddings;
DeepSeek/Gemini add nightly audits; an OpenAI-compatible endpoint adds
reranking and ask.

Reranking is optional. Without an LLM, `/api/search` returns the RRF-fused
order unchanged. When enabled, the top `RERANK_TOP` candidates are reordered by
an LLM judging conceptual relevance against the matched snippet (not the
whole-chat summary, which would bias long mixed-topic chats).

## Journal / Ask the Panopticon

Point `BRAIN_DATA` at a directory containing `journal/YYYY-MM-DD.md` files —
the output of whatever daily distillation pipeline you run (or plain
hand-written notes). Format expectations are minimal:

- `## Section` headers group bullets
- `- ` bullets are the searchable units
- `~~struck-through~~` bullets are treated as revoked by `/api/ask`

With a vault present, deep search returns matching bullets above the chat hits
and `POST /api/ask {"question": ...}` answers natural-language questions from
the journal, citing the dates it used. The context is assembled RAG-lite: the
`ASK_RECENT_DAYS` newest days always enter, older days only when they match the
question's terms, capped at `ASK_CHAR_BUDGET`.

To try it without any account: `python scripts/demo/mock-llm.py` and set
`OPENAI_BASE_URL=http://localhost:9999` (or a real local Ollama).

## Memory distiller (`app/memory_distiller.py`)

| Variable | Default | Purpose |
|---|---|---|
| `MEMORY_SKILL` | `""` (empty) | Skill slug whose sessions feed the profile, matched against `skill_log` activations by time window. Empty = distiller disabled |

Tag your reflective/coaching sessions via `POST /api/skill_log` (see Skills
below) with a dedicated slug and set `MEMORY_SKILL` to it. Only those sessions
are distilled into `memory_profile.json` — time-window matching against real
activations avoids the false positives keyword matching produces.

## Sync clients (`scripts/sync/.env`)

These are read by the cron-driven sync scripts on each *client* machine. Not
read by the server.

| Variable | Required | Purpose |
|---|---|---|
| `COCKPIT_HOST` | yes | IP/hostname of the cockpit server |
| `COCKPIT_USER` | yes | SSH user on the cockpit server |
| `COCKPIT_DATA_ROOT` | yes | Absolute path of `data/` on the server (the host path mounted into the container) |
| `CLIENT_NAME` | yes | Arbitrary tag — becomes a subdirectory under each source |

## Customizing the UI taxonomy

### Skills

The sidebar's "All skills" filter is driven by `SKILLS_MAP` in
`app/cockpit.py`:

```python
SKILLS_MAP = {
    'coding':   {'icon': '💻', 'name': 'Coding'},
    'writing':  {'icon': '✍️', 'name': 'Writing'},
    'research': {'icon': '🔬', 'name': 'Research'},
    'planning': {'icon': '🗺️', 'name': 'Planning'},
}
```

Edit freely. The keys are slugs you send to `POST /api/skill_log` before
running an AI session — e.g. a wrapper script around `claude` could tag the
upcoming session with `coding`, and the UI will then badge that session.

If you don't tag sessions, the `skill` filter is just an empty extra control —
harmless.

### Categories (daily audit)

The LLM is constrained to a fixed category vocabulary in the daily-audit
prompt:

```
Dev, Infra, AI, Writing, Research, Admin, Personal, Health, Finance, Learning, Other
```

To customize, edit `prompt_text` in `app/daily_auditor.py` (look for "CATEGORY
VOCABULARY"). Also update the fallback classifier `_classifyChatFallback()` in
`app/cockpit.py` so the heatmap renders old entries that lack `categories`.

The heatmap auto-discovers whatever categories show up — no UI change needed
beyond the prompt edit.

## Persona

The auditor and distillers share a sarcastic Elder AI persona ("Skippy"). It's
hardcoded in the three `prompt_text` blocks. To swap:

1. Replace the persona description and TONE RULES sections.
2. Keep the JSON FORMAT block exactly the same — the UI parses it.

Suggested neutral version (if you find Skippy grating):

```
You are a behavioral analyst reading the user's AI chat logs.
Be concise, observational, and specific. No flattery, no filler.
The value isn't "what they did" — it's "what this reveals about how they work".
```

## Data directory layout

Once running, you'll see:

```
data/
├── claude/<client_name>/...             # raw, pushed by claude_sync.sh
├── claude_converted/                    # normalized by sync_claude()
├── codex/<client_name>/sessions/        # raw .jsonl, pushed by codex_sync.sh
├── gemini/<client_name>/chats/          # normalized .json, pushed by gemini_sync.sh
├── chatgpt_site/ gemini_site/ claude_site/  # optional web captures (machine=WEB)
├── brain/journal/                       # optional distilled-notes vault (BRAIN_DATA)
├── ai_config/user-core.md               # optional: permanent injected-memory layer
├── ai_config/user-memoria.md            # optional: recent injected-memory layer
├── search_index.json                    # embeddings cache
├── skill_log.jsonl                      # tags from POST /api/skill_log
├── daily_audit.json                     # rolling 14-day audit history
├── weekly_digest.json                   # last weekly digest
└── memory_profile.json                  # long-term distilled profile
```

Backing this up = backing up your forensic record. `data/` is intentionally
gitignored.
