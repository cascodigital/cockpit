# Architecture

Cockpit is a single Python process plus a few cron-driven shell scripts. The
design priority is **portability and deployability** over modularity.

## Components

### `app/cockpit.py` — the server

One file, no framework. Uses Python's stdlib `http.server` with a custom
handler. The index worker runs in a background thread and re-scans the data
directories every 150 seconds.

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
- **Distill.** Once a day (~23:45 local), invoke `daily_auditor.py` and
  `memory_distiller.py`.

### `app/daily_auditor.py` — the auditor

Reads today's chats (files mtimed today), assembles a sampled blob (first 5 +
last 2 messages per chat, capped at 600 chars/msg), and sends it to DeepSeek
(primary) or Gemini (fallback) with a Skippy-flavored prompt that returns
structured JSON. Output appended to a rolling 14-day history in
`data/daily_audit.json`.

The prompt expects strict JSON shape (see `app/daily_auditor.py` for the full
schema). The UI's dashboard renders this directly — change the schema and the
UI will partially break.

### `app/weekly_digest.py` — the meta-auditor

Reads the last 7 entries of `daily_audit.json`, sends them up as
already-summarized blobs, asks for a cross-cutting pattern analysis. Triggered
on-demand by `GET /api/memory/weekly` — there's no schedule. Run it via cron
+ curl if you want a recurring email.

### `app/memory_distiller.py` — the profile builder

Scans Gemini + Claude chats for keywords from `MEMORY_KEYWORDS` (or, if empty,
just the 15 most recent), sends them to DeepSeek/Gemini, and asks for a
long-term profile. Codex is intentionally skipped — `role:developer`
injections (SKILL.md, system prompts) cause false positives.

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
        └─► (every 150s) index_worker()
              ├─► sync_claude()   — convert .jsonl → .json in data/claude_converted/
              ├─► glob + parse each source
              ├─► build BM25 (in-memory)
              └─► batchEmbedContents() — incremental, persisted to disk
        │
        └─► (once/day @ 23:45) distillers
              ├─► daily_auditor → data/daily_audit.json
              └─► memory_distiller → data/memory_profile.json
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
  bump `time.sleep(150)` down or run a second worker — but you're past the
  intended scale by then.
- **No alembic / migrations.** Schema changes happen by re-running distillers
  with the new prompt. Old entries keep their old shape until overwritten.
