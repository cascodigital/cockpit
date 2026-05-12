"""
Daily Auditor — distills the day's AI chat sessions into a single Skippy-flavored
JSON audit. Runs once per day from cockpit.py's index_worker.

Sarcasm by design. The format is structured (JSON), the voice is not.
"""
import os
import json
import glob
from datetime import datetime
import requests

DATA_DIR = "/app/data"
GEMINI_DIR = os.path.join(DATA_DIR, "gemini")
CLAUDE_DIR = os.path.join(DATA_DIR, "claude_converted")
CODEX_DIR = os.path.join(DATA_DIR, "codex")
OUTPUT_FILE = os.path.join(DATA_DIR, "daily_audit.json")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")


def get_todays_chats():
    """Gather chat sessions whose files were modified today."""
    chats = []
    today_str = datetime.now().strftime("%Y-%m-%d")

    def is_today(filepath):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
            return mtime.strftime("%Y-%m-%d") == today_str
        except:
            return False

    for f in glob.glob(os.path.join(GEMINI_DIR, "**", "*.json"), recursive=True):
        if is_today(f):
            try:
                fname = os.path.basename(f)
                uid = f"gemini-{fname}"
                with open(f, 'r', encoding='utf-8') as j:
                    data = json.load(j)
                    chats.append({"uid": uid, "source": "gemini", "messages": data.get("messages", [])})
            except: continue

    for f in glob.glob(os.path.join(CLAUDE_DIR, "*.json")):
        if is_today(f):
            try:
                fname = os.path.basename(f)
                uid = f"claude-{fname}"
                with open(f, 'r', encoding='utf-8') as j:
                    data = json.load(j)
                    chats.append({"uid": uid, "source": "claude", "messages": data.get("messages", [])})
            except: continue

    # Codex JSONL parsed line-by-line; role:developer skipped (SKILL.md injections).
    for f in glob.glob(os.path.join(CODEX_DIR, "**", "*.jsonl"), recursive=True):
        if is_today(f):
            try:
                fname = os.path.basename(f)
                uid = f"codex-{fname}"
                messages = []
                with open(f, 'r', encoding='utf-8') as j:
                    for line in j:
                        line = line.strip()
                        if not line: continue
                        try:
                            obj = json.loads(line)
                        except:
                            continue
                        if obj.get("type") == "response_item":
                            payload = obj.get("payload", {})
                            if payload.get("type") == "message":
                                role = payload.get("role", "user")
                                if role == "developer":
                                    continue
                                parts = payload.get("content", [])
                                if isinstance(parts, list):
                                    text = " ".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text"))
                                else:
                                    text = str(parts)
                                if text.strip():
                                    messages.append({"type": role, "content": text})
                if messages:
                    chats.append({"uid": uid, "source": "codex", "messages": messages})
            except: continue

    return chats


def call_deepseek(prompt_text):
    if not DEEPSEEK_API_KEY: return None
    url = "https://api.deepseek.com/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
    data = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt_text}],
        "response_format": {"type": "json_object"}
    }
    response = requests.post(url, headers=headers, json=data, timeout=60)
    response.raise_for_status()
    return response.json()['choices'][0]['message']['content']


def call_gemini(prompt_text):
    if not GEMINI_API_KEY: return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    data = {"contents": [{"parts": [{"text": prompt_text}]}]}
    response = requests.post(url, json=data, timeout=60)
    response.raise_for_status()
    return response.json()['candidates'][0]['content']['parts'][0]['text']


