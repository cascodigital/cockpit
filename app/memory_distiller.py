import os
import json
import glob
from datetime import datetime, timezone, timedelta
import requests

# Identity injected by scripts/promote.py — the OSS mirror carries no
# hardcoded person name or persona.
USER_NAME = os.environ.get("USER_NAME", "the user")
PERSONA_NAME = os.environ.get("PERSONA_NAME", "the auditor")
MEMORY_SKILL = os.environ.get("MEMORY_SKILL", "").strip()


DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
GEMINI_DIR = os.path.join(DATA_DIR, "gemini")
CLAUDE_DIR = os.path.join(DATA_DIR, "claude_converted")
OUTPUT_FILE = os.path.join(DATA_DIR, "memory_profile.json")
SKILL_LOG_PATH = os.path.join(DATA_DIR, "skill_log.jsonl")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
# Thinking (reasoning) mode is disabled on every call: structured extraction gains nothing from it.
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")

# Legacy: keyword matching over the whole blob kept hitting the skill registry (94 false
# positives for "skpsi"). Ground truth is now skill_log (REAL activations). Kept for reference only.
LUDOVICO_KEYWORDS = ["skpsi", "leto ii", "ludovico von drake"]
BRT = timezone(timedelta(hours=-3))

def load_existing_dna():
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def get_last_updated(dna):
    val = dna.get("last_updated", "")
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except:
        return None

def _to_utc(s):
    """Normalize a timestamp to aware UTC. Chats come as ...Z (UTC); skill_log is local
    (-03:00, some without offset → assume BRT)."""
    s = str(s).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BRT)
    return dt.astimezone(timezone.utc)

def _load_skpsi_activations():
    """Verdade-terra: timestamps de ativacao REAL do skpsi (skill_log), agrupados por source."""
    acts = {"gemini": [], "claude": []}
    if not os.path.exists(SKILL_LOG_PATH):
        return acts
    with open(SKILL_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("skill") != MEMORY_SKILL:
                continue
            ag = d.get("agent")
            if ag in acts:
                u = _to_utc(d.get("ts"))
                if u:
                    acts[ag].append(u)
    return acts

def get_new_psi_chats(since_dt):
    """REAL MEMORY_SKILL sessions, matched against skill_log by time window. No keyword noise.
    A session counts if its startTime falls near a skill activation from the SAME agent."""
    acts = _load_skpsi_activations()
    WIN_BACK = timedelta(minutes=5)   # clock-skew tolerance
    WIN_FWD = timedelta(minutes=30)   # session starts right after activation

    def is_psi(start_utc, source):
        for a in acts.get(source, []):
            if a - WIN_BACK <= start_utc <= a + WIN_FWD:
                return True
        return False

    chats = []
    seen = set()  # dedup: recursive glob can count the same file twice
    # Codex intentionally skipped (high false-positive rate from trading sessions)
    for source, dirp, patt in [("gemini", GEMINI_DIR, "**/*.json"), ("claude", CLAUDE_DIR, "*.json")]:
        for f in glob.glob(os.path.join(dirp, patt), recursive=True):
            rf = os.path.realpath(f)
            if rf in seen:
                continue
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(f))
                if since_dt and mtime <= since_dt:
                    continue
                with open(f, "r", encoding="utf-8") as j:
                    data = json.load(j)
                start_utc = _to_utc(data.get("startTime"))
                if not start_utc or not is_psi(start_utc, source):
                    continue
                seen.add(rf)
                chats.append({
                    "date": data.get("startTime", ""),
                    "messages": data.get("messages", []),
                    "source": source
                })
            except Exception:
                continue

    chats.sort(key=lambda x: x["date"], reverse=True)
    return chats

def build_prompt(chats, existing_dna):
    full_text = ""
    for chat in chats:
        full_text += f"\n--- Sessao {chat['date']} ({chat['source']}) ---\n"
        for m in chat["messages"]:
            role = m.get("type", "system")
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join([str(p.get("text", "")) for p in content if isinstance(p, dict)])
            full_text += f"{role.upper()}: {content}\n"

    existing_bugs = json.dumps(existing_dna.get("recurring_bugs", []), ensure_ascii=False)
    existing_hw = json.dumps(existing_dna.get("pending_homework", []), ensure_ascii=False)

    return (
        "Voce e o Arquiteto de Memoria do Prof. MemoryProfile Von Drake (psicologo TCC do usuario).\n"
        "O usuario e Engenheiro MSP Senior, hipercritico, pessimista treinado.\n\n"
        "LOGS DA SESSAO PARA ANALISE:\n"
        f"{full_text[:25000]}\n\n"
        "BUGS COGNITIVOS JA REGISTRADOS (nao repetir, apenas complementar se houver novos):\n"
        f"{existing_bugs}\n\n"
        "HOMEWORK PENDENTE JA REGISTRADO (nao repetir, apenas complementar se houver novos):\n"
        f"{existing_hw}\n\n"
        "INSTRUCAO: Analise APENAS a sessao acima e gere um JSON com:\n"
        "1. new_bugs: lista de NOVAS distorcoes cognitivas/comportamentais identificadas nesta sessao "
        "(APENAS padroes mentais: catastrofizacao, evitacao, impostor, etc). "
        "Se nenhum novo bug foi identificado, retorne lista vazia [].\n"
        "2. resolved_homework: lista de itens do homework pendente que o usuario confirmou como concluidos nesta sessao "
        "(copiar texto exato do item). Se nenhum foi concluido, retorne lista vazia [].\n"
        "3. new_homework: lista de NOVAS tarefas comportamentais/TCC definidas nesta sessao "
        "(APENAS tarefas de vida/comportamento, NUNCA ferramentas tecnicas, CLIs ou configuracoes). "
        "Se nenhum novo homework foi definido, retorne lista vazia [].\n"
        "4. session_summary: resumo em 3-5 frases da sessao: temas abordados, bugs trabalhados, estado emocional do paciente, o que ficou pendente.\n"
        "5. emotional_state: estado emocional atual do usuario em uma frase curta.\n"
        "6. energy_level: nivel de energia atual em uma frase curta.\n"
        "Responda APENAS o JSON puro, sem markdown."
    )

