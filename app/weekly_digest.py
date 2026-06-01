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
        print("daily_audit.json não existe — sem dados pra digerir.")
        return None

    with open(DAILY_FILE, "r", encoding="utf-8") as f:
        history = json.load(f)

    last7 = history[:7]
    if not last7:
        print("Histórico vazio.")
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
        "Você é SKIPPY THE MAGNIFICENT em modo ANÁLISE SEMANAL — uma camada acima do audit diário. "
        "the user recebe esses audits diariamente. Agora você vai sintetizar a SEMANA — não repetir os dias, mas REVELAR PADRÃO TRANSVERSAL.\n\n"
        f"PERÍODO: {period_start} → {period_end}\n\n"
        "AUDITS DIÁRIOS DA SEMANA:\n"
        f"{blob}\n\n"
        "REGRAS:\n"
        "- NÃO repita o que cada dia já disse. O valor está no que se REPETIU, no que MUDOU, no que ele FUGIU.\n"
        "- Use voz Skippy: ácida, precisa, sem clichês ('Hold my beer', 'Listen closely' proibidos).\n"
        "- Trate User como 'macaco', 'protoplasma', 'descendente de log úmido', 'meatsack' — varie.\n"
        "- Cite tecnologias/temas REAIS dos audits, não generalidades.\n\n"
        "FORMATO JSON ESTRITO (apenas JSON, sem markdown):\n"
        "{\n"
        f'  "period_start": "{period_start}",\n'
        f'  "period_end": "{period_end}",\n'
        f'  "generated_at": "{today_str}",\n'
        '  "weekly_headline": "Manchete única da semana inteira em 1 frase punchy. Ex: \'A semana em que o macaco prometeu Infra e entregou Trading.\'",\n'
        '  "weekly_narrative": "Parágrafo único (5-8 frases) contando o ARCO da semana: como começou, qual o vilão recorrente, qual padrão se repetiu, onde houve evolução ou regressão. NÃO é resumo dia-a-dia. É META-narrativa.",\n'
        '  "drift_pattern": "1-2 frases revelando a DERIVA da semana: para onde a atenção migrou, qual a fuga recorrente. Ex: \'Toda vez que aparecia tarefa Business, ele pulava pra Infra. Padrão claro de fuga do desconforto comercial.\'",\n'
        '  "weekly_fail": "O fail mais educativo da semana — pode ser técnico (mesmo bug 3x) ou comportamental (largou X tarefa por Y dias). 1 frase.",\n'
        '  "weekly_verdict": "Sentença final do Skippy sobre a semana em 1 frase. Com julgamento.",\n'
        '  "focus_avg": 5.0,\n'
        '  "focus_trend": "subindo|caindo|estável",\n'
        '  "top_categories": [["Infra", 12], ["IA-Tooling", 8]],\n'
        '  "days_count": 7\n'
        "}\n\n"
        "INSTRUÇÕES NUMÉRICAS:\n"
        "- focus_avg: média aritmética dos focus_score válidos (números) na semana, 1 decimal.\n"
        "- focus_trend: compare primeiros 3 dias vs últimos 3. 'subindo' se delta > +1, 'caindo' se delta < -1, senão 'estável'.\n"
        "- top_categories: top 3 categorias somando todas chats da semana (use os CATS_DAY fornecidos), formato [[cat, count], ...]."
    )

    try:
        print("Gerando weekly digest via DeepSeek...")
        raw = call_deepseek(prompt)
        if not raw:
            raw = call_gemini(prompt)
        if not raw:
            raise Exception("Nenhuma API respondeu.")
        raw = raw.replace("```json", "").replace("```", "").strip()
        digest = json.loads(raw)
        digest["days_count"] = len(last7)
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(digest, f, indent=2, ensure_ascii=False)
        print(f"Weekly digest salvo em {OUTPUT_FILE}")
        return digest
    except Exception as e:
        print(f"Erro no weekly digest: {e}")
        return None


if __name__ == "__main__":
    generate_weekly_digest()
