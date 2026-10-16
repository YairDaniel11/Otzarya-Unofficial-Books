#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit whether a commentary book was correctly ingested into Otzaria seforim.db."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path


# The generator's link-storage rules (flip / stored type / flags / heading lines) are
# ported once, in the otzaria-db-linker skill's engine, and tested against
# SeforimLibrary's Generator.kt there. This read-only audit reuses them.
_ICL_PATH = (Path(__file__).resolve().parents[2] / "otzaria-db-linker" / "scripts"
             / "insert_commentary_link.py")
_icl_spec = importlib.util.spec_from_file_location("insert_commentary_link", _ICL_PATH)
icl = importlib.util.module_from_spec(_icl_spec)
_icl_spec.loader.exec_module(icl)


def stored_type_name(declared: object) -> str:
    """connection_type.name a declared value is stored under when the generator does not
    demote it to OTHER: "source" -> COMMENTARY, everything else its own type."""
    canonical = icl.canonical_type(declared)
    return "COMMENTARY" if canonical == "SOURCE" else canonical


def resolve_db(path: str | None) -> Path:
    if path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(p)
        return p

    prefs = Path.home() / "AppData/Roaming/otzaria/shared_preferences.json"
    if prefs.exists():
        import re

        text = prefs.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'"flutter\.key-library-path"\s*:\s*"([^"]+)"', text)
        if m:
            lib = Path(m.group(1).replace("\\\\", "\\"))
            cand = lib / "seforim.db"
            if cand.exists():
                return cand

    fallback = Path(r"C:\ProgramData\otzaria\books\seforim.db")
    if fallback.exists():
        return fallback
    raise FileNotFoundError("seforim.db not found; pass --db")


def book_by_title(cur: sqlite3.Cursor, title: str):
    return cur.execute(
        "SELECT id, title, totalLines, orderIndex, hasCommentaryConnection "
        "FROM book WHERE title=?",
        (title,),
    ).fetchone()


def line_count(cur: sqlite3.Cursor, book_id: int) -> int:
    return cur.execute(
        "SELECT COUNT(*) FROM line WHERE bookId=?", (book_id,)
    ).fetchone()[0]


