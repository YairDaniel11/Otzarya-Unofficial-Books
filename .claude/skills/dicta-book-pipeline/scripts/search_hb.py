#!/usr/bin/env python3
"""חיפוש ספר ב‑otzar-HB_catalog.db, טבלת ``hebrew_books`` בלבד.

ספרי דיקטה מקורם בהיברו‑בוקס בלבד — לא לחפש ב‑``otzar_hahochma``.

סכמת ``hebrew_books``:
    id_book INTEGER PRIMARY KEY  -- זה ה‑ID להורדת PDF
    title TEXT
    author TEXT
    printing_place TEXT
    printing_year TEXT            -- גימטריה (תרכו)
    pub_date INTEGER              -- שנה מספרית (1866)
    pages INTEGER
    tags TEXT                     -- מערך JSON של תגים, כגון ["שו\"ת","גאונים"]

שימוש:
    python search_hb.py --title "גאון יעקב"
    python search_hb.py --title "..." --json
    python search_hb.py --title "..." --db /path/to/other.db
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path("~/Downloads/otzaria_latest/otzaria/otzar-HB_catalog.db").expanduser()
TABLE = "hebrew_books"

NIQQUD = re.compile(r"[֑-ׇ]")
QUOTES = re.compile(r'["\']+')
WS = re.compile(r"\s+")


def _load_env_local() -> dict[str, str]:
    here = Path(__file__).resolve().parent.parent
    env_file = here / ".env.local"
    out: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _normalize(s: str | None) -> str:
    if not s:
        return ""
    s = NIQQUD.sub("", s)
    s = QUOTES.sub("", s)
    s = WS.sub(" ", s).strip()
    return s


def _score(title_norm: str, q_norm: str) -> float:
    if not title_norm:
        return 0.0
    if title_norm == q_norm:
        return 100.0
    if title_norm.startswith(q_norm):
        return 80.0
    if q_norm in title_norm:
        return 60.0
    # מילים משותפות
    common = set(title_norm.split()) & set(q_norm.split())
    if common:
        return 30.0 + 5.0 * len(common)
    return 10.0


def search(db: Path, title_query: str, limit: int = 25) -> list[dict]:
    if not db.exists():
        raise FileNotFoundError(f"DB not found: {db}")

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # ודא שהטבלה קיימת
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE,),
        )
        if not cur.fetchone():
            raise RuntimeError(
                f"טבלת '{TABLE}' לא קיימת ב‑DB. ייתכן שזה DB אחר — בדוק עם '.tables'."
            )

        q_norm = _normalize(title_query)

        # שאילתה אחת עם two LIKE patterns בעזרת UNION (להחזרת תוצאות גם עם וגם בלי נירמול)
        rows = conn.execute(
            f"""
            SELECT id_book, title, author, printing_place, printing_year,
                   pub_date, pages, tags
              FROM {TABLE}
             WHERE title LIKE ? OR title LIKE ?
             LIMIT ?
            """,
            (f"%{title_query}%", f"%{q_norm}%", limit * 3),
        ).fetchall()

        scored: list[tuple[float, dict]] = []
        seen_ids: set[int] = set()
        for r in rows:
            d = dict(r)
            bid = d["id_book"]
            if bid in seen_ids:
                continue
            seen_ids.add(bid)

            # פרסור tags JSON
            tags_raw = d.get("tags") or ""
            try:
                d["tags"] = json.loads(tags_raw) if tags_raw else []
            except (json.JSONDecodeError, TypeError):
                d["tags"] = [tags_raw] if tags_raw else []

            score = _score(_normalize(d.get("title")), q_norm)
            scored.append((score, d))

        scored.sort(key=lambda x: -x[0])
        return [d for _, d in scored[:limit]]
    finally:
        conn.close()


def main() -> int:
    env_local = _load_env_local()
    default_db = (
        os.environ.get("HB_CATALOG_DB")
        or env_local.get("HB_CATALOG_DB")
        or str(DEFAULT_DB)
    )
    p = argparse.ArgumentParser(
        description="חיפוש ב‑hebrew_books (otzar-HB_catalog.db)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--title", required=True, help="כותרת לחיפוש")
    p.add_argument("--db", default=default_db,
                   help=f"נתיב ל‑DB (ברירת מחדל: {default_db})")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    db = Path(args.db).expanduser()

    try:
        results = search(db, args.title, args.limit)
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        else:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({
            "ok": True,
            "table": TABLE,
            "db": str(db),
            "count": len(results),
            "download_url_template": "https://download.hebrewbooks.org/downloadhandler.ashx?req={id_book}",
            "results": results,
        }, ensure_ascii=False, indent=2))
        return 0

    if not results:
        print("לא נמצאו תוצאות.")
        return 0

    print(f"{len(results)} תוצאות מתוך טבלת '{TABLE}':")
    print("-" * 90)
    for d in results:
        bid = d["id_book"]
        title = d.get("title") or "?"
        author = d.get("author") or ""
        year = d.get("printing_year") or ""
        place = d.get("printing_place") or ""
        pages = d.get("pages") or ""
        tags = ", ".join(d.get("tags") or [])
        meta = " | ".join(filter(None, [author, f"{place} {year}".strip(), f"{pages}עמ" if pages else "", tags]))
        print(f"  [{bid:>6}] {title}")
        if meta:
            print(f"           {meta}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
