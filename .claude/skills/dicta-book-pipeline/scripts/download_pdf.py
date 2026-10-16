#!/usr/bin/env python3
"""הורדת PDF מ-HebrewBooks לפי מזהה.

URL ההורדה הישיר זהה לכל הספרים:
    https://download.hebrewbooks.org/downloadhandler.ashx?req=<ID>

שימוש:
    python download_pdf.py --id 14053 --out /tmp/source.pdf
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

URL_TEMPLATE = "https://download.hebrewbooks.org/downloadhandler.ashx?req={id}"


def download(book_id: str, out: Path, force: bool = False, json_out: bool = False) -> int:
    if out.exists() and not force:
        if json_out:
            print(json.dumps({"ok": True, "cached": True, "path": str(out), "size": out.stat().st_size}))
        else:
            print(f"קיים ב‑cache: {out}")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    url = URL_TEMPLATE.format(id=book_id)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (dicta-pipeline)"})

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "pdf" not in ctype.lower() and "octet-stream" not in ctype.lower():
                # ייתכן שזה דף שגיאה. בכל זאת ננסה לכתוב, אבל נזהיר.
                pass
            data = resp.read()
    except Exception as e:
        if json_out:
            print(json.dumps({"ok": False, "error": str(e), "url": url}))
        else:
            print(f"שגיאה בהורדה: {e}\nURL: {url}", file=sys.stderr)
        return 1

    out.write_bytes(data)

    # בדיקה בסיסית — PDF מתחיל ב-%PDF
    if not data[:4] == b"%PDF":
        out.unlink(missing_ok=True)
        msg = f"הקובץ שהתקבל אינו PDF (ייתכן ID שגוי). URL: {url}"
        if json_out:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 1

    if json_out:
        print(json.dumps({"ok": True, "cached": False, "path": str(out), "size": len(data)}))
    else:
        print(f"הורד: {out}  ({len(data) / 1024 / 1024:.1f} MB)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="הורדת PDF מ‑HebrewBooks")
    p.add_argument("--id", required=True, help="מזהה הספר ב‑HebrewBooks")
    p.add_argument("--out", required=True, help="נתיב פלט ל‑PDF")
    p.add_argument("--force", action="store_true", help="הורדה מחדש גם אם קיים")
    p.add_argument("--json", action="store_true", help="פלט JSON")
    args = p.parse_args()
    return download(str(args.id), Path(args.out).expanduser(), args.force, args.json)


if __name__ == "__main__":
    sys.exit(main())
