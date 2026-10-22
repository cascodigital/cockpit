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

def generate_daily_audit(target_str=None):
    chats = get_todays_chats(target_str)
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
        block = f"\n--- Chat UID: {chat['uid']} ({chat['source']}) ---\n"
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
        f"- `next_action` deve vir de pendência real nos logs. Se tudo relevante fechou, use monitorar/validar a próxima execução concreta.\n"
        f"- `resolved_threads` é OBRIGATÓRIO sempre que algo fechar, e é o que impede pendência imortal. "
        f"Regra de prova: só entra se o log mostrar CONFIRMAÇÃO DE FUNCIONAMENTO — saída de comando, teste passando, "
        f"serviço respondendo, ou o {USER_NAME} dizendo que funcionou. Intenção ('vou fazer X', 'agora é só rodar') NÃO conta. "
        f"Escreva a evidência entre parênteses no próprio item. Na dúvida, deixe em `open_threads`: "
        f"fechar cedo demais some com o problema do radar, que é pior que repetir.\n"
        f"- Um item NÃO pode aparecer em `open_threads` e `resolved_threads` ao mesmo tempo. Escolha.\n"
        f"- Ao escrever `open_threads`, releia o HISTÓRICO RECENTE: se uma pendência antiga fechou hoje, "
        f"ela vai para `resolved_threads` nomeada IGUAL à do dia em que nasceu, para o encerramento casar.\n"
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
        f"    \"open_threads\": [\"Pendência concreta 1\"],\n"
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
        "model": "deepseek-chat",
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
        f"Abaixo está o histórico de auditoria diária dos últimos dias e um resumo do Prontuário Psicológico "
        f"(MemoryProfile DNA): {profile_text}\n\n"
        f"REGRAS ESTRITAS:\n"
        f"1. LIMITAÇÃO RIGOROSA: o arquivo final NÃO PODE passar de ~700 tokens.\n"
        f"2. FORMATO: Markdown válido, limpo, com estes blocos quando houver conteúdo: "
        f"'## Últimos 3 dias', '## Padrões persistentes', '## Pendências ativas', '## Prontuário psicológico'.\n"
        f"3. FOCO NOS ÚLTIMOS 2-3 DIAS: detalhe técnico e comportamental útil. O que ele estava tentando resolver? "
        f"Onde parou? Quais os erros? O que merece retomada imediata?\n"
        f"4. RESUMO DO RESTO: o restante do mês deve ser bem compacto, apenas tendências e frentes recorrentes.\n"
        f"5. RUÍDO ZERO: ignore completamente `skacoes`, trading automático diário e tarefas rotineiras irrelevantes.\n"
        f"5b. ENCERRAMENTO CUMULATIVO (regra dura — o bloco '## Pendências ativas' vive ou morre aqui): "
        f"antes de listar qualquer pendência, varra TODO o histórico e monte o conjunto de tudo que aparece em "
        f"`ops_brief.resolved_threads` de QUALQUER dia. Uma pendência que apareceu em `open_threads` no dia 3 e em "
        f"`resolved_threads` no dia 7 está MORTA e não pode ser listada — mesmo que reapareça em `open_threads` de "
        f"dias entre 3 e 7, porque o encerramento é posterior. Só sobrevive o que nunca foi resolvido, ou o que foi "
        f"resolvido e REABRIU depois (aparece em `open_threads` com data maior que a do encerramento).\n"
        f"5c. Se uma pendência sobreviver, escreva-a com a data em que nasceu (ex.: 'aberta em 2026-08-04'). "
        f"Pendência sem idade é pendência que ninguém audita.\n"
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

    prompt_text = (
        f"Você é a mente de CONSOLIDAÇÃO de longo prazo do {USER_NAME}. Mantém um arquivo "
        f"'user-core.md' que a IA {PERSONA_NAME} lê no boot. Diferente da memória de curto prazo "
        f"('user-memory.md', janela de 30 dias que decai por tempo), o CORE guarda fatos "
        f"ESTÁVEIS que persistem por meses e só mudam por CONTRADIÇÃO — nunca por idade.\n\n"
        f"NÚCLEO ATUAL (preserve; é a memória já consolidada):\n{existing_core}\n\n"
        f"HISTÓRICO (auditorias, {_days_ok} dias) para minerar:\n{history_text}\n\n"
        f"REGRAS DE CONSOLIDAÇÃO:\n"
        f"1. PRESERVAÇÃO: mantenha TODOS os fatos do núcleo atual, A NÃO SER que algo novo os "
        f"contradiga. NÃO apague por estar velho — só por estar ERRADO ou SUPERADO.\n"
        f"2. PROMOÇÃO: adicione ao core apenas fatos ESTÁVEIS e RECORRENTES (apareceram em vários "
        f"dias, ou são decisões/configs/relacionamentos duráveis). Ruído de um dia só NÃO entra.\n"
        f"3. RECONCILIAÇÃO: se um fato novo contradiz um antigo, ATUALIZE e marque a mudança com "
        f"data (ex.: 'migrou de X para Y em {today_str}'). Aposente o obsoleto, não acumule os dois.\n"
        f"4. NÃO duplique o que já vem de `user-profile.md`/`user-context.md` (identidade, idade, "
        f"saúde, salário, medicação, regras de interação).\n"
        f"5. NÃO inclua pendências/loops abertos (a memória de curto prazo cuida disso) nem ruído "
        f"de trading/`skacoes`.\n"
        f"6. LIMITE: máximo ~500 tokens. É um núcleo enxuto, não um diário.\n"
        f"7. FORMATO: Markdown limpo. Blocos sugeridos quando houver conteúdo: "
        f"'## Stack & ferramentas estáveis', '## Decisões duráveis', '## Relacionamentos & clientes', "
        f"'## Configurações canônicas'.\n"
        f"8. SAÍDA: responda APENAS com o Markdown final, sem cercas de código, sem introdução."
    )

    print("Consolidating long-term memory (core) via DeepSeek...")
    core_md = call_deepseek_text(prompt_text)
    if not core_md:
        print("DeepSeek returned nothing for core. Trying Gemini...")
        core_md = call_gemini(prompt_text)

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