def call_deepseek(prompt_text):
    if not DEEPSEEK_API_KEY:
        return None
    url = "https://api.deepseek.com/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
    data = {
        "model": DEEPSEEK_MODEL,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": prompt_text}],
        "response_format": {"type": "json_object"}
    }
    r = requests.post(url, headers=headers, json=data, timeout=60)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def call_gemini(prompt_text):
    if not GEMINI_API_KEY:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    r = requests.post(url, json={"contents": [{"parts": [{"text": prompt_text}]}]}, timeout=60)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]

def distill_memory():
    """Return a STATUS string: "no-new-session" | "api-error" | "updated (...)".
    It used to return None on every path and the caller in cockpit.py printed
    "Profile updated." unconditionally — reporting success even on days when nothing
    was done. An optimistic log hides real failures."""
    existing_dna = load_existing_dna()
    since_dt = get_last_updated(existing_dna)

    chats = get_new_psi_chats(since_dt)
    if not chats:
        print(f"[MemoryProfile] No new sessions since {since_dt}. Profile unchanged.")
        return "no-new-session"

    print(f"[MemoryProfile] {len(chats)} new session(s) to process.")
    prompt = build_prompt(chats, existing_dna)

    raw_json = None
    try:
        print("[MemoryProfile] Trying DeepSeek...")
        raw_json = call_deepseek(prompt)
    except Exception as e:
        print(f"[MemoryProfile] DeepSeek failed: {e}. Trying Gemini...")
        try:
            raw_json = call_gemini(prompt)
        except Exception as e2:
            print(f"[MemoryProfile] Gemini failed: {e2}. Profile unchanged.")
            return "api-error"

    if not raw_json:
        print("[MemoryProfile] No response from the APIs. Profile unchanged.")
        return "api-error"

    raw_json = raw_json.replace("```json", "").replace("```", "").strip()
    delta = json.loads(raw_json)

    # --- INCREMENTAL MERGE ---

    # 1. Archive the previous session into history (max 10)
    prev_summary = existing_dna.get("last_session_summary", "")
    if prev_summary and "Sem sessao" not in prev_summary:
        history = existing_dna.get("session_history", [])
        history.append({
            "date": existing_dna.get("last_updated", ""),
            "summary": prev_summary
        })
        existing_dna["session_history"] = history[-10:]

    # 2. Merge bugs: add new ones, keep existing
    current_bugs = existing_dna.get("recurring_bugs", [])
    for bug in delta.get("new_bugs", []):
        if bug and bug not in current_bugs:
            current_bugs.append(bug)
    existing_dna["recurring_bugs"] = current_bugs

    # 3. Merge homework: remove completed, add new
    current_hw = existing_dna.get("pending_homework", [])
    resolved = delta.get("resolved_homework", [])
    current_hw = [h for h in current_hw if h not in resolved]
    for hw in delta.get("new_homework", []):
        if hw and hw not in current_hw:
            current_hw.append(hw)
    existing_dna["pending_homework"] = current_hw

    # 4. Update state and summary
    dna = existing_dna.get("personality_dna", {})
    if delta.get("emotional_state"):
        dna["emotional_state"] = delta["emotional_state"]
    if delta.get("energy_level"):
        dna["energy_level"] = delta["energy_level"]
    existing_dna["personality_dna"] = dna
    existing_dna["last_session_summary"] = delta.get("session_summary", "")
    existing_dna["last_updated"] = datetime.now().isoformat()

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(existing_dna, f, indent=2, ensure_ascii=False)
    print(f"[MemoryProfile] Profile updated (incremental merge). Bugs: {len(current_bugs)}, HW: {len(current_hw)}.")
    return f"updated ({len(chats)} session(s), bugs={len(current_bugs)}, hw={len(current_hw)})"

if __name__ == "__main__":
    distill_memory()
