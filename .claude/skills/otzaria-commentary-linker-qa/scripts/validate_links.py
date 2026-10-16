#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic QA for Otzaria commentary `_links.json` files.

Checks: JSON/schema, duplicates, completeness vs citing text, file-derived heRef
(for primary target), optional seforim.db heRef sampling across all path_2 titles,
super-commentary attribution scan (רש"י/תוס'/בד"ה → not Gemara).

Does NOT fully judge semantic match quality — that remains LLM/manual per SKILL.md.

Example:
  python validate_links.py \\
    --links path/to/X_links.json \\
    --citing path/to/X.txt \\
    --target path/to/Y.txt \\
    --skip-line 2 \\
    --db "%APPDATA%/io.github.kdroidfilter.seforimapp/databases/seforim.db"

Batch / multi-path tip: pass the Gemara as --target; super_commentary rows may point
at רש"י/תוספות — those are allowed and checked via --scan-super + DB sample.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from connection_type import canonical_connection_type, kt_trim as _kt_trim  # noqa: E402

EXPECTED_KEYS = {"line_index_1", "line_index_2", "heRef_2", "path_2", "Conection Type"}
# Lateral (non base/dependant) references. Written under their own name from either side,
# never rewritten to "source" — the flip would be meaningless for them. (ein_mishpat is
# NOT here: the generator treats it as an oriented dependant type, see below.)
LATERAL_TYPES = {
    "reference", "quotation", "mesorat_hashas", "mishnah_in_talmud",
    "related", "other", "sifrei_mitzvot", "essay", "allusion", "liturgy", "law", "summary",
}
HEADER_RE = re.compile(r"^<h([1-6])>(.*?)</h\1>\s*$", re.I)
# How SeforimLibrary's generator (otzariasqlite/Generator.kt, ~L71-81, 249-360,
# 2013-2030) stores an entry of a links file named after book S whose path_2 is book T:
#   * "source"                    -> always flipped, stored COMMENTARY (T -> S).
#   * ORIENTED type (below), T.isBaseBook and not S.isBaseBook:
#         title of S declares T ("<X> AL <T>")  -> flipped, stored as its own type
#         otherwise                              -> stored OTHER (lost from the panel)
#   * ORIENTED type, any other base-book combination -> stored as-is, S -> T, i.e. S is
#     the base. Correct for a base-named file; backwards for a citing-named one.
# isBaseBook = the book is in the generator's priority.txt (Otzaria) / Sefaria's base
# list; the reliable source is `book.isBaseBook` in a built seforim.db (--db).
# Policy here stays stricter than the generator for commentary/super_commentary in a
# citing-named file: 'source' is canonical, the old values are a link_direction blocker
# even where the title heuristic would happen to rescue them.
ORIENTED_TYPES = {
    "commentary", "super_commentary", "targum", "midrash", "parshanut",
    "dibur_hamatchil", "ein_mishpat", "elucidation", "footnotes",
}
ORIENTED_OTHER_TYPES = ORIENTED_TYPES - {"commentary", "super_commentary"}
# Dependant types Kotlin knows but never flips / re-types (stored exactly as declared).
AS_IS_DEPENDANT_TYPES = {"explication"}
_DEP_PREFIXES = (   # DEPENDENCY_TITLE_PREFIXES, same order
    "\u05ea\u05dc\u05de\u05d5\u05d3 \u05d1\u05d1\u05dc\u05d9 ",
    "\u05ea\u05dc\u05de\u05d5\u05d3 \u05d9\u05e8\u05d5\u05e9\u05dc\u05de\u05d9 ",
    "\u05de\u05e1\u05db\u05ea ", "\u05de\u05e9\u05e0\u05d4 ", "\u05e1\u05e4\u05e8 ",
)
_ON = " \u05e2\u05dc "
# Java's \s (no UNICODE_CHARACTER_CLASS) is ASCII-only: NBSP is NOT whitespace there.
_JAVA_WS = re.compile(r"[ \t\n\x0b\f\r]+")
_CORPUS_SUFFIX = re.compile(
    r"[ \t\n\x0b\f\r]+\u05e2\u05dc[ \t\n\x0b\f\r]+(?:"
    "\u05d4\u05ea\u05e0\"\u05da|\u05d4\u05ea\u05d5\u05e8\u05d4|\u05d4\u05ea\u05dc\u05de\u05d5\u05d3|"
    "\u05d4\u05de\u05e9\u05e0\u05d4|\u05ea\u05e0\u05da|\u05ea\u05d5\u05e8\u05d4|"
    "\u05ea\u05dc\u05de\u05d5\u05d3|\u05de\u05e9\u05e0\u05d4)$",
    re.I,
)


def normalize_hebrew_label(raw: str) -> str:
    """Port of Generator.normalizeHebrewLabel."""
    s = _kt_trim(raw)
    s = s.replace("“", '"').replace("”", '"')
    s = s.replace("‘", "'").replace("’", "'")
    s = s.replace('"', "\u05f4")
    s = s.replace("''", "\u05f4")
    s = s.replace("\u05f3\u05f3", "\u05f4")
    s = s.replace("`", "\u05f3")
    return _kt_trim(_JAVA_WS.sub(" ", s))


