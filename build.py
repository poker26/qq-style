#!/usr/bin/env python3
"""Сборка SKILL.md и PROMPT.md из шаблонов и rules.toml.

Списки запретов в скилле и в промпте берутся из тех же правил, что читает
check_texts.py, поэтому три формата не расходятся. Запуск: python build.py
"""
from __future__ import annotations

import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent


def ban_table(rules: list[dict]) -> str:
    """Читаемый список запретов: раздел, суть, примеры оборотов."""
    lines = []
    for b in rules:
        ex = [p for p in b["patterns"] if not any(ch in p for ch in "\\[]()|^$?+*")]
        ex_s = ", ".join(f"«{e}»" for e in ex[:5])
        tail = f" Примеры: {ex_s}." if ex_s else ""
        lines.append(f"- **{b['id']}** ({b.get('section', '')}): {b['why']}.{tail}")
    return "\n".join(lines)


def main() -> None:
    data = tomllib.loads((HERE / "rules.toml").read_text(encoding="utf-8"))
    bans = ban_table(data["ban"])
    words = data["thresholds"]["min_words_per_code_cell"]
    for name in ("SKILL", "PROMPT"):
        tpl = (HERE / "templates" / f"{name}.template.md").read_text(encoding="utf-8")
        out = tpl.replace("{{BANS}}", bans).replace("{{MIN_WORDS}}", str(words))
        target = HERE / f"{name}.md"
        target.write_text(out, encoding="utf-8")
        print(f"собран {target.name}: {len(out)} знаков")


if __name__ == "__main__":
    main()
