"""Active pending items ("Pendências ativas") from user-memory.md, managed from chat.

    pendencias.py               numbered list (the same list injected into the assistant)
    pendencias.py mata 2 5      kill items 2 and 5, rewrite the section and list the rest

A killed item goes to PENDENCIAS_MORTAS_FILE and build_pendencias_block ignores it from
then on (including rewordings of it, by word similarity or the same ticket ID), also in
the nightly audit run. The `mata` ("kill") subcommand keeps its production name.
See docs/MEMORY-PIPELINE.md, "Active pending items".
"""
import json
import re
import sys
from datetime import datetime

import daily_auditor as d

MEMORY_FILE = d.os.path.join(d.DATA_DIR, "ai_config", "user-memory.md")


def _history():
    with open(d.OUTPUT_FILE, encoding="utf-8") as f:
        return json.load(f)


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _rewrite_section(history):
    """Regenerate the pending block inside user-memory.md without calling the LLM."""
    try:
        with open(MEMORY_FILE, encoding="utf-8") as f:
            md = f.read()
    except FileNotFoundError:
        return
    block = d.build_pendencias_block(history, _today())
    md = re.sub(r"(?ms)^## Pend[eê]ncias[^\n]*\n.*?(?=^## |\Z)", "", md).strip()
    anchor = "## Prontuário psicológico"
    md = md.replace(anchor, block + "\n\n" + anchor, 1) if anchor in md else md + "\n\n" + block
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        f.write(md)


def main(argv):
    history = _history()
    items = d.build_pendencias_block(history, _today(), itens=True)

    if argv[:1] == ["mata"]:
        try:
            nums = sorted({int(x) for x in argv[1:]})
        except ValueError:
            sys.exit("usage: pendencias.py mata N [N...]")
        if not nums or any(n < 1 or n > len(items) for n in nums):
            sys.exit(f"number out of range (1..{len(items)})")
        try:
            with open(d.PENDENCIAS_MORTAS_FILE, encoding="utf-8") as f:
                killed = json.load(f)
        except Exception:
            killed = []
        for n in nums:
            # Keys "texto"/"morta_em" are the on-disk format shared with production.
            killed.append({"texto": items[n - 1], "morta_em": _today()})
            print(f"KILLED {n}: {items[n - 1][:100]}")
        with open(d.PENDENCIAS_MORTAS_FILE, "w", encoding="utf-8") as f:
            json.dump(killed, f, ensure_ascii=False, indent=1)
        _rewrite_section(history)
        items = d.build_pendencias_block(history, _today(), itens=True)
        print()

    if not items:
        print("No active pending items.")
    for i, t in enumerate(items, 1):
        print(f"{i}. {t}")


if __name__ == "__main__":
    main(sys.argv[1:])
