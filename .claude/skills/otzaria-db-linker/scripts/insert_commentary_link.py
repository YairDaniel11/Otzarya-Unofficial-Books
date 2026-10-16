# -*- coding: utf-8 -*-
"""
Generic engine for writing a project `_links.json` file into the live Otzaria
`seforim.db`, so a commentary/citing book shows up next to its base text in
the app's side-by-side reading view.

This is the write-to-the-real-database half of the workflow. The other half
(producing/updating the `_links.json` file itself) is a separate skill
(otzaria-commentary-linker) and is not this script's job.

Usage (always via Windows-MCP PowerShell, never bash -- see SKILL.md for why):

    chcp 65001; python -X utf8 "<this file>" "<path to a job config JSON>"

The job config JSON carries all the per-run, book-specific detail (titles,
paths, Hebrew strings). Passing it as a file -- instead of passing titles as
command-line arguments -- sidesteps PowerShell's well-documented mangling of
Hebrew text and embedded quotes. See references/db_write_notes.md for the full
schema this script relies on and the reasoning behind each design choice below
(direction, flag mapping, backup policy, etc.) -- this file intentionally
keeps comments short and points there for the "why".

Job config schema (see references/db_write_notes.md for full detail):
{
  "project_root": "C:\\Users\\User\\Downloads\\otzaria-library-main",
  "citing_title": "<commentary/citing book title, exactly as in the book table>",
  "target_title": "<base/target book title, exactly as in the book table -- this is
                    only the file's DEFAULT/primary target; entries whose own path_2
                    names a different book (e.g. a super_commentary entry pointing at
                    Tosafot/Rashi instead of the Gemara) are resolved independently,
                    per entry -- see "Multi-target files" below and db_write_notes.md>",
  "links_json_path": "C:\\...\\links\\<citing_title>_links.json",
  "seforim_db_path": null,          # optional override; auto-discovered if omitted
  "keep_backups": 3,                 # how many _db_backups/seforim.db.bak_* to retain
  "replace_existing": false,         # false = insert only missing rows, never delete.
                                      # true = also refresh existing rows of this file and
                                      # delete stale rows the DB proves this file wrote
                                      # (never-flipped types out of the citing book). See
                                      # db_write_notes.md, "How re-runs work now".
  "delete_reported_stale": false,    # with replace_existing: also delete the STALE? rows
                                      # (dependent rows into the citing book this file does
                                      # not produce) -- only after the user confirmed the list
  "dry_run": true                    # true = do everything except commit; always run this
                                      # first and read the report before setting it to false
}

Multi-target files -- read this before assuming target_title covers everything:
A single `_links.json` file can contain entries that target DIFFERENT books, not
one fixed base text. The most common real case: a `commentary` entry pointing at
the Gemara alongside a `super_commentary` entry pointing at a different book
entirely (Tosafot, Rashi, ...) -- the sibling otzaria-commentary-linker skill's
"ד"ה" special case, where a citing-book line comments on a commentary rather than
on the base text. This script NEVER resolves an entry's target line using a single
target_title-derived line map; it groups entries by (Conection Type, real target
title derived from that entry's own path_2), and resolves each group's book id and
line map independently. Confirmed bug this fixes: an earlier version used one line
map for every entry regardless of type, which silently wrote every super_commentary
row with sourceBookId set to the Gemara instead of the real target (some numbers
happened to fall in-range by coincidence), with no error -- see db_write_notes.md,
"A _links.json file can target MULTIPLE different books", for the full story.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import sys
import unicodedata
from datetime import datetime
from pathlib import Path, PurePosixPath

DEFAULT_DB_PATH = Path(r"C:\ProgramData\otzaria\books\seforim.db")

# ---- Port of SeforimLibrary's link-storage rules (otzariasqlite/Generator.kt) ----
# Kept byte-for-byte equivalent to Generator.kt: normalizeHebrewLabel, comparableLabel,
# titleDeclaresDependencyOn, DEPENDENCY_TITLE_PREFIXES, ORIENTED_DEPENDANT_TYPES and
# the flip/storedType decision in processLinksForBook. Update together.
ORIENTED_DEPENDANT_TYPES = frozenset({
    "COMMENTARY", "SUPER_COMMENTARY", "TARGUM", "MIDRASH", "PARSHANUT",
    "DIBUR_HAMATCHIL", "EIN_MISHPAT", "ELUCIDATION", "FOOTNOTES",
})
_DEP_PREFIXES = (   # DEPENDENCY_TITLE_PREFIXES, same order
    "תלמוד בבלי ",
    "תלמוד ירושלמי ",
    "מסכת ", "משנה ", "ספר ",
)
_ON = " על "
# Java's \s (no UNICODE_CHARACTER_CLASS) is ASCII-only: NBSP is NOT whitespace there.
_JAVA_WS = re.compile(r"[ \t\n\x0b\f\r]+")
_CORPUS_SUFFIX = re.compile(
    r"[ \t\n\x0b\f\r]+על[ \t\n\x0b\f\r]+(?:"
    "התנ\"ך|התורה|התלמוד|"
    "המשנה|תנך|תורה|"
    "תלמוד|משנה)$",
    re.I,
)


def _kt_trim(s: str) -> str:
    """Kotlin String.trim(): strips Char.isWhitespace (incl. Unicode spaces)."""
    def ws(c: str) -> bool:
        return c in "\t\n\x0b\f\r\x1c\x1d\x1e\x1f" or unicodedata.category(c) in ("Zs", "Zl", "Zp")
    i, j = 0, len(s)
    while i < j and ws(s[i]):
        i += 1
    while j > i and ws(s[j - 1]):
        j -= 1
    return s[i:j]


def _comparable_label(raw: str) -> str:
    """Generator.comparableLabel (normalizeHebrewLabel + quote strip + corpus suffix)."""
    s = _kt_trim(raw)
    s = s.replace("“", '"').replace("”", '"')
    s = s.replace("‘", "'").replace("’", "'")
    s = s.replace('"', "״").replace("''", "״").replace("׳׳", "״")
    s = s.replace("`", "׳")
    s = _kt_trim(_JAVA_WS.sub(" ", s))
    for ch in ("״", '"', "׳", "'"):
        s = s.replace(ch, "")
    s = _kt_trim(_JAVA_WS.sub(" ", s))
    return _kt_trim(_CORPUS_SUFFIX.sub("", s))


def _dependency_key(raw: str) -> str:
    v = _comparable_label(raw)
    while True:
        prefix = next((p for p in _DEP_PREFIXES if v.startswith(p)), None)
        if prefix is None:
            return v
        v = _kt_trim(v[len(prefix):])


def title_declares_dependency_on(dependant_title: str, base_title: str) -> bool:
    """Generator.titleDeclaresDependencyOn."""
    norm = _comparable_label(dependant_title or "")
    i = norm.rfind(_ON)
    if i < 0:
        return False
    declared = norm[i + len(_ON):]
    if not _kt_trim(declared):
        return False
    return _dependency_key(declared) == _dependency_key(base_title or "")


def generator_target_title(path2: str) -> str:
    """Target title exactly as Generator.kt derives it from path_2: the last component
    (after a backslash, else Paths.get(path).fileName), minus everything from its last '.'."""
    path2 = path2 or ""
    last = path2.split("\\")[-1] if "\\" in path2 else PurePosixPath(path2).name
    return last.rsplit(".", 1)[0] if "." in last else last


def plan_storage(declared: str, file_title: str, file_is_base: bool,
                 path2_title: str, path2_is_base: bool) -> tuple[bool, str]:
    """(flip, storedType) for one entry of `<file_title>_links.json` pointing at
    path2_title, exactly as Generator.processLinksForBook decides it. `declared` is the
    upper-case ConnectionType name. flip=True stores path_2 book -> file book."""
    points_from_non_base_into_base = (
        declared in ORIENTED_DEPENDANT_TYPES and path2_is_base and not file_is_base
    )
    reversed_dependant = points_from_non_base_into_base and title_declares_dependency_on(
        file_title, path2_title
    )
    flip = declared == "SOURCE" or reversed_dependant
    if declared == "SOURCE":
        stored = "COMMENTARY"
    elif points_from_non_base_into_base and not reversed_dependant:
        stored = "OTHER"
    else:
        stored = declared
    return flip, stored
# ---- end of the Generator.kt port ----


def resolve_db_path(cfg: dict) -> Path:
    if cfg.get("seforim_db_path"):
        p = Path(cfg["seforim_db_path"])
        if not p.exists():
            raise FileNotFoundError(f"seforim_db_path given but not found: {p}")
        return p

    import os

    prefs_path = Path(os.environ.get("APPDATA", "")) / "otzaria" / "shared_preferences.json"
    if prefs_path.exists():
        try:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            lib_path = prefs.get("flutter.key-library-path")
            if lib_path:
                candidate = Path(lib_path) / "seforim.db"
                if candidate.exists():
                    return candidate
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: could not parse {prefs_path}: {e}")

    if DEFAULT_DB_PATH.exists():
        return DEFAULT_DB_PATH

    raise FileNotFoundError(
        "Could not locate seforim.db automatically. Set seforim_db_path explicitly "
        "in the job config, or check %APPDATA%\\otzaria\\shared_preferences.json "
        "for 'flutter.key-library-path'."
    )


def probe_lock(db_path: Path) -> None:
    """Fail fast and clearly if Otzaria (or anything else) has the DB open."""
    probe = sqlite3.connect(str(db_path), timeout=1)
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.rollback()
    except sqlite3.OperationalError as e:
        raise RuntimeError(
            "Cannot get a write lock on seforim.db -- close the Otzaria app "
            f"completely (not just the window) and retry. Detail: {e}"
        )
    finally:
        probe.close()


def backup_db(db_path: Path, project_root: Path, keep: int) -> Path:
    backup_dir = project_root / "_db_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"seforim.db.bak_{stamp}"
    print(f"Backing up {db_path} -> {backup_path} ...")
    shutil.copy2(db_path, backup_path)
    print(f"Backup done ({backup_path.stat().st_size:,} bytes).")

    existing = sorted(
        backup_dir.glob("seforim.db.bak_*"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for stale in existing[keep:]:
        print(f"Pruning old backup: {stale.name}")
        stale.unlink()

    return backup_path


def load_links(links_path: Path) -> list[dict]:
    data = json.loads(links_path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError(f"{links_path} did not contain a non-empty JSON array")
    required = {"line_index_1", "line_index_2", "heRef_2", "path_2", "Conection Type"}
    for i, entry in enumerate(data):
        missing = required - entry.keys()
        if missing:
            raise ValueError(f"entry {i} in {links_path} is missing fields: {missing}")
    return data


def get_book(cur: sqlite3.Cursor, title: str) -> tuple[int, int]:
    """Returns (id, orderIndex). Raises if not found or ambiguous."""
    rows = cur.execute(
        "SELECT id, orderIndex FROM book WHERE title = ?", (title,)
    ).fetchall()
    if not rows:
        raise ValueError(f'No book titled "{title}" found in seforim.db.')
    if len(rows) > 1:
        raise ValueError(f'Multiple books titled "{title}" found: {rows}. Disambiguate by id.')
    return rows[0]


def line_map(cur: sqlite3.Cursor, book_id: int) -> dict[int, int]:
    return dict(cur.execute("SELECT lineIndex, id FROM line WHERE bookId = ?", (book_id,)))


def table_exists(cur: sqlite3.Cursor, name: str) -> bool:
    return (
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def get_book_info(cur: sqlite3.Cursor, title: str) -> dict:
    """id / orderIndex plus the two facts the generator's storage rules depend on:
    book.isBaseBook, and whether the book's source is Sefaria."""
    book_id, order = get_book(cur, title)
    row = cur.execute(
        "SELECT b.isBaseBook, COALESCE(s.name, '') FROM book b "
        "LEFT JOIN source s ON s.id = b.sourceId WHERE b.id = ?",
        (book_id,),
    ).fetchone()
    return {
        "id": book_id,
        "title": title,
        "order": order,
        "is_base": bool(row[0]),
        "is_sefaria": "sefaria" in str(row[1]).lower(),
    }