def comparable_label(raw: str) -> str:
    """Port of Generator.comparableLabel (incl. stripCorpusSuffix)."""
    base = normalize_hebrew_label(raw)
    for ch in ("\u05f4", '"', "\u05f3", "'"):
        base = base.replace(ch, "")
    base = _kt_trim(_JAVA_WS.sub(" ", base))
    return _kt_trim(_CORPUS_SUFFIX.sub("", base))


def _dep_key(raw: str) -> str:
    v = comparable_label(raw)
    while True:
        pre = next((p for p in _DEP_PREFIXES if v.startswith(p)), None)
        if pre is None:
            return v
        v = _kt_trim(v[len(pre):])


def title_declares_dependency_on(dependant: str, base: str) -> bool:
    """Port of Generator.titleDeclaresDependencyOn."""
    norm = comparable_label(dependant or "")
    i = norm.rfind(_ON)
    if i < 0:
        return False
    declared = norm[i + len(_ON):]
    if not _kt_trim(declared):
        return False
    return _dep_key(declared) == _dep_key(base or "")


def default_priority_lists() -> list:
    """The generator's priority.txt files, if SeforimLibrary is checked out next to
    this repo (.../otzaria-books/SeforimLibrary)."""
    here = Path(__file__).resolve()
    if len(here.parents) < 5:
        return []
    sl = here.parents[4].parent / "SeforimLibrary" / "generator"
    return [q for q in (sl / "otzariasqlite/src/commonMain/resources/priority.txt",
                        sl / "sefariasqlite/src/jvmMain/resources/priority.txt") if q.is_file()]


def load_priority_titles(paths) -> set:
    out = set()
    for q in paths:
        if not Path(q).is_file():
            print(f"WARNING: --priority-list {q} not found — ignored", file=sys.stderr)
            continue
        for line in Path(q).read_text(encoding="utf-8-sig").splitlines():
            line = line.strip().replace("\\", "/")
            if line and not line.startswith("#") and line.lower().endswith(".txt"):
                out.add(comparable_label(line.rsplit("/", 1)[-1][:-4]))
    return out


class BaseBookOracle:
    """isBaseBook lookup: seforim.db first (authoritative), then priority.txt for
    titles the DB lacks, else None (unknown)."""

    def __init__(self, db_path, priority_paths):
        self.conn = None
        if db_path and Path(db_path).is_file():
            self.conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
        self.priority = load_priority_titles(priority_paths) if priority_paths else set()
        self.source = "+".join(x for x, on in (("db", self.conn), ("priority.txt", self.priority)) if on) or None

    def is_base(self, title: str):
        if self.conn is not None:
            for t in title_variants(title):
                row = self.conn.execute(
                    "SELECT isBaseBook FROM book WHERE title = ? LIMIT 1", (t,)).fetchone()
                if row is not None:
                    return bool(row[0])
            # not in the DB (e.g. a notes book merged inline): fall back to priority.txt
        if self.priority:
            return comparable_label(title) in self.priority
        return None

    def close(self):
        if self.conn is not None:
            self.conn.close()


def generator_target_title(path2: str) -> str:
    """Target title exactly as Generator.kt derives it from path_2 (~L1975): the last
    component after a backslash, else Paths.get(path).fileName, then everything before
    the last '.' (the whole name when there is none). Folder parts never survive."""
    path2 = path2 or ""
    last = path2.split("\\")[-1] if "\\" in path2 else PurePosixPath(path2).name  # like Path.fileName: ignores a trailing /
    return last.rsplit(".", 1)[0] if "." in last else last


def classify_orientation(ctype, s_title, t_title, oracle):
    """-> (outcome, severity, summary), mirroring Generator.kt for an ORIENTED type."""
    declared = title_declares_dependency_on(s_title, t_title)
    if ctype not in ORIENTED_TYPES:
        # e.g. explication: a valid dependant type the generator never flips and never
        # re-types (not in ORIENTED_DEPENDANT_TYPES) -> always stored as-is.
        if declared:
            return ("as_is", "major",
                    f"{ctype!r} -> {t_title!r}: never flipped by the generator, so "
                    f"{s_title!r} is stored as the BASE although its title names "
                    f"{t_title!r} — backwards; use 'source'")
        return ("as_is", "minor",
                f"{ctype!r} -> {t_title!r}: never flipped by the generator — stored with "
                f"{s_title!r} as the base (right only for a base-named file)")
    t_base, s_base = oracle.is_base(t_title), oracle.is_base(s_title)
    if t_base is None or s_base is None:
        return ("unverifiable", "minor",
                f"cannot tell how the generator stores {ctype!r} -> {t_title!r}: isBaseBook "
                f"unknown (target={t_base}, citing={s_base}; pass --db). Title "
                f"{'names' if declared else 'does NOT name'} the target")
    if t_base and not s_base:
        if declared:
            return ("flipped", "info",
                    f"{ctype!r} -> base {t_title!r}: generator flips it (title names the base)")
        return ("stored_other", "major",
                f"{ctype!r} -> base {t_title!r} from a non-base book whose title does not "
                f"name it: stored as OTHER, never shown as a commentary — use 'source'")
    if declared:
        # The named book's own title says it depends on path_2, yet it is stored as
        # the base of path_2: backwards for certain.
        return ("as_is", "major",
                f"{ctype!r} -> {t_title!r}: stored as-is with {s_title!r} as the BASE, "
                f"although its title names {t_title!r} (target isBaseBook={t_base}) — "
                f"backwards; use 'source'")
    return ("as_is", "info",
            f"{ctype!r} -> {t_title!r}: stored as-is with {s_title!r} as the BASE "
            f"(right for a base-named file, backwards for a citing-named one)")


