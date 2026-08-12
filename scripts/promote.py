#!/usr/bin/env python3
"""Promove o código de PRODUÇÃO para o mirror OSS deste repositório, sanitizado.

Direção ÚNICA: produção -> repo. A produção é a fonte da verdade do código; este
repositório é um artefato gerado. Não existe caminho inverso, e é de propósito:
tentar "deployar o repo" já causou o drift que este script existe para matar
(em 11/08/2026 o repo estava 3 semanas atrás e não tinha o catch-up por data —
publicar o repo por cima teria REGREDIDO a produção).

Uso:
    python3 scripts/promote.py            # mostra o diff, não escreve nada
    python3 scripts/promote.py --write    # aplica no repo (revisar e commitar à mão)

Requer `ssh h46` funcionando (ver docs/MEMORY-PIPELINE.md).
"""
import argparse
import difflib
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REMOTE = os.environ.get("COCKPIT_HOST", "h46")
REMOTE_APP = "/dados/dockers/cockpit/app"

# produção -> repo. Só arquivos de código; NUNCA docker-compose.yml (tem chave
# de API inline) nem nada sob data/.
FILES = {
    "cockpit_v2_docker.py": "app/cockpit.py",
    "daily_auditor.py": "app/daily_auditor.py",
    "ludovico_distiller.py": "app/memory_distiller.py",
    "weekly_distiller.py": "app/weekly_digest.py",
}

# Ordem importa: as entradas mais longas primeiro, senão "André" come "do André"
# e produz o famoso "dthe user" que ficou publicado no GitHub por três semanas.
SUBS = [
    # Identidade -> placeholders parametrizáveis (os prompts são f-strings, então
    # {USER_NAME} e {PERSONA_NAME} interpolam de verdade em runtime).
    (r"\bdo André\b", "do {USER_NAME}"),
    (r"\bdthe André\b", "do {USER_NAME}"),
    (r"\bAndré\b", "{USER_NAME}"),
    (r"\bSKIPPY THE MAGNIFICENT\b", "{PERSONA_NAME}"),
    (r"\bSkippy\b", "{PERSONA_NAME}"),
    # Nomes de arquivo do André -> nomes neutros (a produção tem symlinks das duas formas)
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
    # Rede/infra reais -> placeholders
    (r"192\.168\.\d+\.\d+(:\d+)?", "${INTERNAL_HOST}"),
    (r"\bcraneww0?\b", "${CLIENT_NAME}"),
    (r"\bCrane Worldwide\b", "${CLIENT_NAME}"),
    (r"\bCasco Digital\b", "${COMPANY_NAME}"),
    # Config hardcoded -> env (o repo precisa rodar fora do .46)
    (r'^DATA_DIR = "/app/data"$',
     'DATA_DIR = os.environ.get("DATA_DIR", "/app/data")'),
    (r'd\.get\("skill"\) != "skpsi"', 'd.get("skill") != MEMORY_SKILL'),
]

# Cabeçalho injetado logo após os imports quando o arquivo usa os placeholders.
HEADER = (
    '\n# Identidade injetada por scripts/promote.py — o mirror OSS não carrega\n'
    '# nome de pessoa nem persona hardcoded.\n'
    'USER_NAME = os.environ.get("USER_NAME", "the user")\n'
    'PERSONA_NAME = os.environ.get("PERSONA_NAME", "the auditor")\n'
    'MEMORY_SKILL = os.environ.get("MEMORY_SKILL", "").strip()\n'
)

# Se qualquer um destes sobreviver à sanitização, aborta: é vazamento de segredo.
LEAKS = [
    (r"sk-[A-Za-z0-9]{16,}", "chave OpenAI/DeepSeek"),
    (r"AIza[A-Za-z0-9_\-]{20,}", "chave Google/Gemini"),
    (r"ghp_[A-Za-z0-9]{20,}", "token GitHub"),
    (r"KKvvrr\w*", "senha operacional"),
    (r"\bAndré\b", "nome real não sanitizado"),
    (r"192\.168\.", "IP interno não sanitizado"),
]


def fetch(remote_name):
    out = subprocess.run(["ssh", REMOTE, f"cat {REMOTE_APP}/{remote_name}"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"ERRO: não consegui ler {remote_name} em {REMOTE}: {out.stderr.strip()}")
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="aplicar no repo")
    args = ap.parse_args()

    staged, leaks = {}, []
    for remote_name, repo_path in FILES.items():
        clean = sanitize(fetch(remote_name))
        leaks += check_leaks(clean, repo_path)
        staged[repo_path] = clean

    if leaks:
        print("ABORTADO — sanitização incompleta, isto iria para um repo PÚBLICO:")
        print("\n".join(leaks))
        sys.exit(1)

    changed = 0
    for repo_path, clean in staged.items():
        full = os.path.join(REPO, repo_path)
        old = open(full, encoding="utf-8").read() if os.path.exists(full) else ""
        if old == clean:
            print(f"= {repo_path} (sem mudança)")
            continue
        changed += 1
        diff = list(difflib.unified_diff(old.split("\n"), clean.split("\n"),
                                         f"repo/{repo_path}", f"prod/{repo_path}", lineterm=""))
        print(f"\n~ {repo_path}: {sum(1 for l in diff if l.startswith('+') and not l.startswith('+++'))} linhas novas, "
              f"{sum(1 for l in diff if l.startswith('-') and not l.startswith('---'))} removidas")
        if args.write:
            with open(full, "w", encoding="utf-8") as f:
                f.write(clean)

    if not args.write:
        print(f"\n[dry-run] {changed} arquivo(s) mudariam. Rode com --write para aplicar.")
    else:
        print(f"\n{changed} arquivo(s) atualizados. Revise com `git diff` e commite à mão.")


if __name__ == "__main__":
    main()
