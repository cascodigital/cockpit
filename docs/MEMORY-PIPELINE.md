# The memory pipeline

This is the part of Cockpit that is easy to miss and hard to rebuild from the code:
it reads your own AI sessions, distills them, and **injects the result back into the
next session's system prompt**. The loop is the product. Everything else — search,
BM25, the journal panel — is a window onto it.

If you only read one thing here, read [Source coverage](#source-coverage). The set of
sources the UI can *search* is deliberately larger than the set it *remembers from*,
and confusing the two leads to wrong conclusions about what the assistant knows.

---

## The loop

```
 your CLIs and browsers                      one process, once a day
 ────────────────────────                    ─────────────────────────
 claude / gemini / codex  ──┐
 Claude web               ──┤   sync    ┌─> daily_auditor.py ──> daily_audit.json   (30 days, 1 entry/day)
                            ├─ every ──>│         │
 ChatGPT web ─┐  indexed    │  5 min    │         ├─> generate_user_memory() ──> user-memory.md   (~700 tokens, 30d window)
 Gemini web  ─┴─ but not ───┘           │         └─> generate_user_core()   ──> user-core.md     (stable facts, decays only by contradiction)
                 audited                │
                                        └─> memory_distiller.py ──> memory_profile.json  (therapy-session profile)
                                                                          │
                                            (summary line feeds the memory prompt above)

 user-profile.md + user-core.md + user-memory.md
        │
        └──> concatenated and passed as --append-system-prompt when the shell
             wrapper launches the assistant  ──> the next session boots knowing
             what you did, what is still open, and what closed.
```

The three memory layers are deliberately different in how they decay:

| Layer | File | Written by | Decay rule |
|---|---|---|---|
| Identity | `user-profile.md` | **nobody — hand-maintained** | never; goes stale silently |
| Long term | `user-core.md` | `generate_user_core()` | only by **contradiction**, never by age |
| Short term | `user-memory.md` | `generate_user_memory()` | 30-day rolling window |

`user-profile.md` being hand-maintained is the weak link: nothing writes it, so nothing
tells you when it is wrong. Check its date before trusting it.

---

## Source coverage

Not every source that Cockpit indexes feeds memory. This is the single most
misunderstood thing about the system:

| Source | Searchable in UI | Feeds daily audit | Feeds therapy profile |
|---|:--:|:--:|:--:|
| `gemini` (CLI) | yes | yes | yes |
| `claude_converted` (CLI) | yes | yes | yes |
| `codex` (CLI) | yes | yes | **no** |
| `claude_site` (web) | yes | yes | **no** |
| `chatgpt_site` (web) | yes | **no** | **no** |
| `gemini_site` (web) | yes | **no** | **no** |

Web sources are excluded from memory by default, and the reason is **date quality**,
not principle. Only include a web source whose sync recovers the conversation's real
creation date:

- `claude_site` **is** included: its sync reads the conversation's `created_at` from
  the authenticated API, so days land correctly.
- `gemini_site` is **not**: 141 of its 199 conversations carry the sentinel date
  `2025-12-31` and the rest carry the import date. Those dates are wrong in the
  timeline too, not just in memory — fix the sync before wiring it in.
- `chatgpt_site` has usable dates but is excluded by choice; the source is being
  retired.

Note that a web source's per-message `timestamp` is a capture stamp, constant across
the whole conversation — only `startTime` is meaningful, so day membership for these
is decided at conversation granularity.

The therapy profile is stricter still: `memory_distiller.py` buckets activations by
`source`, and only `gemini` and `claude` have buckets. A session run through any other
agent runner is invisible to it — the activation is logged, the session is searchable,
and the profile silently never updates.

---

## Which day does a chat belong to?

The audit runs once a day from an external scheduler (03:05 in the reference setup) and audits **yesterday** (`daily_auditor.py` with no argument
defaults to `now - 1 day`). Passing a date audits that date instead, which makes
catch-up possible after an outage:

```bash
docker exec cockpit python /app/daily_auditor.py 2026-08-04
```

Day membership comes from the **message timestamps**, converted to local time, not
from the file's mtime. This matters more than it sounds:

- mtime is stamped by the **sync**, not by the conversation. A chat that happened
  entirely on the 7th but only synced at 08:04 on the 8th used to be audited as the
  8th's work.
- The nightly web backfill rewrites *old* conversations with a *fresh* mtime, which
  used to drag months-old chats into today's audit.
- Timestamps are UTC; the user is UTC-3. Comparing UTC dates misfiles the first three
  hours of every day.

mtime survives only as a cheap pre-filter (a file containing day D cannot have been
written before D), which keeps the run from parsing ~3,500 files / 825 MB every night.
A session that crosses midnight counts on both days, on purpose.

---

## How a pending item dies

`ops_brief.open_threads` lists what is unresolved. Without a matching close signal,
items are immortal: they reappear every day forever, and the memory file fills with
work that finished weeks ago.

Asking the LLM to obey close rules was not enough: the memory model kept listing
items that had closed or that were weeks old. The pending list is therefore built
**by code**, and the LLM only feeds it raw material:

1. **Birth criteria** — the audit prompt only allows an `open_threads` item that has
   a concrete action *and* a verifiable close condition written as a
   `[fecha quando: ...]` ("closes when") suffix, prefixed with the source chat number
   (`[#3] ...`). Passive watch items ("Monitorar", "Acompanhar", "Aguardar"...) are
   forbidden, and at most 5 items per day are accepted. When in doubt, nothing opens:
   a real problem resurfaces in tomorrow's logs, a phantom one never leaves.

2. **`ops_brief.resolved_threads`** — the daily audit must record what *closed*, with
   the evidence inline. The evidence bar is deliberately high: a command's output, a
   passing test, a service responding, or the user saying it worked. Intent ("I'll
   fix X", "now just run it") does not count.

3. **Deterministic block** — `build_pendencias_block()` (in `daily_auditor.py`) reads
   the `open_threads` of the last 10 days, keeps only items with a close condition,
   groups rewordings of the same item by word similarity (Jaccard >= 0.4), and drops a
   group when a `resolved_threads` entry from the same day or later covers it (>= 50%
   of its words, or the same ticket ID such as `SUP-123`). Up to 10 survivors are
   written with their birth date into the `## Pendências ativas` section of
   `user-memory.md`; the LLM is told not to write that section at all, and any it
   writes is stripped.

4. **Hard TTL** — `expire_open_threads()` empties `open_threads` on audit days older
   than 14 days, so an item the model keeps re-emitting cannot live forever.

5. **Manual kill** — `pendencias.py` lists the active items and `pendencias.py mata N`
   ("kill") marks item N as dead in `ai_config/pendencias-mortas.json`. Killed items,
   and later rewordings of them, stay out of every future block:

   ```bash
   docker exec cockpit python /app/pendencias.py
   docker exec cockpit python /app/pendencias.py mata 2 5
   ```

6. **No-pending sessions** — skills listed in `NO_PENDING_SKILLS` (for example client
   work that already has its own ticket queue) never create pending items. The skill
   launcher appends `<agent> <timestamp> <skill> [session-id]` lines to
   `ai_config/sessoes-sem-pendencia.txt`; `_marcar_sem_pendencia()` matches Claude
   sessions by session ID, Codex sessions by the `name:` frontmatter of the injected
   SKILL.md, and Gemini sessions by a launch within 10 minutes before the first message.
   Those chats are tagged `[SEM PENDÊNCIAS]` in the prompt and any `open_threads` item
   citing them is dropped after the LLM answers.

For any of this to work, the recent-audit context handed to the model must carry
`open_threads` and `resolved_threads`. If it doesn't, the close rule is dead letter.

---

## Sampling: why the middle of a chat matters

Each chat is sampled — head, middle, and tail — never head-and-tail alone. A problem
is stated at the start of a conversation and solved in the middle; sampling only the
edges shows the model the problem and hides the fix, which is exactly how pendings
become immortal. Gaps are marked inline (`[... N messages omitted ...]`) so the model
knows the record is not contiguous.

The character budget is **split per chat** rather than truncating one long
concatenation at the end. A single global cut is silently biased toward whichever
source is appended first; per-chat budgeting guarantees every conversation of the day
is represented.

The same principle applies to the audit history handed to the memory generators: it is
projected down to the fields that matter (dropping each day's per-chat array, which is
most of the weight) and trimmed **by whole days, oldest first**. Slicing a JSON dump at
a byte offset hands the model invalid JSON and a window it believes is complete.

---

## Operational notes

- **Deploys are a file copy.** The host's `app/` directory is bind-mounted to `/app`.
  Editing the host file *is* editing production. The auditor is exec'd as a fresh
  process every run, so it needs no restart — and restarting costs a full reindex.
- **Filename bridge.** The HTTP endpoints read `user-core.md` / `user-memory.md`, while
  the generators write locally-named files; symlinks bridge the two. A restore that
  does not preserve symlinks will 404 the memory panel with no other symptom.
- **Status, not optimism.** `memory_distiller.distill_memory()` returns a status
  (`no-new-session`, `api-error`, `updated (...)`) and the index worker logs it. It
  used to log "Profile updated." on every run, including runs that changed nothing.
- **Core size is enforced.** `generate_user_core()` estimates tokens (`chars / 4`);
  above 1.4x the 500-token limit it asks the model once more to cut, and keeps the
  retry only if it is shorter. Technical configuration (IPs, ports, versions, cron,
  model assignments) is excluded from the core by prompt rule, because it belongs in
  the canonical `.md` docs under `ai_config/`, which win on conflict.
- **The repo is generated.** See [`scripts/promote.py`](../scripts/promote.py):
  production is the source of truth for code, this mirror is a sanitized artifact, and
  the direction is one-way. Publishing the mirror over production regresses it.