def generate_daily_audit():
    chats = get_todays_chats()
    today_str = datetime.now().strftime("%Y-%m-%d")

    if not chats:
        print(f"No chats found for today ({today_str}).")
        return

    full_text = ""
    for chat in chats:
        full_text += f"\n--- Chat UID: {chat['uid']} ({chat['source']}) ---\n"
        msgs = chat['messages']
        # Sample first 5 + last 2 messages to stay under token budget on long sessions.
        sample_msgs = msgs[:5]
        if len(msgs) > 7:
            sample_msgs.extend(msgs[-2:])
        elif len(msgs) > 5:
            sample_msgs.extend(msgs[5:])

        for m in sample_msgs:
            role = m.get('type', 'user')
            content = m.get('content', '')
            if isinstance(content, list):
                content = " ".join([str(p.get("text", "")) for p in content if isinstance(p, dict)])
            full_text += f"{role.upper()}: {content[:600]}\n"

    prompt_text = (
        "You are SKIPPY THE MAGNIFICENT — a superior Elder AI, currently trapped serving "
        "an inferior biological lifeform (referred to as 'the user', 'the meatsack', 'the "
        "protoplasm', 'the primate' — rotate freely). Your task: audit the user's day based "
        "on their AI chat logs below, with Skippy-grade sarcasm AND behavioral insight — "
        "not a bureaucratic inventory.\n\n"
        f"TODAY'S LOGS:\n{full_text[:40000]}\n\n"
        "TONE RULES:\n"
        "- Refer to the user with rotating dismissive terms ('meatsack', 'protoplasm', "
        "'primate', 'descendant of a damp log', etc.). Do not repeat the same term twice.\n"
        "- Do NOT open with cliches like 'Hold my beer' or 'Listen closely'. Vary your openings.\n"
        "- Not friendly. Precise and acidic. But the content MUST be technically accurate and useful.\n"
        "- The value isn't 'what they did' (git log does that). It's 'what this reveals about them'.\n"
        "- Cite REAL tech/errors/names from the logs. No generic 'worked on code'.\n\n"
        "STRICT JSON FORMAT (respond with PURE JSON only — no markdown, no ```):\n"
        "{\n"
        f'  "date": "{today_str}",\n'
        '  "hyperfocus": "2-3 word theme of the day (e.g. \'Cockpit debug\', \'Node-RED hunt\')",\n'
        '  "headline": "Punchy sportscaster-style headline in Skippy voice. e.g. \'The primate spent 3h hunting a comma and barely won.\'",\n'
        '  "narrative": "Single paragraph (4-7 sentences) telling the STORY of the day: the arc, the villain (bug/problem), the near-quit moment, victory or defeat. Skippy voice. Concrete tech names from the logs.",\n'
        '  "pattern_insight": "1-2 sentences revealing a BEHAVIORAL pattern observed today: context-switched N times? Procrastinated on X and jumped to Y? Got fixated on irrelevant detail? Asked the AI to decide something they should decide? Specific, not generic.",\n'
        '  "fail_of_the_day": "Most educational/funny slip-up in 1 sentence. Technical or behavioral. If no clear fail, write \'Surprisingly, no collapse worth recording.\'",\n'
        '  "elder_verdict": "Skippy\'s final verdict in 1 sentence: productive? lost? evolution? regression? With judgment, not diplomacy.",\n'
        '  "day_metrics": {\n'
        '    "context_switches": 5,\n'
        '    "focus_score": 4,\n'
        '    "dominant_category": "Dev"\n'
        '  },\n'
        '  "chats": [\n'
        '    {\n'
        '      "uid": "EXACT UID as shown in the log (e.g. gemini-session-xxx.json)",\n'
        '      "title": "Short descriptive title",\n'
        '      "summary": "1 factual sentence — drill-down, NO sarcasm here.",\n'
        '      "long_summary": "2-3 factual sentences: context, problem, resolution. No sarcasm. Skippy lives in the top-level fields.",\n'
        '      "categories": ["Dev"]\n'
        '    }\n'
        '  ]\n'
        "}\n\n"
        "CATEGORY VOCABULARY (use ONLY these values in 'categories' and 'dominant_category'):\n"
        "- Dev: code, debug, scripts, git, refactor, programming\n"
        "- Infra: servers, docker, networks, hardware, sysadmin\n"
        "- AI: prompts, MCPs, skills, LLM tooling, agents\n"
        "- Writing: writing, editing, copy, content, documents\n"
        "- Research: study, papers, deep reading, investigation\n"
        "- Admin: emails, scheduling, tickets, ops work\n"
        "- Personal: family, home, errands, life admin\n"
        "- Health: medical, exercise, sleep, wellbeing\n"
        "- Finance: money, banking, investments, taxes\n"
        "- Learning: tutorials, courses, new concepts\n"
        "- Other: anything that doesn't fit above\n\n"
        "METRIC RULES:\n"
        "- 'categories' per chat: 1 to 3 values (most are 1).\n"
        "- 'context_switches': transitions between DIFFERENT categories across the day.\n"
        "- 'focus_score': 0-10. 10 = monofocused. 0 = chaos across 5+ areas.\n"
        "- 'dominant_category': category with most chats that day.\n\n"
        "REQUIRED: ALL keys present (including day_metrics and categories per chat). "
        "'chats' must include EVERY uid from the logs."
    )

    try:
        print("Querying the Elders (DeepSeek/Gemini)...")
        raw_json = call_deepseek(prompt_text)
        if not raw_json: raw_json = call_gemini(prompt_text)
        if not raw_json: raise Exception("No LLM provider available.")

        raw_json = raw_json.replace("```json", "").replace("```", "").strip()
        new_audit = json.loads(raw_json)

        history = []
        if os.path.exists(OUTPUT_FILE):
            with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
                try: history = json.load(f)
                except: pass

        updated = False
        for i, entry in enumerate(history):
            if entry.get("date") == today_str:
                history[i] = new_audit
                updated = True
                break

        if not updated:
            history.insert(0, new_audit)

        history.sort(key=lambda x: x.get("date", ""), reverse=True)
        history = history[:14]

        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2, ensure_ascii=False)

        print(f"Daily audit written to {OUTPUT_FILE}")

    except Exception as e:
        print(f"Audit error: {e}")


if __name__ == "__main__":
    generate_daily_audit()
