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
| `DEEPSEEK_API_KEY` | — | Primary LLM for audits/digests. Cheap, JSON-mode reliable |
| `GEMINI_API_KEY` | — | Fallback LLM, AND the embeddings provider for semantic search |
| `OPENAI_API_KEY` | — | Optional. Enables LLM reranking of search results |
| `RERANK_MODEL` | `gpt-4o-mini` | OpenAI model used for reranking |
| `RERANK_TOP` | `30` | Number of top candidates reranked per query |

You need at least one of `DEEPSEEK_API_KEY` / `GEMINI_API_KEY`. Without
`GEMINI_API_KEY`, semantic search degrades to BM25-only.

Reranking is optional. Without `OPENAI_API_KEY`, `/api/search` returns the
RRF-fused order unchanged. When set, the top `RERANK_TOP` candidates are
reordered by an LLM judging conceptual relevance against the matched snippet
(not the whole-chat summary, which would bias long mixed-topic chats).

## Memory distiller (`app/memory_distiller.py`)

| Variable | Default | Purpose |
|---|---|---|
| `MEMORY_KEYWORDS` | `""` (empty) | Comma-separated keywords. Only chats containing any of them feed the distiller. Empty = take the 15 most recent regardless |
| `MEMORY_LIMIT` | `15` | Max chats sent to the distiller in one run |

Use case for `MEMORY_KEYWORDS`: if you have a journaling / coaching / planning
keyword you use consistently (e.g. `journal,reflection,coaching`), the profile
will be much sharper than letting the distiller chew on random debug sessions.

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
├── search_index.json                    # embeddings cache
├── skill_log.jsonl                      # tags from POST /api/skill_log
├── daily_audit.json                     # rolling 14-day audit history
├── weekly_digest.json                   # last weekly digest
└── memory_profile.json                  # long-term distilled profile
```

Backing this up = backing up your forensic record. `data/` is intentionally
gitignored.