def is_heading_content(content: object) -> bool:
    """SeforimRepository.getHeadingLineIds: content LIKE '<h1%' .. '<h4%' (LIKE is
    ASCII-case-insensitive, no leading-whitespace tolerance)."""
    return isinstance(content, str) and content[:3].lower() in ("<h1", "<h2", "<h3", "<h4")


_heading_cache: dict[int, set[int]] = {}


def heading_line_ids(cur: sqlite3.Cursor, book_id: int) -> set[int]:
    if book_id not in _heading_cache:
        _heading_cache[book_id] = {
            int(lid) for lid, content in cur.execute(
                "SELECT id, content FROM line WHERE bookId = ?", (book_id,)
            ) if is_heading_content(content)
        }
    return _heading_cache[book_id]


def count_visible_chars(html: str, end_exclusive: int) -> int:
    """common/HtmlCharCounter.kt countVisibleChars(html, endExclusive). Kotlin indexes
    UTF-16 code units, so the walk is done on those, not on Python code points."""
    units = memoryview(html.encode("utf-16-le")).cast("H")
    if len(units) == 0 or end_exclusive <= 0:
        return 0
    lt, gt, amp, semi = ord("<"), ord(">"), ord("&"), ord(";")
    count = 0
    in_tag = False
    i = 0
    n = min(len(units), end_exclusive)
    while i < n:
        c = units[i]
        if in_tag:
            if c == gt:
                in_tag = False
        elif c == lt:
            in_tag = True
        elif c == amp:
            end = min(n, i + 10)
            j = i + 1
            terminated = False
            while j < end:
                if units[j] == semi:
                    terminated = True
                    break
                j += 1
            count += 1
            if terminated:
                i = j
        else:
            count += 1
        i += 1
    return count


