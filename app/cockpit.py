import os
import json
import glob
import http.server
import socketserver
from datetime import datetime
import threading
import re
import time
import subprocess
import sys

# AUTO-INSTALL DEPENDENCIES
try:
    import requests
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
    import requests

try:
    from rank_bm25 import BM25Okapi
    import numpy as np
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "rank_bm25", "numpy"])
    from rank_bm25 import BM25Okapi
    import numpy as np

import ludovico_distiller
import daily_auditor

# CONFIGURAÇÃO
PORT = int(os.environ.get('PORT', 8000))
GEMINI_BASE_DIR = os.environ.get('GEMINI_DATA', '/app/data/gemini')
CLAUDE_PROJECTS_DIR = os.environ.get('CLAUDE_DATA', '/app/data/claude')
CLAUDE_CONVERTED_DIR = os.environ.get('CLAUDE_CONVERTED', '/app/data/claude_converted')
CODEX_BASE_DIR = os.environ.get('CODEX_DATA', '/app/data/codex')
CHATGPT_SITE_DIR = os.environ.get('CHATGPT_SITE_DATA', '/app/data/chatgpt_site')
DATA_DIR = '/app/data'
APP_VERSION = '6.2'
SKILL_LOG_PATH = os.path.join(DATA_DIR, 'skill_log.jsonl')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
EMBED_DISABLED = False  # set True on first API failure
EMBED_RUNNING = False  # prevent concurrent embed threads
SEARCH_INDEX_PATH = os.path.join(DATA_DIR, 'search_index.json')

SKILLS_MAP = {
    'skippy':      {'icon': '🍺', 'name': 'Skippy'},
    'skacoes':     {'icon': '📈', 'name': 'Trading'},
    'skadv':       {'icon': '⚖️', 'name': 'Advogado'},
    'skcnpj':      {'icon': '🤝', 'name': 'Saul'},
    'skcpf':       {'icon': '🧅', 'name': 'Architect'},
    'skcreator':   {'icon': '🏗️', 'name': 'Creator'},
    'skeng':       {'icon': '🇬🇧', 'name': 'English'},
    'skhumanizer': {'icon': '✍️', 'name': 'Humanizer'},
    'skmcp':       {'icon': '🔌', 'name': 'MCP'},
    'skmed':       {'icon': '💊', 'name': 'Hans'},
    'skmimi':      {'icon': '🐢', 'name': 'Mimi'},
    'skprompt':    {'icon': '✨', 'name': 'Prompt'},
    'skpsi':       {'icon': '🧬', 'name': 'Skippy Psi'},
    'skrelatorio': {'icon': '📊', 'name': 'Relatorio'},
    'skresume':    {'icon': '🧾', 'name': 'Resume'},
    'skrolo':      {'icon': '🎵', 'name': 'Rolo'},
    'sksup':       {'icon': '🎫', 'name': 'Suporte'},
    'skvet':       {'icon': '🐱', 'name': 'Chico'},
}

CHAT_INDEX = []
CHAT_MESSAGES = {}
SKILL_LOG = []
BM25_INDEX = None
BM25_UIDS = []
EMBED_INDEX = {}
SEARCH_LOCK = threading.Lock()

def load_skill_log():
    global SKILL_LOG
    entries = []
    if os.path.exists(SKILL_LOG_PATH):
        try:
            with open(SKILL_LOG_PATH, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except: continue
        except: pass
    SKILL_LOG = entries

def match_skill(session_start_ts, agent):
    if not SKILL_LOG or not session_start_ts:
        return None
    try:
        if isinstance(session_start_ts, str) and 'T' in session_start_ts:
            sess_dt = datetime.fromisoformat(session_start_ts.replace('Z', '+00:00'))
            sess_epoch = sess_dt.timestamp()
        else:
            sess_epoch = float(session_start_ts)
            if sess_epoch < 10000000000:
                sess_epoch *= 1000
            sess_epoch = sess_epoch / 1000
    except:
        return None

    best_match = None
    best_diff = 300

    for entry in SKILL_LOG:
        if entry.get('agent', '') != agent:
            continue
        try:
            entry_ts = entry.get('ts', '')
            if 'T' in str(entry_ts):
                entry_epoch = datetime.fromisoformat(entry_ts).timestamp()
            else:
                entry_epoch = float(entry_ts)
        except:
            continue

        diff = sess_epoch - entry_epoch
        if 0 <= diff < best_diff:
            best_diff = diff
            best_match = entry.get('skill')

    return best_match

def clean_content(content):
    if not content: return ''
    if isinstance(content, list):
        text_parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get('type') == 'text' or ('text' in p and 'type' not in p): text_parts.append(p.get('text', ''))
            else: text_parts.append(str(p))
        content = ' '.join(text_parts)
    content = re.sub(r'--- Content from.*?--- End of content ---', '', content, flags=re.DOTALL)
    content = re.sub(r'<local-command-caveat>.*?</local-command-caveat>', '', content, flags=re.DOTALL)
    return content.strip()

def format_date(val):
    try:
        if isinstance(val, str) and 'T' in val:
            dt = datetime.fromisoformat(val.replace('Z', '+00:00')).astimezone()
            return dt.strftime('%d/%m/%Y %H:%M:%S'), dt.timestamp()
        fval = float(val)
        if fval < 10000000000: fval *= 1000
        dt = datetime.fromtimestamp(fval/1000)
        return dt.strftime('%d/%m/%Y %H:%M:%S'), dt.timestamp()
    except:
        return "Data Desconhecida", 0

def convert_claude_log(jsonl_path):
    messages = []
    start_ts = None
    models = set()
    try:
        with open(jsonl_path, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    msg_obj = entry.get('message', {})
                    role = entry.get('type')
                    if not role or role not in ['user', 'assistant']:
                        role = msg_obj.get('role')
                    if not role: continue
                    model = entry.get('model') or msg_obj.get('model') or ''
                    if role == 'assistant' and model:
                        models.add(model)
                    content_raw = msg_obj.get('content')
                    ts = entry.get('timestamp')
                    if not start_ts: start_ts = ts
                    final_content = ''
                    if isinstance(content_raw, str):
                        final_content = content_raw
                    elif isinstance(content_raw, list):
                        for b in content_raw:
                            if b.get('type') == 'text': final_content += b.get('text', '') + '\n'
                    if not final_content.strip() or '<command-name>' in final_content: continue
                    messages.append({
                        'type': 'claude' if role == 'assistant' else 'user',
                        'content': final_content.strip(),
                        'timestamp': ts
                    })
                except: continue
    except: return None
    if not messages: return None
    session_id = os.path.basename(jsonl_path).replace('.jsonl', '')
    result = {'sessionId': session_id, 'startTime': start_ts or messages[0]['timestamp'], 'messages': messages}
    if models:
        result['model'] = ', '.join(sorted(models))
    return result

def extract_codex_content(content):
    if not content:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if 'text' in item:
                    parts.append(item.get('text') or '')
                elif item.get('type') in ('output_text', 'input_text') and 'content' in item:
                    parts.append(item.get('content') or '')
            else:
                parts.append(str(item))
        return '\n'.join(p for p in parts if p)
    return str(content)

def convert_codex_log(jsonl_path):
    messages = []
    session_id = os.path.basename(jsonl_path).replace('.jsonl', '')
    start_ts = None
    try:
        with open(jsonl_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except:
                    continue

                ts = entry.get('timestamp')
                if entry.get('type') == 'session_meta':
                    payload = entry.get('payload', {})
                    session_id = payload.get('id', session_id)
                    start_ts = payload.get('timestamp') or ts or start_ts
                    continue

                if entry.get('type') != 'response_item':
                    continue
                payload = entry.get('payload', {})
                if payload.get('type') != 'message':
                    continue
                role = payload.get('role')
                if role not in ('user', 'assistant'):
                    continue

                content = clean_content(extract_codex_content(payload.get('content')))
                if not content:
                    continue
                if role == 'user' and (
                    content.startswith('<environment_context>')
                    or content.startswith('Use o SKILL.md abaixo')
                ):
                    continue
                messages.append({
                    'type': 'codex' if role == 'assistant' else 'user',
                    'content': content,
                    'timestamp': ts
                })
                if not start_ts:
                    start_ts = ts
    except:
        return None
    if not messages:
        return None
    return {'sessionId': session_id, 'startTime': start_ts or messages[0]['timestamp'], 'messages': messages}

def sync_claude():
    if not os.path.exists(CLAUDE_PROJECTS_DIR): return
    if not os.path.exists(CLAUDE_CONVERTED_DIR): os.makedirs(CLAUDE_CONVERTED_DIR)
    for f in glob.glob(os.path.join(CLAUDE_PROJECTS_DIR, '**', '*.jsonl'), recursive=True):
        fname = os.path.basename(f)
        out_path = os.path.join(CLAUDE_CONVERTED_DIR, "session-CLAUDE-" + fname.replace('.jsonl', '.json'))
        if not os.path.exists(out_path) or os.path.getmtime(f) > os.path.getmtime(out_path):
            data = convert_claude_log(f)
            if data:
                data['machine'] = 'DKR' if '/docker/' in f else ('LNX' if '/linux/' in f else 'WIN')
                with open(out_path, 'w', encoding='utf-8') as out: json.dump(data, out, indent=2)

def get_summary(messages, source):
    for m in messages:
        if m.get('type') != 'user': continue
        c = clean_content(m.get('content', ''))
        if len(c) > 5: return c[:100] + '...' if len(c) > 100 else c
    labels = {
        'gemini': 'Sessao Gemini',
        'claude': 'Sessao Claude',
        'codex': 'Sessao Codex',
        'deepseek': 'Sessao DeepSeek',
        'chatgpt': 'Sessao ChatGPT',
    }
    return labels.get(source, 'Sessao')

def compact_chat_item(uid, chat, skill, summary):
    """Return sidebar/search metadata without the full message payload."""
    return {
        'uid': uid,
        'sid': chat.get('sid', ''),
        'machine': chat.get('machine', ''),
        'source': chat.get('source', ''),
        'date': chat.get('date', ''),
        'raw_date': chat.get('raw_date', 0),
        'skill': skill,
        'summary': summary,
    }

# ─── SEARCH ENGINE ───────────────────────────────────────────────────────────

def tokenize(text):
    return re.findall(r'\w+', text.lower())

def get_chat_text(uid):
    """Build searchable text for a chat (all messages, capped at 8000 chars)."""
    data = CHAT_MESSAGES.get(uid, {})
    msgs = data.get('msgs', [])
    parts = []
    for m in msgs:
        content = m.get('content', '')
        if content:
            parts.append(content[:400])
    return ' '.join(parts)[:8000]

def get_snippet(text, query_terms, context=220):
    """Extract a relevant snippet around the first matched term."""
    text_lower = text.lower()
    best_pos = -1
    for term in query_terms:
        if len(term) < 3:
            continue
        pos = text_lower.find(term.lower())
        if pos != -1:
            best_pos = pos
            break
    if best_pos == -1:
        return text[:context] + ('...' if len(text) > context else '')
    start = max(0, best_pos - 80)
    end = min(len(text), best_pos + context)
    prefix = '...' if start > 0 else ''
    suffix = '...' if end < len(text) else ''
    return prefix + text[start:end] + suffix

def build_bm25_index():
    global BM25_INDEX, BM25_UIDS
    if not CHAT_INDEX:
        return
    uids = [c['uid'] for c in CHAT_INDEX]
    corpus = [tokenize(get_chat_text(uid)) for uid in uids]
    with SEARCH_LOCK:
        BM25_INDEX = BM25Okapi(corpus)
        BM25_UIDS = uids
    print(f'[BM25] Index built: {len(uids)} chats.')

def load_embed_index():
    global EMBED_INDEX
    if os.path.exists(SEARCH_INDEX_PATH):
        try:
            with open(SEARCH_INDEX_PATH, 'r') as f:
                EMBED_INDEX = json.load(f)
            print(f'[Embed] Loaded {len(EMBED_INDEX)} embeddings from disk.')
        except:
            EMBED_INDEX = {}

def save_embed_index():
    try:
        with open(SEARCH_INDEX_PATH, 'w') as f:
            json.dump(EMBED_INDEX, f)
    except Exception as e:
        print(f'[Embed] Save error: {e}')

def get_embeddings_batch(texts):
    """Get embeddings via Gemini batchEmbedContents."""
    if not GEMINI_API_KEY:
        return None
    try:
        requests_payload = [
            {'model': 'models/gemini-embedding-001', 'content': {'parts': [{'text': ((t or '').strip() or '[empty]')[:8000]}]}}
            for t in texts
        ]
        r = requests.post(
            f'https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:batchEmbedContents?key={GEMINI_API_KEY}',
            json={'requests': requests_payload},
            timeout=60
        )
        data = r.json()
        if 'embeddings' in data:
            return [e['values'] for e in data['embeddings']]
        print(f'[Embed] API error: {str(data)[:200]}')
        return None
    except Exception as e:
        print(f'[Embed] Request error: {e}')
        return None

def update_embed_index():
    """Incrementally embed chats not yet in the index."""
    global EMBED_DISABLED, EMBED_RUNNING
    if not GEMINI_API_KEY or EMBED_DISABLED or EMBED_RUNNING:
        return
    EMBED_RUNNING = True
    try:
        _do_embed_index()
    finally:
        EMBED_RUNNING = False

def _do_embed_index():
    """Inner embedding logic."""
    global EMBED_DISABLED
    if not GEMINI_API_KEY or EMBED_DISABLED:
        return
    missing = [c['uid'] for c in CHAT_INDEX if c['uid'] not in EMBED_INDEX]
    if not missing:
        return
    print(f'[Embed] Indexing {len(missing)} new chats...', flush=True)
    batch_size = 20
    consecutive_failures = 0
    for i in range(0, len(missing), batch_size):
        batch = missing[i:i + batch_size]
        texts = [get_chat_text(uid) for uid in batch]
        embeddings = None
        for attempt in range(3):
            embeddings = get_embeddings_batch(texts)
            if embeddings:
                break
            wait = 5 * (attempt + 1)
            print(f'[Embed] Attempt {attempt+1} failed, retrying in {wait}s...', flush=True)
            time.sleep(wait)
        if embeddings:
            for uid, emb in zip(batch, embeddings):
                EMBED_INDEX[uid] = emb
            save_embed_index()
            consecutive_failures = 0
            print(f'[Embed] Batch {i // batch_size + 1} done. Total: {len(EMBED_INDEX)}/{len(CHAT_INDEX)}', flush=True)
            time.sleep(1)
        else:
            consecutive_failures += 1
            print(f'[Embed] Batch {i // batch_size + 1} failed after 3 attempts. Pausing...', flush=True)
            if consecutive_failures >= 2:
                print(f'[Embed] 2 consecutive failures — disabling until restart. BM25 still active.', flush=True)
                EMBED_DISABLED = True
                return
            time.sleep(30)

def cosine_sim(a, b):
    a, b = np.array(a, dtype=np.float32), np.array(b, dtype=np.float32)
    norm_a, norm_b = np.linalg.norm(a), np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))

def hybrid_search(query, top_k=30):
    """RRF fusion of BM25 + semantic embeddings."""
    query_terms = tokenize(query)
    rrf_scores = {}
    K = 60  # RRF constant

    # BM25 ranking
    if BM25_INDEX and BM25_UIDS:
        with SEARCH_LOCK:
            scores = BM25_INDEX.get_scores(query_terms)
            uids_snapshot = BM25_UIDS[:]
        bm25_ranked = sorted(zip(uids_snapshot, scores), key=lambda x: x[1], reverse=True)
        for rank, (uid, score) in enumerate(bm25_ranked):
            if score > 0:
                rrf_scores[uid] = rrf_scores.get(uid, 0) + 1.0 / (rank + K)

    # Semantic ranking
    if GEMINI_API_KEY and EMBED_INDEX:
        q_embs = get_embeddings_batch([query])
        if q_embs:
            q_emb = q_embs[0]
            sem_scores = [(uid, cosine_sim(q_emb, emb)) for uid, emb in EMBED_INDEX.items()]
            sem_ranked = sorted(sem_scores, key=lambda x: x[1], reverse=True)
            for rank, (uid, score) in enumerate(sem_ranked):
                if score > 0.25:
                    rrf_scores[uid] = rrf_scores.get(uid, 0) + 1.0 / (rank + K)

    ranked = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)

    output = []
    chat_map = {c['uid']: c for c in CHAT_INDEX}
    for uid, score in ranked[:top_k]:
        chat = chat_map.get(uid)
        if not chat:
            continue
        text = get_chat_text(uid)
        snippet = get_snippet(text, query_terms)
        output.append({
            'uid': uid,
            'sid': chat.get('sid', ''),
            'score': round(score, 6),
            'snippet': snippet,
            'date': chat.get('date', ''),
            'source': chat.get('source', ''),
            'machine': chat.get('machine', ''),
            'skill': chat.get('skill'),
            'summary': chat.get('summary', ''),
        })
    return output

