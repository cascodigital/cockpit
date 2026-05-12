"""
Memory Distiller — extracts a long-term profile from chats matching configured
keywords. Useful for distilling recurring themes across a corpus of sessions
(e.g. coaching, journaling, debugging diaries).

Configure MEMORY_KEYWORDS in your .env as a comma-separated list. If empty,
the distiller falls back to the N most recent chats regardless of content.
"""
import os
import json
import glob
from datetime import datetime
import requests

DATA_DIR = "/app/data"
GEMINI_DIR = os.path.join(DATA_DIR, "gemini")
CLAUDE_DIR = os.path.join(DATA_DIR, "claude_converted")
OUTPUT_FILE = os.path.join(DATA_DIR, "memory_profile.json")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
MEMORY_KEYWORDS = [
    k.strip().lower() for k in os.environ.get("MEMORY_KEYWORDS", "").split(",") if k.strip()
]
MEMORY_LIMIT = int(os.environ.get("MEMORY_LIMIT", "15"))


def get_recent_chats(limit=15):
    """Return up to `limit` chats matching MEMORY_KEYWORDS (or most recent if empty).

    Codex is intentionally NOT scanned: developer-role injections (SKILL.md, system
    prompts) produce false positives. The distiller is meant for human dialogues.
    """
    chats = []

    for f in glob.glob(os.path.join(GEMINI_DIR, "**", "*.json"), recursive=True):
        try:
            with open(f, 'r', encoding='utf-8') as j:
                data = json.load(j)
                if MEMORY_KEYWORDS:
                    content_str = str(data).lower()
                    if not any(kw in content_str for kw in MEMORY_KEYWORDS):
                        continue
                chats.append({"date": data.get("startTime", ""), "messages": data.get("messages", []), "source": "gemini"})
        except: continue

    for f in glob.glob(os.path.join(CLAUDE_DIR, "*.json")):
        try:
            with open(f, 'r', encoding='utf-8') as j:
                data = json.load(j)
                if MEMORY_KEYWORDS:
                    content_str = str(data).lower()
                    if not any(kw in content_str for kw in MEMORY_KEYWORDS):
                        continue
                chats.append({"date": data.get("startTime", ""), "messages": data.get("messages", []), "source": "claude"})
        except: continue

    chats.sort(key=lambda x: x["date"], reverse=True)
    return chats[:limit]


def call_deepseek(prompt_text):
    if not DEEPSEEK_API_KEY:
        return None
    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}"
    }
    data = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt_text}],
        "response_format": {"type": "json_object"}
    }
    response = requests.post(url, headers=headers, json=data, timeout=60)
    response.raise_for_status()
    return response.json()['choices'][0]['message']['content']


def call_gemini(prompt_text):
    if not GEMINI_API_KEY:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    data = {"contents": [{"parts": [{"text": prompt_text}]}]}
    response = requests.post(url, json=data, timeout=60)
    response.raise_for_status()
    return response.json()['candidates'][0]['content']['parts'][0]['text']


def distill_memory():
    relevant_chats = get_recent_chats(limit=MEMORY_LIMIT)
    if not relevant_chats:
        print("No chats found for distillation.")
        return

    full_text = ""
    for chat in relevant_chats:
        full_text += f"\n--- Session {chat['date']} ({chat['source']}) ---\n"
        for m in chat['messages']:
            role = m.get('type', 'system')
            content = m.get('content', '')
            if isinstance(content, list):
                content = " ".join([str(p.get("text", "")) for p in content if isinstance(p, dict)])
            full_text += f"{role.upper()}: {content}\n"

    prompt_text = (
        "You are SKIPPY THE MAGNIFICENT acting as the user's Memory Architect. "
        "Synthesize a long-term profile from the chat logs below — patterns, "
        "recurring themes, blocks, open threads. Skippy voice: acidic but technically precise.\n\n"
        f"LOGS FOR ANALYSIS:\n{full_text[:30000]}\n\n"
        "OUTPUT a JSON object with these keys (JSON only, no markdown):\n"
        "1. personality_dna: Brief summary of the user's current state of mind and interests.\n"
        "2. recurring_bugs: List of recurring cognitive distortions, blockers, or mistakes.\n"
        "3. pending_homework: Open tasks, experiments, or commitments left dangling.\n"
        "4. last_session_summary: Executive summary of the most recent conversation.\n"
    )

    try:
        raw_json = None
        try:
            print("Querying DeepSeek...")
            raw_json = call_deepseek(prompt_text)
        except Exception as e:
            print(f"DeepSeek failed: {e}. Falling back to Gemini...")
            raw_json = call_gemini(prompt_text)

        if not raw_json:
            print("No LLM provider returned a response.")
            return

        raw_json = raw_json.replace("```json", "").replace("```", "").strip()
        dna_data = json.loads(raw_json)
        dna_data["last_updated"] = datetime.now().isoformat()

        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(dna_data, f, indent=2, ensure_ascii=False)

        print(f"Memory profile written to {OUTPUT_FILE}")
    except Exception as e:
        print(f"Distillation error: {e}")


if __name__ == "__main__":
    distill_memory()
