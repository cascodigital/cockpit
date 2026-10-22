# Architecture

Cockpit is a single Python process plus a few cron-driven shell scripts. The
design priority is **portability and deployability** over modularity.

## Components

### `app/cockpit.py` — the server

One file, no framework. Uses Python's stdlib `http.server` with a custom
handler. The index worker runs in a background thread and re-scans the data
directories every `INDEX_INTERVAL` seconds (default 150).

Why no FastAPI/Flask: zero dependencies on the HTTP side reduces the surface
area for breakage. The whole UI is a single HTML string served from
`get_template()`.

Key responsibilities:

- **Ingest.** On each tick, glob `data/{claude,gemini,codex}/` and parse any
  file whose mtime is newer than the cached one. Normalize to a common
  `{type, content, timestamp}` message shape.
- **Index.** BM25 over all chats (in-memory, rebuilt on change). Embeddings
  via Gemini's `batchEmbedContents`, persisted to `data/search_index.json`.
- **Search.** Reciprocal Rank Fusion of BM25 + semantic at search time.
- **Distill.** Once a day (~23:45 local), invoke `memory_distiller.py`. The
  daily audit is no longer triggered here; it runs from an external scheduler.

### `app/daily_auditor.py` — the auditor

Run once a day by an external scheduler
(`docker exec cockpit python /app/daily_auditor.py [YYYY-MM-DD]`, defaulting to
yesterday). Selects the chats whose message timestamps fall on the target day,
samples each one head + middle + tail (600 chars/msg, character budget split per
chat), and sends the result to DeepSeek (primary) or Gemini (fallback) with a
persona prompt that returns structured JSON. The entry is upserted into a rolling
30-day history in `data/daily_audit.json`, then `generate_user_memory()` and
`generate_user_core()` refresh the injected-memory files under `data/ai_config/`.
Before saving, `open_threads` items from no-pending sessions are dropped and audit
days older than 14 days lose their `open_threads`; the `## Pendências ativas`
section of `user-memory.md` is built by `build_pendencias_block()` in code, not by
the LLM (see [MEMORY-PIPELINE.md](MEMORY-PIPELINE.md#how-a-pending-item-dies)).
All DeepSeek calls use `DEEPSEEK_MODEL` (default `deepseek-flash`) with thinking
mode disabled.

### `app/pendencias.py` — the pending-item CLI

Lists the active pending items exactly as they are injected
(`python /app/pendencias.py`) and kills items by number
(`python /app/pendencias.py mata 2 5`), rewriting the section of `user-memory.md`
in place without an LLM call.

The prompt expects strict JSON shape (see `app/daily_auditor.py` for the full
schema). The UI's dashboard renders this directly — change the schema and the
UI will partially break.

### `app/weekly_digest.py` — the meta-auditor

Reads the last 7 entries of `daily_audit.json`, sends them up as
already-summarized blobs, asks for a cross-cutting pattern analysis. Triggered
on-demand by `GET /api/memory/weekly` — there's no schedule. Run it via cron
+ curl if you want a recurring email.

### `app/memory_distiller.py` — the profile builder

Selects Gemini + Claude sessions whose start time falls within a window after a
`MEMORY_SKILL` activation recorded via `POST /api/skill_log`, sends them to
DeepSeek/Gemini, and merges the result incrementally into
`data/memory_profile.json` (recurring patterns, open homework, latest session
summary). Codex is intentionally skipped because of false positives. It returns
a status string that the index worker logs (`no-new-session`, `api-error` or
`updated (...)`).

### `scripts/sync/*.sh` — the ingest layer

Cron-driven rsync scripts. Each client machine pushes its own session
directory to the server. The server is passive — it never SSHes back out.

Three scripts, one per CLI source. They share a `.env` with four variables.
See `scripts/sync/README.md`.

## Data flow

```
.jsonl/.json on client
  └─► (cron rsync) ───► data/claude/$CLIENT_NAME/
                        data/codex/$CLIENT_NAME/sessions/
                        data/gemini/$CLIENT_NAME/chats/
        │
        └─► (every INDEX_INTERVAL s) index_worker()
              ├─► sync_claude()   — convert .jsonl → .json in data/claude_converted/
              ├─► glob + parse each source
              ├─► build BM25 (in-memory)
              └─► batchEmbedContents() — incremental, persisted to disk
        │
        ├─► (once/day @ 23:45, in-process) memory_distiller → data/memory_profile.json
        │
        └─► (once/day, external scheduler) daily_auditor → data/daily_audit.json
              └─► data/ai_config/user-memory.md, data/ai_config/user-core.md
```

## Index worker invariants

- `file_metadata[uid] = mtime` cache prevents re-reading unchanged files.
  Crucial: a fresh start re-reads everything; long-lived processes only touch
  what changed.
- `something_changed` gates the BM25 rebuild and embedding update. No-op
  ticks are cheap.
- Embeddings run in a background thread to avoid blocking the next ingest
  tick on API latency. Two consecutive batch failures disable embedding
  until restart — BM25 keeps working.

## What's NOT here

- **No auth/ACL.** Put it behind a reverse proxy if exposed.
- **No DB.** Filesystem is the source of truth. `search_index.json` is the
  only persisted derived artifact.
- **No queue.** Index work is in-process. If you index thousands of chats,
  lower `INDEX_INTERVAL` or run a second worker — but you're past the
  intended scale by then.
- **No alembic / migrations.** Schema changes happen by re-running distillers
  with the new prompt. Old entries keep their old shape until overwritten.
