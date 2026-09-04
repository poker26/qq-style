#!/usr/bin/env python3
"""Проверка русского объясняющего текста по канону qq-style.

Правила живут в rules.toml (общий слой) и в словаре проекта (glossary-*.toml,
подключается флагом). Скрипт не зависит от языковой модели и не требует
сторонних пакетов: Python 3.11 или новее.

Что проверяется:
  * запрещённые обороты из rules.toml и словаря проекта;
  * тире в роли связки (исключения: диапазоны, формулы, фамилии, таблицы);
  * объём пояснения перед кодовой ячейкой блокнота (порог из rules.toml).

Что читается:
  * .md, .txt: живой текст без блоков кода;
  * .ipynb: markdown-ячейки, плюс порог слов перед каждой кодовой;
  * .py: строковые литералы (тексты ошибок, подписи), с --docstrings ещё и докстроки;
  * .html: текст между тегами.

Запуск:
  python check_texts.py content/lessons                 # общий слой
  python check_texts.py --glossary glossary-qq.toml .  # плюс словарь проекта
  python check_texts.py --report ...                    # сводка по правилам
  python check_texts.py --only meta-leaks,dash ...      # часть правил

Пустой вывод и код 0 означают, что текст можно показывать людям.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
FENCE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]*`")
MATH = re.compile(r"\$[^$\n]*\$")
LINK_TARGET = re.compile(r"\]\([^)\n]*\)")
DASH = re.compile(r"[—–]")


class Rule:
    def __init__(self, rid: str, why: str, patterns: list[str], section: str = "", prose_only: bool = False) -> None:
        self.id = rid
        self.why = why
        self.section = section
        # prose_only: правило про типографику, действует на md/txt/ipynb, где текст
        # это текст; в строках Python и HTML лежит встроенный код, там оно шумит.
        self.prose_only = prose_only
        self.res = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in patterns]

    def hits(self, text: str) -> list[str]:
        out = []
        for r in self.res:
            for m in r.finditer(text):
                out.append(m.group(0))
        return out


def load_rules(path: Path) -> tuple[list[Rule], list[re.Pattern[str]], int]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    rules = [Rule(b["id"], b["why"], b["patterns"], b.get("section", ""), b.get("prose_only", False)) for b in data.get("ban", [])]
    allow = [re.compile(p) for p in data.get("dash", {}).get("allow", [])]
    words = int(data.get("thresholds", {}).get("min_words_per_code_cell", 0))
    return rules, allow, words


def load_glossary(path: Path) -> list[Rule]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return [Rule(b["id"], b["why"], b["patterns"], "3", b.get("prose_only", False)) for b in data.get("ban", [])]


PROSE_SUFFIXES = {".md", ".txt", ".ipynb"}
JINJA = re.compile(r"\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}", re.DOTALL)


# ─────────────────────────── извлечение текста ───────────────────────────

def prose_lines(markdown: str) -> list[tuple[int, str]]:
    """Строки живого текста с номерами: без блоков кода и служебных строк."""
    out: list[tuple[int, str]] = []
    fence = False
    for n, line in enumerate(markdown.splitlines(), 1):
        if line.strip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        s = line.strip()
        if not s or s.startswith("<!--") or re.match(r"^[a-z_]+:\s", s):
            continue  # маркеры блоков и директивы (title:, row:, stage:)
        # Инлайн-код, ссылки и формулы это не проза: кавычки и пробелы там законны.
        clean = INLINE_CODE.sub("код", line)
        clean = MATH.sub("формула", clean)
        clean = LINK_TARGET.sub(")", clean)
        clean = re.sub(r"[ \t]+$", "", clean)
        if clean.lstrip().startswith("|"):
            clean = re.sub(r" {2,}", " ", clean)  # выравнивание таблиц пробелами законно
        if clean.strip():
            out.append((n, clean))
    return out


def prose_words(markdown: str) -> int:
    body = FENCE.sub("", markdown)
    keep = [ln for ln in body.splitlines()
            if ln.strip() and not ln.lstrip().startswith(("#", "|", "-", "*", ">"))
            and not re.match(r"^\s*\d+\.", ln)]
    return len(" ".join(keep).split())


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "pre", "code"):
            self.skip += 1
        for k, v in attrs:
            if k in ("title", "aria-label", "placeholder", "alt") and v:
                self.parts.append(v)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "pre", "code") and self.skip:
            self.skip -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip and data.strip():
            self.parts.append(data.strip())


def texts_of(path: Path, docstrings: bool) -> list[tuple[int, str]]:
    """Единицы текста файла с номером строки (для блокнотов номер ячейки)."""
    suf = path.suffix.lower()
    if suf in (".md", ".txt"):
        return prose_lines(path.read_text(encoding="utf-8", errors="replace"))
    if suf == ".ipynb":
        nb = json.loads(path.read_text(encoding="utf-8"))
        out = []
        for i, cell in enumerate(nb.get("cells", []), 1):
            if cell.get("cell_type") == "markdown":
                src = "".join(cell.get("source", []))
                out.extend((i, ln) for _, ln in prose_lines(src))
        return out
    if suf == ".py":
        out = []
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            return out
        doc_nodes = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                    doc_nodes.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in doc_nodes and not docstrings:
                    continue
                if re.search(r"[А-Яа-яЁё]", node.value):
                    for ln in node.value.splitlines():
                        if ln.strip():
                            out.append((node.lineno, ln))
        return out
    if suf in (".html", ".htm"):
        p = _Text()
        p.feed(JINJA.sub(" ", path.read_text(encoding="utf-8", errors="replace")))
        return [(0, t) for t in p.parts if re.search(r"[А-Яа-яЁё]", t)]
    return []


def thin_cells(path: Path, min_words: int) -> list[str]:
    """Блокнот: слова markdown между кодовыми ячейками делятся между ними."""
    if path.suffix.lower() != ".ipynb" or not min_words:
        return []
    nb = json.loads(path.read_text(encoding="utf-8"))
    out = []
    buf = 0
    pending: list[int] = []
    for i, cell in enumerate(nb.get("cells", []), 1):
        if cell.get("cell_type") == "markdown":
            if pending:
                share = buf / len(pending)
                out.extend(f"ячейка {c}: {share:.0f} слов пояснения" for c in pending if share < min_words)
                pending, buf = [], 0
            buf += prose_words("".join(cell.get("source", [])))
        elif cell.get("cell_type") == "code" and "".join(cell.get("source", [])).strip():
            pending.append(i)
    if pending:
        share = buf / len(pending)
        out.extend(f"ячейка {c}: {share:.0f} слов пояснения" for c in pending if share < min_words)
    return out


# ─────────────────────────── проверка ───────────────────────────

def dash_ok(line: str, allow: list[re.Pattern[str]]) -> bool:
    return any(a.search(line) for a in allow)


def collect(paths: list[Path]) -> list[Path]:
    exts = {".md", ".txt", ".ipynb", ".py", ".html", ".htm"}
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(f for f in sorted(p.rglob("*")) if f.suffix.lower() in exts and "node_modules" not in f.parts and ".git" not in f.parts)
        elif p.exists():
            files.append(p)
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка текста по канону qq-style")
    ap.add_argument("paths", nargs="+", help="файлы или каталоги")
    ap.add_argument("--rules", default=str(HERE / "rules.toml"))
    ap.add_argument("--glossary", action="append", default=[], help="словарь проекта (toml), можно несколько")
    ap.add_argument("--only", default="", help="проверять только эти правила (id через запятую; dash и thin тоже id)")
    ap.add_argument("--skip", default="", help="не проверять эти правила")
    ap.add_argument("--docstrings", action="store_true", help="в .py проверять и докстроки")
    ap.add_argument("--report", action="store_true", help="сводка по правилам вместо построчного вывода")
    ap.add_argument("--max", type=int, default=0, help="показать не больше N находок на файл (0 = все)")
    a = ap.parse_args()
    # Консоль Windows падает на символах вне своей кодировки: печатаем с заменой.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    rules, allow, min_words = load_rules(Path(a.rules))
    for g in a.glossary:
        rules.extend(load_glossary(Path(g)))
    only = {s.strip() for s in a.only.split(",") if s.strip()}
    skip = {s.strip() for s in a.skip.split(",") if s.strip()}
    active = [r for r in rules if (not only or r.id in only) and r.id not in skip]
    check_dash = (not only or "dash" in only) and "dash" not in skip
    check_thin = (not only or "thin" in only) and "thin" not in skip

    total = 0
    by_rule: dict[str, int] = {}
    for f in collect([Path(p) for p in a.paths]):
        found: list[str] = []
        is_prose = f.suffix.lower() in PROSE_SUFFIXES
        for line_no, text in texts_of(f, a.docstrings):
            for r in active:
                if r.prose_only and not is_prose:
                    continue
                for h in r.hits(text):
                    found.append(f"{f}:{line_no}: [{r.id}] «{h[:60]}» → {r.why}")
                    by_rule[r.id] = by_rule.get(r.id, 0) + 1
            if check_dash and DASH.search(text) and not dash_ok(text, allow):
                found.append(f"{f}:{line_no}: [dash] тире в роли связки: {text.strip()[:100]}")
                by_rule["dash"] = by_rule.get("dash", 0) + 1
        if check_thin:
            for t in thin_cells(f, min_words):
                found.append(f"{f}: [thin] {t} (порог {min_words})")
                by_rule["thin"] = by_rule.get("thin", 0) + 1
        total += len(found)
        if not a.report:
            try:
                for line in (found[: a.max] if a.max else found):
                    print(line)
                if a.max and len(found) > a.max:
                    print(f"{f}: … ещё {len(found) - a.max}")
            except (BrokenPipeError, OSError):
                return 1  # читатель закрыл канал (например, head): находки были
    if a.report:
        for rid, n in sorted(by_rule.items(), key=lambda kv: -kv[1]):
            print(f"{n:6d}  {rid}")
        print(f"{total:6d}  всего")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