# ─── INDEX WORKER ────────────────────────────────────────────────────────────

def index_worker():
    global CHAT_INDEX, CHAT_MESSAGES
    file_metadata = {}
    last_distill_date = None
    last_ludovico_date = None
    while True:
        try:
            sync_claude()
            load_skill_log()
            current_chats = {}
            something_changed = False

            if os.path.isdir(GEMINI_BASE_DIR):
                for f in glob.glob(os.path.join(GEMINI_BASE_DIR, '**', 'chats', '*.json'), recursive=True):
                    fname = os.path.basename(f)
                    uid = f"gemini-{fname}"
                    mtime = os.path.getmtime(f)
                    if uid not in file_metadata or file_metadata[uid] < mtime:
                        try:
                            with open(f, 'r', encoding='utf-8') as j:
                                data = json.load(j); raw = data.get('messages', []); proc = []
                                for msg in raw:
                                    role = msg.get('type'); content = msg.get('content', '')
                                    if role == 'assistant': role = 'gemini'
                                    content = clean_content(content)
                                    mc = msg.copy(); mc['type'] = role; mc['content'] = content
                                    proc.append(mc)
                                sid = data.get("sessionId", fname.replace(".json",""))
                                start_raw = data.get("startTime") or data.get("lastUpdated") or mtime
                                date_str, raw_ts = format_date(start_raw)
                                CHAT_MESSAGES[uid] = {"msgs": proc, "sid": sid, "machine": 'DKR' if 'docker' in f.lower() else ('LNX' if 'linux' in f.lower() else 'WIN'), "source": "gemini", "date": date_str, "raw_date": raw_ts, "start_raw": start_raw}
                                file_metadata[uid] = mtime
                                something_changed = True
                        except: continue
                    if uid in CHAT_MESSAGES:
                        m = CHAT_MESSAGES[uid]
                        skill = match_skill(m.get('start_raw'), 'gemini')
                        current_chats[uid] = compact_chat_item(uid, m, skill, get_summary(m['msgs'], 'gemini'))

            if os.path.isdir(CLAUDE_CONVERTED_DIR):
                for f in glob.glob(os.path.join(CLAUDE_CONVERTED_DIR, '*.json')):
                    fname = os.path.basename(f)
                    uid = f"claude-{fname}"
                    mtime = os.path.getmtime(f)
                    if uid not in file_metadata or file_metadata[uid] < mtime:
                        try:
                            with open(f, 'r', encoding='utf-8') as j:
                                data = json.load(j); raw = data.get('messages', []); proc = []
                                model = data.get('model', '').lower()
                                source = 'deepseek' if 'deepseek' in model else 'claude'
                                for msg in raw:
                                    content = clean_content(msg.get('content', ''))
                                    mc = msg.copy(); mc['content'] = content
                                    proc.append(mc)
                                sid = data.get("sessionId", "").replace("CLAUDE-","")
                                start_raw = data.get('startTime', mtime)
                                date_str, raw_ts = format_date(start_raw)
                                CHAT_MESSAGES[uid] = {"msgs": proc, "sid": sid, "machine": data.get("machine", "WIN"), "source": source, "date": date_str, "raw_date": raw_ts, "start_raw": start_raw}
                                file_metadata[uid] = mtime
                                something_changed = True
                        except: continue
                    if uid in CHAT_MESSAGES:
                        m = CHAT_MESSAGES[uid]
                        skill = match_skill(m.get('start_raw'), 'claude')
                        current_chats[uid] = compact_chat_item(uid, m, skill, get_summary(m['msgs'], 'claude'))

            if os.path.isdir(CODEX_BASE_DIR):
                for f in glob.glob(os.path.join(CODEX_BASE_DIR, '**', '*.jsonl'), recursive=True):
                    fname = os.path.basename(f)
                    rel_name = os.path.relpath(f, CODEX_BASE_DIR).replace(os.sep, '__')
                    uid = f"codex-{rel_name}"
                    mtime = os.path.getmtime(f)
                    if uid not in file_metadata or file_metadata[uid] < mtime:
                        data = convert_codex_log(f)
                        if data:
                            sid = data.get("sessionId", fname.replace(".jsonl", ""))
                            start_raw = data.get('startTime', mtime)
                            date_str, raw_ts = format_date(start_raw)
                            machine = 'DKR' if 'docker' in f.lower() else ('WIN' if 'windows' in f.lower() else 'LNX')
                            CHAT_MESSAGES[uid] = {"msgs": data.get('messages', []), "sid": sid, "machine": machine, "source": "codex", "date": date_str, "raw_date": raw_ts, "start_raw": start_raw}
                            file_metadata[uid] = mtime
                            something_changed = True
                    if uid in CHAT_MESSAGES:
                        m = CHAT_MESSAGES[uid]
                        skill = match_skill(m.get('start_raw'), 'codex')
                        current_chats[uid] = compact_chat_item(uid, m, skill, get_summary(m['msgs'], 'codex'))

            if os.path.isdir(CHATGPT_SITE_DIR):
                for f in glob.glob(os.path.join(CHATGPT_SITE_DIR, '**', '*.json'), recursive=True):
                    fname = os.path.basename(f)
                    rel_name = os.path.relpath(f, CHATGPT_SITE_DIR).replace(os.sep, '__')
                    uid = f"chatgpt-{rel_name}"
                    mtime = os.path.getmtime(f)
                    if uid not in file_metadata or file_metadata[uid] < mtime:
                        try:
                            with open(f, 'r', encoding='utf-8') as j:
                                data = json.load(j)
                                proc = []
                                for msg in data.get('messages', []):
                                    role = msg.get('type') or msg.get('role') or 'chatgpt'
                                    role = 'user' if role == 'user' else 'chatgpt'
                                    content = clean_content(msg.get('content', ''))
                                    if content:
                                        mc = msg.copy(); mc['type'] = role; mc['content'] = content
                                        proc.append(mc)
                                if not proc:
                                    continue
                                sid = data.get("sessionId") or fname.replace(".json", "")
                                start_raw = data.get('startTime') or data.get('capturedAt') or mtime
                                date_str, raw_ts = format_date(start_raw)
                                summary = data.get('summary') or get_summary(proc, 'chatgpt')
                                if data.get('title') and summary == get_summary(proc, 'chatgpt'):
                                    summary = data.get('title')
                                CHAT_MESSAGES[uid] = {"msgs": proc, "sid": sid, "machine": "WEB", "source": "chatgpt", "date": date_str, "raw_date": raw_ts, "start_raw": start_raw, "url": data.get("url", ""), "title": data.get("title", "")}
                                file_metadata[uid] = mtime
                                something_changed = True
                        except Exception as e:
                            print(f'[ChatGPT] parse error {f}: {e}')
                            continue
                    if uid in CHAT_MESSAGES:
                        m = CHAT_MESSAGES[uid]
                        current_chats[uid] = compact_chat_item(uid, m, None, m.get('title') or get_summary(m['msgs'], 'chatgpt'))

            if something_changed:
                sorted_list = list(current_chats.values())
                sorted_list.sort(key=lambda x: x['raw_date'], reverse=True)
                CHAT_INDEX = sorted_list
                print(f'[{datetime.now()}] Index: {len(CHAT_INDEX)} chats (updated).')
                build_bm25_index()
                if GEMINI_API_KEY:
                    threading.Thread(target=update_embed_index, daemon=True).start()

        except Exception as e: print(f'Worker Error: {e}')
        
        now = datetime.now()
        if now.hour == 23 and now.minute >= 45 and last_distill_date != now.date():
            last_distill_date = now.date()
            # Atualiza o memory profile primeiro para a memória diária já ler o estado mais recente.
            try:
                import importlib
                importlib.reload(ludovico_distiller)
                ludovico_distiller.distill_memory()
                print(f'[{now}] [MemoryProfile] Updated.')
            except Exception as e:
                print(f'[{now}] [MemoryProfile] Error: {e}')
            try:
                import importlib
                importlib.reload(daily_auditor)
                daily_auditor.generate_daily_audit()
                print(f'[{now}] [Daily Audit] Updated.')
            except Exception as e:
                print(f'[{now}] [Daily Audit] Error: {e}')
        time.sleep(150)

