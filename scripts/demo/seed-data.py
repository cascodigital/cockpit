"""
Seed the data/ directory with synthetic chats and a fake daily-audit history.

Use this to populate the UI for screenshots, demos, or testing without needing
real chat logs. Generates ~30 fictional sessions across the last 14 days, plus
a matching daily_audit.json so the dashboard renders fully.

Usage:
    python scripts/demo/seed-data.py [--data-dir ./data]

Re-running overwrites. Safe to run repeatedly.
"""
import argparse
import json
import os
import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path

random.seed(42)  # deterministic output

SOURCES = ["claude", "gemini", "codex"]
MACHINES = {"claude": ["WIN", "LNX", "DKR"], "gemini": ["WIN", "LNX"], "codex": ["WIN", "LNX"]}

# Fictional but plausible chat topics. Mix of dev/infra/AI/research/personal.
SESSIONS = [
    ("Dev", "Debug Python asyncio race condition",
     "I have a race condition in this asyncio worker pool — two tasks read the same item from the queue.",
     "The issue is that you're calling `queue.get()` outside the lock context. Move it inside the `async with self.lock:` block. Here's the fix..."),
    ("Dev", "Refactor TypeScript module to use Result type",
     "Can you help refactor this error handling to use a Result<T, E> pattern instead of throwing?",
     "Define a `Result<T, E> = { ok: true; value: T } | { ok: false; error: E }` discriminated union, then..."),
    ("Infra", "nginx 502 after deploy",
     "Getting 502 Bad Gateway on the new endpoint. Worked locally but breaks in prod.",
     "Check `upstream_response_time` in the access log. Likely culprit: gunicorn timeout shorter than nginx `proxy_read_timeout`."),
    ("Infra", "Kubernetes pod stuck in CrashLoopBackOff",
     "Pod keeps restarting. Logs show OOMKilled but I set the limit to 2Gi.",
     "OOMKilled with 2Gi limit usually means: (1) JVM heap not configured for cgroup limits, or (2) requests vs limits mismatch causing scheduler eviction. Check `kubectl describe pod` exit code first."),
    ("AI", "Design RAG pipeline for codebase Q&A",
     "Want to build a tool that answers questions about my own monorepo. What's the right embedding strategy?",
     "For code, chunk by function/class boundary, not by token count. Embed each chunk with metadata (file path, signature). Use hybrid BM25+semantic for retrieval — pure semantic misses identifier names."),
    ("AI", "Choosing between Claude and GPT-4 for agentic tasks",
     "Building an agent that needs to call ~15 tools in sequence. Which model handles long tool chains better?",
     "Claude tends to be more reliable with structured tool use and rarely hallucinates tool names. GPT-4 is faster per call. For 15-step chains, prioritize reliability — fewer retries beats raw speed."),
    ("Writing", "Edit blog post draft for clarity",
     "I wrote this technical post but it feels too dense. Can you suggest cuts?",
     "Three suggestions: (1) Drop paragraph 3 — it repeats the intro. (2) The 'background' section can be a single bullet list. (3) Lead with the result, then explain how you got there."),
    ("Research", "Survey of vector DB performance benchmarks",
     "Looking for objective benchmarks comparing Qdrant, Weaviate, and pgvector for ~10M vectors.",
     "ANN-Benchmarks is the standard. Filter by your dataset size. Caveat: benchmarks rarely include filter latency, which dominates real workloads. Test with your own filter predicates."),
    ("Research", "Reading list on systems verification",
     "Want to learn formal methods for distributed systems. Where to start?",
     "Start with Lamport's 'Specifying Systems' for TLA+. Then Cassandra's Jepsen reports for applied examples. Skip Coq unless you really want it — too heavy for a first pass."),
    ("Dev", "Fix flaky integration test",
     "This test passes locally but fails in CI 30% of the time.",
     "Classic flake. Check for: (1) hardcoded ports, (2) async work not awaited, (3) shared state between test runs. Run it with `--repeat-each=20` locally and watch which one fails first."),
    ("Infra", "Set up WireGuard mesh for homelab",
     "Want my laptop, NAS, and a VPS to all talk over WireGuard. Best topology?",
     "Hub-and-spoke with VPS as hub is simplest. Full mesh only if you need <50ms peer-to-peer latency. Each node needs its own keypair; use `wg genpsk` for preshared keys to harden against future key compromise."),
    ("Infra", "Docker volume permissions broken after restart",
     "After `docker compose restart`, the container can't write to its volume. UID mismatch?",
     "Yes. The container's UID doesn't match the host's volume owner. Two fixes: (1) `chown -R 1000:1000 /path/to/volume` on host, or (2) add `user: \"${UID}:${GID}\"` to docker-compose."),
    ("AI", "Prompt engineering for JSON-mode reliability",
     "DeepSeek JSON mode sometimes returns the JSON wrapped in markdown ``` fences. How to harden?",
     "Strip ```json and ``` after receiving the response — both providers leak fences occasionally. Don't fight the model; parse defensively."),
    ("Learning", "Understanding Rust ownership for someone coming from Go",
     "Moving from Go to Rust. Borrow checker keeps fighting me. Best mental model?",
     "Think of `&T` as a read-lease, `&mut T` as an exclusive write-lease. The checker enforces: 'you can have many readers OR one writer, never both'. Most fights are because you're trying to do both simultaneously."),
    ("Dev", "Bash script to clean stale git branches",
     "Need a one-liner to delete local branches whose upstream is gone.",
     "`git fetch -p && git branch -vv | awk '/: gone]/ {print $1}' | xargs -r git branch -D` — but read each branch name first. The `-D` is destructive."),
    ("Personal", "Plan weekend road trip route",
     "Driving from Lisbon to Porto this weekend. Best stops?",
     "If you have a full day: Óbidos (medieval town), Nazaré (big waves season), Coimbra (university). All on or near the A8/A1 corridor."),
    ("Admin", "Draft email rejecting a meeting tactfully",
     "Need to decline a 'quick sync' that's actually a sales pitch.",
     "Keep it short and warm: 'Thanks for thinking of me. I'm not the right fit for this conversation right now — would rather not take your time.' Don't apologize or over-explain."),
    ("Dev", "PostgreSQL slow query investigation",
     "This query takes 8s on prod but 200ms on staging. Same schema.",
     "Three places to look: (1) `pg_stat_statements` for actual plan, (2) `ANALYZE` may be stale on prod — run it. (3) Index bloat from frequent UPDATEs; check `pgstattuple`."),
    ("Infra", "Backup strategy for self-hosted services",
     "I have ~12 docker-compose stacks. Currently zero backups. Want 3-2-1 strategy.",
     "Use `restic` with two repos: one local (NAS), one offsite (B2/S3). Snapshot daily, prune weekly. Don't back up the container images — they're reproducible. Back up volumes and configs only."),
    ("AI", "Embedding model selection for non-English text",
     "Working with mostly Portuguese docs. Are multilingual embeddings worth the quality drop?",
     "For Romance languages, `text-embedding-3-large` and Gemini's `text-embedding-004` both handle PT well. Quality drop vs English is minor (~5%). Skip BGE/Jina unless your corpus is huge — overhead isn't worth it."),
    ("Writing", "Restructure technical README",
     "My OSS README is 800 lines. People don't read past line 50. How to cut?",
     "Move everything except: tagline, screenshot, quick start (3 commands), and one architecture diagram, to /docs. Link liberally. Users who need depth click through; users who don't keep scrolling."),
    ("Research", "Literature review on context window scaling",
     "Want to understand the engineering behind 1M+ token context. Worth reading?",
     "Read the Gemini 1.5 paper first (Ring Attention is the key idea). Skip the YaRN/RoPE rabbit hole unless you're implementing — for users, the takeaway is: long context degrades quality past ~32K tokens in practice."),
    ("Dev", "Migrate Node.js project from CommonJS to ESM",
     "Old codebase, 300+ files. Migration script that won't break imports?",
     "Use `cjstoesm` for the bulk rewrite. Then manually fix: (1) `__dirname` (no longer exists in ESM), (2) JSON imports (need assertion), (3) dynamic imports of CommonJS-only deps. Budget 2 days for the manual cleanup."),
    ("AI", "Debug agent loop that won't terminate",
     "My agent keeps calling tools in a loop, never returning to the user. Stuck on step 8.",
     "Two questions: (1) does the agent get the tool result back in its context? (2) is your system prompt biased toward 'always check one more thing'? Both are common. Add a `max_iterations` guard regardless."),
    ("Learning", "First steps with Zig",
     "Want to try Zig. Coming from C and Rust. What's the gotcha?",
     "Comptime is the killer feature but also the source of all confusion. Resist the urge to make everything comptime; start with regular fns and only escalate when you actually need it."),
    ("Infra", "TLS cert renewal failing for internal service",
     "certbot keeps failing on the internal `*.lab.home` domain. DNS-01 challenge.",
     "Are you using a public CA? You can't — they only issue for public-resolvable names. Use Smallstep CA or step-ca for internal certs. Or self-sign and add the CA to your trust store."),
    ("Dev", "Code review of database migration",
     "Adding a NOT NULL column to a 50M-row table. Reviewer says it's unsafe.",
     "Reviewer is right. NOT NULL + default on 50M rows = exclusive lock for minutes. Three-step pattern: (1) add column nullable, (2) backfill in batches, (3) add NOT NULL constraint with `NOT VALID` then `VALIDATE`."),
    ("Admin", "Quarterly OKR review template",
     "Need to write up Q1 OKRs. What's a clean format?",
     "Per objective: 1 line of intent, 3 measurable key results, 1 line of 'what we'll stop doing to make room'. The third line is the one most teams skip and the one that actually drives focus."),
    ("Personal", "Choosing a mechanical keyboard switch",
     "Tactile vs linear, 45g vs 55g, MX vs Topre. Help me narrow down.",
     "Type for a living: tactile, 55-65g, MX-compatible (cheapest path to swap if you change your mind). Topre is great but locks you into one form factor and ~3x the price."),
    ("Dev", "Set up pre-commit hook for Python project",
     "Want black, ruff, and pytest on every commit. What's the modern setup?",
     "Use `pre-commit` framework. Config in `.pre-commit-config.yaml`. Run `pre-commit install` once per clone. Pytest in pre-commit is controversial — most teams gate that on pre-push instead."),
]


