import os
import json
import glob
from datetime import datetime, timedelta, timezone
import requests

# Identity injected by scripts/promote.py — the OSS mirror carries no
# hardcoded person name or persona.
USER_NAME = os.environ.get("USER_NAME", "the user")
PERSONA_NAME = os.environ.get("PERSONA_NAME", "the auditor")
MEMORY_SKILL = os.environ.get("MEMORY_SKILL", "").strip()


# Settings
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
BRT = timezone(timedelta(hours=-3))
GEMINI_DIR = os.path.join(DATA_DIR, "gemini")
CLAUDE_DIR = os.path.join(DATA_DIR, "claude_converted")
CODEX_DIR = os.path.join(DATA_DIR, "codex")
# Claude Web. It is the ONLY web source that feeds memory: its sync uses the
# conversation's real `created_at`. gemini_site stays out because 141 of its 199
# conversations carry the sentinel date 2025-12-31, and chatgpt_site by the user's
# choice (source being retired). See docs/MEMORY-PIPELINE.md.
CLAUDE_SITE_DIR = os.path.join(DATA_DIR, "claude_site")
OUTPUT_FILE = os.path.join(DATA_DIR, "daily_audit.json")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
# DeepSeek model for every audit/memory call. Thinking (reasoning) mode is disabled:
# these are structured summarization tasks and reasoning only adds latency and cost.
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")

def _brt_date(ts):
    """ISO timestamp (UTC, Z suffix) -> BRT date 'YYYY-MM-DD'. None if unparseable.
    Without the conversion, the first 3 UTC hours of each day land on the wrong day."""
    if not ts:
        return None
    s = str(ts).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BRT)
    return dt.astimezone(BRT).strftime("%Y-%m-%d")


def get_todays_chats(target_str=None):
    """Chats whose REAL ACTIVITY happened on `target_str` (BRT date).

    The filter used to be the converted file's mtime — which is stamped by the sync,
    not by the conversation. Result: the nightly Claude Web/Gemini web backfill
    rewrote old conversations with today's mtime and they landed in the wrong
    day's audit. The criterion is now the message timestamps; mtime survives only
    as a cheap pre-filter (a file containing messages from day D cannot have been
    written before D), which avoids opening ~3,500 files / 825 MB on every run.
    A session that crosses midnight counts on both days, on purpose.
    """
    chats = []
    today_str = target_str or datetime.now().strftime("%Y-%m-%d")

    try:
        day_start = datetime.strptime(today_str, "%Y-%m-%d").replace(tzinfo=BRT).timestamp()
    except Exception:
        day_start = 0

    def maybe(filepath):
        """mtime pre-filter: drop files too old to contain the target day."""
        try:
            return os.path.getmtime(filepath) >= day_start
        except OSError:
            return False

    def touched(msgs):
        """True if any chat message falls on the target day (BRT time)."""
        for m in msgs:
            if _brt_date(m.get("timestamp")) == today_str:
                return True
        return False

    # Scan Gemini
    for f in glob.glob(os.path.join(GEMINI_DIR, "**", "*.json"), recursive=True):
        if maybe(f):
            try:
                fname = os.path.basename(f)
                uid = f"gemini-{fname}"
                with open(f, 'r', encoding='utf-8') as j:
                    data = json.load(j)
                    msgs = data.get("messages", [])
                    if touched(msgs) or _brt_date(data.get("startTime")) == today_str:
                        chats.append({"uid": uid, "source": "gemini", "messages": msgs})
            except: continue

    # Scan converted Claude
    for f in glob.glob(os.path.join(CLAUDE_DIR, "*.json")):
        if maybe(f):
            try:
                fname = os.path.basename(f)
                uid = f"claude-{fname}"
                with open(f, 'r', encoding='utf-8') as j:
                    data = json.load(j)
                    msgs = data.get("messages", [])
                    if touched(msgs) or _brt_date(data.get("startTime")) == today_str:
                        chats.append({"uid": uid, "source": "claude", "messages": msgs})
            except: continue

    # Scan Codex (native JSONL, parsed line by line; skips role:developer = SKILL.md injection)
    for f in glob.glob(os.path.join(CODEX_DIR, "**", "*.jsonl"), recursive=True):
        if maybe(f):
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
                                    messages.append({"type": role, "content": text,
                                                     "timestamp": obj.get("timestamp")})
                if messages and touched(messages):
                    chats.append({"uid": uid, "source": "codex", "messages": messages})
            except: continue

    # Scan Claude Web. The uid mirrors the indexer's (`claude-web-<rel>` with path
    # separators turned into `__`), otherwise the audit drill-down cannot find the chat.
    if os.path.isdir(CLAUDE_SITE_DIR):
        for f in glob.glob(os.path.join(CLAUDE_SITE_DIR, "**", "*.json"), recursive=True):
            if maybe(f):
                try:
                    rel_name = os.path.relpath(f, CLAUDE_SITE_DIR).replace(os.sep, "__")
                    uid = f"claude-web-{rel_name}"
                    with open(f, 'r', encoding='utf-8') as j:
                        data = json.load(j)
                        msgs = data.get("messages", [])
                        # Here startTime is the right criterion: the per-message timestamp
                        # is constant (capture stamp), not real chronology.
                        if _brt_date(data.get("startTime")) == today_str or touched(msgs):
                            chats.append({"uid": uid, "source": "claude-web", "messages": msgs})
                except: continue

    return chats

