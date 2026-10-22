#!/usr/bin/env python3
"""Promote PRODUCTION code into this repository's OSS mirror, sanitized.

ONE direction only: production -> repo. Production is the source of truth for the
code; this repository is a generated artifact. There is no reverse path, on purpose:
trying to "deploy the repo" already caused the drift this script exists to kill
(on 2026-08-11 the repo was 3 weeks behind and lacked date-based catch-up —
publishing the repo over production would have REGRESSED it).

Usage:
    python3 scripts/promote.py            # show the diff, write nothing
    python3 scripts/promote.py --write    # apply to the repo (review and commit by hand)

Requires working SSH to the production host (COCKPIT_HOST or the private rules file;
see docs/MEMORY-PIPELINE.md). Without the private rules file it still runs, but warns.
"""
import argparse
import difflib
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Private rules (real client names, secret fragments to block, production host) live
# OUTSIDE the repo, so the public mirror never carries the very strings it filters.
# JSON: {"remote": "...", "remote_app": "...", "subs": [[regex, repl]], "leaks": [[regex, label]]}
PRIVATE_RULES = os.path.expanduser(
    os.environ.get("COCKPIT_PRIVATE_RULES", "~/ai/config/cockpit-promote-private.json"))
PRIVATE = {}
if os.path.exists(PRIVATE_RULES):
    import json
    with open(PRIVATE_RULES, encoding="utf-8") as fh:
        PRIVATE = json.load(fh)

REMOTE = os.environ.get("COCKPIT_HOST") or PRIVATE.get("remote", "")
REMOTE_APP = os.environ.get("COCKPIT_REMOTE_APP") or PRIVATE.get("remote_app", "/app")

# production -> repo. Code files only; NEVER docker-compose.yml (it has an inline
# API key) nor anything under data/.
FILES = {
    "cockpit_v2_docker.py": "app/cockpit.py",
    "daily_auditor.py": "app/daily_auditor.py",
    "ludovico_distiller.py": "app/memory_distiller.py",
    "weekly_distiller.py": "app/weekly_digest.py",
}

# Order matters: longest entries first, otherwise "André" eats "do André"
# and produces the infamous "dthe user" that stayed published on GitHub for three weeks.
SUBS = [
    # Identity -> configurable placeholders (prompts that are f-strings interpolate
    # {USER_NAME} and {PERSONA_NAME} at runtime).
    (r"\bdo André\b", "do {USER_NAME}"),
    (r"\bdthe André\b", "do {USER_NAME}"),
    (r"\bAndré\b", "{USER_NAME}"),
    (r"\bAndre\b", "{USER_NAME}"),  # unaccented spelling used in some prompts
    (r"\bSKIPPY THE MAGNIFICENT\b", "{PERSONA_NAME}"),
    (r"\bSkippy\b", "{PERSONA_NAME}"),
    # Personal file names -> neutral names (production has symlinks for both forms)
    (r"andre-memoria\.md", "user-memory.md"),
    (r"andre-core\.md", "user-core.md"),
    (r"perfil-andre\.md", "user-profile.md"),
    (r"prontuario\.md", "user-context.md"),
    (r"\bgenerate_andre_memory\b", "generate_user_memory"),
    (r"\bgenerate_andre_core\b", "generate_user_core"),
    (r"ludovico_dna\.json", "memory_profile.json"),
    (r"\bludovico_distiller\b", "memory_distiller"),
    (r"\bLudovico\b", "MemoryProfile"),
    (r"\bweekly_distiller\b", "weekly_digest"),
    # Real network/infra -> placeholders
    (r"192\.168\.\d+\.\d+(:\d+)?", "${INTERNAL_HOST}"),
    (r"\bCasco Digital\b", "${COMPANY_NAME}"),
    # Hardcoded config -> env (the repo must run outside the production host)
    (r'^DATA_DIR = "/app/data"$',
     'DATA_DIR = os.environ.get("DATA_DIR", "/app/data")'),
    (r'd\.get\("skill"\) != "skpsi"', 'd.get("skill") != MEMORY_SKILL'),
] + [tuple(x) for x in PRIVATE.get("subs", [])]