# ─── HTTP HANDLER ────────────────────────────────────────────────────────────

class HistoryHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path == '/':
                self.send_response(200); self.send_header('Content-type', 'text/html; charset=utf-8'); self.end_headers()
                self.wfile.write(self.generate_html().encode('utf-8'))
            elif self.path == '/api/chats':
                self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps(CHAT_INDEX).encode('utf-8'))
            elif self.path.startswith('/api/chat/'):
                uid = self.path.replace('/api/chat/', '')
                data = CHAT_MESSAGES.get(uid, {"msgs":[]})
                self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps(data).encode('utf-8'))
            elif self.path == '/api/memory/ludovico':
                dna_path = os.path.join(DATA_DIR, 'ludovico_dna.json')
                if os.path.exists(dna_path):
                    with open(dna_path, 'r', encoding='utf-8') as f: data = f.read()
                    self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
                    self.wfile.write(data.encode('utf-8'))
                else: self.send_response(404); self.end_headers(); self.wfile.write(b'{"error": "not found"}')
            elif self.path == '/api/memory/daily':
                daily_path = os.path.join(DATA_DIR, 'daily_audit.json')
                if os.path.exists(daily_path):
                    with open(daily_path, 'r', encoding='utf-8') as f: data = f.read()
                    self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
                    self.wfile.write(data.encode('utf-8'))
                else: self.send_response(404); self.end_headers(); self.wfile.write(b'{"error": "not found"}')
            elif self.path == '/api/memory/core':
                core_path = os.path.join(DATA_DIR, 'ai_config', 'user-core.md')
                if os.path.exists(core_path):
                    with open(core_path, 'r', encoding='utf-8') as f: data = f.read()
                    self.send_response(200); self.send_header('Content-type', 'text/plain; charset=utf-8'); self.end_headers()
                    self.wfile.write(data.encode('utf-8'))
                else: self.send_response(404); self.end_headers(); self.wfile.write(b'core memory not found')
            elif self.path == '/api/memory/memoria':
                mem_path = os.path.join(DATA_DIR, 'ai_config', 'user-memory.md')
                if os.path.exists(mem_path):
                    with open(mem_path, 'r', encoding='utf-8') as f: data = f.read()
                    self.send_response(200); self.send_header('Content-type', 'text/plain; charset=utf-8'); self.end_headers()
                    self.wfile.write(data.encode('utf-8'))
                else: self.send_response(404); self.end_headers(); self.wfile.write(b'day memory not found')
            elif self.path == '/api/memory/weekly':
                # Sempre regenera — chamado 1×/semana pelo n8n. Custo: 1 call DeepSeek.
                try:
                    import weekly_distiller, importlib
                    importlib.reload(weekly_distiller)
                    digest = weekly_distiller.generate_weekly_digest()
                    if digest:
                        self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
                        self.wfile.write(json.dumps(digest, ensure_ascii=False).encode('utf-8'))
                    else:
                        self.send_response(500); self.end_headers()
                        self.wfile.write(b'{"error": "weekly digest generation failed"}')
                except Exception as e:
                    self.send_response(500); self.end_headers()
                    self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))
            elif self.path == '/api/search/status':
                status = {
                    'bm25_ready': BM25_INDEX is not None,
                    'bm25_count': len(BM25_UIDS),
                    'embed_ready': len(EMBED_INDEX) > 0,
                    'embed_count': len(EMBED_INDEX),
                    'total_chats': len(CHAT_INDEX),
                    'embed_provider': 'gemini' if GEMINI_API_KEY else 'none',
                }
                self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps(status).encode('utf-8'))
            elif self.path == '/favicon.ico': self.send_response(204); self.end_headers()
            else: self.send_error(404)
        except Exception as e: self.send_response(500); self.end_headers(); self.wfile.write(str(e).encode())

    def do_POST(self):
        try:
            if self.path == '/api/skill_log':
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                skill = data.get('skill', '')
                agent = data.get('agent', '')
                ts = data.get('ts', datetime.now().isoformat())
                if skill and agent:
                    entry = json.dumps({"ts": ts, "skill": skill, "agent": agent})
                    with open(SKILL_LOG_PATH, 'a', encoding='utf-8') as f:
                        f.write(entry + '\n')
                    load_skill_log()
                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(b'{"ok": true}')
                    print(f'[SkillLog] {agent}/{skill} @ {ts}')
                else:
                    self.send_response(400); self.end_headers(); self.wfile.write(b'{"error": "missing skill or agent"}')

            elif self.path == '/api/search':
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                query = data.get('query', '').strip()
                if not query:
                    self.send_response(400); self.end_headers(); self.wfile.write(b'{"error": "no query"}')
                    return
                results = hybrid_search(query)
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps(results).encode('utf-8'))

            else:
                self.send_error(404)
        except Exception as e:
            self.send_response(500); self.end_headers(); self.wfile.write(str(e).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def generate_html(self):
        opts = ''.join([f'<option value="{k}">{v["icon"]} {v["name"]}</option>' for k, v in SKILLS_MAP.items()])
        return self.get_template().replace('{{OPTS}}', opts).replace('{{META_JSON}}', json.dumps(SKILLS_MAP))

    def get_template(self):
        return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cockpit v6.2</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-dark.min.css">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
    <style>
        :root {
            --bg-primary: #0a0e14;
            --bg-secondary: #111720;
            --bg-tertiary: #1e2638;
            --border: #253345;
            --text-primary: #e8ecf2;
            --text-muted: #8899aa;
            --accent-blue: #3b82f6;
            --accent-amber: #f59e0b;
            --accent-green: #22c55e;
            --accent-purple: #a855f7;
        }
        * { box-sizing: border-box; }
        body { background: var(--bg-primary); color: var(--text-primary); height: 100vh; overflow: hidden; font-family: 'Inter', -apple-system, system-ui, sans-serif; margin: 0; }

        /* SIDEBAR */
        .sidebar {
            background: var(--bg-secondary);
            border-right: 1px solid var(--border);
            height: 100vh;
            display: flex;
            flex-direction: column;
        }
        .sidebar-header {
            padding: 16px;
            border-bottom: 1px solid var(--border);
            flex-shrink: 0;
        }
        .sidebar-header h6 {
            font-size: 0.7rem;
            letter-spacing: 2px;
            font-weight: 700;
            color: var(--accent-blue);
            margin: 0 0 12px 0;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .sidebar-header h6::before {
            content: '';
            width: 6px;
            height: 6px;
            background: var(--accent-green);
            border-radius: 50%;
            box-shadow: 0 0 8px var(--accent-green);
            display: inline-block;
        }
        .search-row { display: flex; gap: 4px; margin-bottom: 0; }
        .search-input {
            background: var(--bg-primary);
            border: 1px solid var(--border);
            color: var(--text-primary);
            font-size: 0.8rem;
            border-radius: 8px;
            padding: 8px 12px;
            flex: 1;
            transition: border-color 0.2s;
        }
        .search-input:focus { border-color: var(--accent-blue); outline: none; box-shadow: 0 0 0 2px rgba(59,130,246,0.15); }
        .search-input::placeholder { color: var(--text-muted); }
        .search-input.semantic-active { border-color: var(--accent-purple); box-shadow: 0 0 0 2px rgba(168,85,247,0.15); }
        .search-btn {
            background: var(--bg-primary);
            border: 1px solid var(--border);
            color: var(--text-muted);
            padding: 6px 10px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 0.8rem;
            transition: all 0.15s;
            white-space: nowrap;
            display: flex;
            align-items: center;
            gap: 4px;
        }
        .search-btn:hover { color: var(--accent-purple); border-color: var(--accent-purple); }
        .search-btn.active { background: rgba(168,85,247,0.15); color: var(--accent-purple); border-color: var(--accent-purple); }
        .search-mode-badge {
            font-size: 0.6rem;
            padding: 2px 8px;
            border-radius: 10px;
            margin-top: 6px;
            display: none;
            align-items: center;
            gap: 4px;
        }
        .search-mode-badge.show { display: flex; }
        .search-mode-badge.mode-semantic { background: rgba(168,85,247,0.15); color: var(--accent-purple); border: 1px solid rgba(168,85,247,0.3); }
        .search-mode-badge.mode-filter { background: rgba(59,130,246,0.1); color: var(--accent-blue); border: 1px solid rgba(59,130,246,0.2); }
        .filter-row { display: flex; gap: 4px; margin-top: 8px; }
        .filter-grid { display: flex; gap: 4px; margin-top: 6px; flex-wrap: wrap; }
        .filter-btn {
            flex: 1 1 calc(50% - 4px);
            background: var(--bg-primary);
            border: 1px solid var(--border);
            color: var(--text-muted);
            font-size: 0.7rem;
            font-weight: 600;
            padding: 6px 0;
            border-radius: 6px;
            cursor: pointer;
            transition: all 0.15s;
            text-align: center;
        }
        .filter-btn:hover, .filter-btn.active { background: var(--bg-tertiary); color: var(--text-primary); border-color: var(--accent-blue); }
        .filter-btn-all { flex: 1 1 100%; }
        .filter-select {
            background: var(--bg-primary);
            border: 1px solid var(--border);
            color: #c0ccda;
            font-size: 0.7rem;
            padding: 6px 8px;
            border-radius: 6px;
            width: 100%;
            margin-top: 6px;
        }
        .filter-select:focus { border-color: var(--accent-blue); outline: none; }
        .filter-select option { background: #111720; color: #e8ecf2; padding: 6px; }
        .filter-select option:checked { background: #253345; }

        /* CHAT LIST */
        .chat-list { overflow-y: auto; flex: 1; }
        .chat-item {
            padding: 12px 16px;
            border-bottom: 1px solid rgba(30,42,58,0.5);
            cursor: pointer;
            transition: background 0.15s;
            border-left: 3px solid transparent;
        }
        .chat-item:hover { background: var(--bg-tertiary); }
        .chat-item.active { background: var(--bg-tertiary); }
        .chat-item.active.src-gemini { border-left-color: var(--accent-blue); }
        .chat-item.active.src-claude { border-left-color: var(--accent-amber); }
        .chat-item.active.src-codex { border-left-color: var(--accent-green); }
        .chat-item.active.src-deepseek { border-left-color: var(--accent-purple); }
        .chat-item.active.src-chatgpt { border-left-color: #10b981; }
        .chat-item-meta {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 4px;
        }
        .chat-item-badges { display: flex; gap: 4px; align-items: center; flex-wrap: wrap; }
        .badge-src {
            font-size: 0.5rem;
            padding: 2px 6px;
            border-radius: 3px;
            text-transform: uppercase;
            font-weight: 800;
            letter-spacing: 0.5px;
        }
        .badge-gemini { background: rgba(59,130,246,0.2); color: var(--accent-blue); }
        .badge-claude { background: rgba(245,158,11,0.2); color: var(--accent-amber); }
        .badge-codex { background: rgba(34,197,94,0.18); color: var(--accent-green); }
        .badge-deepseek { background: rgba(168,85,247,0.18); color: var(--accent-purple); }
        .badge-chatgpt { background: rgba(16,185,129,0.18); color: #34d399; }
        .badge-win { background: rgba(59,130,246,0.15); color: #60a5fa; }
        .badge-lnx { background: rgba(245,158,11,0.15); color: #fbbf24; }
        .badge-dkr { background: rgba(168,85,247,0.15); color: #c084fc; }
        .badge-web { background: rgba(16,185,129,0.15); color: #34d399; }
        .badge-skill {
            font-size: 0.5rem;
            padding: 2px 6px;
            border-radius: 3px;
            background: rgba(34,197,94,0.15);
            color: var(--accent-green);
            font-weight: 700;
        }
        .chat-item-date { font-size: 0.65rem; color: var(--text-muted); }
        .chat-item-summary {
            font-size: 0.8rem;
            color: var(--text-primary);
            opacity: 0.95;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            line-height: 1.4;
        }
        .chat-item-snippet {
            font-size: 0.72rem;
            color: var(--text-muted);
            line-height: 1.5;
            margin-top: 4px;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }
        .snippet-highlight { color: var(--accent-purple); font-weight: 600; }

        /* MAIN CONTENT */
        .main-panel {
            height: 100vh;
            display: flex;
            flex-direction: column;
            background: var(--bg-primary);
        }

        /* CHAT HEADER BAR */
        .chat-header {
            padding: 12px 24px;
            border-bottom: 1px solid var(--border);
            background: var(--bg-secondary);
            display: none;
            align-items: center;
            justify-content: space-between;
            flex-shrink: 0;
        }
        .chat-header.visible { display: flex; }
        .chat-header-left {
            display: flex;
            align-items: center;
            gap: 12px;
            font-size: 0.8rem;
            color: var(--text-muted);
            min-width: 0;
            flex: 1;
        }
        .chat-header-cmd {
            font-family: 'JetBrains Mono', 'Fira Code', monospace;
            font-size: 0.75rem;
            color: var(--accent-green);
            background: var(--bg-primary);
            padding: 4px 10px;
            border-radius: 4px;
            border: 1px solid var(--border);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .chat-header-skill {
            font-size: 0.7rem;
            padding: 3px 8px;
            border-radius: 4px;
            background: rgba(34,197,94,0.15);
            color: var(--accent-green);
            font-weight: 700;
            white-space: nowrap;
        }
        .chat-header-actions { display: flex; gap: 6px; flex-shrink: 0; }
        .header-btn {
            background: var(--bg-primary);
            border: 1px solid var(--border);
            color: var(--text-muted);
            padding: 5px 10px;
            border-radius: 6px;
            font-size: 0.75rem;
            cursor: pointer;
            transition: all 0.15s;
            display: flex;
            align-items: center;
            gap: 4px;
        }
        .header-btn:hover { color: var(--text-primary); border-color: var(--accent-blue); background: var(--bg-tertiary); }
        .header-btn.share-btn:hover { border-color: var(--accent-green); color: var(--accent-green); }

        /* MESSAGES */
        .messages-scroll {
            flex: 1;
            overflow-y: auto;
            padding: 24px;
        }
        .welcome {
            height: 100%;
            display: flex;
            align-items: center;
            justify-content: center;
            opacity: 0.15;
        }
        .welcome h1 { font-size: 2.5rem; font-weight: 800; letter-spacing: 4px; margin: 0; }
        .welcome p { font-size: 0.85rem; letter-spacing: 2px; margin: 4px 0 0 0; }

        .message {
            margin-bottom: 20px;
            padding: 16px 20px;
            border-radius: 12px;
            max-width: 88%;
            position: relative;
            line-height: 1.7;
            font-size: 0.9rem;
        }
        .message p { margin-bottom: 8px; }
        .message p:last-child { margin-bottom: 0; }
        .msg-user {
            background: var(--bg-tertiary);
            margin-left: auto;
            border: 1px solid var(--border);
        }
        .msg-assistant {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-left: 3px solid var(--accent-blue);
        }
        .msg-assistant.from-claude { border-left-color: var(--accent-amber); }
        .msg-assistant.from-codex { border-left-color: var(--accent-green); }
        .msg-assistant.from-deepseek { border-left-color: var(--accent-purple); }
        .msg-assistant.from-chatgpt { border-left-color: #10b981; }
        .msg-role {
            font-size: 0.65rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: var(--text-muted);
            margin-bottom: 8px;
        }

        /* CODE BLOCKS */
        pre {
            background: #010409 !important;
            padding: 14px;
            border-radius: 8px;
            border: 1px solid var(--border);
            overflow-x: auto;
            margin: 10px 0;
        }
        code { font-family: 'JetBrains Mono', 'Fira Code', monospace; font-size: 0.82rem; }
        p code, li code {
            background: rgba(59,130,246,0.1);
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.82rem;
            color: #a5d0ff;
        }

        /* DNA PANEL */
        .dna-card {
            background: var(--bg-secondary);
            border: 1px solid var(--accent-amber);
            border-radius: 12px;
            padding: 24px;
            max-width: 100%;
        }
        .dna-card h4 { color: var(--accent-amber); font-size: 1rem; font-weight: 700; letter-spacing: 1px; }
        .dna-section { margin-bottom: 16px; }
        .dna-section strong { color: var(--accent-amber); font-size: 0.8rem; }

        /* LOADING */
        .loading-box { text-align: center; padding: 60px 0; }
        .loading-box .spinner-border { width: 1.5rem; height: 1.5rem; border-width: 2px; }
        .loading-box p { font-size: 0.8rem; color: var(--text-muted); margin-top: 12px; }

        /* SCROLLBAR */
        ::-webkit-scrollbar { width: 5px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 10px; }
        ::-webkit-scrollbar-thumb:hover { background: #2a3a4a; }

        /* TOAST */
        .toast-msg {
            position: fixed;
            bottom: 24px;
            right: 24px;
            background: var(--bg-tertiary);
            border: 1px solid var(--accent-green);
            color: var(--accent-green);
            padding: 10px 20px;
            border-radius: 8px;
            font-size: 0.8rem;
            font-weight: 600;
            opacity: 0;
            transition: opacity 0.3s;
            pointer-events: none;
            z-index: 999;
        }
        .toast-msg.show { opacity: 1; }

        /* SHARE MODAL */
        .share-overlay {
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.7);
            backdrop-filter: blur(4px);
            z-index: 1000;
            display: none;
            align-items: center;
            justify-content: center;
        }
        .share-overlay.show { display: flex; }
        .share-box {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 28px;
            max-width: 440px;
            width: 90%;
        }
        .share-box h5 { font-size: 0.95rem; font-weight: 700; margin-bottom: 16px; }
        .share-option {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 12px 16px;
            background: var(--bg-primary);
            border: 1px solid var(--border);
            border-radius: 10px;
            cursor: pointer;
            transition: all 0.15s;
            margin-bottom: 8px;
        }
        .share-option:hover { border-color: var(--accent-blue); background: var(--bg-tertiary); }
        .share-option i { font-size: 1.2rem; color: var(--accent-blue); width: 24px; text-align: center; }
        .share-option-text { flex: 1; }
        .share-option-text strong { font-size: 0.85rem; display: block; }
        .share-option-text span { font-size: 0.7rem; color: var(--text-muted); }
        .share-close { margin-top: 12px; text-align: center; }
        .share-close button { background: none; border: none; color: var(--text-muted); font-size: 0.8rem; cursor: pointer; }
        .share-close button:hover { color: var(--text-primary); }

        /* COUNT BADGE */
        .chat-count {
            font-size: 0.6rem;
            color: var(--text-muted);
            background: var(--bg-primary);
            padding: 2px 6px;
            border-radius: 10px;
            border: 1px solid var(--border);
        }
    </style>
</head>
<body>
    <div class="container-fluid p-0"><div class="row g-0">
        <!-- SIDEBAR -->
        <div class="col-md-3 sidebar">
            <div class="sidebar-header">
                <h6>COCKPIT v6.2</h6>
                <div class="d-flex gap-2" style="margin-bottom:4px;">
                    <button class="header-btn" onclick="showDNA()" title="Memory Profile"><i class="bi bi-clipboard2-pulse"></i></button>
                    <button class="header-btn" onclick="showDailyAudit()" title="Daily Audit"><i class="bi bi-journal-text"></i></button>
                    <button class="header-btn" onclick="showCore()" title="Memória de Longo Prazo (Core)" style="font-size:1.05rem;line-height:1;">🐢</button>
                </div>
                <div class="search-row">
                    <input type="text" id="search-box" class="search-input" placeholder="Buscar... (auto aprofunda se o filtro falhar)">
                    <button class="search-btn" id="sem-btn" onclick="doSemanticSearch()" title="Busca semântica por significado">
                        <i class="bi bi-stars"></i>
                    </button>
                </div>
                <div class="search-mode-badge" id="mode-badge">
                    <i class="bi bi-stars"></i> <span id="mode-label"></span>
                    <span style="margin-left:auto;cursor:pointer;opacity:0.6;" onclick="clearSearch()">✕</span>
                </div>
                <div class="filter-row">
                    <button class="filter-btn filter-btn-all active" data-filter="all" onclick="setFilter('src','all',this)">Todas</button>
                </div>
                <div class="filter-grid">
                    <button class="filter-btn" data-filter="gemini" onclick="setFilter('src','gemini',this)"><i class="bi bi-stars"></i> Gemini</button>
                    <button class="filter-btn" data-filter="claude" onclick="setFilter('src','claude',this)"><i class="bi bi-chat-dots"></i> Claude</button>
                    <button class="filter-btn" data-filter="codex" onclick="setFilter('src','codex',this)"><i class="bi bi-terminal"></i> Codex</button>
                    <button class="filter-btn" data-filter="deepseek" onclick="setFilter('src','deepseek',this)"><i class="bi bi-lightning-charge"></i> DeepSeek</button>
                    <button class="filter-btn" data-filter="chatgpt" onclick="setFilter('src','chatgpt',this)"><i class="bi bi-globe2"></i> ChatGPT</button>
                </div>
                <div class="d-flex gap-2">
                    <select id="skill-filter" class="filter-select" style="flex:1"><option value="all">Todas Skills</option>{{OPTS}}</select>
                    <select id="mach-filter" class="filter-select" style="flex:1"><option value="all">Todas Maq</option><option value="WIN">WIN</option><option value="LNX">LNX</option><option value="DKR">DKR</option></select>
                </div>
            </div>
            <div class="chat-list" id="chat-list"></div>
        </div>

        <!-- MAIN -->
        <div class="col-md-9 main-panel">
            <div class="chat-header" id="chat-header">
                <div class="chat-header-left">
                    <span class="chat-header-cmd" id="header-cmd"></span>
                    <span class="chat-header-skill" id="header-skill" style="display:none;"></span>
                    <span class="chat-count" id="msg-count"></span>
                </div>
                <div class="chat-header-actions">
                    <button class="header-btn" onclick="copyCmd()" title="Copiar comando"><i class="bi bi-clipboard"></i> Copiar</button>
                    <button class="header-btn share-btn" onclick="openShare()" title="Compartilhar"><i class="bi bi-share"></i> Compartilhar</button>
                </div>
            </div>
            <div class="messages-scroll" id="chat-display">
                <div id="welcome-msg" class="welcome">
                    <div class="text-center"><h1>COCKPIT</h1><p>The Elder's Panopticon v6.2</p></div>
                </div>
                <div id="loading-overlay" style="display:none;" class="loading-box">
                    <div class="spinner-border text-primary"></div>
                    <p>Carregando sessao...</p>
                </div>
                <div id="messages-container"></div>
            </div>
        </div>
    </div></div>

    <!-- SHARE MODAL -->
    <div class="share-overlay" id="share-modal">
        <div class="share-box">
            <h5><i class="bi bi-share"></i> Compartilhar conversa</h5>
            <div class="share-option" onclick="shareAsHTML()">
                <i class="bi bi-filetype-html"></i>
                <div class="share-option-text">
                    <strong>Baixar como HTML</strong>
                    <span>Arquivo standalone — abre em qualquer browser</span>
                </div>
            </div>
            <div class="share-option" onclick="shareAsText()">
                <i class="bi bi-clipboard2-data"></i>
                <div class="share-option-text">
                    <strong>Copiar como texto</strong>
                    <span>Cola no WhatsApp, Telegram, email...</span>
                </div>
            </div>
            <div class="share-close"><button onclick="closeShare()">Cancelar</button></div>
        </div>
    </div>

    <!-- TOAST -->
    <div class="toast-msg" id="toast"></div>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <script>
        let index = [], currentChat = null, currentItem = null, srcFilter = 'all';
        let searchMode = 'filter'; // 'filter' | 'semantic'
        let semanticResults = null;
        let searchDebounce = null;
        let searchToken = 0;
        const meta = {{META_JSON}};

        async function load() {
            try { const r = await fetch('/api/chats'); index = await r.json(); apply(); }
            catch(e) { console.error(e); }
        }

        function setFilter(type, val, btn) {
            if (type === 'src') {
                srcFilter = val;
                document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
            }
            if (searchMode === 'filter') apply();
        }

        function resetSearchMode() {
            searchMode = 'filter';
            semanticResults = null;
            document.getElementById('search-box').classList.remove('semantic-active');
            document.getElementById('mode-badge').className = 'search-mode-badge';
            document.getElementById('sem-btn').classList.remove('active');
        }

        function setModeBadge(modeClass, label) {
            const badge = document.getElementById('mode-badge');
            document.getElementById('mode-label').textContent = label;
            badge.className = `search-mode-badge show ${modeClass}`;
        }

        async function runDeepSearch(query, opts = {}) {
            const {auto = false, keepInput = true} = opts;
            if (!query) {
                if (!keepInput) clearSearch();
                return [];
            }

            searchMode = 'semantic';
            const btn = document.getElementById('sem-btn');
            const startedToken = ++searchToken;
            btn.innerHTML = '<div class="spinner-border spinner-border-sm" style="width:14px;height:14px;border-width:2px;"></div>';
            btn.disabled = true;
            btn.classList.add('active');
            document.getElementById('search-box').classList.add('semantic-active');
            setModeBadge('mode-semantic', auto ? 'Busca profunda...' : 'Busca semântica...');

            try {
                const r = await fetch('/api/search', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({query})
                });
                const results = await r.json();
                if (startedToken !== searchToken) return results;

                semanticResults = results;
                setModeBadge('mode-semantic', `${auto ? 'Busca profunda' : 'Semântico'} — ${results.length} resultados`);
                renderList(results, query);
                return results;
            } catch(e) {
                if (startedToken === searchToken) {
                    console.error(e);
                    toast(auto ? 'Erro na busca profunda' : 'Erro na busca semântica');
                    resetSearchMode();
                    apply();
                }
                return [];
            } finally {
                if (startedToken === searchToken) {
                    btn.innerHTML = '<i class="bi bi-stars"></i>';
                    btn.disabled = false;
                }
            }
        }

        function scheduleDeepSearch(query, filteredCount) {
            clearTimeout(searchDebounce);
            if (query.length < 3 || filteredCount > 0) return;
            const tokenAtSchedule = searchToken + 1;
            searchDebounce = setTimeout(async () => {
                if (document.getElementById('search-box').value.trim().toLowerCase() !== query) return;
                if (searchMode !== 'filter') return;
                const currentFiltered = index.filter(c => {
                    const q = query;
                    const matchTxt = !q || c.summary.toLowerCase().includes(q);
                    const sk = document.getElementById('skill-filter').value;
                    const mach = document.getElementById('mach-filter').value;
                    const matchSk = (sk === 'all') || (c.skill === sk);
                    const matchIA = (srcFilter === 'all') || (c.source === srcFilter);
                    const matchMach = (mach === 'all') || (c.machine === mach);
                    return matchTxt && matchSk && matchIA && matchMach;
                });
                if (currentFiltered.length === 0 && tokenAtSchedule > searchToken) {
                    await runDeepSearch(query, {auto: true});
                }
            }, 250);
        }

        function apply() {
            const t = document.getElementById('search-box').value.toLowerCase();
            const sk = document.getElementById('skill-filter').value;
            const mach = document.getElementById('mach-filter').value;
            const filtered = index.filter(c => {
                const matchTxt = !t || c.summary.toLowerCase().includes(t);
                const matchSk = (sk === 'all') || (c.skill === sk);
                const matchIA = (srcFilter === 'all') || (c.source === srcFilter);
                const matchMach = (mach === 'all') || (c.machine === mach);
                return matchTxt && matchSk && matchIA && matchMach;
            });
            if (t) {
                if (filtered.length > 0) {
                    setModeBadge('mode-filter', `Filtro rápido — ${filtered.length} resultados`);
                } else {
                    setModeBadge('mode-filter', 'Filtro rápido zerado; aprofundando...');
                }
                scheduleDeepSearch(t.trim(), filtered.length);
            } else {
                clearTimeout(searchDebounce);
                resetSearchMode();
            }
            renderList(filtered, null);
        }

        function renderList(chats, query) {
            document.getElementById('chat-list').innerHTML = chats.map(c => {
                const skillInfo = c.skill && meta[c.skill] ? meta[c.skill] : null;
                const skillBadge = skillInfo ? `<span class="badge-skill">${skillInfo.icon} ${skillInfo.name}</span>` : '';
                let snippetHtml = '';
                if (query && c.snippet) {
                    const highlighted = escapeHtml(c.snippet).replace(
                        new RegExp('(' + escapeRegex(query) + ')', 'gi'),
                        '<span class="snippet-highlight">$1</span>'
                    );
                    snippetHtml = `<div class="chat-item-snippet">${highlighted}</div>`;
                }
                return `
                <div class="chat-item src-${c.source} ${currentChat && currentChat.uid === c.uid ? 'active' : ''}"
                     onclick="showChat('${c.uid}')" id="item-${c.uid.replace(/[^a-zA-Z0-9]/g,'_')}">
                    <div class="chat-item-meta">
                        <div class="chat-item-badges">
                            <span class="badge-src badge-${c.source}">${c.source}</span>
                            <span class="badge-src badge-${c.machine.toLowerCase()}">${c.machine}</span>
                            ${skillBadge}
                            <span class="chat-item-date">${c.date}</span>
                        </div>
                    </div>
                    <div class="chat-item-summary">${escapeHtml(c.summary)}</div>
                    ${snippetHtml}
                </div>`;
            }).join('');
        }

        function escapeHtml(t) {
            const d = document.createElement('div'); d.textContent = t; return d.innerHTML;
        }

        function escapeRegex(s) {
            return s.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
        }

        // ── SEMANTIC SEARCH ──────────────────────────────────────────────────

        async function doSemanticSearch() {
            const query = document.getElementById('search-box').value.trim();
            if (!query) { clearSearch(); return; }
            clearTimeout(searchDebounce);
            await runDeepSearch(query, {auto: false});
        }

        function clearSearch() {
            clearTimeout(searchDebounce);
            document.getElementById('search-box').value = '';
            resetSearchMode();
            apply();
        }

        // ── EVENTS ───────────────────────────────────────────────────────────

        document.getElementById('search-box').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                doSemanticSearch();
            } else if (e.key === 'Escape') {
                clearSearch();
            } else {
                // Typing: switch back to fast filter mode
                if (searchMode === 'semantic') {
                    resetSearchMode();
                }
            }
        });
        document.getElementById('search-box').oninput = function() {
            if (searchMode === 'filter') apply();
        };
        document.getElementById('skill-filter').onchange = apply;
        document.getElementById('mach-filter').onchange = apply;

        // ── CHAT DISPLAY ─────────────────────────────────────────────────────

        async function showChat(uid) {
            currentItem = index.find(i => i.uid === uid);
            // Also check semantic results
            if (!currentItem && semanticResults) {
                currentItem = semanticResults.find(i => i.uid === uid);
            }
            if (!currentItem) {
                currentItem = {uid: uid, source: '', sid: '', date: '', skill: null, summary: ''};
            }
            document.querySelectorAll('.chat-item').forEach(e => e.classList.remove('active'));
            const el = document.getElementById('item-'+uid.replace(/[^a-zA-Z0-9]/g,'_'));
            if (el) el.classList.add('active');

            const welcome = document.getElementById('welcome-msg');
            const container = document.getElementById('messages-container');
            const loading = document.getElementById('loading-overlay');
            const header = document.getElementById('chat-header');

            welcome.style.display = 'none';
            container.innerHTML = '';
            loading.style.display = 'block';

            try {
                const r = await fetch('/api/chat/' + uid);
                currentChat = await r.json();
                currentChat.uid = uid;
                loading.style.display = 'none';

                const sessionId = currentItem.sid || currentChat.sid || '';
                currentItem = {
                    ...currentItem,
                    sid: sessionId,
                    source: currentItem.source || currentChat.source || 'chat',
                    machine: currentItem.machine || currentChat.machine || '',
                    date: currentItem.date || currentChat.date || '',
                };
                const cmdMap = {
                    claude: `claude --resume ${sessionId}`,
                    gemini: `gemini --conversation ${sessionId}`,
                    codex: `codex resume ${sessionId}`,
                    deepseek: `deepseek --resume ${sessionId}`,
                    chatgpt: currentChat.url || `https://chatgpt.com/c/${sessionId}`
                };
                const cmd = sessionId ? (cmdMap[currentItem.source] || `${currentItem.source} ${sessionId}`) : `chat ${uid}`;
                document.getElementById('header-cmd').textContent = '> ' + cmd;
                document.getElementById('msg-count').textContent = currentChat.msgs.length + ' msgs';

                const skillEl = document.getElementById('header-skill');
                if (currentItem.skill && meta[currentItem.skill]) {
                    skillEl.textContent = meta[currentItem.skill].icon + ' ' + meta[currentItem.skill].name;
                    skillEl.style.display = 'inline';
                } else {
                    skillEl.style.display = 'none';
                }

                header.classList.add('visible');

                const src = currentItem.source;
                container.innerHTML = currentChat.msgs.map(m => {
                    const isUser = m.type === 'user';
                    const cls = isUser ? 'msg-user' : `msg-assistant from-${src}`;
                    const name = isUser ? 'User' : ({claude: 'Claude', gemini: 'Gemini', codex: 'Codex', deepseek: 'DeepSeek', chatgpt: 'ChatGPT'}[src] || src);
                    return `<div class="message ${cls}"><div class="msg-role">${name}</div>${marked.parse(m.content||'')}</div>`;
                }).join('');

                document.querySelectorAll('pre code').forEach(b => hljs.highlightElement(b));
                document.getElementById('chat-display').scrollTop = 0;
            } catch(e) {
                loading.style.display = 'none';
                container.innerHTML = '<p style="color:#ef4444;text-align:center;padding:40px;">Erro ao carregar conversa.</p>';
            }
        }

        function copyCmd() {
            const text = document.getElementById('header-cmd').textContent.replace('> ','');
            navigator.clipboard.writeText(text);
            toast('Comando copiado!');
        }

        // SHARE
        function openShare() { document.getElementById('share-modal').classList.add('show'); }
        function closeShare() { document.getElementById('share-modal').classList.remove('show'); }

        function shareAsHTML() {
            if (!currentChat || !currentItem) return;
            const src = currentItem.source;
            const date = currentItem.date;
            const skillInfo = currentItem.skill && meta[currentItem.skill] ? meta[currentItem.skill] : null;
            const skillLabel = skillInfo ? ` &mdash; ${skillInfo.icon} ${skillInfo.name}` : '';
            let msgs = currentChat.msgs.map(m => {
                const isUser = m.type === 'user';
                const name = isUser ? 'User' : ({claude: 'Claude', gemini: 'Gemini', codex: 'Codex', deepseek: 'DeepSeek', chatgpt: 'ChatGPT'}[src] || src);
                const bg = isUser ? '#1a2030' : '#111720';
                const border = isUser ? '#1e2a3a' : ({claude: '#f59e0b', gemini: '#3b82f6', codex: '#22c55e', deepseek: '#8b5cf6', chatgpt: '#10b981'}[src] || '#3b82f6');
                const content = (m.content||'').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                return `<div style="margin-bottom:16px;padding:16px 20px;border-radius:12px;max-width:88%;background:${bg};border:1px solid #1e2a3a;${isUser?'margin-left:auto;':'border-left:3px solid '+border+';'}">
                    <div style="font-size:0.65rem;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#5a6a7a;margin-bottom:8px;">${name}</div>
                    <div style="white-space:pre-wrap;word-wrap:break-word;">${content}</div>
                </div>`;
            }).join('');

            const html = `<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Chat ${src} - ${date}</title>
<style>body{background:#0a0e14;color:#d4dae4;font-family:-apple-system,system-ui,sans-serif;max-width:860px;margin:0 auto;padding:24px;line-height:1.7;font-size:0.9rem;}
pre{background:#010409;padding:14px;border-radius:8px;border:1px solid #1e2a3a;overflow-x:auto;}code{font-family:monospace;font-size:0.82rem;}
.hdr{text-align:center;padding:20px 0 30px;border-bottom:1px solid #1e2a3a;margin-bottom:24px;}
.hdr h2{font-size:1rem;color:#8899aa;font-weight:600;margin:0;letter-spacing:1px;}
.hdr small{color:#3a4a5a;font-size:0.7rem;}</style></head>
<body><div class="hdr"><h2>${src.toUpperCase()} SESSION${skillLabel}</h2><small>${date} &mdash; ${currentChat.msgs.length} mensagens</small></div>${msgs}
<div style="text-align:center;padding:24px;color:#6a7a8a;font-size:0.65rem;border-top:1px solid #1e2a3a;margin-top:24px;">Exported from Cockpit v6.2</div></body></html>`;

            const blob = new Blob([html], {type: 'text/html'});
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = `chat-${src}-${Date.now()}.html`;
            a.click();
            closeShare();
            toast('HTML exportado!');
        }

        function shareAsText() {
            if (!currentChat || !currentItem) return;
            const src = currentItem.source;
            let txt = currentChat.msgs.map(m => {
                const name = m.type === 'user' ? 'User' : ({claude: 'Claude', gemini: 'Gemini', codex: 'Codex', deepseek: 'DeepSeek', chatgpt: 'ChatGPT'}[src] || src);
                return `[${name}]\\n${m.content||''}`;
            }).join('\\n\\n---\\n\\n');
            navigator.clipboard.writeText(txt);
            closeShare();
            toast('Texto copiado para o clipboard!');
        }

        // DNA
        async function showDNA() {
            const welcome = document.getElementById('welcome-msg');
            const container = document.getElementById('messages-container');
            const loading = document.getElementById('loading-overlay');
            const header = document.getElementById('chat-header');

            welcome.style.display = 'none';
            header.classList.remove('visible');
            container.innerHTML = '';
            loading.style.display = 'block';
            currentChat = null;

            try {
                const r = await fetch('/api/memory/ludovico');
                const data = await r.json();
                loading.style.display = 'none';
                function formatDna(v) {
                    if (typeof v === 'string') return v;
                    if (Array.isArray(v)) {
                        return v.map(item => {
                            if (typeof item !== 'object') return `- ${item}`;
                            const lines = [];
                            if (item.name || item.type) lines.push(`**${item.name || item.type}**`);
                            if (item.description) lines.push(item.description);
                            if (item.example) lines.push(`> *${item.example}*`);
                            if (item.due_date) lines.push(`> Prazo: ${item.due_date}`);
                            if (item.due_dates) lines.push(`> Datas: ${Object.entries(item.due_dates).map(([k,v])=>`${k}: ${v}`).join(' | ')}`);
                            if (item.summary) lines.push(`**${String(item.date||'').slice(0,10)}** — ${item.summary}`);
                            return lines.join('\\n');
                        }).join('\\n\\n---\\n\\n');
                    }
                    if (typeof v === 'object') {
                        return Object.entries(v).map(([k, val]) => `**${k}:** ${val}`).join('\\n\\n');
                    }
                    return String(v);
                }
                let html = '<div class="dna-card"><h4><i class="bi bi-clipboard2-pulse"></i> MEMORY PROFILE</h4>';
                for (const [k, v] of Object.entries(data)) {
                    html += `<div class="dna-section"><strong>${k.toUpperCase()}</strong><div style="font-size:0.85rem;margin-top:4px;">${marked.parse(formatDna(v))}</div></div>`;
                }
                html += '</div>';
                container.innerHTML = html;
            } catch(e) {
                loading.style.display = 'none';
                container.innerHTML = '<p style="color:#ef4444;text-align:center;padding:40px;">DNA nao encontrado.</p>';
            }
        }

        async function showCore() {
            const welcome = document.getElementById('welcome-msg');
            const container = document.getElementById('messages-container');
            const loading = document.getElementById('loading-overlay');
            const header = document.getElementById('chat-header');

            welcome.style.display = 'none';
            header.classList.remove('visible');
            container.innerHTML = '';
            loading.style.display = 'block';
            currentChat = null;

            try {
                const [rc, rm] = await Promise.all([
                    fetch('/api/memory/core'),
                    fetch('/api/memory/memoria')
                ]);
                loading.style.display = 'none';
                const coreMd = rc.ok ? await rc.text() : null;
                const memMd  = rm.ok ? await rm.text() : null;
                if (!coreMd && !memMd) {
                    container.innerHTML = '<p style="color:#6a7a8a;text-align:center;padding:40px;">🐢 Memória ainda não gerada. Primeira consolidação roda hoje às 23:45.</p>';
                    return;
                }
                let html = '<div class="dna-card"><h4>🐢 INJECTED MEMORY CONTEXT</h4>';
                html += '<div style="font-size:0.7rem;color:#6a7a8a;margin:-6px 0 16px;">Dynamic assistant context: long-term core plus recent working memory. Static identity/profile data stays separate.</div>';
                html += '<div class="dna-section"><strong>📌 LONGO PRAZO &mdash; core (só muda por contradição)</strong>';
                html += `<div style="font-size:0.9rem;margin-top:6px;">${coreMd ? marked.parse(coreMd) : '<em style="color:#6a7a8a;">Núcleo ainda não gerado — roda 23:45.</em>'}</div></div>`;
                html += '<div class="dna-section" style="margin-top:16px;"><strong>🗓️ CURTO PRAZO &mdash; user-memory.md (janela 30 dias)</strong>';
                html += `<div style="font-size:0.9rem;margin-top:6px;">${memMd ? marked.parse(memMd) : '<em style="color:#6a7a8a;">Sem memória de curto prazo.</em>'}</div></div>`;
                html += '</div>';
                container.innerHTML = html;
            } catch(e) {
                loading.style.display = 'none';
                container.innerHTML = '<p style="color:#ef4444;text-align:center;padding:40px;">Erro ao carregar memória.</p>';
            }
        }

        function _classifyChatFallback(chat) {
            const t = ((chat.title || '') + ' ' + (chat.summary || '') + ' ' + (chat.long_summary || '')).toLowerCase();
            const rules = [
                ['Infra', /docker|vpn|wireguard|servidor|server|hass|home.?assistant|nginx|cloudflare|kubernetes|hardware|disco|hd |ssd |rede |firewall|proxmox/],
                ['MSP-Support', /glpi|chamado|suporte|atendimento|ticket|ITSM/],
                ['Business', /m365|office.?365|microsoft.?365|sharepoint|onedrive|proposta|prospecç|cliente|graal|casco|inpi|marca|venda|orçamento/],
                ['Trading', /análise técnica|fundamental|treemap|gráfico de ações|skacoes/],
                ['Finanças', /rdor3|trading|ações|b3 |stop.?loss|investimento|carteira|conta |fatura/],
                ['Saúde', /médic|farmác|sintoma|exame|remédio|medicament|cortic|dor /],
                ['IA-Tooling', /prompt|skill|mcp|claude code|gemini|agente|llm|cockpit|skippy/],
                ['Dev', /código|debug|script|git |github|refactor|bug|python|javascript|node|typescript/],
                ['Aprendizado', /tutorial|estudo|aprend|pesqu|docum/],
                ['Pessoal', /família|casa |compra|lazer|currículo|vaga |emprego/]
            ];
            const hits = [];
            for (const [name, rx] of rules) if (rx.test(t)) hits.push(name);
            return hits.length ? hits.slice(0, 2) : ['Outros'];
        }

        function _buildHeatmapData(entries) {
            // Para cada entry, garante categories nos chats e day_metrics
            const CATS = ['Infra','MSP-Support','Business','Dev','IA-Tooling','Trading','Finanças','Saúde','Aprendizado','Pessoal','Outros'];
            const present = new Set();
            const days = entries.map(e => {
                const counts = {};
                (e.chats || []).forEach(c => {
                    let cats = (c.categories && c.categories.length) ? c.categories : _classifyChatFallback(c);
                    cats.forEach(cat => {
                        counts[cat] = (counts[cat] || 0) + 1;
                        present.add(cat);
                    });
                });
                return { date: e.date, counts, total: (e.chats || []).length };
            });
            // ordena categorias pela ordem canônica, só as presentes
            const cats = CATS.filter(c => present.has(c));
            return { cats, days };
        }

        function _heatColor(intensity) {
            // 0..1 → teal escuro a teal claro
            if (intensity <= 0) return 'rgba(255,255,255,0.04)';
            const a = 0.15 + intensity * 0.7;
            return `rgba(20,184,166,${a.toFixed(2)})`;
        }

        function _jumpToDay(date) {
            const card = document.getElementById('daycard_' + date);
            const body = document.getElementById('daybody_' + date);
            if (!card) return;
            if (body) body.style.display = 'block';
            card.scrollIntoView({ behavior: 'smooth', block: 'center' });
            // flash highlight
            const orig = card.style.boxShadow;
            const origBorder = card.style.borderColor;
            card.style.boxShadow = '0 0 0 2px rgba(20,184,166,0.6), 0 0 24px rgba(20,184,166,0.35)';
            card.style.borderColor = 'rgba(20,184,166,0.5)';
            setTimeout(() => { card.style.boxShadow = orig; card.style.borderColor = origBorder; }, 1500);
        }

        async function showDailyAudit() {
            const welcome = document.getElementById('welcome-msg');
            const container = document.getElementById('messages-container');
            const loading = document.getElementById('loading-overlay');
            const header = document.getElementById('chat-header');

            welcome.style.display = 'none';
            header.classList.remove('visible');
            container.innerHTML = '';
            loading.style.display = 'block';
            currentChat = null;

            try {
                const r = await fetch('/api/memory/daily');
                const data = await r.json();
                loading.style.display = 'none';

                let html = '<div class="dna-card"><h4><i class="bi bi-grid-3x3-gap-fill"></i> DAILY PROTOPLASM AUDIT — DASHBOARD</h4><div style="margin-top:16px;">';

                if (!Array.isArray(data) || data.length === 0) {
                    html += '<p>Nenhuma auditoria encontrada.</p>';
                    container.innerHTML = html + '</div></div>';
                    return;
                }

                // === STATS GLOBAIS (últimos 14 dias) ===
                const totalSessions = data.reduce((s, e) => s + ((e.chats || []).length), 0);
                const switchesArr = data.map(e => (e.day_metrics && e.day_metrics.context_switches) || null).filter(x => x != null);
                const avgSwitches = switchesArr.length ? (switchesArr.reduce((a,b)=>a+b,0) / switchesArr.length).toFixed(1) : '—';
                const latestFocus = (data[0].day_metrics && data[0].day_metrics.focus_score != null) ? data[0].day_metrics.focus_score : '—';
                // tema dominante: contagem global de categorias
                const heat = _buildHeatmapData(data);
                const totalsByCat = {};
                heat.days.forEach(d => Object.entries(d.counts).forEach(([k,v]) => { totalsByCat[k] = (totalsByCat[k] || 0) + v; }));
                const dominantTheme = Object.entries(totalsByCat).sort((a,b)=>b[1]-a[1])[0];
                const dominantLabel = dominantTheme ? `${dominantTheme[0]} (${dominantTheme[1]})` : '—';

                // dia mais focado (maior focus_score)
                const focusedDay = data.filter(e => e.day_metrics && e.day_metrics.focus_score != null)
                                       .sort((a,b) => b.day_metrics.focus_score - a.day_metrics.focus_score)[0];
                const focusedLabel = focusedDay ? `${focusedDay.date} (${focusedDay.day_metrics.focus_score}/10)` : '—';

                html += `<div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:24px;">
                    <div style="background:rgba(20,184,166,0.08); border:1px solid rgba(20,184,166,0.25); border-radius:8px; padding:14px;">
                        <div style="color:#5eead4; font-size:0.7rem; letter-spacing:1.5px; text-transform:uppercase; margin-bottom:6px;">Sessões (${data.length}d)</div>
                        <div style="color:#fff; font-size:1.8rem; font-weight:700;">${totalSessions}</div>
                    </div>
                    <div style="background:rgba(139,92,246,0.08); border:1px solid rgba(139,92,246,0.25); border-radius:8px; padding:14px;">
                        <div style="color:#a78bfa; font-size:0.7rem; letter-spacing:1.5px; text-transform:uppercase; margin-bottom:6px;">Switches médio/dia</div>
                        <div style="color:#fff; font-size:1.8rem; font-weight:700;">${avgSwitches}</div>
                    </div>
                    <div style="background:rgba(251,191,36,0.06); border:1px solid rgba(251,191,36,0.25); border-radius:8px; padding:14px;">
                        <div style="color:#fbbf24; font-size:0.7rem; letter-spacing:1.5px; text-transform:uppercase; margin-bottom:6px;">Foco em ${data[0].date || '?'}</div>
                        <div style="color:#fff; font-size:1.8rem; font-weight:700;">${latestFocus}<span style="font-size:0.9rem; color:#9ca3af;">/10</span></div>
                    </div>
                    <div style="background:rgba(59,130,246,0.08); border:1px solid rgba(59,130,246,0.25); border-radius:8px; padding:14px;">
                        <div style="color:#60a5fa; font-size:0.7rem; letter-spacing:1.5px; text-transform:uppercase; margin-bottom:6px;">Tema dominante</div>
                        <div style="color:#fff; font-size:1.05rem; font-weight:700; line-height:1.3;">${dominantLabel}</div>
                    </div>
                    <div style="background:rgba(34,197,94,0.06); border:1px solid rgba(34,197,94,0.25); border-radius:8px; padding:14px;">
                        <div style="color:#4ade80; font-size:0.7rem; letter-spacing:1.5px; text-transform:uppercase; margin-bottom:6px;">Dia mais focado</div>
                        <div style="color:#fff; font-size:0.95rem; font-weight:700; line-height:1.3;">${focusedLabel}</div>
                    </div>
                </div>`;

                // === HEATMAP ===
                if (heat.cats.length > 0 && heat.days.length > 0) {
                    const maxCount = Math.max(1, ...heat.days.flatMap(d => Object.values(d.counts)));
                    // dias mais antigos à esquerda → mais recentes à direita
                    const sortedDays = [...heat.days].reverse();

                    html += `<div style="margin-bottom:24px;">
                        <div style="color:#9ca3af; font-size:0.75rem; letter-spacing:2px; text-transform:uppercase; margin-bottom:10px;"><i class="bi bi-grid-3x3"></i> Heatmap — categorias × últimos ${sortedDays.length} dias</div>
                        <div style="overflow-x:auto;">
                        <table style="border-collapse:separate; border-spacing:3px; font-size:0.78rem;">
                            <thead><tr>
                                <th style="text-align:right; padding-right:8px; color:#6b7280; font-weight:500; min-width:110px;"></th>`;
                    sortedDays.forEach(d => {
                        const dd = (d.date || '').slice(5); // MM-DD
                        html += `<th style="color:#6b7280; font-weight:500; padding:2px 4px; min-width:34px; font-size:0.7rem;">${dd}</th>`;
                    });
                    html += `</tr></thead><tbody>`;
                    heat.cats.forEach(cat => {
                        html += `<tr><td style="text-align:right; padding-right:8px; color:#d1d5db; white-space:nowrap; font-size:0.78rem;">${cat}</td>`;
                        sortedDays.forEach(d => {
                            const c = d.counts[cat] || 0;
                            const intensity = c / maxCount;
                            const color = _heatColor(intensity);
                            const text = c > 0 ? c : '';
                            const tip = c > 0 ? `${cat} — ${d.date}: ${c} sessão${c>1?'ões':''} (clique para abrir o dia)` : '';
                            const clickAttr = c > 0 ? ` onclick="_jumpToDay('${d.date}')" style="cursor:pointer; ` : ` style="`;
                            html += `<td title="${tip}"${clickAttr}background:${color}; width:30px; height:24px; text-align:center; border-radius:3px; color:${intensity>0.5?'#0f172a':'#9ca3af'}; font-weight:600; font-size:0.72rem;">${text}</td>`;
                        });
                        html += `</tr>`;
                    });
                    html += `</tbody></table></div></div>`;
                }

                // === LISTA DE DIAS (compacta, click expande Skippy) ===
                html += `<div style="color:#9ca3af; font-size:0.75rem; letter-spacing:2px; text-transform:uppercase; margin-bottom:10px;"><i class="bi bi-calendar3"></i> Histórico</div>`;
                data.forEach((entry, entryIdx) => {
                    const dayId = `daybody_${entry.date}`;
                    const cardId = `daycard_${entry.date}`;
                    const chatsId = `chats_${entry.date}`;
                    const focus = entry.day_metrics && entry.day_metrics.focus_score != null ? entry.day_metrics.focus_score : null;
                    const sw = entry.day_metrics && entry.day_metrics.context_switches != null ? entry.day_metrics.context_switches : null;
                    const focusBadge = focus != null ? `<span style="background:rgba(251,191,36,0.12); color:#fbbf24; padding:2px 8px; border-radius:10px; font-size:0.72rem; font-weight:600;" title="Focus score">⚡ ${focus}/10</span>` : '';
                    const switchBadge = sw != null ? `<span style="background:rgba(139,92,246,0.12); color:#a78bfa; padding:2px 8px; border-radius:10px; font-size:0.72rem; font-weight:600;" title="Context switches">⇄ ${sw}</span>` : '';

                    html += `<div id="${cardId}" data-day-card="${entry.date}" style="margin-bottom:10px; background:rgba(0,0,0,0.18); border:1px solid rgba(255,255,255,0.06); border-radius:6px; overflow:hidden; transition:box-shadow 0.4s, border-color 0.4s;">
                        <div onclick="const el=document.getElementById('${dayId}'); el.style.display = el.style.display==='none' ? 'block' : 'none';" style="cursor:pointer; padding:10px 14px; display:flex; justify-content:space-between; align-items:center; gap:10px;">
                            <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
                                <strong style="color:#e5e7eb; font-size:0.95rem;">${entry.date || '?'}</strong>
                                <span style="background:rgba(20,184,166,0.15); color:#2dd4bf; padding:2px 8px; border-radius:10px; font-size:0.72rem; font-weight:600;">${entry.hyperfocus || 'N/A'}</span>
                                ${focusBadge}
                                ${switchBadge}
                            </div>
                            <span style="color:#6b7280; font-size:0.78rem;">${(entry.chats || []).length} sessões <i class="bi bi-chevron-down"></i></span>
                        </div>
                        <div id="${dayId}" style="display:none; padding:0 14px 14px 14px; border-top:1px solid rgba(255,255,255,0.06);">`;

                    if (entry.headline) html += `<div style="font-size:1.05rem; font-weight:700; color:#fbbf24; margin:14px 0; line-height:1.4; font-style:italic;"><i class="bi bi-megaphone-fill" style="color:#f59e0b;"></i> ${entry.headline}</div>`;
                    if (entry.narrative) html += `<div style="background:rgba(20,184,166,0.05); border-left:3px solid #14b8a6; padding:12px 14px; margin-bottom:10px; line-height:1.6; color:#e5e7eb; font-size:0.92rem;">${entry.narrative}</div>`;
                    if (entry.pattern_insight) html += `<div style="background:rgba(139,92,246,0.08); border-left:3px solid #8b5cf6; padding:10px 14px; margin-bottom:10px; color:#ddd6fe; font-size:0.88rem; line-height:1.5;"><strong style="color:#a78bfa;"><i class="bi bi-eye"></i> Padrão:</strong> ${entry.pattern_insight}</div>`;
                    if (entry.fail_of_the_day) html += `<div style="background:rgba(239,68,68,0.06); border-left:3px solid #ef4444; padding:10px 14px; margin-bottom:10px; color:#fecaca; font-size:0.88rem; line-height:1.5;"><strong style="color:#f87171;"><i class="bi bi-bug-fill"></i> Fail:</strong> ${entry.fail_of_the_day}</div>`;
                    if (entry.elder_verdict) html += `<div style="background:linear-gradient(90deg,rgba(20,184,166,0.1),rgba(0,0,0,0.2)); border:1px solid rgba(20,184,166,0.3); padding:12px 14px; margin-bottom:14px; color:#5eead4; font-size:0.92rem; line-height:1.5; border-radius:4px;"><strong style="color:#2dd4bf;"><i class="bi bi-stars"></i> Veredito:</strong> <em>${entry.elder_verdict}</em></div>`;

                    if (entry.chats && entry.chats.length > 0) {
                        html += `<div style="margin-top:8px;">
                            <div style="cursor:pointer; user-select:none; color:#9ca3af; font-size:0.82rem; padding:6px 0;" onclick="const el=document.getElementById('${chatsId}'); el.style.display = el.style.display==='none' ? 'block' : 'none';">
                                <i class="bi bi-chevron-right"></i> drill-down: ${entry.chats.length} sessões
                            </div>
                            <ul id="${chatsId}" style="display:none; margin:6px 0 0 0; padding-left:0; list-style:none;">`;
                        entry.chats.forEach(chat => {
                            const safeUid = (chat.uid || '').replace(/[^a-zA-Z0-9]/g, '_') + Math.random().toString(36).substring(7);
                            const cats = (chat.categories || _classifyChatFallback(chat)).map(c => `<span style="background:rgba(255,255,255,0.05); color:#9ca3af; padding:1px 6px; border-radius:8px; font-size:0.68rem; margin-right:4px;">${c}</span>`).join('');
                            html += `<li style="margin-bottom:8px; padding:10px; background:rgba(0,0,0,0.25); border-radius:4px; border-left:2px solid #3b82f6;">
                                <div style="color:#93c5fd; font-weight:600; margin-bottom:4px; font-size:0.9rem;">${chat.title || 'Chat'} ${cats}</div>
                                <div style="font-size:0.83rem; color:#9ca3af; margin-bottom:6px;">${chat.summary || ''}</div>
                                <div style="display:none; line-height:1.5; color:#cbd5e1; margin-bottom:8px; padding:8px; background:rgba(0,0,0,0.3); border-radius:3px; font-size:0.83rem;" id="ls_${safeUid}">${chat.long_summary || ''}</div>
                                <button onclick="document.getElementById('ls_${safeUid}').style.display = document.getElementById('ls_${safeUid}').style.display==='none' ? 'block' : 'none';" style="background:rgba(255,255,255,0.05); color:#9ca3af; border:1px solid rgba(255,255,255,0.1); padding:3px 8px; border-radius:3px; font-size:0.72rem; cursor:pointer; margin-right:6px;"><i class="bi bi-arrows-expand"></i> resumo</button>
                                <button onclick="showChat('${chat.uid}')" style="background:#3b82f6; color:white; border:none; padding:3px 8px; border-radius:3px; font-size:0.72rem; cursor:pointer;"><i class="bi bi-chat-left-text"></i> abrir chat</button>
                            </li>`;
                        });
                        html += `</ul></div>`;
                    } else if (entry.summary && entry.summary.length > 0) {
                        html += `<ul style="margin:8px 0 0 0; padding-left:20px; font-size:0.88rem; color:#d1d5db;">`;
                        entry.summary.forEach(p => { html += `<li style="margin-bottom:4px;">${p}</li>`; });
                        html += `</ul>`;
                    }

                    html += `</div></div>`;
                });

                html += '</div></div>';
                container.innerHTML = html;
                return;
            } catch(e) {
                loading.style.display = 'none';
                container.innerHTML = '<p style="color:#ef4444;text-align:center;padding:40px;">Erro ao carregar auditoria diária.</p>';
                return;
            }
        }


        function toast(msg) {
            const t = document.getElementById('toast');
            t.textContent = msg;
            t.classList.add('show');
            setTimeout(() => t.classList.remove('show'), 2000);
        }

        document.getElementById('share-modal').addEventListener('click', function(e) { if (e.target === this) closeShare(); });

        load();
    </script>
</body>
</html>"""

if __name__ == "__main__":
    load_embed_index()
    threading.Thread(target=index_worker, daemon=True).start()
    print(f"--- COCKPIT v{APP_VERSION} STARTING ON PORT {PORT} ---")
    print(f"--- Embeddings: {'ENABLED via Gemini gemini-embedding-001' if GEMINI_API_KEY else 'DISABLED — set GEMINI_API_KEY'} ---")
    server_address = ('0.0.0.0', PORT)
    httpd = socketserver.ThreadingTCPServer(server_address, HistoryHandler)
    httpd.serve_forever()