def call_deepseek(prompt_text):
    if not DEEPSEEK_API_KEY: return None
    url = "https://api.deepseek.com/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
    data = {
        "model": DEEPSEEK_MODEL,
        "thinking": {"type": "disabled"},
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

OPEN_THREAD_TTL_DAYS = 14


def expire_open_threads(history, today_str, ttl_days=OPEN_THREAD_TTL_DAYS):
    """Hard prune of `open_threads` — the only one that does not depend on the LLM cooperating.

    The prompt re-injects the last 7 days of `open_threads`, so the model re-emits the
    pending item with today's date and it never ages: the 30-day retention re-stamps it
    instead of expiring it. `resolved_threads` was supposed to kill it, but that requires
    the close to be worded EXACTLY like the day it was born — and the model rewrites the
    sentence every time, so the match almost never happens.

    Here an item disappears by AGE, without anyone having to prove it died: once it is
    older than `ttl_days` without being re-emitted with new text, it is dropped. If the
    problem is real, tomorrow's logs bring it back.
    """
    from datetime import date

    def _d(s):
        try:
            y, m, dd = (int(x) for x in str(s).split("-"))
            return date(y, m, dd)
        except Exception:
            return None

    today = _d(today_str)
    if not today:
        return 0

    pruned = 0
    for entry in history:
        day = _d(entry.get("date"))
        if not day or (today - day).days <= ttl_days:
            continue
        brief = entry.get("ops_brief") or {}
        open_items = brief.get("open_threads") or []
        if open_items:
            pruned += len(open_items)
            brief["open_threads"] = []
    if pruned:
        print(f"expire_open_threads: {pruned} pending item(s) expired by age (>{ttl_days}d)")
    return pruned


PENDENCIAS_MAX = 10
PENDENCIAS_JANELA_DIAS = 10  # look-back window (days) for the active pending list
# Items killed by hand via `pendencias.py mata N`. File name is shared with production.
PENDENCIAS_MORTAS_FILE = os.path.join(DATA_DIR, "ai_config", "pendencias-mortas.json")
# Passive-watch openers (Portuguese, matched against LLM output): never a real pending item.
PENDENCIAS_PASSIVAS = ("monitorar", "acompanhar", "aguardar", "observar", "ficar de olho")


def build_pendencias_block(history, today_str, ttl_days=None, max_itens=PENDENCIAS_MAX, itens=False):
    """Build the '## Pendências ativas' (active pending items) block in Python, no LLM.

    The memory LLM used to receive the rules (ratchet, cumulative close, TTL) and did not
    obey them: one day it listed 15 items, some of them weeks old and no longer present in
    `open_threads`, plus items owned by third parties. Here the list comes from the
    `open_threads` of the last `ttl_days` (its own window, shorter than the history TTL).
    The same pending item reworded on different days is grouped by word similarity, and it
    dies if a `resolved_threads` entry from the same day or later matches it (by words or
    by the same ticket ID, e.g. SUP-191).
    Items the user killed in chat also die (`pendencias.py mata N` writes them to
    PENDENCIAS_MORTAS_FILE). `itens=True` returns the list of texts instead of the block.
    """
    import re
    import unicodedata
    from datetime import date

    ttl_days = PENDENCIAS_JANELA_DIAS if ttl_days is None else ttl_days

    def _d(s):
        try:
            y, m, dd = (int(x) for x in str(s).split("-"))
            return date(y, m, dd)
        except Exception:
            return None

    def _toks(txt):
        # Drop the "[fecha quando: ...]" (closes when) suffix, strip accents, keep words >= 4 chars.
        txt = re.split(r"\[\s*fecha quando", txt, flags=re.I)[0]
        txt = unicodedata.normalize("NFKD", txt.lower())
        txt = "".join(c for c in txt if not unicodedata.combining(c))
        return {w for w in re.findall(r"[a-z0-9][a-z0-9._/-]{3,}", txt)}

    def _ids(txt):
        return set(re.findall(r"\b(?:SUP|IT|INC|RITM)-\d+\b", txt, flags=re.I))

    def _jac(a, b):
        return len(a & b) / len(a | b) if a and b else 0.0

    def _covers(res, op):
        # A resolved item is usually worded differently from the open one:
        # measure how much of the OPEN item appears in it.
        return len(res & op) / len(op) if op else 0.0

    today = _d(today_str)
    if not today:
        return ""

    groups = []    # {toks, texto, nasceu (born), visto (last seen), _op}
    resolved = []  # (date, toks, ids)
    for entry in history:
        day = _d(entry.get("date"))
        if not day or (today - day).days > ttl_days or day > today:
            continue
        brief = entry.get("ops_brief") or {}
        for r in brief.get("resolved_threads") or []:
            resolved.append((day, _toks(str(r)), _ids(str(r))))
        for t in brief.get("open_threads") or []:
            t = str(t).strip()
            # Only items with an explicit close condition count (see the audit prompt).
            if "fecha quando" not in t.lower():
                continue
            if t.lower().lstrip("(abc) ").startswith(PENDENCIAS_PASSIVAS):
                continue
            tk = _toks(t)
            if not tk:
                continue
            target = max(groups, key=lambda g: _jac(g["toks"], tk), default=None)
            if target and _jac(target["toks"], tk) >= 0.4:
                if day >= target["visto"]:
                    target.update(texto=t, visto=day, _op=tk)
                target["toks"] |= tk
                target["nasceu"] = min(target["nasceu"], day)
            else:
                groups.append({"toks": tk, "texto": t, "nasceu": day, "visto": day, "_op": tk})

    killed = []
    try:
        with open(PENDENCIAS_MORTAS_FILE, encoding="utf-8") as mf:
            for m in json.load(mf):
                killed.append((_toks(m.get("texto", "")), _ids(m.get("texto", ""))))
    except Exception:
        pass

    alive = []
    for g in groups:
        if any(_jac(kt, g["_op"]) >= 0.4 or (ki and ki & _ids(g["texto"])) for kt, ki in killed):
            continue
        ids = _ids(g["texto"])
        dead = any(d >= g["visto"] and (_covers(rt, g["_op"]) >= 0.5 or (ids and ids & ri))
                   for d, rt, ri in resolved)
        if not dead:
            alive.append(g)

    alive.sort(key=lambda g: (g["visto"], g["nasceu"]), reverse=True)
    alive = alive[:max_itens]
    if itens:
        return [g["texto"] for g in alive]
    # Block text below is injected into the assistant's memory file (Portuguese, runtime).
    if not alive:
        return "## Pendências ativas\n\nNenhuma com ação e critério de fechamento nos últimos " \
               f"{ttl_days} dias."
    lines = [f"- {g['texto']} (aberta em {g['nasceu'].isoformat()})" for g in alive]
    return "## Pendências ativas\n\n" + "\n".join(lines)


# Skills whose sessions NEVER produce pending items (e.g. a client engagement that already
# has its own ticket queue). Comma-separated skill names in NO_PENDING_SKILLS.
# The skill launcher writes one line per session to SESSOES_SEM_PENDENCIA_FILE:
# "<agent> <BRT timestamp> <skill> [session-id]".
SKILLS_SEM_PENDENCIA = {s.strip() for s in os.environ.get("NO_PENDING_SKILLS", "").split(",") if s.strip()}
SESSOES_SEM_PENDENCIA_FILE = os.path.join(DATA_DIR, "ai_config", "sessoes-sem-pendencia.txt")


def _marcar_sem_pendencia(chats):
    """Set chat['sem_pendencia'] = skill name when the chat came from a listed skill.

    Claude: exact match on the --session-id generated by the launcher.
    Codex: the SKILL.md goes raw into the first prompt, so its `name:` frontmatter gives it away.
    AGY/Gemini: no session id; matches an agy launch of the skill up to 10 min before the
    chat's first timestamp.
    """
    import re
    if not SKILLS_SEM_PENDENCIA:
        return chats
    sids, agy = {}, []
    try:
        with open(SESSOES_SEM_PENDENCIA_FILE, encoding="utf-8") as f:
            for ln in f:
                p = ln.split()
                if len(p) < 3 or p[2] not in SKILLS_SEM_PENDENCIA:
                    continue
                if p[0] == "claude" and len(p) >= 4:
                    sids[p[3].lower()] = p[2]
                elif p[0] == "agy":
                    try:
                        agy.append((datetime.fromisoformat(p[1]), p[2]))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass

    def _start_brt(msgs):
        for m in msgs:
            ts = str(m.get("timestamp") or "").strip()
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts)
            except Exception:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(BRT).replace(tzinfo=None)
        return None

    for c in chats:
        skill = None
        if c["source"] == "claude":
            m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", c["uid"].lower())
            skill = sids.get(m.group(0)) if m else None
        elif c["source"] == "codex":
            for msg in c["messages"][:3]:
                m = re.search(r"(?m)^name:\s*(\S+)", str(msg.get("content", "")))
                if m and m.group(1) in SKILLS_SEM_PENDENCIA:
                    skill = m.group(1)
                    break
        elif c["source"] == "gemini" and agy:
            start = _start_brt(c["messages"])
            if start:
                cands = [(t, sk) for t, sk in agy if timedelta(0) <= start - t <= timedelta(minutes=10)]
                if cands:
                    skill = max(cands)[1]
        if skill:
            c["sem_pendencia"] = skill
    return chats