# Generic placeholders / colophons. Author bylines are book-specific — pass --skip-line
# and/or rely on short-line-after-h1 heuristics via --auto-byline-max-len.
PLACEHOLDER_RE = re.compile(r"@@@חסר\s*עמוד\s*מקורי@@@", re.I)
COLOPHON_RE = re.compile(
    r"^\s*(?:סליק\s+מסכת\b|תם\s+ונשלם\b|סליק\b)",
    re.I,
)

# Explicit intermediate + ד"ה. NOTE: the connective between the commentator's name and
# ד"ה is very often "ב" fused onto ד"ה itself (e.g. "רש\"י בד\"ה X" = "Rashi, in the
# dibur-hamatchil X"), not a bare space before ד"ה — every alternative below allows an
# optional ב there (`ב?ד["״]ה`). A regex that requires ד"ה immediately after the name
# (no ב) silently fails to flag real openers and is exactly how a past run's super-scan
# reported 0 problems while ~32 explicit-opener lines were still wrongly linked to the
# Gemara (confirmed case: אבן העוזר על תלמוד בבלי, round-3 audit). Also covers רשב"ם
# (the running commentary after Rashi ends, e.g. in Bava Batra) and חוס' (a recurring
# print/OCR variant of תוס' seen in at least one source) — both were missing here too.
EXPLICIT_SUPER_RE = re.compile(
    r"""^\s*(?:
        <b>\s*(?:ב?תוס['׳]?|ותוס['׳]?|תוספות|בתוספות|ותוספות|
                 ב?חוס['׳]?|ותוס['׳]?|
                 ב?רש["״]י|ורש["״]י|
                 ב?רשב["״]ם|ורשב["״]ם|
                 תוספות\s*ישנים)\s*</b>\s*ב?ד["״]ה
      | <b>\s*(?:ב?תוס['׳]?|ותוס['׳]?|תוספות|ב?חוס['׳]?|ב?רש["״]י|ב?רשב["״]ם|תוספות\s*ישנים)\s+ב?ד["״]ה
      | (?:ב?תוס['׳]?|ותוס['׳]?|תוספות|ב?חוס['׳]?|ב?רש["״]י|ב?רשב["״]ם)\s*ב?ד["״]ה
    )""",
    re.I | re.X,
)
# Bare continuation (no commentator name at all — inherits whichever intermediate book
# the run was already in). Allow both "ד\"ה X" and the far more common "בד\"ה X", and the
# composite "שם בד\"ה X" form.
BDH_RE = re.compile(
    r'^\s*(?:<b>\s*)?(?:שם\s+)?(?:<b>\s*)?ב?ד["״]ה(?:\s*</b>)?\b',
    re.I,
)
# Primary-text labels that reset "current intermediate commentator" inheritance
PRIMARY_LABEL_RE = re.compile(
    r'^\s*<b>\s*(?:ב?גמרא|בגמ[\'׳]?|במשנה|משנה|פסוק|תורה|נביא|כתובים)\s*</b>',
    re.I,
)

GEMATRIA = [
    "א", "ב", "ג", "ד", "ה", "ו", "ז", "ח", "ט", "י",
    "יא", "יב", "יג", "יד", "טו", "טז", "יז", "יח", "יט", "כ",
    "כא", "כב", "כג", "כד", "כה", "כו", "כז", "כח", "כט", "ל",
    "לא", "לב", "לג", "לד", "לה", "לו", "לז", "לח", "לט", "מ",
    "מא", "מב", "מג", "מד", "מה", "מו", "מז", "מח", "מט", "נ",
    "נא", "נב", "נג", "נד", "נה", "נו", "נז", "נח", "נט", "ס",
    "סא", "סב", "סג", "סד", "סה", "סו", "סז", "סח", "סט", "ע",
    "עא", "עב", "עג", "עד", "עה", "עו", "עז", "עח", "עט", "פ",
    "פא", "פב", "פג", "פד", "פה", "פו", "פז", "פח", "פט", "צ",
    "צא", "צב", "צג", "צד", "צה", "צו", "צז", "צח", "צט", "ק",
]