# Header injected right after the imports when the file uses the placeholders.
HEADER = (
    '\n# Identity injected by scripts/promote.py — the OSS mirror carries no\n'
    '# hardcoded person name or persona.\n'
    'USER_NAME = os.environ.get("USER_NAME", "the user")\n'
    'PERSONA_NAME = os.environ.get("PERSONA_NAME", "the auditor")\n'
    'MEMORY_SKILL = os.environ.get("MEMORY_SKILL", "").strip()\n'
)

# If any of these survives sanitization, abort: it is a secret leak.
LEAKS = [
    (r"sk-[A-Za-z0-9]{16,}", "OpenAI/DeepSeek key"),
    (r"AIza[A-Za-z0-9_\-]{20,}", "Google/Gemini key"),
    (r"ghp_[A-Za-z0-9]{20,}", "GitHub token"),
    (r"\bAndré\b", "unsanitized real name"),
    (r"\bAndre\b", "unsanitized real name"),
    (r"192\.168\.", "unsanitized internal IP"),
] + [tuple(x) for x in PRIVATE.get("leaks", [])]


def fetch(remote_name):
    if not REMOTE:
        sys.exit("ERROR: set COCKPIT_HOST or create the private rules file (see PRIVATE_RULES).")
    out = subprocess.run(["ssh", REMOTE, f"cat {REMOTE_APP}/{remote_name}"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"ERROR: could not read {remote_name} on {REMOTE}: {out.stderr.strip()}")
    return out.stdout


def sanitize(text):
    for pat, repl in SUBS:
        text = re.sub(pat, repl, text, flags=re.MULTILINE)
    if "{USER_NAME}" in text or "{PERSONA_NAME}" in text or "MEMORY_SKILL" in text:
        lines = text.split("\n")
        for i, ln in enumerate(lines):
            if ln.startswith("import ") or ln.startswith("from "):
                last_import = i
        lines.insert(last_import + 1, HEADER)
        text = "\n".join(lines)
    return text


def check_leaks(text, label):
    problems = []
    for pat, what in LEAKS:
        for m in re.finditer(pat, text):
            problems.append(f"  {label}: {what} -> {m.group(0)[:40]!r}")
    return problems


def main():
    if not PRIVATE:
        print(f"WARNING: private rules not found at {PRIVATE_RULES}; client names and "
              "secret fragments are NOT being filtered.", file=sys.stderr)
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="apply to the repo")
    args = ap.parse_args()

    staged, leaks = {}, []
    for remote_name, repo_path in FILES.items():
        clean = sanitize(fetch(remote_name))
        leaks += check_leaks(clean, repo_path)
        staged[repo_path] = clean

    if leaks:
        print("ABORTED — incomplete sanitization, this would go to a PUBLIC repo:")
        print("\n".join(leaks))
        sys.exit(1)

    changed = 0
    for repo_path, clean in staged.items():
        full = os.path.join(REPO, repo_path)
        old = open(full, encoding="utf-8").read() if os.path.exists(full) else ""
        if old == clean:
            print(f"= {repo_path} (unchanged)")
            continue
        changed += 1
        diff = list(difflib.unified_diff(old.split("\n"), clean.split("\n"),
                                         f"repo/{repo_path}", f"prod/{repo_path}", lineterm=""))
        print(f"\n~ {repo_path}: {sum(1 for l in diff if l.startswith('+') and not l.startswith('+++'))} lines added, "
              f"{sum(1 for l in diff if l.startswith('-') and not l.startswith('---'))} lines removed")
        if args.write:
            with open(full, "w", encoding="utf-8") as f:
                f.write(clean)

    if not args.write:
        print(f"\n[dry-run] {changed} file(s) would change. Run with --write to apply.")
    else:
        print(f"\n{changed} file(s) updated. Review with `git diff` and commit by hand.")


if __name__ == "__main__":
    main()