_ANCHOR_OT = " אות "


def anchor_label_from_heref(heref: str) -> str | None:
    """Generator.anchorLabelFromHeRef."""
    idx = heref.rfind(_ANCHOR_OT)
    if idx < 0:
        return None
    tail = _kt_trim(heref[idx + len(_ANCHOR_OT):])
    if not tail or len(tail) > 6:
        return None
    ok = all("א" <= ch <= "ת" or ch in ('"', "׳", "״") for ch in tail)
    return tail if ok else None


def write_satellites(
    cur: sqlite3.Cursor,
    pending: list[dict],
    *,
    has_anchor: bool,
    has_range: bool,
    has_coverage: bool,
) -> tuple[int, int]:
    """Writes link_anchor / link_range / link_coverage rows for a just-inserted group,
    exactly as Generator.processLinksForBook does (buildLinkAnchor, queueRangeSide):

    * anchor -- only when the link is NOT flipped (the anchor is on the stored source
      side, which is then the file's own line): side=0, `start`/`end` converted from raw
      offsets to visible chars, label from heRef_2. A flipped link gets no anchor.
    * range -- side 0 = stored source, 1 = stored target, so a flip swaps which file end
      lands on which side. end == start is a plain link; an end before the start, a
      missing end line or a heading end line drops the range; coverage rows skip
      heading lines.

    Returns (anchors_written, ranges_written)."""
    anchors = 0
    ranges = 0
    for p in pending:
        entry = p["entry"]
        link_id = p["link_id"]

        if has_anchor and not p["flip"] and entry.get("start") is not None:
            row = cur.execute("SELECT content FROM line WHERE id=?", (p["file_line_id"],)).fetchone()
            content = row[0] if row else None
            try:
                raw_start = int(entry["start"])
            except (TypeError, ValueError):
                raw_start = None
            length = len(content.encode("utf-16-le")) // 2 if isinstance(content, str) else -1
            if raw_start is not None and isinstance(content, str) and 0 <= raw_start <= length:
                raw_end = None
                if entry.get("end") is not None:
                    try:
                        raw_end = int(entry["end"])
                    except (TypeError, ValueError):
                        raw_end = None
                    if raw_end is not None and not raw_start <= raw_end <= length:
                        raw_end = None
                cur.execute(
                    """
                    INSERT OR IGNORE INTO link_anchor (linkId, side, charStart, charEnd, label)
                    VALUES (?, 0, ?, ?, ?)
                    """,
                    (
                        link_id,
                        count_visible_chars(content, raw_start),
                        count_visible_chars(content, raw_end) if raw_end is not None else None,
                        anchor_label_from_heref(str(entry.get("heRef_2", ""))),
                    ),
                )
                anchors += 1

        if not has_range:
            continue

        file_end = (entry.get("line_index_1_end"), p["file_book_id"], p["file_idx"])
        path2_end = (entry.get("line_index_2_end"), p["path2_book_id"], p["path2_idx"])
        sides = ((0, *path2_end), (1, *file_end)) if p["flip"] else ((0, *file_end), (1, *path2_end))
        for side, raw_end_1based, book_id, start_0based in sides:
            if raw_end_1based is None:
                continue
            try:
                end_0 = int(raw_end_1based) - 1
            except (TypeError, ValueError):
                continue
            if end_0 == start_0based:
                continue
            end_row = None
            if end_0 > start_0based:
                end_row = cur.execute(
                    "SELECT id, content FROM line WHERE bookId=? AND lineIndex=?", (book_id, end_0)
                ).fetchone()
            if not end_row or is_heading_content(end_row[1]):
                print(f"  range end {end_0 + 1} reversed/missing/heading (side {side}) -- range dropped")
                continue
            # LinkRangeQueries.sq insertRange: two producers of one row keep the WIDEST
            # range per side, whatever order their files are processed in.
            cur.execute(
                """
                INSERT INTO link_range (linkId, side, endLineId, endLineIndex)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(linkId, side) DO UPDATE
                SET endLineId = excluded.endLineId, endLineIndex = excluded.endLineIndex
                WHERE excluded.endLineIndex > link_range.endLineIndex
                """,
                (link_id, side, end_row[0], end_0),
            )
            ranges += 1
            if has_coverage:
                for mid_id, content in cur.execute(
                    "SELECT id, content FROM line WHERE bookId=? AND lineIndex BETWEEN ? AND ?",
                    (book_id, start_0based + 1, end_0),
                ).fetchall():
                    if is_heading_content(content):
                        continue
                    cur.execute(
                        """
                        INSERT OR IGNORE INTO link_coverage (lineId, linkId, side)
                        VALUES (?, ?, ?)
                        """,
                        (mid_id, link_id, side),
                    )
    return anchors, ranges