def line_map(cur: sqlite3.Cursor, book_id: int) -> dict[int, int]:
    return {
        li: lid
        for li, lid in cur.execute(
            "SELECT lineIndex, id FROM line WHERE bookId=?", (book_id,)
        )
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", help="Path to live seforim.db")
    ap.add_argument("--citing", required=True, help="Commentary book title")
    ap.add_argument("--target", required=True, help="Base/tractate book title")
    ap.add_argument("--links", help="Optional path to *_links.json")
    ap.add_argument(
        "--type-id",
        type=int,
        help="connectionTypeId to audit (overrides --type and the links file)",
    )
    ap.add_argument(
        "--type",
        help="connection type name to audit, e.g. footnotes (default: each links-file "
             "entry's own stored type, else COMMENTARY)",
    )
    args = ap.parse_args()

    failures: list[str] = []
    notes: list[str] = []

    try:
        db_path = resolve_db(args.db)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        print(f"ERROR: cannot open DB: {e}", file=sys.stderr)
        return 2

    cur = conn.cursor()
    ids_by_name = {
        str(name).upper(): int(cid)
        for cid, name in cur.execute("SELECT id, name FROM connection_type")
    }
    data = None
    if args.links and Path(args.links).exists():
        data = json.loads(Path(args.links).read_text(encoding="utf-8"))

    # --type-id / --type force one stored type for every entry (direction is still the
    # generator's). Otherwise each entry is checked against the type it is stored under.
    forced_type_id = args.type_id
    if forced_type_id is None and args.type:
        try:
            forced_type_id = ids_by_name.get(stored_type_name(args.type))
        except ValueError as e:
            failures.append(str(e))
        if forced_type_id is None:
            failures.append(f"connection type {args.type!r} is not in this DB's connection_type table")
    if forced_type_id is not None:
        wanted = {next((k for k, v in ids_by_name.items() if v == forced_type_id), "?")}
    elif data is not None:
        wanted = set()
        for item in data:
            try:
                declared = icl.canonical_type(item.get("Conection Type"))
            except ValueError:
                continue
            if declared == "LINKER":
                continue
            wanted.add("COMMENTARY" if declared == "SOURCE" else declared)
            if declared in icl.ORIENTED_DEPENDANT_TYPES:
                wanted.add("OTHER")  # the generator's demotion, see icl.plan_storage
        needed = {n for n in wanted if n != "OTHER"}
        unknown = sorted(n for n in needed if n not in ids_by_name)
        if unknown:
            failures.append(f"connection_type missing from this DB for links-file types: {unknown}")
    else:
        wanted = {"COMMENTARY"}
    type_ids = sorted(ids_by_name[n] for n in wanted if n in ids_by_name)
    if forced_type_id is not None:
        type_ids = [forced_type_id]
    id_to_name = {v: k for k, v in ids_by_name.items()}
    type_label = ",".join(f"{i}={id_to_name.get(i, '?')}" for i in type_ids) or "none"
    type_in = "(" + ",".join(str(i) for i in type_ids) + ")" if type_ids else "(NULL)"
    expect_rows: set[tuple[int, int, int]] = set()

    citing = book_by_title(cur, args.citing)
    target = book_by_title(cur, args.target)

    print("## תוצאת ביקורת הכנסה ל-DB")
    print(f"- DB: {db_path}")

    if not citing:
        failures.append(f'המפרש "{args.citing}" לא נמצא ב-book (כותרת מדויקת)')
        print("- מפרש: חסר")
    else:
        c_lines = line_count(cur, citing[0])
        print(
            f"- מפרש: {citing[1]} (id={citing[0]}, totalLines={citing[2]}, "
            f"line_rows={c_lines}, hasCommentaryConnection={citing[4]})"
        )
        if citing[2] <= 0:
            failures.append("למפרש totalLines=0")
        if citing[2] != c_lines:
            failures.append(
                f"אי-התאמה totalLines({citing[2]}) מול COUNT(line)={c_lines}"
            )

    if not target:
        failures.append(f'הבסיס "{args.target}" לא נמצא ב-book (כותרת מדויקת)')
        print("- בסיס: חסר")
    else:
        t_lines = line_count(cur, target[0])
        print(
            f"- בסיס: {target[1]} (id={target[0]}, totalLines={target[2]}, "
            f"line_rows={t_lines}, hasCommentaryConnection={target[4]})"
        )
        if target[2] <= 0:
            failures.append("לבסיס totalLines=0")
        if target[2] != t_lines:
            failures.append(
                f"אי-התאמה totalLines({target[2]}) מול COUNT(line)={t_lines} בבסיס"
            )

    base_to_citing = reverse = 0
    if citing and target:
        base_to_citing = cur.execute(
            "SELECT COUNT(*) FROM link WHERE sourceBookId=? AND targetBookId=? "
            f"AND connectionTypeId IN {type_in}",
            (target[0], citing[0]),
        ).fetchone()[0]
        reverse = cur.execute(
            "SELECT COUNT(*) FROM link WHERE sourceBookId=? AND targetBookId=? "
            f"AND connectionTypeId IN {type_in}",
            (citing[0], target[0]),
        ).fetchone()[0]
        print(f"- {args.target} -> {args.citing} (type={type_label}): {base_to_citing}")
        print(f"- {args.citing} -> {args.target} (type={type_label}): {reverse}")

        bhl_c = cur.execute(
            "SELECT * FROM book_has_links WHERE bookId=?", (citing[0],)
        ).fetchone()
        bhl_t = cur.execute(
            "SELECT * FROM book_has_links WHERE bookId=?", (target[0],)
        ).fetchone()
        print(f"- book_has_links {args.citing}: {bhl_c}")
        print(f"- book_has_links {args.target}: {bhl_t}")

        if data is None:
            # No links file: the classic commentary shape, stored base -> citing.
            if base_to_citing == 0:
                failures.append(
                    f"no {type_label} rows {args.target} -> {args.citing} (base -> commentary); "
                    f"the commentary will not show in the panel"
                )
            if reverse and not base_to_citing:
                failures.append(
                    f"rows only in the reverse direction {args.citing} -> {args.target} "
                    f"(written as the JSON reads, not flipped)"
                )
            elif reverse and base_to_citing:
                notes.append(f"{reverse} rows also exist in the reverse direction; check for leftovers")
            if base_to_citing > 0:
                expect_rows = {(target[0], citing[0], t) for t in type_ids}
            else:
                expect_rows = set()
        else:
            expect_rows = set()  # filled from the per-entry coverage below

    # JSON coverage: every entry against ITS OWN path_2 book, stored the way
    # SeforimLibrary's Generator.kt stores it (icl.plan_storage: flip / stored type).
    if args.links and citing:
        links_path = Path(args.links)
        if not links_path.exists():
            failures.append(f"links file not found: {links_path}")
        else:
            try:
                file_book = icl.get_book_info(cur, args.citing)
            except ValueError as e:
                failures.append(str(e))
                file_book = None
            cmap = line_map(cur, citing[0])
            books: dict[str, dict | None] = {}
            per_book: dict[str, list[int]] = {}   # title -> [ok, missing_db]
            missing_lines = missing_book = skipped_heading = skipped_linker = skipped_sefaria = 0
            unknown_types: set[str] = set()
            for item in data if file_book else []:
                raw_type = item.get("Conection Type")
                try:
                    declared = icl.canonical_type(raw_type)
                except ValueError:
                    unknown_types.add(repr(raw_type))
                    continue
                if declared == "LINKER":
                    skipped_linker += 1
                    continue
                p2_title = icl.generator_target_title(str(item.get("path_2", "")))
                if p2_title not in books:
                    try:
                        info = icl.get_book_info(cur, p2_title)
                        info["lines"] = line_map(cur, info["id"])
                    except ValueError:
                        info = None
                    books[p2_title] = info
                p2 = books[p2_title]
                if p2 is None:
                    missing_book += 1
                    continue
                if file_book["is_sefaria"] and p2["is_sefaria"]:
                    skipped_sefaria += 1
                    continue
                c_idx = max(int(item["line_index_1"]) - 1, 0)
                t_idx = max(int(item["line_index_2"]) - 1, 0)
                if c_idx not in cmap or t_idx not in p2["lines"]:
                    missing_lines += 1
                    continue
                f_line, t_line = cmap[c_idx], p2["lines"][t_idx]
                if (f_line in icl.heading_line_ids(cur, citing[0])
                        or t_line in icl.heading_line_ids(cur, p2["id"])):
                    skipped_heading += 1
                    continue
                flip, stored = icl.plan_storage(
                    declared, args.citing, file_book["is_base"], p2_title, p2["is_base"]
                )
                type_id = forced_type_id if forced_type_id is not None else ids_by_name.get(stored)
                src_book, tgt_book = (p2["id"], citing[0]) if flip else (citing[0], p2["id"])
                src_line, tgt_line = (t_line, f_line) if flip else (f_line, t_line)
                n = cur.execute(
                    "SELECT COUNT(*) FROM link WHERE sourceBookId=? AND "
                    "targetBookId=? AND sourceLineId=? AND targetLineId=? "
                    "AND connectionTypeId=?",
                    (src_book, tgt_book, src_line, tgt_line, type_id),
                ).fetchone()[0]
                tally = per_book.setdefault(p2_title, [0, 0])
                if n:
                    tally[0] += 1
                    expect_rows.add((src_book, tgt_book, type_id))
                else:
                    tally[1] += 1
            ok = sum(t[0] for t in per_book.values())
            missing_db = sum(t[1] for t in per_book.values())
            print(
                f"- JSON coverage ({links_path.name}): {ok}/{len(data)} "
                f"(missing line={missing_lines}, missing in DB={missing_db}, "
                f"path_2 book not in DB={missing_book}, heading lines skipped={skipped_heading}, "
                f"linker skipped={skipped_linker}, Sefaria<->Sefaria skipped={skipped_sefaria})"
            )
            for title, (n_ok, n_missing) in sorted(per_book.items()):
                print(f"  - path_2 {title}: {n_ok} ok, {n_missing} missing")
            if unknown_types:
                failures.append(f"unrecognized Conection Type values: {sorted(unknown_types)}")
            if missing_lines:
                failures.append(f"{missing_lines} JSON entries point at a line that is not in the DB")
            if missing_book:
                failures.append(f"{missing_book} JSON entries point at a path_2 book that is not in the DB")
            if missing_db:
                failures.append(
                    f"{missing_db}/{len(data)} JSON entries have no row stored the way the "
                    f"generator stores them (book, direction, type)"
                )
            ok_target = per_book.get(args.target, [0, 0])[0]
            if target and missing_db == 0 and base_to_citing + reverse != ok_target:
                notes.append(
                    f"DB has {base_to_citing + reverse} {type_label} rows between {args.target} and "
                    f"{args.citing}; the JSON accounts for {ok_target} (extra rows: another file, or leftovers)"
                )

    # Flags, exactly as Generator.kt derives them from the stored rows checked above.
    if citing:
        cols = {r[1] for r in cur.execute("PRAGMA table_info(book)")}
        flag_by_type = {ids_by_name.get(t): c for t, c in icl.TYPE_FLAG_COLUMNS.items()}
        source_ids = {ids_by_name.get(t) for t in icl.SOURCE_CONNECTION_TYPES}
        for src_book, tgt_book, type_id in sorted(expect_rows):
            bhl_src = cur.execute("SELECT hasSourceLinks FROM book_has_links WHERE bookId=?", (src_book,)).fetchone()
            bhl_tgt = cur.execute("SELECT hasTargetLinks FROM book_has_links WHERE bookId=?", (tgt_book,)).fetchone()
            if not bhl_src or bhl_src[0] != 1:
                failures.append(f"book_has_links.hasSourceLinks!=1 for book id {src_book}")
            if not bhl_tgt or bhl_tgt[0] != 1:
                failures.append(f"book_has_links.hasTargetLinks!=1 for book id {tgt_book}")
            column = flag_by_type.get(type_id)
            if column in cols:
                for book_id in (src_book, tgt_book):
                    if cur.execute(f"SELECT {column} FROM book WHERE id=?", (book_id,)).fetchone()[0] != 1:
                        failures.append(f"book id {book_id}: {column}!=1 although it has such links")
            if (type_id in source_ids and src_book != tgt_book and "hasSourceConnection" in cols
                    and cur.execute("SELECT hasSourceConnection FROM book WHERE id=?", (tgt_book,)).fetchone()[0] != 1):
                failures.append(f"book id {tgt_book}: hasSourceConnection!=1 although it is a dependent text")

    conn.close()

    if notes:
        print("- הערות:")
        for n in notes:
            print(f"  - {n}")

    if failures:
        print("- סטטוס: FAIL")
        print("- כשלים:")
        for i, f in enumerate(failures, 1):
            print(f"  {i}. {f}")
        print(
            "- המלצה: אם המשתמש מבקש תיקון — גיבוי DB, סגירת אוצריא, "
            "הכנסת קישורים בכיוון בסיס→מפרש, עדכון דגלים, ואז ביקורת חוזרת."
        )
        return 1

    print("- סטטוס: PASS")
    print("- הכנסת הספר/הקישורים ל-DB נראית תקינה לתצוגת מפרש על הבסיס.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
