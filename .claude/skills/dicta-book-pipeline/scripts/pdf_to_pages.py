#!/usr/bin/env python3
"""המרת PDF לעמודי PNG (עוטף pdftoppm).

דרושה התקנה של poppler-utils:
    brew install poppler

שימוש:
    python pdf_to_pages.py --pdf source.pdf --out-dir pages/ --dpi 300
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def extract(pdf: Path, out_dir: Path, dpi: int = 300, fmt: str = "png",
            first: int | None = None, last: int | None = None,
            json_out: bool = False) -> int:
    if not pdf.exists():
        msg = f"PDF לא נמצא: {pdf}"
        if json_out:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 2

    if not shutil.which("pdftoppm"):
        msg = "pdftoppm לא מותקן. הרץ: brew install poppler"
        if json_out:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 3

    out_dir.mkdir(parents=True, exist_ok=True)

    # אם כבר יש עמודים — נחזיר אותם בלי המרה מחדש
    existing = sorted(out_dir.glob(f"page-*.{fmt}"))
    if existing and first is None and last is None:
        if json_out:
            print(json.dumps({
                "ok": True, "cached": True,
                "count": len(existing),
                "pages": [str(p) for p in existing],
            }))
        else:
            print(f"קיים ב‑cache: {len(existing)} עמודים")
        return 0

    cmd = [
        "pdftoppm",
        "-r", str(dpi),
        f"-{fmt}",
        str(pdf),
        str(out_dir / "page"),
    ]
    if first is not None:
        cmd += ["-f", str(first)]
    if last is not None:
        cmd += ["-l", str(last)]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        msg = e.stderr or e.stdout or str(e)
        if json_out:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 1

    pages = sorted(out_dir.glob(f"page-*.{fmt}"))
    if json_out:
        print(json.dumps({
            "ok": True, "cached": False,
            "count": len(pages),
            "pages": [str(p) for p in pages],
        }))
    else:
        print(f"חולץ: {len(pages)} עמודים → {out_dir}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="חילוץ עמודי PDF ל‑PNG")
    p.add_argument("--pdf", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--fmt", choices=["png", "jpeg"], default="png")
    p.add_argument("--first", type=int)
    p.add_argument("--last", type=int)
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    return extract(
        Path(a.pdf).expanduser(),
        Path(a.out_dir).expanduser(),
        a.dpi, a.fmt, a.first, a.last, a.json,
    )


if __name__ == "__main__":
    sys.exit(main())