# The 24 SeforimLibrary ConnectionType names (core/.../models/Link.kt).
KNOWN_CONNECTION_TYPES = frozenset({
    "COMMENTARY", "SUPER_COMMENTARY", "TARGUM", "REFERENCE", "SOURCE", "MIDRASH",
    "QUOTATION", "MESORAT_HASHAS", "EIN_MISHPAT", "DIBUR_HAMATCHIL", "PARSHANUT",
    "MISHNAH_IN_TALMUD", "RELATED", "OTHER", "LINKER", "SIFREI_MITZVOT", "ESSAY",
    "ALLUSION", "LITURGY", "ELUCIDATION", "EXPLICATION", "LAW", "SUMMARY", "FOOTNOTES",
})
# Link.kt `fromKnownStringOrNull` spelling aliases, after its trim/lowercase/' '->'_'.
_TYPE_ALIASES = {
    "supercommentary": "super_commentary",
    "quotation_auto": "quotation", "quotation_auto_tanakh": "quotation",
    "ein_mishpat_/_ner_mitsvah": "ein_mishpat", "ein_mishpat_/_ner_mitzvah": "ein_mishpat",
    "related_passage": "related",
    "ellucidation": "elucidation",
    "footnote": "footnotes",
    "": "other", "none": "other",
}


def canonical_type(name: object) -> str:
    """Upper-case ConnectionType name a `Conection Type` value parses as (Link.kt).

    Stricter than the generator on purpose: an unknown value (a typo, or the trap
    spelling "sifrei mitsvot") is refused instead of silently becoming OTHER."""
    key = str(name if name is not None else "").strip().lower().replace(" ", "_")
    canonical = _TYPE_ALIASES.get(key, key).upper()
    if canonical not in KNOWN_CONNECTION_TYPES:
        raise ValueError(
            f'Unrecognized "Conection Type" value {name!r}. Valid: {sorted(KNOWN_CONNECTION_TYPES)}'
        )
    return canonical


def resolve_connection_type_id(cur: sqlite3.Cursor, stored_name: str) -> int:
    """connection_type.id of a STORED type name (see plan_storage), by name."""
    row = cur.execute(
        "SELECT id FROM connection_type WHERE upper(name) = upper(?)", (stored_name,)
    ).fetchone()
    if not row:
        valid = [r[0] for r in cur.execute("SELECT name FROM connection_type").fetchall()]
        raise ValueError(
            f"connection_type {stored_name!r} is missing from this seforim.db (built before "
            f"SeforimLibrary added it?). Present: {valid}"
        )
    return row[0]


# Generator.kt setConnFlag(...): a book gets the flag when it is EITHER end of a link
# of that stored type. No other type sets any of these four columns.
TYPE_FLAG_COLUMNS = {
    "TARGUM": "hasTargumConnection",
    "REFERENCE": "hasReferenceConnection",
    "COMMENTARY": "hasCommentaryConnection",
    "OTHER": "hasOtherConnection",
}
# Generator.kt hasSourceConnection: the book is the stored TARGET of a non-self link of
# one of these types. FOOTNOTES is (deliberately, upstream) not in this list.
SOURCE_CONNECTION_TYPES = (
    "COMMENTARY", "SUPER_COMMENTARY", "TARGUM", "MIDRASH",
    "PARSHANUT", "DIBUR_HAMATCHIL", "EIN_MISHPAT", "ELUCIDATION",
)


