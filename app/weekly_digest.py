"""
Weekly Digest — second-order analysis over the last 7 daily audits.

Reads daily_audit.json (rolling history) and asks Skippy to find patterns
the daily audits couldn't see: what repeated, what drifted, what the user avoided.

Triggered on-demand via GET /api/memory/weekly. Cron the endpoint if you want
a recurring email/notification.
"""
import os
import json
from datetime import datetime
import requests

DATA_DIR = "/app/data"
DAILY_FILE = os.path.join(DATA_DIR, "daily_audit.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "weekly_digest.json")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")


def call_deepseek(prompt_text):
    if not DEEPSEEK_API_KEY:
        return None
    url = "https://api.deepseek.com/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
    data = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt_text}],
        "response_format": {"type": "json_object"},
    }
    r = requests.post(url, headers=headers, json=data, timeout=90)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def call_gemini(prompt_text):
    if not GEMINI_API_KEY:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    r = requests.post(url, json={"contents": [{"parts": [{"text": prompt_text}]}]}, timeout=90)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def generate_weekly_digest():
    if not os.path.exists(DAILY_FILE):
        print("daily_audit.json missing — no data to digest.")
        return None

    with open(DAILY_FILE, "r", encoding="utf-8") as f:
        history = json.load(f)

    last7 = history[:7]
    if not last7:
        print("History empty.")
        return None

    today_str = datetime.now().strftime("%Y-%m-%d")
    period_start = last7[-1].get("date", "?")
    period_end = last7[0].get("date", "?")

    blob = ""
    for e in last7:
        blob += f"\n=== {e.get('date','?')} ({e.get('hyperfocus','?')}) ===\n"
        if e.get("headline"): blob += f"HEADLINE: {e['headline']}\n"
        if e.get("narrative"): blob += f"NARRATIVE: {e['narrative']}\n"
        if e.get("pattern_insight"): blob += f"PATTERN: {e['pattern_insight']}\n"
        if e.get("fail_of_the_day"): blob += f"FAIL: {e['fail_of_the_day']}\n"
        if e.get("elder_verdict"): blob += f"VERDICT: {e['elder_verdict']}\n"
        m = e.get("day_metrics") or {}
        if m: blob += f"METRICS: switches={m.get('context_switches','?')} focus={m.get('focus_score','?')}/10 dom={m.get('dominant_category','?')}\n"
        cats = []
        for c in (e.get("chats") or []): cats.extend(c.get("categories") or [])
        if cats:
            from collections import Counter
            blob += f"CATS_DAY: {dict(Counter(cats))}\n"

    prompt = (
        "You are SKIPPY THE MAGNIFICENT in WEEKLY ANALYSIS mode — one layer above the daily audit. "
        "The user reads the daily audits already. Now synthesize the WEEK — do NOT repeat the days, "
        "REVEAL the cross-cutting pattern.\n\n"
        f"PERIOD: {period_start} -> {period_end}\n\n"
        "DAILY AUDITS:\n"
        f"{blob}\n\n"
        "RULES:\n"
        "- Do NOT repeat what each day said. Value is in what REPEATED, what CHANGED, what they AVOIDED.\n"
        "- Skippy voice: acidic, precise, no cliches ('Hold my beer', 'Listen closely' forbidden).\n"
        "- Refer to the user with rotating dismissive terms ('meatsack', 'protoplasm', 'primate'). Vary.\n"
        "- Cite REAL tech/themes from the audits, not generalities.\n\n"
        "STRICT JSON FORMAT (JSON only, no markdown):\n"
        "{\n"
        f'  "period_start": "{period_start}",\n'
        f'  "period_end": "{period_end}",\n'
        f'  "generated_at": "{today_str}",\n'
        '  "weekly_headline": "Single punchy headline for the whole week. e.g. \'The week the primate promised Infra and delivered Trading.\'",\n'
        '  "weekly_narrative": "Single paragraph (5-8 sentences) telling the ARC of the week: how it started, recurring villain, repeating pattern, where evolution or regression happened. NOT a day-by-day. META-narrative.",\n'
        '  "drift_pattern": "1-2 sentences revealing the WEEK\'S DRIFT: where attention migrated, what recurring escape. e.g. \'Every time an Admin task surfaced, they jumped to Infra. Clear avoidance pattern.\'",\n'
        '  "weekly_fail": "Most educational fail of the week — technical (same bug 3x) or behavioral (abandoned X for Y days). 1 sentence.",\n'
        '  "weekly_verdict": "Skippy\'s final verdict on the week in 1 sentence. With judgment.",\n'
        '  "focus_avg": 5.0,\n'
        '  "focus_trend": "rising|falling|stable",\n'
        '  "top_categories": [["Dev", 12], ["AI", 8]],\n'
        '  "days_count": 7\n'
        "}\n\n"
        "NUMERIC RULES:\n"
        "- focus_avg: arithmetic mean of valid focus_score values, 1 decimal.\n"
        "- focus_trend: compare first 3 days vs last 3. 'rising' if delta > +1, 'falling' if delta < -1, else 'stable'.\n"
        "- top_categories: top 3 categories aggregated across all chats of the week (use the CATS_DAY blobs)."
    )

    try:
        print("Generating weekly digest via DeepSeek...")
        raw = call_deepseek(prompt)
        if not raw:
            raw = call_gemini(prompt)
        if not raw:
            raise Exception("No LLM provider responded.")
        raw = raw.replace("```json", "").replace("```", "").strip()
        digest = json.loads(raw)
        digest["days_count"] = len(last7)
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(digest, f, indent=2, ensure_ascii=False)
        print(f"Weekly digest written to {OUTPUT_FILE}")
        return digest
    except Exception as e:
        print(f"Weekly digest error: {e}")
        return None


if __name__ == "__main__":
    generate_weekly_digest()