def to_gematria(n: int) -> str:
    if 1 <= n <= len(GEMATRIA):
        return GEMATRIA[n - 1]
    rem, parts = n, []
    for val, let in [
        (400, "ת"), (300, "ש"), (200, "ר"), (100, "ק"), (90, "צ"), (80, "פ"),
        (70, "ע"), (60, "ס"), (50, "נ"), (40, "מ"), (30, "ל"), (20, "כ"),
        (10, "י"), (9, "ט"), (8, "ח"), (7, "ז"), (6, "ו"), (5, "ה"),
        (4, "ד"), (3, "ג"), (2, "ב"), (1, "א"),
    ]:
        while rem >= val:
            parts.append(let)
            rem -= val
    s = "".join(parts)
    return s.replace("יה", "טו").replace("יו", "טז")


def load_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def is_header(line: str) -> bool:
    return bool(HEADER_RE.match(line.strip()))


def is_blank(line: str) -> bool:
    return line.strip() == ""


def is_front_matter(line: str, *, byline_max_len: int, line_index: int, lines: list[str]) -> bool:
    """Generic skips: placeholders, colophons, and a short byline right under <h1>."""
    s = line.strip()
    if not s:
        return False
    if PLACEHOLDER_RE.search(s) or (s.startswith("@@@") and "חסר" in s):
        return True
    plain = re.sub(r"<[^>]+>", "", s).strip()
    if COLOPHON_RE.match(plain):
        return True
    # Short non-heading line immediately after <h1> is usually an author byline
    if (
        byline_max_len > 0
        and line_index == 2
        and len(lines) >= 1
        and is_header(lines[0])
        and HEADER_RE.match(lines[0].strip())
        and int(HEADER_RE.match(lines[0].strip()).group(1)) == 1
        and len(s) <= byline_max_len
        and not is_header(s)
    ):
        return True
    return False


def content_indices(
    lines: list[str],
    skip: set[int],
    auto_front_matter: bool,
    byline_max_len: int,
) -> list[int]:
    out = []
    for i, raw in enumerate(lines, start=1):
        if i in skip or is_blank(raw) or is_header(raw):
            continue
        if auto_front_matter and is_front_matter(
            raw, byline_max_len=byline_max_len, line_index=i, lines=lines
        ):
            continue
        out.append(i)
    return out


def derive_herefs(target_lines: list[str], book_title: str) -> dict[int, str]:
    """1-based physical line -> heRef for non-h1/h2 lines after an h2 section starts."""
    herefs: dict[int, str] = {}
    current_h2: str | None = None
    counter = 0
    for i, raw in enumerate(target_lines, start=1):
        m = HEADER_RE.match(raw.strip())
        if m:
            level = int(m.group(1))
            content = m.group(2).strip()
            if level == 1:
                continue
            if level == 2:
                label = re.sub(r"^דף\s+", "", content)
                current_h2 = label
                counter = 0
                continue
        if current_h2 is None:
            continue
        if m and int(m.group(1)) <= 2:
            continue
        counter += 1
        herefs[i] = f"{book_title} {current_h2}, {to_gematria(counter)}"
    return herefs


def guess_book_title(target_path: Path, target_lines: list[str]) -> str:
    if target_lines:
        m = HEADER_RE.match(target_lines[0].strip())
        if m and int(m.group(1)) == 1:
            return m.group(2).strip()
    return target_path.stem


def title_variants(title: str) -> list[str]:
    variants = [title]
    if title.startswith("רשי "):
        variants.append('רש"י ' + title[4:])
    if title.startswith("רשבם "):
        variants.append('רשב"ם ' + title[5:])
    return list(dict.fromkeys(variants))


def resolve_book_id(conn: sqlite3.Connection, title: str) -> int | None:
    for variant in title_variants(title):
        row = conn.execute(
            "SELECT id, title FROM book WHERE title = ? LIMIT 1", (variant,)
        ).fetchone()
        if row:
            return int(row[0])
    rows_by_id = {}
    for variant in title_variants(title):
        for row in conn.execute(
            "SELECT id, title FROM book WHERE title LIKE ? LIMIT 5", (f"%{variant}%",)
        ).fetchall():
            rows_by_id[int(row[0])] = row
    if len(rows_by_id) == 1:
        return next(iter(rows_by_id))
    return None


def path_title(path2: str) -> str:
    return path2[:-4] if path2.endswith(".txt") else path2


def is_intermediate_path(path2: str) -> bool:
    return any(x in path2 for x in ('רש"י', "רשי", "תוספות", "תוס'", "ישנים", 'רשב"ם', "רשבם"))


def super_kind(line: str) -> str | None:
    s = line.strip()
    if EXPLICIT_SUPER_RE.match(s):
        return "explicit"
    if BDH_RE.match(s):
        return "bdh"
    return None