def make_claude_chat(date, topic, src_msg, ai_msg):
    """Generate a synthetic Claude Code .jsonl session."""
    session_id = str(uuid.uuid4())
    lines = []
    msg_user = {
        "type": "user",
        "message": {"role": "user", "content": src_msg},
        "timestamp": date.isoformat() + "Z",
    }
    msg_assistant = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": ai_msg}]},
        "timestamp": (date + timedelta(minutes=2)).isoformat() + "Z",
    }
    lines.append(json.dumps(msg_user))
    lines.append(json.dumps(msg_assistant))
    return session_id, "\n".join(lines)


def make_gemini_chat(date, topic, src_msg, ai_msg):
    """Generate a synthetic Gemini CLI .json session (already in cockpit's normalized shape)."""
    session_id = str(uuid.uuid4())
    return session_id, {
        "sessionId": session_id,
        "startTime": date.isoformat() + "Z",
        "lastUpdated": (date + timedelta(minutes=2)).isoformat() + "Z",
        "messages": [
            {"type": "user", "content": src_msg, "timestamp": date.isoformat() + "Z"},
            {"type": "assistant", "content": ai_msg, "timestamp": (date + timedelta(minutes=2)).isoformat() + "Z"},
        ],
    }


def make_codex_chat(date, topic, src_msg, ai_msg):
    """Generate a synthetic Codex CLI .jsonl session."""
    session_id = str(uuid.uuid4())
    lines = [
        json.dumps({
            "type": "session_meta",
            "payload": {"id": session_id, "timestamp": date.isoformat() + "Z"},
            "timestamp": date.isoformat() + "Z",
        }),
        json.dumps({
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": src_msg}]},
            "timestamp": date.isoformat() + "Z",
        }),
        json.dumps({
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": ai_msg}]},
            "timestamp": (date + timedelta(minutes=2)).isoformat() + "Z",
        }),
    ]
    return session_id, "\n".join(lines)