def delete_satellites(cur: sqlite3.Cursor, link_ids: list[int]) -> None:
    """Deletes the link_anchor / link_range / link_coverage rows of `link_ids`."""
    rows = [(int(i),) for i in link_ids]
    for table in ("link_anchor", "link_range", "link_coverage"):
        if table_exists(cur, table):
            cur.executemany(f"DELETE FROM {table} WHERE linkId=?", rows)


def delete_links(cur: sqlite3.Cursor, link_ids: list[int]) -> None:
    """Deletes link rows and their satellite rows. The satellites' ON DELETE CASCADE
    only fires with PRAGMA foreign_keys=ON, which this connection does not set, so
    without this they would survive as orphans."""
    delete_satellites(cur, link_ids)
    cur.executemany("DELETE FROM link WHERE id=?", [(int(i),) for i in link_ids])


def recompute_book_flags(cur: sqlite3.Cursor, book_ids: set[int]) -> None:
    """Sets book_has_links and book.has*Connection for `book_ids` exactly as the
    generator derives them from the whole link table (reset, then set)."""
    ids_by_name = {str(n).upper(): int(i) for i, n in cur.execute("SELECT id, name FROM connection_type")}

    def in_list(names) -> str:
        ids = [str(ids_by_name[n]) for n in names if n in ids_by_name]
        return "(" + ",".join(ids) + ")" if ids else "(NULL)"

    book_cols = {r[1] for r in cur.execute("PRAGMA table_info(book)")}
    for book_id in sorted(book_ids):
        has_src = cur.execute("SELECT EXISTS(SELECT 1 FROM link WHERE sourceBookId=?)", (book_id,)).fetchone()[0]
        has_tgt = cur.execute("SELECT EXISTS(SELECT 1 FROM link WHERE targetBookId=?)", (book_id,)).fetchone()[0]
        cur.execute(
            "INSERT INTO book_has_links(bookId, hasSourceLinks, hasTargetLinks) VALUES (?, ?, ?) "
            "ON CONFLICT(bookId) DO UPDATE SET hasSourceLinks=excluded.hasSourceLinks, "
            "hasTargetLinks=excluded.hasTargetLinks",
            (book_id, has_src, has_tgt),
        )
        values = {}
        for type_name, column in TYPE_FLAG_COLUMNS.items():
            ids = in_list([type_name])
            values[column] = cur.execute(
                f"SELECT EXISTS(SELECT 1 FROM link WHERE sourceBookId=? AND connectionTypeId IN {ids}) "
                f"OR EXISTS(SELECT 1 FROM link WHERE targetBookId=? AND connectionTypeId IN {ids})",
                (book_id, book_id),
            ).fetchone()[0]
        values["hasSourceConnection"] = cur.execute(
            f"SELECT EXISTS(SELECT 1 FROM link WHERE targetBookId=? AND sourceBookId != ? "
            f"AND connectionTypeId IN {in_list(SOURCE_CONNECTION_TYPES)})",
            (book_id, book_id),
        ).fetchone()[0]
        values = {k: v for k, v in values.items() if k in book_cols}
        if values:
            cur.execute(
                "UPDATE book SET " + ", ".join(f"{k}=?" for k in values) + " WHERE id=?",
                (*values.values(), book_id),
            )


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: insert_commentary_link.py <path to job config JSON>")
        return 1

    try:
        return run(Path(sys.argv[1]))
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        # These are the anticipated, "explain and stop" failure modes (bad path, book not
        # found, DB locked, unrecognized type, ...) -- print cleanly instead of a traceback.
        print(f"ERROR: {e}")
        return 1