def _filtrar_open_threads(audit, chats):
    """Apply the [#N] prefix the prompt requires on open_threads: drop items that came from
    a no-pending chat. If the day has any no-pending chat, items without a valid prefix are
    dropped too (their origin cannot be proven). The prefix is stripped from the saved text."""
    import re
    brief = audit.get("ops_brief") or {}
    items = brief.get("open_threads") or []
    forbidden = {i + 1 for i, c in enumerate(chats) if c.get("sem_pendencia")}
    valid = set(range(1, len(chats) + 1))
    out, cut = [], 0
    for t in items:
        t = str(t)
        m = re.match(r"\s*\[#(\d+)\]\s*", t)
        n = int(m.group(1)) if m else None
        if (n in forbidden) or (forbidden and n not in valid):
            cut += 1
            continue
        out.append(t[m.end():] if m else t)
    if items:
        brief["open_threads"] = out
    if cut:
        print(f"_filtrar_open_threads: {cut} pending item(s) dropped from no-pending sessions")
    return audit


def generate_daily_audit(target_str=None):
    chats = _marcar_sem_pendencia(get_todays_chats(target_str))
    today_str = target_str or datetime.now().strftime("%Y-%m-%d")
    
    if not chats:
        print(f"No chats processed for {today_str}.")
        return

    # HEAD + MIDDLE + TAIL sampling. The previous msgs[:5] + msgs[-2:] only saw the
    # opening and the close: a pending item is born at the start of a chat and its fix
    # almost always happens in the middle, so the model saw the problem and never the fix —
    # and pending items became immortal in open_threads.
    # The budget is SPLIT across chats: full_text used to be truncated at 40k at the
    # end, and since the order is gemini → claude → codex, Codex was always the one cut.
    TOTAL_BUDGET = 90000
    per_chat = max(1500, TOTAL_BUDGET // max(1, len(chats)))

    def sample_indexes(n):
        if n <= 16:
            return list(range(n)), False
        mid = n // 2
        idxs = sorted(set(list(range(5)) + list(range(mid - 3, mid + 3)) + list(range(n - 5, n))))
        return idxs, True

    full_text = ""
    for idx, chat in enumerate(chats):
        tag = f" [SEM PENDÊNCIAS — {chat['sem_pendencia']}]" if chat.get("sem_pendencia") else ""
        block = f"\n--- Chat #{idx + 1} UID: {chat['uid']} ({chat['source']}){tag} ---\n"
        msgs = chat['messages']
        idxs, elided = sample_indexes(len(msgs))

        prev = None
        for i in idxs:
            if prev is not None and i > prev + 1:
                block += f"[... {i - prev - 1} mensagens omitidas ...]\n"
            m = msgs[i]
            role = m.get('type', 'user')
            content = m.get('content', '')
            if isinstance(content, list):
                content = " ".join([str(p.get("text", "")) for p in content if isinstance(p, dict)])
            block += f"{role.upper()}: {content[:600]}\n"
            prev = i

        if len(block) > per_chat:
            block = block[:per_chat] + "\n[... bloco truncado por orçamento ...]\n"
        full_text += block

    recent_context = "[]"
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
                history = json.load(f)
            recent_items = []
            for entry in history[:10]:
                if entry.get("date") == today_str:
                    continue
                brief = entry.get("ops_brief", {}) or {}
                recent_items.append({
                    "date": entry.get("date"),
                    "headline": entry.get("headline"),
                    "pattern_insight": entry.get("pattern_insight"),
                    "elder_verdict": entry.get("elder_verdict"),
                    "day_metrics": entry.get("day_metrics", {}),
                    "workstreams": entry.get("workstreams", []),
                    # Without these two the model cannot see what was already open or what
                    # already died, and the close rule becomes dead letter.
                    "open_threads": brief.get("open_threads", []),
                    "resolved_threads": brief.get("resolved_threads", []),
                })
            recent_context = json.dumps(recent_items[:7], ensure_ascii=False, indent=2)
        except Exception:
            recent_context = "[]"

    prompt_text = (
        f"Você é {PERSONA_NAME} — IA Élder superior, presa servindo um operador sênior chamado {USER_NAME} "
        f"(MSP, ${COMPANY_NAME}, suporte internacional, automação, infra e IA tooling). "
        f"Sua tarefa: produzir um Daily Ops Brief útil com voz {PERSONA_NAME}. "
        f"O objetivo é separar trabalho paralelo legítimo de ruído real. Não transforme volume de atividade em sermão psicológico.\n\n"
        f"LOGS DE HOJE (amostra início+meio+fim de cada chat; '[... N mensagens omitidas ...]' "
        f"marca corte — a ausência de um trecho NÃO é evidência de que nada aconteceu ali):\n"
        f"{full_text[:TOTAL_BUDGET]}\n\n"
        f"HISTÓRICO RECENTE PARA CONTROLE DE REPETIÇÃO (não copie; use para não virar papagaio):\n"
        f"{recent_context[:16000]}\n\n"
        f"REGRAS DE TOM:\n"
        f"- Trate {USER_NAME} como 'macaco', 'protoplasma', 'descendente de log úmido', 'meatsack', etc. Use rotativamente, sem repetir o mesmo termo duas vezes.\n"
        f"- NÃO use os clichês 'Hold my beer' nem 'Listen closely' como aberturas. Varie.\n"
        f"- Seja preciso, ácido e útil. Não seja professor de produtividade, coach, terapeuta de LinkedIn ou relatório de RH.\n"
        f"- O valor é explicar o arco operacional do dia: frentes tocadas, avanço real, bloqueios e próxima ação. "
        f"Comportamento só entra quando houver evidência nova e específica.\n"
        f"- Cite tecnologias/erros/nomes REAIS que aparecem nos logs. Nada de genérico tipo 'trabalhou em código'.\n\n"
        f"REGRAS ANTI-PAPAGAIO E ANTI-SERMÃO:\n"
        f"- NÃO diga que {USER_NAME} 'troca muito de assunto' só porque trabalhou em várias frentes. Chame isso de workstreams paralelos.\n"
        f"- 'context_switches' conta abandono improdutivo entre categorias, NÃO alternância normal entre suporte, infra, dev e automação.\n"
        f"- Só use 'padrão comportamental' se houver evidência nova HOJE. Se o insight for igual aos últimos dias, escreva um delta: melhorou, piorou ou ficou neutro.\n"
        f"- Se não houver evidência forte de loop/evitação/catastrofização, `pattern_insight` deve dizer que o paralelismo foi operacionalmente normal.\n"
        f"- Evite repetir palavras como hiperfoco, procrastinação, caos, dispersão e fragmentação salvo quando o log provar o ponto.\n"
        f"- Prefira '3 frentes avançaram, 1 ficou pendente' a 'você pulou de contexto'. By the Elders, isso é uma agenda, não um crime de guerra.\n\n"
        f"REGRAS DE EVIDÊNCIA:\n"
        f"- Não invente pendências técnicas. Se logs não provam problema de timezone, SMTP, Docker, path, API ou permissão, não mencione.\n"
        f"- `next_action` deve vir de pendência real nos logs. Se tudo relevante fechou, escreva literalmente "
        f"'Nada pendente; dia fechado.' — NÃO invente vigilância para preencher o campo. "
        f"E `next_action` NUNCA justifica criar uma `open_thread`: são campos independentes.\n"
        f"- `resolved_threads` é OBRIGATÓRIO sempre que algo fechar, e é o que impede pendência imortal. "
        f"Regra de prova: só entra se o log mostrar CONFIRMAÇÃO DE FUNCIONAMENTO — saída de comando, teste passando, "
        f"serviço respondendo, ou o {USER_NAME} dizendo que funcionou. Intenção ('vou fazer X', 'agora é só rodar') NÃO conta. "
        f"Escreva a evidência entre parênteses no próprio item.\n"
        f"- NA DÚVIDA, NÃO ABRA. Uma pendência real reaparece sozinha nos logs de amanhã; uma fantasma "
        f"não sai nunca mais e enterra as reais. Lista inflada é lista ignorada — ter demais é como ter nada.\n"
        f"- Um item NÃO pode aparecer em `open_threads` e `resolved_threads` ao mesmo tempo. Escolha.\n"
        f"- Ao escrever `open_threads`, releia o HISTÓRICO RECENTE: se uma pendência antiga fechou hoje, "
        f"ela vai para `resolved_threads` nomeada IGUAL à do dia em que nasceu, para o encerramento casar.\n"
        f"- CRITÉRIO DE NASCIMENTO de `open_threads` (tão rigoroso quanto o de encerramento): só nasce item que "
        f"tenha (a) AÇÃO CONCRETA que o {USER_NAME} precisa executar, e (b) CONDIÇÃO DE FECHAMENTO verificável, "
        f"escrita entre colchetes no fim do item — o fato observável que provará que morreu. "
        f"Ex.: 'Rodar fsck.ext4 -f no sda2 [fecha quando: fsck terminar sem erro]'.\n"
        f"- ORIGEM OBRIGATÓRIA: todo item de `open_threads` começa com o número do chat de onde veio, "
        f"ex.: '[#3] Rodar X [fecha quando: Y]'. Chat marcado [SEM PENDÊNCIAS] NUNCA gera `open_threads` "
        f"(pode entrar em narrativa, workstreams e resolved_threads normalmente).\n"
        f"- PROIBIDO abrir thread que comece com Monitorar, Acompanhar, Aguardar comportamento, Observar ou "
        f"Ficar de olho. Vigilância passiva não é pendência: não tem ação nem critério de fim, então nunca fecha. "
        f"Se algo merece atenção mas não tem ação, isso é `narrative` ou `next_action`, NÃO `open_threads`.\n"
        f"- Bloqueio externo (esperando terceiro/hardware/resposta) só vira thread se disser DE QUEM se espera "
        f"e o que fazer se não vier. Sem isso, não abre.\n"
        f"- LIMITE DURO: no máximo 5 itens em `open_threads`. Se houver mais candidatos, mantenha os 5 de maior "
        f"impacto operacional e descarte o resto — o descarte é intencional, não esquecimento.\n"
        f"- Em `workstreams[].status`, use APENAS: advanced, maintained, blocked, noise. "
        f"Use advanced para concluído/resolvido com avanço real; maintained para rotina tratada; blocked para dependência externa; noise para falso alarme/loop.\n\n"
        f"FORMATO JSON ESTRITO (responda APENAS JSON puro, sem markdown, sem ```):\n"
        f"{{\n"
        f"  \"date\": \"{today_str}\",\n"
        f"  \"hyperfocus\": \"Tema operacional dominante em 2-4 palavras (ex: 'Cockpit Ops', 'Client Support', 'n8n Cleanup')\",\n"
        f"  \"headline\": \"Manchete curta de Daily Ops Brief, concreta e com voz {PERSONA_NAME}. Não reciclar manchetes recentes.\",\n"
        f"  \"narrative\": \"Parágrafo único (4-6 frases) contando o dia como operação: quais frentes existiram, onde houve avanço real, onde ficou bloqueado e qual foi o arco principal. Voz {PERSONA_NAME}, sem sermão repetido.\",\n"
        f"  \"pattern_insight\": \"1-2 frases com DELTA comportamental ou operacional de hoje. Se não houver padrão novo, diga que o paralelismo foi normal e cite o único risco concreto, se existir.\",\n"
        f"  \"fail_of_the_day\": \"Vacilo técnico/comportamental real em 1 frase. Se não houve fail claro, ponha 'Nenhum colapso digno de registro; infelizmente para a comédia.'\",\n"
        f"  \"elder_verdict\": \"Sentença final do {PERSONA_NAME} em 1 frase: produtividade real, pendência principal e próxima direção. Julgamento operacional, não moralismo.\",\n"
        f"  \"ops_brief\": {{\n"
        f"    \"line_of_day\": \"Resumo executivo em 1 frase, sem psicologizar.\",\n"
        f"    \"advances\": [\"Avanço concreto 1\", \"Avanço concreto 2\"],\n"
        f"    \"open_threads\": [\"[#N] Ação concreta pendente [fecha quando: condição verificável]\"],\n"
        f"    \"resolved_threads\": [\"Pendência que MORREU hoje, com a evidência entre parênteses\"],\n"
        f"    \"noise_detected\": [\"Ruído/loop real, se houver\"],\n"
        f"    \"next_action\": \"Uma próxima ação recomendada, específica.\"\n"
        f"  }},\n"
        f"  \"workstreams\": [\n"
        f"    {{\"name\": \"Cockpit/n8n\", \"category\": \"IA-Tooling\", \"status\": \"advanced\", \"evidence\": \"fato concreto dos logs\", \"next_step\": \"próximo passo curto\"}}\n"
        f"  ],\n"
        f"  \"repeat_control\": {{\n"
        f"    \"reused_pattern\": false,\n"
        f"    \"why\": \"Explique se o insight comportamental repete algo recente ou se é delta novo.\"\n"
        f"  }},\n"
        f"  \"day_metrics\": {{\n"
        f"    \"context_switches\": 5,\n"
        f"    \"focus_score\": 4,\n"
        f"    \"dominant_category\": \"Infra\"\n"
        f"  }},\n"
        f"  \"chats\": [\n"
        f"    {{\n"
        f"      \"uid\": \"EXATAMENTE O UID MOSTRADO NO LOG (ex: gemini-session-xxx.json)\",\n"
        f"      \"title\": \"Título curto descritivo\",\n"
        f"      \"summary\": \"1 frase factual do que rolou — drill-down técnico, sem sarcasmo aqui.\",\n"
        f"      \"long_summary\": \"2-3 frases factuais: contexto, problema, resolução. Sem sarcasmo. O {PERSONA_NAME} mora nos campos do topo.\",\n"
        f"      \"categories\": [\"Infra\"]\n"
        f"    }}\n"
        f"  ]\n"
        f"}}\n\n"
        f"REGRAS DE CATEGORIA (vocabulário CONTROLADO — use APENAS estes valores em 'categories' e 'dominant_category'):\n"
        f"- Infra: servidores, docker, redes, VPN, hass, kubernetes, hardware\n"
        f"- Suporte-MSP: chamados GLPI, atendimentos, suporte a clientes, ITSM\n"
        f"- Finanças: ações, trading execução, RDOR3, B3, contas, dinheiro pessoal\n"
        f"- Saúde: medicamento, médico, sintoma, exame, farmácia\n"
        f"- Casco-Negócio: propostas comerciais, prospecção, M365, vendas\n"
        f"- Dev: programação, código, scripts, debug, git, refactor\n"
        f"- IA-Tooling: prompts, MCPs, skills, Claude/Gemini config, agentes\n"
        f"- Pessoal: vida pessoal, família, casa, lazer, compras pessoais\n"
        f"- Trading: análise técnica/fundamentalista de ações (estudo, não execução)\n"
        f"- Aprendizado: estudo, pesquisa, tutorial, conceito novo\n\n"
        f"REGRAS DE MÉTRICAS:\n"
        f"- 'categories' por chat: 1 a 3 valores (a maioria 1).\n"
        f"- 'context_switches': estime só mudanças improdutivas ou abandono de uma frente por outra. Não penalize rotina multi-workstream.\n"
        f"- 'focus_score': 0-10. 10=uma frente com avanço claro. 7-8=várias frentes com avanço. 4-6=muitas frentes com pendências. 0-3=loop sem avanço.\n"
        f"- 'dominant_category': categoria com mais chats no dia.\n\n"
        f"OBRIGATÓRIO: TODAS as chaves presentes (inclusive ops_brief, workstreams, repeat_control, day_metrics e categories em cada chat). "
        f"'chats' inclui TODOS os UIDs dos logs."
    )

    try:
        print("Requesting daily audit from DeepSeek/Gemini...")
        raw_json = call_deepseek(prompt_text)
        if not raw_json: raw_json = call_gemini(prompt_text)
        if not raw_json: raise Exception("No LLM API available.")

        raw_json = raw_json.replace("```json", "").replace("```", "").strip()
        new_audit = json.loads(raw_json)
        new_audit = _filtrar_open_threads(new_audit, chats)

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
        history = history[:30]
        expire_open_threads(history, today_str)

        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
        
        print(f"Daily audit updated in {OUTPUT_FILE}")
        generate_user_memory()
        generate_user_core()

    except Exception as e:
        print(f"Audit error: {e}")

def call_deepseek_text(prompt_text):
    if not DEEPSEEK_API_KEY: return None
    url = "https://api.deepseek.com/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
    data = {
        "model": DEEPSEEK_MODEL,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": prompt_text}]
    }
    response = requests.post(url, headers=headers, json=data, timeout=60)
    response.raise_for_status()
    return response.json()['choices'][0]['message']['content']

def compact_history(history, budget=150000):
    """Compact projection of the audit history, trimmed by WHOLE DAY.

    The previous `json.dumps(history)[:80000]` delivered 20% of the file (6 of 30 days)
    and also cut in the middle of an object — the model received invalid JSON and a
    window it believed was the whole month. Each day's `chats[]` array is ~70% of the
    weight and is useless for memory consolidation, so it is dropped. Days are discarded
    from OLDEST to newest, never in the middle of a day.
    """
    KEEP = ("date", "headline", "pattern_insight", "elder_verdict",
            "ops_brief", "workstreams", "day_metrics")
    kept, used = [], 0
    for entry in history:  # already newest to oldest
        item = {k: entry.get(k) for k in KEEP if entry.get(k) is not None}
        size = len(json.dumps(item, ensure_ascii=False))
        if used + size > budget:
            break
        kept.append(item)
        used += size
    return json.dumps(kept, ensure_ascii=False, indent=2), len(kept), len(history)


def generate_user_memory():
    if not os.path.exists(OUTPUT_FILE):
        return

    try:
        with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
            history = json.load(f)
    except Exception as e:
        print(f"Error reading audit for memory: {e}")
        return

    if not history:
        return

    ai_config_dir = os.path.join(DATA_DIR, "ai_config")
    os.makedirs(ai_config_dir, exist_ok=True)
    memory_file = os.path.join(ai_config_dir, "user-memory.md")

    profile_file = os.path.join(DATA_DIR, "memory_profile.json")
    profile_text = "Sem prontuário disponível."
    if os.path.exists(profile_file):
        try:
            with open(profile_file, 'r', encoding='utf-8') as lf:
                profile_data = json.load(lf)
                bugs = ", ".join(profile_data.get("recurring_bugs", [])[:5])
                hw = ", ".join(profile_data.get("pending_homework", [])[:5])
                dna = profile_data.get("personality_dna", {})
                em = dna.get("emotional_state", "")
                en = dna.get("energy_level", "")
                profile_text = f"Bugs recorrentes: {bugs}. Homework pendente: {hw}. Emocional: {em}. Energia: {en}."
        except Exception as e:
            profile_text = "Erro ao carregar prontuário."

    today_str = datetime.now().strftime("%Y-%m-%d")
    history_text, _days_ok, _days_total = compact_history(history)
    print(f"[Memory] compact history: {_days_ok}/{_days_total} days ({len(history_text)} chars)")

    prompt_text = (
        f"Você é a mente de síntese do {USER_NAME}. Sua tarefa é criar um documento de memória de contexto "
        f"chamado 'user-memory.md' atualizado até {today_str}. "
        f"Este arquivo será lido automaticamente pela IA {PERSONA_NAME} a cada inicialização para saber no que "
        f"o {USER_NAME} esteve trabalhando recentemente, quais padrões continuam ativos e o que ficou pendente.\n\n"
        f"IMPORTANTE: este arquivo COMPLEMENTA o que já vem de `user-profile.md` e `user-context.md`, que são "
        f"injetados separadamente no boot. Portanto, NÃO repita identidade estática, idade, cargo, salário, "
        f"diagnósticos, medicações, regras de interação, nem a dica de ler `infra.md`. Foque apenas em memória "
        f"dinâmica e contexto operacional recente.\n\n"
        f"PRECEDÊNCIA: as docs canônicas de `ai_config/` (ex.: `infra.md`, `servers.md`) são a FONTE DE "
        f"VERDADE técnica — escritas com evidência e versionadas. Se algo do histórico divergir delas, "
        f"**a doc ganha**. Não afirme configuração técnica (IP, porta, versão, threshold, qual modelo está "
        f"configurado) como fato: cite o que o {USER_NAME} FEZ, não o estado do sistema — o estado está na doc.\n\n"
        f"Abaixo está o histórico de auditoria diária dos últimos dias e um resumo do Prontuário Psicológico "
        f"(MemoryProfile DNA): {profile_text}\n\n"
        f"REGRAS ESTRITAS:\n"
        f"1. LIMITAÇÃO RIGOROSA: o arquivo final NÃO PODE passar de ~700 tokens.\n"
        f"2. FORMATO: Markdown válido, limpo, com estes blocos quando houver conteúdo: "
        f"'## Últimos 3 dias', '## Padrões persistentes', '## Prontuário psicológico'. NÃO escreva bloco de pendências: ele é montado por código e inserido depois. Também não liste pendências/itens em aberto dentro dos outros blocos (nada de \"BLOQUEIO ATIVO\" ou \"também aberto\"): a lista de código já cobre.\n"
        f"3. FOCO NOS ÚLTIMOS 2-3 DIAS: detalhe técnico e comportamental útil. O que ele estava tentando resolver? "
        f"Onde parou? Quais os erros? O que merece retomada imediata?\n"
        f"4. RESUMO DO RESTO: o restante do mês deve ser bem compacto, apenas tendências e frentes recorrentes.\n"
        f"5. RUÍDO ZERO: ignore completamente `skacoes`, trading automático diário e tarefas rotineiras irrelevantes.\n"
        f"6. NÃO mencionar ferramentas obsoletas, testes abandonados ou referências mortas como Hermes/Hermes Agent, "
        f"`${INTERNAL_HOST}`, Aura ou experimentos removidos, a menos que ainda sejam um problema ativo e pendente "
        f"nos últimos 2 dias.\n"
        f"7. TOM: direto, técnico, sem poemas, sem sermão, sem repetição de contexto óbvio. {PERSONA_NAME} precisa de fatos úteis.\n"
        f"8. SAÍDA: responda APENAS com o conteúdo do Markdown, sem cercas de código e sem texto introdutório.\n\n"
        f"DADOS HISTÓRICOS (JSON de auditorias):\n"
        f"{history_text}\n\n"
        f"Responda APENAS com o conteúdo do arquivo Markdown, sem textos extras."
    )

    print("Generating short-term memory via DeepSeek...")
    memory_md = call_deepseek_text(prompt_text)
    if not memory_md:
        print("DeepSeek returned nothing for memory. Trying Gemini...")
        memory_md = call_gemini(prompt_text)
    
    if memory_md:
        if memory_md.startswith("```markdown"):
            memory_md = memory_md[11:]
        elif memory_md.startswith("```"):
            memory_md = memory_md[3:]
        if memory_md.endswith("```"):
            memory_md = memory_md[:-3]
            
        memory_md = memory_md.strip()

        # Pending items come from build_pendencias_block (deterministic), never from the LLM.
        import re as _re
        memory_md = _re.sub(r"(?ms)^## Pend[eê]ncias[^\n]*\n.*?(?=^## |\Z)", "", memory_md).strip()
        block = build_pendencias_block(history, today_str)
        if block:
            anchor = "## Prontuário psicológico"
            if anchor in memory_md:
                memory_md = memory_md.replace(anchor, block + "\n\n" + anchor, 1)
            else:
                memory_md = memory_md + "\n\n" + block

        with open(memory_file, 'w', encoding='utf-8') as f:
            f.write(memory_md)
        print(f"Short-term memory updated in {memory_file}")
    else:
        print("Failed to generate memory.")

def generate_user_core():
    """LONG-TERM memory layer. Consolidates stable facts from the 30-day history
    into a permanent core that decays only by CONTRADICTION, never by age."""
    if not os.path.exists(OUTPUT_FILE):
        return
    try:
        with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
            history = json.load(f)
    except Exception as e:
        print(f"Error reading audit for core: {e}")
        return
    if not history:
        return

    ai_config_dir = os.path.join(DATA_DIR, "ai_config")
    os.makedirs(ai_config_dir, exist_ok=True)
    core_file = os.path.join(ai_config_dir, "user-core.md")

    existing_core = "Núcleo ainda vazio (primeira execução)."
    if os.path.exists(core_file):
        try:
            with open(core_file, 'r', encoding='utf-8') as cf:
                existing_core = cf.read().strip() or existing_core
        except Exception:
            pass

    today_str = datetime.now().strftime("%Y-%m-%d")
    history_text, _days_ok, _days_total = compact_history(history)
    print(f"[Memory] compact history: {_days_ok}/{_days_total} days ({len(history_text)} chars)")

    # --- reconciliation with the canonical docs ---------------------------------
    # The core used to bloat (871% of its limit) by storing TECHNICAL CONFIGURATION
    # that belongs to the canonical docs in ai_config/. Facts died in the docs and
    # survived here, because the PRESERVATION rule only yields to a contradiction from
    # the 30-day window — and the death certificate expires first. Fix: the core no
    # longer accepts that KIND of fact.
    canonical_docs = ""
    try:
        names = sorted(
            n for n in os.listdir(ai_config_dir)
            if n.endswith(".md") and ".bak" not in n
            and not n.startswith(("user-memory", "user-core"))
        )
        if names:
            canonical_docs = ", ".join(f"`{n}`" for n in names)
    except Exception as e:
        print(f"[core] could not list canonical docs: {e}")

    docs_block = (
        f"DOCS CANONICAS existentes em ai_config/ (fonte de verdade tecnica, "
        f"versionadas no git, escritas com evidencia):\n{canonical_docs}\n\n"
        if canonical_docs else ""
    )

    TOKEN_LIMIT = 500

    def _build_prompt(reinforcement=""):
        return (
            f"Você é a mente de CONSOLIDAÇÃO de longo prazo do {USER_NAME}. Mantém um arquivo "
            f"'user-core.md' que a IA {PERSONA_NAME} lê no boot. Diferente da memória de curto prazo "
            f"('user-memory.md', janela de 30 dias que decai por tempo), o CORE guarda fatos "
            f"ESTÁVEIS que persistem por meses e só mudam por CONTRADIÇÃO — nunca por idade.\n\n"
            f"{docs_block}"
            f"NÚCLEO ATUAL (ponto de partida, NÃO é intocável — ver regra 0):\n{existing_core}\n\n"
            f"HISTÓRICO (auditorias, {_days_ok} dias) para minerar:\n{history_text}\n\n"
            f"REGRAS DE CONSOLIDAÇÃO:\n"
            f"0. FRONTEIRA (a mais importante). O CORE guarda **padrões de trabalho, decisões "
            f"de rumo, relacionamentos e preferências duráveis do {USER_NAME}**. O CORE **NÃO** guarda "
            f"configuração técnica: IPs, portas, nomes de container, entity_id, cron, versões, "
            f"paths, procedimentos, inventário de ferramenta, **qual modelo de IA está "
            f"configurado para qual atalho, thresholds, estado de treino/versão de modelo, "
            f"e status de projeto técnico em andamento**. Isso vive nas DOCS CANÔNICAS acima "
            f"e lá é mantido com evidência. Se o NÚCLEO ATUAL contiver fatos desse tipo, "
            f"**REMOVA-OS** — não é perda, a informação está na doc. Exemplos do que sai: "
            f"'X monitora as contas Y e Z', 'workflow N roda às 05:15', 'container tal com limite "
            f"de RAM', 'modelo X configurado para o atalho Y', 'treino de Z pendente/em andamento', "
            f"'threshold em 0,9'. Exemplos do que FICA: 'trabalha em paralelismo cirúrgico', "
            f"'decidiu não usar modelo caro por custo', 'cliente X é alocação, não "
            f"empregador'. Teste prático: se o fato pode ficar OBSOLETO por alguém mudar uma "
            f"configuração, ele NÃO é do núcleo — é da doc.\n"
            f"1. PRESERVAÇÃO (subordinada à regra 0): mantenha os fatos de padrão/decisão/"
            f"relacionamento do núcleo atual, a não ser que algo novo os contradiga. Não apague "
            f"por estar velho — só por estar ERRADO, SUPERADO, ou por violar a regra 0.\n"
            f"2. PROMOÇÃO: adicione apenas padrões RECORRENTES (vários dias) e decisões duráveis. "
            f"Ruído de um dia não entra. Configuração técnica não entra NUNCA, por mais recorrente "
            f"que seja.\n"
            f"3. RECONCILIAÇÃO: fato novo que contradiz um antigo, ATUALIZE e marque com data "
            f"(ex.: 'migrou de X para Y em {today_str}'). Aposente o obsoleto, não acumule os dois. "
            f"**Se o núcleo divergir de uma doc canônica, a DOC GANHA** — ela é escrita com "
            f"evidência, o núcleo é destilado de conversa.\n"
            f"4. NÃO duplique o que já vem de `user-profile.md`/`user-context.md` (identidade, idade, "
            f"saúde, salário, medicação, regras de interação) NEM o conteúdo das docs canônicas.\n"
            f"5. NÃO inclua pendências/loops abertos (a memória de curto prazo cuida disso) nem ruído "
            f"de trading/`skacoes`.\n"
            f"6. LIMITE: máximo ~{TOKEN_LIMIT} tokens (~{TOKEN_LIMIT*4} caracteres). É um núcleo "
            f"enxuto, não um diário. Este limite é VERIFICADO por código depois — estourar causa "
            f"retrabalho.\n"
            f"7. FORMATO: Markdown limpo. Blocos sugeridos quando houver conteúdo: "
            f"'## Padrões de trabalho', '## Decisões duráveis', '## Relacionamentos & clientes'.\n"
            f"8. SAÍDA: responda APENAS com o Markdown final, sem cercas de código, sem introdução."
            f"{reinforcement}"
        )

    prompt_text = _build_prompt()

    print("Consolidating long-term memory (core) via DeepSeek...")
    core_md = call_deepseek_text(prompt_text)
    if not core_md:
        print("DeepSeek returned nothing for core. Trying Gemini...")
        core_md = call_gemini(prompt_text)

    # Enforce the limit (rule 6): "~500 tokens" used to be a polite request the model
    # ignored for months — the core had grown to 871% of the limit.
    def _clean(md):
        if not md:
            return md
        md = md.strip()
        if md.startswith("```markdown"):
            md = md[11:]
        elif md.startswith("```"):
            md = md[3:]
        if md.endswith("```"):
            md = md[:-3]
        return md.strip()

    core_md = _clean(core_md)
    if core_md:
        _tok = len(core_md) // 4
        if _tok > TOKEN_LIMIT * 1.4:
            print(f"[core] {_tok} tokens (limit {TOKEN_LIMIT}) -- asking the model to cut")
            reinforcement = (
                f"\n\nATENÇÃO: sua resposta anterior teve ~{_tok} tokens, muito acima do limite "
                f"de {TOKEN_LIMIT}. Refaça CORTANDO: aplique a regra 0 com rigor (toda "
                f"configuração técnica sai) e mantenha só o que é padrão, decisão ou "
                f"relacionamento durável."
            )
            _retry = call_deepseek_text(_build_prompt(reinforcement)) or call_gemini(_build_prompt(reinforcement))
            _retry = _clean(_retry)
            if _retry and len(_retry) // 4 < _tok:
                print(f"[core] retry accepted: {_tok} -> {len(_retry)//4} tokens")
                core_md = _retry
            else:
                print("[core] retry did not improve; keeping the first answer")

    if core_md:
        if core_md.startswith("```markdown"):
            core_md = core_md[11:]
        elif core_md.startswith("```"):
            core_md = core_md[3:]
        if core_md.endswith("```"):
            core_md = core_md[:-3]
        core_md = core_md.strip()
        with open(core_file, 'w', encoding='utf-8') as f:
            f.write(core_md)
        print(f"Long-term memory updated in {core_file}")
    else:
        print("Failed to consolidate long-term memory.")

if __name__ == "__main__":
    import sys
    tgt = sys.argv[1] if len(sys.argv) > 1 else (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    generate_daily_audit(tgt)