def first_words(s: str, n: int = 16) -> str:
    t = re.sub(r"<[^>]+>", "", s or "")
    t = re.sub(r"\s+", " ", t).strip()
    return " ".join(t.split()[:n])


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--links", required=True, type=Path)
    p.add_argument("--citing", required=True, type=Path)
    p.add_argument("--target", required=True, type=Path, help="Primary (Gemara) target .txt")
    p.add_argument("--skip-line", action="append", type=int, default=[], dest="skip_lines")
    p.add_argument(
        "--no-auto-front-matter",
        action="store_true",
        help="Do not auto-skip byline-under-h1 / colophons / @@@חסר@@@",
    )
    p.add_argument(
        "--auto-byline-max-len",
        type=int,
        default=80,
        help="If >0, treat a short line 2 under <h1> as author byline (0 disables)",
    )
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--priority-list", type=Path, action="append", default=None,
                   help="generator priority.txt used for isBaseBook when --db is absent "
                        "(default: sibling SeforimLibrary checkout, if present)")
    p.add_argument("--book-id", type=int, default=None, help="Override primary target bookId")
    p.add_argument("--book-title", default=None, help="Override primary title for heRef/DB")
    p.add_argument("--db-sample", type=int, default=20, help="Random DB heRef samples across path_2")
    p.add_argument("--scan-super", action="store_true", default=True,
                   help="Scan רש\"י/תוס'/בד\"ה attribution (default on)")
    p.add_argument("--no-scan-super", action="store_true", help="Disable super-attribution scan")
    p.add_argument("--expected-linker", type=int, default=None,
                   help="Expected count of Conection Type=linker entries")
    p.add_argument("--json-out", type=Path, default=None)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()
    random.seed(args.seed)
    scan_super = args.scan_super and not args.no_scan_super
    auto_fm = not args.no_auto_front_matter
    byline_max = args.auto_byline_max_len

    issues: list[dict] = []
    stats: dict = {}

    # --- source integrity ---
    if not args.citing.is_file() or args.citing.stat().st_size == 0:
        print(json.dumps({
            "ok": False,
            "error": f"citing missing or empty: {args.citing}",
        }, ensure_ascii=False))
        return 2
    if args.links.name.endswith(".links.json"):
        issues.append({
            "severity": "blocker", "check": "source_integrity", "line_index_1": None,
            "summary": f"bad links filename {args.links.name!r} — expected *_links.json",
        })
    bad_sibling = args.links.with_name(args.links.name.replace("_links.json", ".links.json"))
    if bad_sibling.is_file() and bad_sibling != args.links:
        issues.append({
            "severity": "blocker", "check": "source_integrity", "line_index_1": None,
            "summary": f"leftover bad-name file exists: {bad_sibling.name}",
        })

    skip = set(args.skip_lines)
    citing = load_lines(args.citing)
    target = load_lines(args.target)

    if citing:
        h1 = citing[0].strip()
        if not HEADER_RE.match(h1):
            issues.append({
                "severity": "major", "check": "source_integrity", "line_index_1": 1,
                "summary": f"citing line 1 is not <h1>: {first_words(h1, 12)!r}",
            })
    hebrew = sum(1 for c in "".join(citing) if "\u0590" <= c <= "\u05FF")
    if hebrew < 200:
        issues.append({
            "severity": "blocker", "check": "source_integrity", "line_index_1": None,
            "summary": f"citing has too little Hebrew content ({hebrew} letters)",
        })
    stats["citing_bytes"] = args.citing.stat().st_size
    stats["citing_lines"] = len(citing)
    stats["citing_hebrew_letters"] = hebrew

    try:
        links = json.loads(args.links.read_text(encoding="utf-8"))
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"JSON parse failed: {e}"}, ensure_ascii=False))
        return 2

    if not isinstance(links, list):
        print(json.dumps({"ok": False, "error": "links root must be a JSON array"}, ensure_ascii=False))
        return 2

    book_title = args.book_title or guess_book_title(args.target, target)
    expected_path2 = f"{args.target.stem}.txt"

    type_counts: Counter = Counter()
    canon_counts: Counter = Counter()
    oriented_groups: dict = {}
    commentary_like: list[dict] = []
    linker_entries: list[dict] = []

    for idx, e in enumerate(links):
        if not isinstance(e, dict):
            issues.append({
                "severity": "blocker", "check": "schema", "line_index_1": None,
                "summary": f"entry #{idx} is not an object",
            })
            continue
        ctype = e.get("Conection Type")
        type_counts[ctype] += 1
        # All classification uses the generator's normalized name (Link.kt: trim,
        # lowercase, ' '->'_', aliases); ctype stays raw for messages.
        canon = canonical_connection_type(ctype)
        canon_counts[canon] += 1
        keys = set(e.keys())

        if "Conection Type" not in e and "Connection Type" in e:
            issues.append({
                "severity": "blocker", "check": "schema",
                "line_index_1": e.get("line_index_1"),
                "summary": ('uses "Connection Type" — the generator reads only the misspelled '
                            '"Conection Type" key, ignores this one and stores the entry as OTHER'),
            })
        elif ctype is None:
            issues.append({
                "severity": "minor", "check": "schema",
                "line_index_1": e.get("line_index_1"),
                "summary": ('"Conection Type" is ' + ("null" if "Conection Type" in e else "missing")
                            + ' — the generator reads it as "" and stores OTHER (not a '
                            'dependent-text link); probably an authoring error'),
            })

        if canon == "linker":
            linker_entries.append(e)
            for req in EXPECTED_KEYS:
                if req not in e:
                    issues.append({
                        "severity": "blocker", "check": "schema",
                        "line_index_1": e.get("line_index_1"),
                        "summary": f"linker entry missing {req}",
                    })
            continue

        commentary_like.append(e)
        required_ok = EXPECTED_KEYS <= keys
        unknown = keys - EXPECTED_KEYS - {
            "start", "end", "line_index_1_end", "line_index_2_end", "heRef_1", "path_1",
            "ref_2",  # Sefaria anchor: required on Sefaria targets, not an "unexpected" key
        }
        # Prefer exactly 5 keys for commentary/super_commentary
        if keys != EXPECTED_KEYS:
            if not required_ok:
                issues.append({
                    "severity": "blocker", "check": "schema",
                    "line_index_1": e.get("line_index_1"),
                    "summary": f"missing keys: {sorted(EXPECTED_KEYS - keys)}",
                })
            elif unknown:
                issues.append({
                    "severity": "info", "check": "schema",
                    "line_index_1": e.get("line_index_1"),
                    "summary": f"unexpected keys: {sorted(unknown)}",
                })
            elif keys != EXPECTED_KEYS:
                # has extras that are known optional — already handled
                pass

        # A citing-named file states the link from the dependant's side, so the
        # canonical value is "source" — always flipped by the generator to base ->
        # dependant. commentary/super_commentary are flipped only when path_2 is a base
        # book, the citing book is not, and its title names that base (see
        # classify_orientation); anywhere else they store the pair backwards. Policy:
        # keep them a blocker in a citing-named file regardless.
        if canon == "source":
            pass
        elif canon in ("commentary", "super_commentary"):
            issues.append({
                "severity": "blocker", "check": "link_direction",
                "line_index_1": e.get("line_index_1"),
                "summary": (
                    f"Conection Type={ctype!r} in a citing-named file stores the link "
                    f"backwards (מפרש as base) — must be 'source'"
                ),
            })
        elif canon in ORIENTED_OTHER_TYPES or canon in AS_IS_DEPENDANT_TYPES:
            # Valid, but its stored direction depends on isBaseBook + the title
            # heuristic; resolved once per (type, path_2) after the loop (no flood).
            g = oriented_groups.setdefault((canon, e.get("path_2") or ""), [0, e.get("line_index_1")])
            g[0] += 1
        elif canon == "other":
            if ctype is not None:
                issues.append({
                    "severity": "info", "check": "schema",
                    "line_index_1": e.get("line_index_1"),
                    "summary": f"Conection Type {ctype!r} = OTHER (not a dependent-text link)",
                })
        elif canon in LATERAL_TYPES:
            # Lateral references are not a base/dependant relation, so they are written
            # under their own name from either side and get no flip. Legitimate, but
            # worth surfacing since they don't count toward commentary coverage.
            issues.append({
                "severity": "info", "check": "schema",
                "line_index_1": e.get("line_index_1"),
                "summary": f"lateral Conection Type {ctype!r} (not a dependent-text link)",
            })
        else:
            issues.append({
                "severity": "major", "check": "schema",
                "line_index_1": e.get("line_index_1"),
                "summary": (f"unexpected Conection Type: {ctype!r} — rejected by the "
                            f"generator's ConnectionType parser, stored as OTHER"),
            })

        for k in ("line_index_1", "line_index_2"):
            if k in e and not isinstance(e[k], int):
                issues.append({
                    "severity": "blocker", "check": "schema",
                    "line_index_1": e.get("line_index_1"),
                    "summary": f"{k} is not int: {e[k]!r}",
                })

        # path_2 may be the base text OR an intermediate book (super-commentary).
        # Since every entry is "source", path_2 alone carries that distinction.
        path2 = e.get("path_2")
        if path2 and path2 != expected_path2 and not is_intermediate_path(path2):
            issues.append({
                "severity": "major", "check": "schema",
                "line_index_1": e.get("line_index_1"),
                "summary": (
                    f"path_2={path2!r} is neither the base {expected_path2!r} nor a "
                    f"Rashi/Tosafot-like intermediate book"
                ),
            })

    stats["type_counts"] = dict(type_counts)
    stats["type_counts_canonical"] = {str(k): v for k, v in canon_counts.items()}
    if args.expected_linker is not None:
        got = canon_counts.get("linker", 0)   # " Linker" etc. count, like the generator
        if got != args.expected_linker:
            issues.append({
                "severity": "major", "check": "linker_preserve", "line_index_1": None,
                "summary": f"linker count {got} != expected {args.expected_linker}",
            })

    # duplicates among commentary+super only
    c1 = Counter(e.get("line_index_1") for e in commentary_like)
    for d, n in c1.items():
        if d is None:
            continue
        if n > 1:
            issues.append({
                "severity": "blocker", "check": "duplicates",
                "line_index_1": d, "summary": f"appears {n} times among commentary/super",
            })

    auto_skipped = []
    if auto_fm:
        for i, raw in enumerate(citing, start=1):
            if (
                i not in skip
                and not is_blank(raw)
                and not is_header(raw)
                and is_front_matter(
                    raw, byline_max_len=byline_max, line_index=i, lines=citing
                )
            ):
                auto_skipped.append(i)
    stats["auto_skipped_front_matter"] = auto_skipped

    expected = set(content_indices(citing, skip, auto_fm, byline_max))
    actual = {k for k in c1 if isinstance(k, int)}
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    stats["citing_content_lines"] = len(expected)
    stats["link_entries_commentary_like"] = len(commentary_like)
    stats["link_entries_total"] = len(links)
    stats["missing_count"] = len(missing)
    stats["extra_count"] = len(extra)
    stats["duplicate_line_index_1"] = sorted(
        k for k, n in c1.items() if isinstance(k, int) and n > 1
    )

    for m in missing[:100]:
        issues.append({
            "severity": "blocker", "check": "completeness",
            "line_index_1": m, "summary": "content line missing from links",
            "preview": citing[m - 1][:160] if 1 <= m <= len(citing) else "",
        })
    if len(missing) > 100:
        issues.append({
            "severity": "blocker", "check": "completeness", "line_index_1": None,
            "summary": f"...and {len(missing) - 100} more missing (total {len(missing)})",
        })
    for x in extra:
        issues.append({
            "severity": "major", "check": "completeness",
            "line_index_1": x,
            "summary": "line_index_1 is not an expected content line (header/blank/skipped)",
            "preview": citing[x - 1][:160] if 1 <= x <= len(citing) else "",
        })

    # file-derived heRef for primary Gemara path only
    derived = derive_herefs(target, book_title)
    file_mismatch = 0
    for e in commentary_like:
        if e.get("path_2") != expected_path2:
            continue
        li1, li2 = e.get("line_index_1"), e.get("line_index_2")
        if not isinstance(li2, int):
            continue
        exp = derived.get(li2)
        got = e.get("heRef_2")
        if exp is None:
            issues.append({
                "severity": "major", "check": "heRef_file",
                "line_index_1": li1,
                "summary": f"line_index_2={li2} has no derived heRef (out of range / before first h2)",
            })
            file_mismatch += 1
        elif got != exp:
            file_mismatch += 1
            if file_mismatch <= 30:
                issues.append({
                    "severity": "major", "check": "heRef_file",
                    "line_index_1": li1,
                    "summary": f"heRef_2={got!r} != file-derived={exp!r} (line_index_2={li2})",
                })
    if file_mismatch > 30:
        issues.append({
            "severity": "major", "check": "heRef_file", "line_index_1": None,
            "summary": f"...and more heRef_file mismatches (total {file_mismatch})",
        })
    stats["heref_file_mismatches"] = file_mismatch
    stats["book_title_used"] = book_title

    # --- super-attribution scan ---
    by_li1 = {e["line_index_1"]: e for e in commentary_like if isinstance(e.get("line_index_1"), int)}
    super_wrong: list[dict] = []
    super_ok = 0
    super_candidates = 0
    if scan_super:
        last_intermediate: str | None = None
        for i, raw in enumerate(citing, start=1):
            if is_blank(raw) or is_header(raw):
                last_intermediate = None
                continue
            if PRIMARY_LABEL_RE.match(raw.strip()):
                last_intermediate = None
            kind = super_kind(raw)
            if not kind:
                # track explicit intermediate mentions for inheritance hint
                if EXPLICIT_SUPER_RE.match(raw.strip()) or (
                    'רש"י' in raw[:40] or "תוס" in raw[:40]
                ):
                    if "רש" in raw[:50]:
                        last_intermediate = "rashi"
                    elif "תוס" in raw[:50]:
                        last_intermediate = "tosafot"
                continue
            super_candidates += 1
            e = by_li1.get(i)
            if not e:
                super_wrong.append({
                    "line_index_1": i, "kind": kind,
                    "issue": "NO_ENTRY", "preview": first_words(raw),
                })
                continue
            ctype = e.get("Conection Type")
            path2 = e.get("path_2") or ""
            # The super-commentary relation now lives entirely in path_2: the entry
            # must point at the intermediate book, not at the base text.
            ok = is_intermediate_path(path2)
            if ok:
                super_ok += 1
            else:
                super_wrong.append({
                    "line_index_1": i,
                    "kind": kind,
                    "issue": "SHOULD_BE_SUPER_COMMENTARY",
                    "Conection Type": ctype,
                    "path_2": path2,
                    "heRef_2": e.get("heRef_2"),
                    "line_index_2": e.get("line_index_2"),
                    "inferred_intermediate": last_intermediate,
                    "preview": first_words(raw),
                })
                issues.append({
                    "severity": "major",
                    "check": "super_commentary",
                    "line_index_1": i,
                    "summary": (
                        f"{kind} label but path_2={path2!r} type={ctype!r} "
                        f"(expected path_2 → the Rashi/Tosafot book itself)"
                    ),
                    "preview": first_words(raw),
                })
    stats["super_candidates"] = super_candidates
    stats["super_ok"] = super_ok
    stats["super_wrong_count"] = len(super_wrong)

    # --- DB heRef sample across all path_2 ---
    db_path = args.db
    if db_path is None:
        appdata = os.environ.get("APPDATA", "")
        cand = Path(appdata) / "io.github.kdroidfilter.seforimapp" / "databases" / "seforim.db"
        if cand.is_file():
            db_path = cand
    stats["db_path"] = str(db_path) if db_path else None
    db_ok = 0
    db_fail: list[dict] = []
    if db_path and Path(db_path).is_file() and commentary_like:
        conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
        sample_n = min(args.db_sample, len(commentary_like))
        sample = random.sample(commentary_like, sample_n)
        # Force-sample some entries pointing at an intermediate book (רש"י/תוספות) —
        # identified by path_2, not by the type: every entry in a citing-named file
        # is "source".
        supers = [e for e in commentary_like if is_intermediate_path(e.get("path_2") or "")]
        for e in random.sample(supers, min(5, len(supers))):
            if e not in sample:
                sample.append(e)
        book_cache: dict[str, int | None] = {}
        for e in sample:
            title = path_title(e.get("path_2") or "")
            if title not in book_cache:
                if title == book_title and args.book_id is not None:
                    book_cache[title] = args.book_id
                else:
                    book_cache[title] = resolve_book_id(conn, title)
            bid = book_cache[title]
            if bid is None:
                db_fail.append({
                    "line_index_1": e.get("line_index_1"),
                    "issue": "BOOK_NOT_FOUND",
                    "path_2": e.get("path_2"),
                })
                continue
            li2 = e.get("line_index_2")
            if not isinstance(li2, int):
                continue
            row = conn.execute(
                "SELECT heRef FROM line WHERE bookId=? AND lineIndex=?",
                (bid, li2 - 1),
            ).fetchone()
            if not row:
                db_fail.append({
                    "line_index_1": e.get("line_index_1"),
                    "issue": "LINE_NOT_FOUND",
                    "path_2": e.get("path_2"),
                    "line_index_2": li2,
                })
            elif row[0] != e.get("heRef_2"):
                db_fail.append({
                    "line_index_1": e.get("line_index_1"),
                    "issue": "HEREF_MISMATCH",
                    "heRef_2": e.get("heRef_2"),
                    "db_heRef": row[0],
                    "path_2": e.get("path_2"),
                    "line_index_2": li2,
                })
            else:
                db_ok += 1
        for f in db_fail[:25]:
            issues.append({
                "severity": "major", "check": "heRef_db",
                "line_index_1": f.get("line_index_1"),
                "summary": json.dumps(f, ensure_ascii=False),
            })
        if len(db_fail) > 25:
            issues.append({
                "severity": "major", "check": "heRef_db", "line_index_1": None,
                "summary": f"...and more heRef_db failures (total {len(db_fail)})",
            })
        conn.close()
        stats["db_sample_n"] = len(sample)
        stats["db_sample_ok"] = db_ok
        stats["db_sample_fail"] = len(db_fail)
        stats["db_books_resolved"] = {k: v for k, v in book_cache.items()}
    else:
        issues.append({
            "severity": "info", "check": "heRef_db", "line_index_1": None,
            "summary": "DB not available — skipped heRef_db check",
        })
        # Not a silent pass: say it where a human running the tool will see it.
        print("WARNING: heRef_db check SKIPPED (no --db and no %APPDATA% seforim.db) — "
              "0 DB samples verified", file=sys.stderr)
        stats["db_sample_n"] = 0
        stats["db_sample_ok"] = 0
        stats["db_sample_fail"] = 0

    # --- orientation of footnotes/targum/midrash/... entries (aggregated) ---
    orient_stats: dict = {}
    if oriented_groups:
        prio = args.priority_list if args.priority_list is not None else default_priority_lists()
        oracle = BaseBookOracle(db_path, prio)
        s_title = args.citing.stem
        for (ctype, path2), (n, first_li) in sorted(oriented_groups.items()):
            outcome, severity, summary = classify_orientation(
                ctype, s_title, generator_target_title(path2), oracle)
            orient_stats[f"{ctype} -> {path2}"] = {"entries": n, "outcome": outcome}
            issues.append({
                "severity": severity, "check": "link_direction", "line_index_1": first_li,
                "summary": f"{n} entries: {summary}",
            })
        stats["orientation_oracle"] = oracle.source
        oracle.close()
    stats["oriented_groups"] = orient_stats

    sev = Counter(i["severity"] for i in issues)
    stats["issues_by_severity"] = dict(sev)
    blockers = sev.get("blocker", 0)
    majors = sev.get("major", 0)
    ok = blockers == 0 and majors == 0 and stats["missing_count"] == 0

    report = {
        "ok": ok,
        "stats": stats,
        "missing_line_index_1": missing,
        "extra_line_index_1": extra,
        "super_wrong": super_wrong[:200],
        "issues": issues,
        "note": (
            "Semantic match beyond super-attribution scan is NOT fully checked — "
            "required separately per SKILL.md (sample + low-conf list)."
        ),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_out:
        args.json_out.write_text(text, encoding="utf-8")
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