def make_daily_audit_entry(date, day_chats):
    """Build one daily-audit entry mimicking what daily_auditor.py would produce."""
    date_str = date.strftime("%Y-%m-%d")
    cats_today = [c["category"] for c in day_chats]
    from collections import Counter
    counter = Counter(cats_today)
    dominant = counter.most_common(1)[0][0] if counter else "Other"
    unique_cats = len(set(cats_today))
    focus_score = max(2, 10 - unique_cats * 2)

    skippy_lines = [
        ("Another day where the meatsack pretended to multitask",
         "The primate spent the morning context-switching between {a} and {b}, then crashed in {dom} for the afternoon. Standard.",
         "Pattern: every time {a} got hard, the protoplasm jumped to {b}. Avoidance, not exploration.",
         "Tried to debug three things at once. Solved one. Forgot the other two.",
         "Productive in the sense that lemurs are productive: lots of movement, occasional fruit."),
        ("The meatsack actually shipped something today, briefly",
         "Started in {dom}, got stuck, escaped to {a}, came back, finished. The escape was longer than the work.",
         "Returns to {dom} every time something else stalls. Predictable as a thermostat.",
         "Asked the AI to decide a thing he already had decided. Classic.",
         "Net positive. Don't get used to it."),
        ("Hyperfocus achieved, almost entirely on the wrong problem",
         "Eight hours on {dom}, two minutes on the thing that was actually blocking him. Beautiful inversion.",
         "When {a} is the priority, he works on {b}. Reliable.",
         "Spent 40 minutes choosing a tool before installing any of the candidates.",
         "Output exists. Direction questionable."),
    ]
    template = random.choice(skippy_lines)
    other_cats = [c for c in unique_cats and set(cats_today) - {dominant} or set()]
    a = random.choice(list(set(cats_today) - {dominant})) if len(set(cats_today)) > 1 else dominant
    b = random.choice(list(set(cats_today) - {dominant, a})) if len(set(cats_today)) > 2 else a

    return {
        "date": date_str,
        "hyperfocus": dominant,
        "headline": template[0],
        "narrative": template[1].format(dom=dominant, a=a, b=b),
        "pattern_insight": template[2].format(dom=dominant, a=a, b=b),
        "fail_of_the_day": template[3],
        "elder_verdict": template[4],
        "day_metrics": {
            "context_switches": unique_cats,
            "focus_score": focus_score,
            "dominant_category": dominant,
        },
        "chats": [
            {
                "uid": c["uid"],
                "title": c["title"],
                "summary": c["summary"],
                "long_summary": c["long_summary"],
                "categories": [c["category"]],
            }
            for c in day_chats
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="./data", help="Path to data directory (default: ./data)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    (data_dir / "claude" / "demo").mkdir(parents=True, exist_ok=True)
    (data_dir / "gemini" / "demo" / "chats").mkdir(parents=True, exist_ok=True)
    (data_dir / "codex" / "demo" / "sessions").mkdir(parents=True, exist_ok=True)
    (data_dir / "claude_converted").mkdir(parents=True, exist_ok=True)

    today = datetime.now()
    all_chats_by_date = {}

    for i, (cat, title, src, ai) in enumerate(SESSIONS):
        # Spread across last 14 days, weighted toward recent
        days_ago = random.choices(range(14), weights=[1.0 - x * 0.05 for x in range(14)])[0]
        chat_date = today - timedelta(days=days_ago, hours=random.randint(8, 22), minutes=random.randint(0, 59))
        source = random.choice(SOURCES)
        machine = random.choice(MACHINES[source])

        if source == "claude":
            sid, content = make_claude_chat(chat_date, title, src, ai)
            target_dir = data_dir / "claude" / "demo"
            (target_dir / "docker" if machine == "DKR" else target_dir / "linux" if machine == "LNX" else target_dir).mkdir(parents=True, exist_ok=True)
            sub = "docker" if machine == "DKR" else "linux" if machine == "LNX" else ""
            path = data_dir / "claude" / "demo" / sub / f"{sid}.jsonl" if sub else data_dir / "claude" / "demo" / f"{sid}.jsonl"
            path.write_text(content)
            uid = f"claude-session-CLAUDE-{sid}.json"
        elif source == "gemini":
            sid, data = make_gemini_chat(chat_date, title, src, ai)
            path = data_dir / "gemini" / "demo" / "chats" / f"session-{sid}.json"
            path.write_text(json.dumps(data, indent=2))
            uid = f"gemini-session-{sid}.json"
        else:
            sid, content = make_codex_chat(chat_date, title, src, ai)
            path = data_dir / "codex" / "demo" / "sessions" / f"rollout-{sid}.jsonl"
            path.write_text(content)
            uid = f"codex-demo__sessions__rollout-{sid}.jsonl"

        # Set file mtime to the chat date so daily auditor's "today" check works
        os.utime(path, (chat_date.timestamp(), chat_date.timestamp()))

        date_key = chat_date.date()
        all_chats_by_date.setdefault(date_key, []).append({
            "uid": uid,
            "title": title,
            "summary": src[:80] + ("..." if len(src) > 80 else ""),
            "long_summary": f"{src} {ai[:120]}...",
            "category": cat,
        })

    # Generate daily audit for each day with chats
    audit_entries = []
    for date_key in sorted(all_chats_by_date.keys(), reverse=True):
        day_date = datetime.combine(date_key, datetime.min.time())
        audit_entries.append(make_daily_audit_entry(day_date, all_chats_by_date[date_key]))

    audit_path = data_dir / "daily_audit.json"
    audit_path.write_text(json.dumps(audit_entries[:14], indent=2, ensure_ascii=False))

    # Generate a memory profile too
    memory_profile = {
        "personality_dna": "Senior engineer with strong infra and AI tooling lean. Tendency to spend more time choosing tools than using them. Goes deep on Rust/Python systems work; recoils from admin tasks.",
        "recurring_bugs": [
            "Confuses 'researching' with 'doing' when starting a new project",
            "Will refactor working code rather than ship the unfinished feature",
            "Asks the AI to validate decisions already made, then second-guesses anyway",
        ],
        "pending_homework": [
            "Finish the RAG pipeline draft started 5 days ago",
            "Decide on vector DB after the benchmark survey concludes",
            "Submit OKR template to team (drafted twice, never sent)",
        ],
        "last_session_summary": "Discussed Postgres migration safety patterns. Concluded with the three-step pattern (nullable add, batched backfill, validated constraint). Did not commit any code.",
        "last_updated": datetime.now().isoformat(),
    }
    (data_dir / "memory_profile.json").write_text(json.dumps(memory_profile, indent=2))

    # Generate a weekly digest
    weekly = {
        "period_start": (today - timedelta(days=6)).strftime("%Y-%m-%d"),
        "period_end": today.strftime("%Y-%m-%d"),
        "generated_at": today.strftime("%Y-%m-%d"),
        "weekly_headline": "The week the primate kept promising to ship and kept reading papers instead",
        "weekly_narrative": "Started the week strong with three Dev sessions in two days, then drifted into AI research for the middle of the week, recovered briefly on infra topics, then closed with a long Friday spent choosing a vector database benchmark methodology. The vector DB still hasn't been picked. The migration still hasn't shipped. The week ended with more open tabs than it began.",
        "drift_pattern": "Every time a Dev task hit a real obstacle, the meatsack escaped into Research or AI tooling. The pattern is reliable: 60-minute deep work, 90-minute 'investigation', return to Dev for 20 minutes before the next escape.",
        "weekly_fail": "Spent 3 hours benchmarking vector DBs without writing a single line of the code that would actually use them.",
        "weekly_verdict": "Net learning, net not-shipping. The protoplasm needs less reading and more committing.",
        "focus_avg": 4.6,
        "focus_trend": "stable",
        "top_categories": [["Dev", 9], ["AI", 7], ["Infra", 5]],
        "days_count": 7,
    }
    (data_dir / "weekly_digest.json").write_text(json.dumps(weekly, indent=2))

    print(f"Seeded {len(SESSIONS)} sessions across {len(audit_entries)} days into {data_dir}")
    print(f"  - {data_dir}/claude/demo/")
    print(f"  - {data_dir}/gemini/demo/chats/")
    print(f"  - {data_dir}/codex/demo/sessions/")
    print(f"  - {data_dir}/daily_audit.json ({len(audit_entries)} days)")
    print(f"  - {data_dir}/memory_profile.json")
    print(f"  - {data_dir}/weekly_digest.json")


if __name__ == "__main__":
    main()