def run(config_path: Path) -> int:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    project_root = Path(cfg["project_root"])
    citing_title = cfg["citing_title"]
    target_title = cfg["target_title"]
    links_path = Path(cfg["links_json_path"])
    replace_existing = bool(cfg.get("replace_existing", False))
    if replace_existing and links_path.name != f"{citing_title}_links.json":
        # The generator reads a links file as the links of the book its NAME gives, and
        # replace mode's proof that a row is this file's own rests on that book being
        # citing_title. A mismatch means the job describes a different book than the file.
        raise ValueError(
            f"replace_existing refused: the links file is named {links_path.name!r}, but the "
            f"generator would only read it as {citing_title!r}'s links if it were named "
            f"{citing_title + '_links.json'!r}. Fix citing_title or the file name."
        )
    keep_backups = int(cfg.get("keep_backups", 3))
    dry_run = bool(cfg.get("dry_run", True))

    print(f"{'DRY RUN -- ' if dry_run else ''}citing={citing_title!r} target={target_title!r}")

    db_path = resolve_db_path(cfg)
    print(f"seforim.db: {db_path}")
    if not links_path.exists():
        raise FileNotFoundError(f"links_json_path not found: {links_path}")

    data = load_links(links_path)
    print(f"links file: {links_path.name} ({len(data)} entries)")

    probe_lock(db_path)

    backup_path = None
    if not dry_run:
        backup_path = backup_db(db_path, project_root, keep_backups)
    else:
        print("(dry run: skipping backup -- no write will happen)")

    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.text_factory = str
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    cur = conn.cursor()

    _heading_cache.clear()  # per-connection cache: ids differ between DBs
    has_anchor = table_exists(cur, "link_anchor")
    has_range = table_exists(cur, "link_range")
    has_coverage = table_exists(cur, "link_coverage")

    try:
        # target_title is only the file's DEFAULT/primary target (see module docstring).
        # It's still resolved eagerly so the printed summary matches the old behavior and
        # the common single-target case costs no extra query.
        citing = get_book_info(cur, citing_title)
        citing_id, citing_order = citing["id"], citing["order"]
        citing_lines = line_map(cur, citing_id)
        target_book_cache: dict[str, dict] = {}

        def resolve_target(title: str) -> dict:
            if title not in target_book_cache:
                info = get_book_info(cur, title)
                info["lines"] = line_map(cur, info["id"])
                target_book_cache[title] = info
            return target_book_cache[title]

        primary = resolve_target(target_title)
        print(f"target book: {target_title} id={primary['id']} orderIndex={primary['order']} isBaseBook={primary['is_base']}")
        print(f"citing book: {citing_title} id={citing_id} orderIndex={citing_order} isBaseBook={citing['is_base']}")
        print(f"target has {len(primary['lines'])} lines, citing has {len(citing_lines)} lines")

        # Entries are first collected by (Conection Type, REAL target book from each
        # entry's own path_2) -- never assumed to be target_title: a links file can mix
        # targets (commentary -> Gemara, super_commentary -> Tosafot/Rashi, ...). The
        # title is derived from path_2 exactly as Generator.kt does (generator_target_title).
        raw_groups: dict[tuple[str, str], list[dict]] = {}
        for entry in data:
            real_target_title = generator_target_title(str(entry["path_2"]))
            raw_groups.setdefault((entry["Conection Type"], real_target_title), []).append(entry)
        # Fail on an unknown type before anything is written.
        declared_by_group = {key: canonical_type(key[0]) for key in raw_groups}

        report = {"inserted": {}, "refreshed_existing": {}, "skipped_existing": 0,
                  "deleted_stale_own_rows": {}, "reported_stale_candidates": {},
                  "deleted_reported_stale": {}, "other_rows_kept": {}, "stored_as_other": {},
                  "skipped_missing_line": 0, "skipped_heading_line": 0, "skipped_duplicate": 0,
                  "skipped_target_book_not_found": 0, "skipped_linker": 0,
                  "skipped_both_sefaria": 0, "anchors": 0, "ranges": 0}
        touched_book_ids: set[int] = set()

        # ---- 1. plan: every row this file produces, grouped by its STORED identity ----
        # Grouping by the stored (type, source book, target book) -- not by the raw JSON
        # string -- is what keeps two spellings that store alike ("quotation" and
        # "quotation_auto_tanakh", None/""/"other", a demoted oriented type and "other")
        # from treating each other's rows as pre-existing, or deleting them.
        planned: dict[tuple[int, int, int], dict] = {}
        planned_keys: set[tuple[int, int, int]] = set()
        named_book_ids: set[int] = set()
        for (json_type_name, real_target_title), entries in raw_groups.items():
            declared = declared_by_group[(json_type_name, real_target_title)]
            if declared == "LINKER":
                # Generator.kt: "linker"-typed rows are never imported.
                print(f'SKIPPING {len(entries)} "linker" entries -> "{real_target_title}" (the generator never imports them).')
                report["skipped_linker"] += len(entries)
                continue
            try:
                target = resolve_target(real_target_title)
            except ValueError as e:
                print(f'SKIPPING {len(entries)} entries for "{json_type_name} -> {real_target_title}": {e}')
                report["skipped_target_book_not_found"] += len(entries)
                continue
            named_book_ids.add(target["id"])
            if citing["is_sefaria"] and target["is_sefaria"]:
                # Generator.kt: Sefaria ships its own links between two Sefaria books.
                print(f'SKIPPING {len(entries)} entries -> "{real_target_title}": both books are Sefaria books.')
                report["skipped_both_sefaria"] += len(entries)
                continue

            flip, stored = plan_storage(
                declared, citing_title, citing["is_base"], real_target_title, target["is_base"]
            )
            type_id = resolve_connection_type_id(cur, stored)
            src, tgt = (target, citing) if flip else (citing, target)
            group = planned.setdefault((type_id, src["id"], tgt["id"]), {
                "label": f"{stored}: {src['title']} -> {tgt['title']}",
                "type_id": type_id, "stored": stored, "flip": flip, "src": src, "tgt": tgt,
                "rows": {}, "sources": [],
            })
            group["sources"].append(json_type_name)
            if stored == "OTHER" and declared != "OTHER":
                print(
                    f'WARNING: {len(entries)} {json_type_name!r} entries -> {real_target_title!r} are '
                    f"stored as OTHER by the generator, which the commentary panel never shows: "
                    f"they point from a non-base book into the base book {real_target_title!r}, but "
                    f"the title {citing_title!r} does not name it. Use 'source' if this is a commentary."
                )
                report["stored_as_other"][f"{json_type_name} -> {real_target_title}"] = len(entries)

            heading_ids = heading_line_ids(cur, citing_id) | heading_line_ids(cur, target["id"])
            for entry in entries:
                # Generator.kt: (line_index - 1).coerceAtLeast(0)
                f_idx = max(int(entry["line_index_1"]) - 1, 0)  # citing book, 0-based
                t_idx = max(int(entry["line_index_2"]) - 1, 0)  # path_2 book, 0-based
                if f_idx not in citing_lines or t_idx not in target["lines"]:
                    report["skipped_missing_line"] += 1
                    continue
                f_line_id = citing_lines[f_idx]
                t_line_id = target["lines"][t_idx]
                if f_line_id in heading_ids or t_line_id in heading_ids:
                    report["skipped_heading_line"] += 1
                    continue
                if flip:
                    source_line_id, target_line_id, target_line_index = t_line_id, f_line_id, f_idx
                else:
                    source_line_id, target_line_id, target_line_index = f_line_id, t_line_id, t_idx
                key = (source_line_id, target_line_id, type_id)
                if key in planned_keys:
                    report["skipped_duplicate"] += 1
                    continue
                planned_keys.add(key)
                group["rows"][key] = {
                    "fields": (src["id"], tgt["id"], source_line_id, target_line_id,
                               target_line_index, tgt["order"], type_id),
                    "entry": entry,
                    "flip": flip,
                    "file_book_id": citing_id,
                    "file_idx": f_idx,
                    "file_line_id": f_line_id,
                    "path2_book_id": target["id"],
                    "path2_idx": t_idx,
                }

        ids_by_name = {str(n).upper(): int(i) for i, n in cur.execute("SELECT id, name FROM connection_type")}
        linker_id = ids_by_name.get("LINKER")
        # Stored types the generator never flips: a row of one of these whose SOURCE is
        # the citing book can only have been written from the citing book's own file
        # (every other file stores its own book as the source of such rows; the Sefaria
        # importer only links Sefaria books; LinkerToOtzaria writes LINKER only).
        own_only_ids = {
            ids_by_name[n] for n in KNOWN_CONNECTION_TYPES
            - {"SOURCE", "LINKER", "COMMENTARY", *ORIENTED_DEPENDANT_TYPES}
            if n in ids_by_name
        }

        # ---- 2. write, group by group ----
        next_id = (cur.execute("SELECT MAX(id) FROM link").fetchone()[0] or 0) + 1
        verify_targets: dict[str, tuple[int, int, int, int]] = {}
        for (type_id, src_id, tgt_id), group in planned.items():
            label = group["label"]
            existing: dict[tuple[int, int, int], int] = {}
            extra_ids: list[int] = []
            for link_id, s_line, t_line in cur.execute(
                "SELECT id, sourceLineId, targetLineId FROM link "
                "WHERE sourceBookId=? AND targetBookId=? AND connectionTypeId=?",
                (src_id, tgt_id, type_id),
            ).fetchall():
                key = (s_line, t_line, type_id)
                if key in group["rows"] and key not in existing:
                    existing[key] = link_id
                else:
                    extra_ids.append(link_id)

            pending = []
            refreshed = 0
            # Provably this file's rows: never-flipped type out of a non-Sefaria citing book.
            own_row = (not group["flip"] and type_id in own_only_ids
                       and src_id == citing_id and not citing["is_sefaria"])
            for key, row in group["rows"].items():
                if key in existing:
                    if not replace_existing:
                        report["skipped_existing"] += 1
                        continue
                    link_id = existing[key]
                    cur.execute(
                        "UPDATE link SET targetLineIndex=?, targetBookOrderIndex=? WHERE id=?",
                        (row["fields"][4], row["fields"][5], link_id),
                    )
                    # One stored row can have two producers: the file of its stored SOURCE
                    # book (writing it as written) and the file of its stored TARGET book
                    # (writing it flipped) -- e.g. a base book's base-named "commentary" file
                    # and the commentary's own "source" file. Only satellites this file
                    # provably owns are dropped before rewriting:
                    #   * all of them, when only this file can produce the row at all;
                    #   * the side-0 anchors, when this file writes the row as written (an
                    #     anchor lives on the stored source side, i.e. this file's book).
                    # Everything else (the other producer's anchor, ranges/coverage that
                    # either producer may have written) is left, and this file's ranges are
                    # merged with the generator's widest-range rule.
                    if own_row:
                        delete_satellites(cur, [link_id])
                    elif not row["flip"] and not citing["is_sefaria"] and has_anchor:
                        cur.execute("DELETE FROM link_anchor WHERE linkId=? AND side=0", (link_id,))
                    refreshed += 1
                else:
                    link_id = next_id
                    next_id += 1
                    cur.execute(
                        """
                        INSERT INTO link (
                            id, sourceBookId, targetBookId, sourceLineId, targetLineId,
                            targetLineIndex, targetBookOrderIndex, connectionTypeId
                        ) VALUES (?,?,?,?,?,?,?,?)
                        """,
                        (link_id, *row["fields"]),
                    )
                    report["inserted"][label] = report["inserted"].get(label, 0) + 1
                pending.append({**row, "link_id": link_id})
            if refreshed:
                report["refreshed_existing"][label] = refreshed

            # Rows of the same stored identity that this file no longer produces.
            own = replace_existing and own_row
            if extra_ids and own:
                delete_links(cur, extra_ids)
                report["deleted_stale_own_rows"][label] = len(extra_ids)
                print(f'Deleted {len(extra_ids)} stale "{label}" rows (only this file can have written them).')
            elif extra_ids:
                report["other_rows_kept"][label] = len(extra_ids)
                print(f'KEPT {len(extra_ids)} other "{label}" rows this file does not produce '
                      f"(they may come from another book's file).")

            if pending:
                touched_book_ids.update((src_id, tgt_id))
                verify_targets[label] = (type_id, src_id, tgt_id, len(group["rows"]))
                print(f'Wrote {len(pending)} "{label}" links '
                      f"({len(pending) - refreshed} new, {refreshed} refreshed; from {sorted(set(group['sources']), key=str)}).")
                anchors, ranges = write_satellites(
                    cur, pending, has_anchor=has_anchor, has_range=has_range, has_coverage=has_coverage,
                )
                report["anchors"] += anchors
                report["ranges"] += ranges
            elif group["rows"]:
                print(f'All {len(group["rows"])} "{label}" links already exist (replace_existing=false).')
            else:
                print(f'Nothing to insert for "{label}" (all entries skipped).')

        # ---- 3. replace mode: rows of an earlier version of this file ----
        if replace_existing:
            planned_triples = set(planned)
            # (a) Provably this file's: never-flipped types OUT of the citing book.
            if not citing["is_sefaria"] and own_only_ids:
                stale = [
                    (lid, t, tgt) for lid, t, tgt in cur.execute(
                        "SELECT id, connectionTypeId, targetBookId FROM link WHERE sourceBookId=? "
                        f"AND connectionTypeId IN ({','.join(map(str, sorted(own_only_ids)))})",
                        (citing_id,),
                    ).fetchall()
                    if (t, citing_id, tgt) not in planned_triples
                ]
                if stale:
                    by = {}
                    for _lid, t, tgt in stale:
                        by[(t, tgt)] = by.get((t, tgt), 0) + 1
                        touched_book_ids.add(tgt)
                    delete_links(cur, [lid for lid, _t, _tgt in stale])
                    touched_book_ids.add(citing_id)
                    for (t, tgt), n in by.items():
                        lab = f"type {t}: {citing_title} -> book {tgt}"
                        report["deleted_stale_own_rows"][lab] = n
                        print(f'Deleted {n} stale "{lab}" rows (only this file can have written them).')
            # (b) Dependent-text rows INTO the citing book that this file no longer
            # produces. They may be an earlier version of this file (a line re-typed from
            # commentary->Gemara to super_commentary->Rashi leaves its old row behind), OR
            # another book's links (a super-commentary on this book, a base book's own
            # file). The DB cannot tell which, so they are reported, and deleted only when
            # the job sets delete_reported_stale=true after the user confirmed the list.
            # Also reported: dependent rows OUT of a non-base citing book INTO a base book.
            # The current generator never stores that from this file (it flips such an
            # entry, or demotes it to OTHER when the title does not name the base -- e.g.
            # an old Siddur -> Bereshit COMMENTARY row); only another book's "source" file
            # could, so it is not provable either.
            dep_ids = sorted(ids_by_name[n] for n in {"COMMENTARY", *ORIENTED_DEPENDANT_TYPES} if n in ids_by_name)
            cand = cur.execute(
                "SELECT l.id, l.sourceBookId, l.targetBookId, l.connectionTypeId, l.sourceLineId, "
                "l.targetLineId, COALESCE(s1.name,''), COALESCE(s2.name,'') FROM link l "
                "JOIN book b1 ON b1.id = l.sourceBookId JOIN book b2 ON b2.id = l.targetBookId "
                "LEFT JOIN source s1 ON s1.id = b1.sourceId LEFT JOIN source s2 ON s2.id = b2.sourceId "
                "WHERE (l.targetBookId=? OR (l.sourceBookId=? AND ? = 0 AND b2.isBaseBook = 1)) "
                f"AND l.connectionTypeId IN ({','.join(map(str, dep_ids)) or 'NULL'}) "
                "AND l.sourceBookId != l.targetBookId",
                (citing_id, citing_id, int(citing["is_base"])),
            ).fetchall() if dep_ids else []
            cand = [
                r for r in cand
                if (r[4], r[5], r[3]) not in planned_keys
                and not ("sefaria" in r[6].lower() and "sefaria" in r[7].lower())
                and r[3] != linker_id
            ]
            if cand:
                by = {}
                for _lid, s, t, ty, *_ in cand:
                    by[(s, t, ty)] = by.get((s, t, ty), 0) + 1
                titles = dict(cur.execute("SELECT id, title FROM book WHERE id IN (%s)" % ",".join(
                    str(b) for key in by for b in key[:2])).fetchall())
                names = {v: k for k, v in ids_by_name.items()}
                delete = bool(cfg.get("delete_reported_stale", False))
                if delete and citing["is_sefaria"]:
                    raise ValueError("delete_reported_stale is refused for a Sefaria book: its "
                                     "dependent rows come from the Sefaria import, not from this file.")
                for (s, t, ty), n in sorted(by.items()):
                    lab = f"{names.get(ty, ty)}: {titles.get(s, s)} -> {titles.get(t, t)}"
                    report["deleted_reported_stale" if delete else "reported_stale_candidates"][lab] = n
                    print(f'{"Deleted" if delete else "STALE?"} {n} "{lab}" rows this file does not produce'
                          + ("" if delete else " -- kept; set delete_reported_stale=true (after the user "
                                               "confirms this list) to delete them."))
                if delete:
                    delete_links(cur, [r[0] for r in cand])
                    for _lid, s, t, *_ in cand:
                        touched_book_ids.update((s, t))

        # book_has_links + book.has*Connection, recomputed for every touched book with
        # the generator's own rules (Generator.kt, "Updating book_has_links ..."), so a
        # deletion also clears flags that no longer hold.
        recompute_book_flags(cur, touched_book_ids)

        if dry_run:
            conn.rollback()
            print("\nDRY RUN complete -- nothing was written. Report:")
        else:
            conn.commit()
            print("\nCommitted. Report:")

        print(json.dumps(report, ensure_ascii=False, indent=2))

        if not dry_run:
            for group_label, (type_id, src_id, tgt_id, wanted) in verify_targets.items():
                verify = cur.execute(
                    "SELECT COUNT(*) FROM link WHERE sourceBookId=? AND targetBookId=? AND connectionTypeId=?",
                    (src_id, tgt_id, type_id),
                ).fetchone()[0]
                print(f'VERIFY "{group_label}": {verify} rows in DB ({wanted} from this file)')
            print(f"Backup kept at: {backup_path}")
            print("Close and reopen Otzaria, then open the target book to check the commentary panel.")

        return 0

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
