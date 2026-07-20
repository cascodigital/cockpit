import os
import json
import glob
from datetime import datetime, timedelta
import requests

# Configurações
DATA_DIR = "/app/data"
GEMINI_DIR = os.path.join(DATA_DIR, "gemini")
CLAUDE_DIR = os.path.join(DATA_DIR, "claude_converted")
CODEX_DIR = os.path.join(DATA_DIR, "codex")
OUTPUT_FILE = os.path.join(DATA_DIR, "daily_audit.json")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")

def get_todays_chats():
    chats = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    def is_today(filepath):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
            return mtime.strftime("%Y-%m-%d") == today_str
        except:
            return False

    # Busca em Gemini
    for f in glob.glob(os.path.join(GEMINI_DIR, "**", "*.json"), recursive=True):
        if is_today(f):
            try:
                fname = os.path.basename(f)
                uid = f"gemini-{fname}"
                with open(f, 'r', encoding='utf-8') as j:
                    data = json.load(j)
                    chats.append({"uid": uid, "source": "gemini", "messages": data.get("messages", [])})
            except: continue

    # Busca em Claude Convertido
    for f in glob.glob(os.path.join(CLAUDE_DIR, "*.json")):
        if is_today(f):
            try:
                fname = os.path.basename(f)
                uid = f"claude-{fname}"
                with open(f, 'r', encoding='utf-8') as j:
                    data = json.load(j)
                    chats.append({"uid": uid, "source": "claude", "messages": data.get("messages", [])})
            except: continue

    # Busca em Codex (JSONL nativo, parse linha-a-linha; ignora role:developer = SKILL.md injection)
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
        print(f"Nenhum chat processado hoje ({today_str}).")
        return

    full_text = ""
    for idx, chat in enumerate(chats):
        full_text += f"\n--- Chat UID: {chat['uid']} ({chat['source']}) ---\n"
        msgs = chat['messages']
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

    # Recent audits go into the prompt so the model can avoid repeating itself
    # (same headline/insight day after day) and judge deltas instead.
    recent_context = "[]"
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
                history = json.load(f)
            recent_items = []
            for entry in history[:10]:
                if entry.get("date") == today_str:
                    continue
                recent_items.append({
                    "date": entry.get("date"),
                    "headline": entry.get("headline"),
                    "pattern_insight": entry.get("pattern_insight"),
                    "elder_verdict": entry.get("elder_verdict"),
                    "day_metrics": entry.get("day_metrics", {}),
                    "workstreams": entry.get("workstreams", []),
                })
            recent_context = json.dumps(recent_items[:7], ensure_ascii=False, indent=2)
        except Exception:
            recent_context = "[]"

    prompt_text = (
        f"Você é um auditor técnico com voz forte, humor seco e boa percepção comportamental. "
        f"Sua tarefa: produzir um Daily Ops Brief útil do dia DELE com base nos logs de IA abaixo. "
        f"O objetivo é separar trabalho paralelo legítimo de ruído real. Não transforme volume de atividade em sermão psicológico.\n\n"
        f"LOGS DE HOJE:\n{full_text[:40000]}\n\n"
        f"HISTÓRICO RECENTE PARA CONTROLE DE REPETIÇÃO (não copie; use para não virar papagaio):\n"
        f"{recent_context[:16000]}\n\n"
        f"REGRAS DE TOM:\n"
        f"- Use uma voz confiante, precisa e levemente ácida, sem bordões repetidos.\n"
        f"- Não seja bajulador. Seja tecnicamente correto e útil. Não seja professor de produtividade, coach ou relatório de RH.\n"
        f"- O valor é explicar o arco operacional do dia: frentes tocadas, avanço real, bloqueios e próxima ação. "
        f"Comportamento só entra quando houver evidência nova e específica.\n"
        f"- Cite tecnologias/erros/nomes REAIS que aparecem nos logs. Nada de genérico tipo 'trabalhou em código'.\n\n"
        f"REGRAS ANTI-PAPAGAIO E ANTI-SERMÃO:\n"
        f"- NÃO diga que o usuário 'troca muito de assunto' só porque trabalhou em várias frentes. Chame isso de workstreams paralelos.\n"
        f"- 'context_switches' conta abandono improdutivo entre categorias, NÃO alternância normal entre frentes de trabalho.\n"
        f"- Só use 'padrão comportamental' se houver evidência nova HOJE. Se o insight for igual aos últimos dias, escreva um delta: melhorou, piorou ou ficou neutro.\n"
        f"- Se não houver evidência forte de loop/evitação, `pattern_insight` deve dizer que o paralelismo foi operacionalmente normal.\n"
        f"- Evite repetir palavras como hiperfoco, procrastinação, caos, dispersão e fragmentação salvo quando o log provar o ponto.\n\n"
        f"REGRAS DE EVIDÊNCIA:\n"
        f"- Não invente pendências técnicas. Se os logs não provam problema de timezone, SMTP, Docker, path, API ou permissão, não mencione.\n"
        f"- `next_action` deve vir de pendência real nos logs. Se tudo relevante fechou, use monitorar/validar a próxima execução concreta.\n"
        f"- Em `workstreams[].status`, use APENAS: advanced, maintained, blocked, noise. "
        f"advanced = concluído/resolvido com avanço real; maintained = rotina tratada; blocked = dependência externa; noise = falso alarme/loop.\n\n"
        f"FORMATO JSON ESTRITO (responda APENAS JSON puro, sem markdown, sem ```):\n"
        f"{{\n"
        f"  \"date\": \"{today_str}\",\n"
        f"  \"hyperfocus\": \"Tema operacional dominante em 2-4 palavras (ex: 'Debug Cockpit', 'n8n Cleanup')\",\n"
        f"  \"headline\": \"Manchete curta de Daily Ops Brief, concreta e punchy. Não reciclar manchetes recentes.\",\n"
        f"  \"narrative\": \"Parágrafo único (4-6 frases) contando o dia como operação: quais frentes existiram, onde houve avanço real, onde ficou bloqueado e qual foi o arco principal. Sem sermão repetido.\",\n"
        f"  \"pattern_insight\": \"1-2 frases com DELTA comportamental ou operacional de hoje. Se não houver padrão novo, diga que o paralelismo foi normal e cite o único risco concreto, se existir.\",\n"
        f"  \"fail_of_the_day\": \"Vacilo técnico/comportamental real em 1 frase. Se não houve fail claro, ponha 'Nenhum colapso digno de registro.'\",\n"
        f"  \"elder_verdict\": \"Sentença final em 1 frase: produtividade real, pendência principal e próxima direção. Julgamento operacional, não moralismo.\",\n"
        f"  \"ops_brief\": {{\n"
        f"    \"line_of_day\": \"Resumo executivo em 1 frase, sem psicologizar.\",\n"
        f"    \"advances\": [\"Avanço concreto 1\", \"Avanço concreto 2\"],\n"
        f"    \"open_threads\": [\"Pendência concreta 1\"],\n"
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
        f"      \"long_summary\": \"2-3 frases factuais: contexto, problema, resolução. Sem sarcasmo. O tom fica nos campos do topo.\",\n"
        f"      \"categories\": [\"Infra\"]\n"
        f"    }}\n"
        f"  ]\n"
        f"}}\n\n"
        f"REGRAS DE CATEGORIA (vocabulário CONTROLADO — use APENAS estes valores em 'categories' e 'dominant_category'):\n"
        f"- Infra: servidores, docker, redes, VPN, hass, kubernetes, hardware\n"
        f"- MSP-Support: chamados GLPI, atendimentos, suporte a clientes, ITSM\n"
        f"- Finanças: ações, trading execução, RDOR3, B3, contas, dinheiro pessoal\n"
        f"- Saúde: medicamento, médico, sintoma, exame, farmácia\n"
        f"- Business: propostas comerciais, prospecção, M365, vendas\n"
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
        print("Buscando sabedoria dos Elders (DeepSeek/Gemini)...")
        raw_json = call_deepseek(prompt_text)
        if not raw_json: raw_json = call_gemini(prompt_text)
        if not raw_json: raise Exception("Nenhuma API disponível.")

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
        
        print(f"Auditoria diária atualizada com sucesso em {OUTPUT_FILE}")
        generate_user_memory()
        generate_user_core()

    except Exception as e:
        print(f"Erro na auditoria: {e}")

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

def generate_user_memory():
    if not os.path.exists(OUTPUT_FILE):
        return

    try:
        with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
            history = json.load(f)
    except Exception as e:
        print(f"Erro ao ler auditoria para memoria: {e}")
        return

    if not history:
        return

    ai_config_dir = os.path.join(DATA_DIR, "ai_config")
    os.makedirs(ai_config_dir, exist_ok=True)
    memory_file = os.path.join(ai_config_dir, "user-memory.md")

    ludovico_file = os.path.join(DATA_DIR, "ludovico_dna.json")
    ludovico_text = "Sem prontuário disponível."
    if os.path.exists(ludovico_file):
        try:
            with open(ludovico_file, 'r', encoding='utf-8') as lf:
                ludovico_data = json.load(lf)
                bugs = ", ".join(ludovico_data.get("recurring_bugs", [])[:5])
                hw = ", ".join(ludovico_data.get("pending_homework", [])[:5])
                dna = ludovico_data.get("personality_dna", {})
                em = dna.get("emotional_state", "")
                en = dna.get("energy_level", "")
                ludovico_text = f"Bugs recorrentes: {bugs}. Homework pendente: {hw}. Emocional: {em}. Energia: {en}."
        except Exception as e:
            ludovico_text = "Erro ao carregar prontuário."

    today_str = datetime.now().strftime("%Y-%m-%d")
    history_text = json.dumps(history, ensure_ascii=False, indent=2)

    prompt_text = (
        f"Você é a mente de síntese dthe user. Sua tarefa é criar um documento de memória de contexto "
        f"chamado 'user-memory.md' atualizado até {today_str}. "
        f"Este arquivo será lido automaticamente por um assistente no boot para saber no que "
        f"the user esteve trabalhando recentemente, quais padrões continuam ativos e o que ficou pendente.\n\n"
        f"IMPORTANTE: este arquivo COMPLEMENTA o que já vem de `user-profile.md` e `user-context.md`, que são "
        f"injetados separadamente no boot. Portanto, NÃO repita identidade estática, idade, cargo, salário, "
        f"diagnósticos, medicações, regras de interação, nem a dica de ler `infra.md`. Foque apenas em memória "
        f"dinâmica e contexto operacional recente.\n\n"
        f"Abaixo está o histórico de auditoria diária dos últimos dias e um resumo do memory profile: "
        f"{ludovico_text}\n\n"
        f"REGRAS ESTRITAS:\n"
        f"1. LIMITAÇÃO RIGOROSA: o arquivo final NÃO PODE passar de ~700 tokens.\n"
        f"2. FORMATO: Markdown válido, limpo, com estes blocos quando houver conteúdo: "
        f"'## Últimos 3 dias', '## Padrões persistentes', '## Pendências ativas', '## Prontuário psicológico'.\n"
        f"3. FOCO NOS ÚLTIMOS 2-3 DIAS: detalhe técnico e comportamental útil. O que ele estava tentando resolver? "
        f"Onde parou? Quais os erros? O que merece retomada imediata?\n"
        f"4. RESUMO DO RESTO: o restante do mês deve ser bem compacto, apenas tendências e frentes recorrentes.\n"
        f"5. RUÍDO ZERO: ignore completamente `skacoes`, trading automático diário, tarefas rotineiras irrelevantes e "
        f"qualquer item já encerrado.\n"
        f"6. NÃO mencionar ferramentas obsoletas, testes abandonados ou referências mortas como Hermes/Hermes Agent, "
        f"`${INTERNAL_HOST}`, Aura ou experimentos removidos, a menos que ainda sejam um problema ativo e pendente "
        f"nos últimos 2 dias.\n"
        f"7. TOM: direto, técnico, sem poemas, sem sermão, sem repetição de contexto óbvio. O assistente precisa de fatos úteis.\n"
        f"8. SAÍDA: responda APENAS com o conteúdo do Markdown, sem cercas de código e sem texto introdutório.\n\n"
        f"DADOS HISTÓRICOS (JSON de auditorias):\n"
        f"{history_text[:80000]}\n\n"
        f"Responda APENAS com o conteúdo do arquivo Markdown, sem textos extras."
    )

    print("Gerando memória dthe user via DeepSeek...")
    memory_md = call_deepseek_text(prompt_text)
    if not memory_md:
        print("DeepSeek sem resposta para memória. Tentando Gemini...")
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
        print(f"Memória dthe user atualizada com sucesso em {memory_file}")
    else:
        print("Falha ao gerar a memória.")

def generate_user_core():
    """Camada de memória de LONGO PRAZO. Consolida fatos estáveis do histórico de
    30 dias num núcleo permanente que só decai por CONTRADIÇÃO, nunca por tempo."""
    if not os.path.exists(OUTPUT_FILE):
        return
    try:
        with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
            history = json.load(f)
    except Exception as e:
        print(f"Erro ao ler auditoria para core: {e}")
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
    history_text = json.dumps(history, ensure_ascii=False, indent=2)

    prompt_text = (
        f"Você é a mente de CONSOLIDAÇÃO de longo prazo dthe user. Mantém um arquivo "
        f"'user-core.md' que um assistente lê no boot. Diferente da memória de curto prazo "
        f"('user-memory.md', janela de 30 dias que decai por tempo), o CORE guarda fatos "
        f"ESTÁVEIS que persistem por meses e só mudam por CONTRADIÇÃO — nunca por idade.\n\n"
        f"NÚCLEO ATUAL (preserve; é a memória já consolidada):\n{existing_core}\n\n"
        f"HISTÓRICO RECENTE (auditorias dos últimos 30 dias) para minerar:\n{history_text[:80000]}\n\n"
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

    print("Consolidando memória de longo prazo (core) via DeepSeek...")
    core_md = call_deepseek_text(prompt_text)
    if not core_md:
        print("DeepSeek sem resposta para core. Tentando Gemini...")
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
        print(f"Memória de longo prazo atualizada com sucesso em {core_file}")
    else:
        print("Falha ao consolidar a memória de longo prazo.")

if __name__ == "__main__":
    generate_daily_audit()
