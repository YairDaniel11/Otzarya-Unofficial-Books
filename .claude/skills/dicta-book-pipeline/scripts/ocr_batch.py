#!/usr/bin/env python3
"""שליחת תמונות עמוד לשרת ה‑OCR (oneocr / win11) במקביל.

API:
    POST <OCRWIN_URL>
    Header: X-API-Key: <OCRWIN_API_KEY>
    Body: multipart/form-data, field "file"
    Response: {"text": "..."} (or {"content": ...})

מקור: /Users/david/Documents/Otzaria_Website/src/app/api/ocrwin/route.js

URL ו‑API key נטענים בסדר:
    1. CLI flags --url --api-key
    2. env vars OCRWIN_URL, OCRWIN_API_KEY
    3. .env.local באותה תיקיית skill

שימוש:
    python ocr_batch.py --in-dir pages/ --out ocr.txt --concurrency 8
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

PAGE_SEP = "\n\n=== PAGE {n} ===\n\n"


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


def _multipart_body(file_bytes: bytes, filename: str) -> tuple[bytes, str]:
    boundary = "----dicta" + uuid.uuid4().hex
    parts = []
    parts.append(f"--{boundary}".encode())
    parts.append(
        f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode()
    )
    parts.append(b"Content-Type: image/png")
    parts.append(b"")
    parts.append(file_bytes)
    parts.append(f"--{boundary}--".encode())
    parts.append(b"")
    body = b"\r\n".join(parts)
    return body, f"multipart/form-data; boundary={boundary}"


def ocr_one(image: Path, url: str, api_key: str, retries: int = 2,
            timeout: int = 120) -> tuple[Path, str | None, str | None]:
    """מחזיר (path, text, error)."""
    body, ctype = _multipart_body(image.read_bytes(), image.name)
    headers = {"X-API-Key": api_key, "Content-Type": ctype}

    last_err: str | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    text = data.get("text") or data.get("content") or ""
                else:
                    text = str(data)
            except json.JSONDecodeError:
                text = raw
            return image, text, None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = str(e)
            if attempt < retries:
                time.sleep(2 ** attempt)
        except Exception as e:
            last_err = str(e)
            break
    return image, None, last_err


def _page_num(p: Path) -> int:
    m = re.search(r"(\d+)", p.stem)
    return int(m.group(1)) if m else 0


def main() -> int:
    p = argparse.ArgumentParser(description="OCR מקבילי על עמודי PNG")
    p.add_argument("--in-dir", required=True, help="תיקיית עמודים")
    p.add_argument("--out", required=True, help="קובץ פלט (טקסט מאוחד)")
    p.add_argument("--ext", default="png", choices=["png", "jpg", "jpeg"])
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--url", help="OCRWIN_URL")
    p.add_argument("--api-key", help="OCRWIN_API_KEY")
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    env_local = _load_env_local()
    url = args.url or os.environ.get("OCRWIN_URL") or env_local.get("OCRWIN_URL")
    api_key = args.api_key or os.environ.get("OCRWIN_API_KEY") or env_local.get("OCRWIN_API_KEY")

    if not url or not api_key:
        msg = "חסר OCRWIN_URL/OCRWIN_API_KEY. הגדר env vars או צור .env.local ב‑skill"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 2

    in_dir = Path(args.in_dir).expanduser()
    out_file = Path(args.out).expanduser()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    pages = sorted(in_dir.glob(f"*.{args.ext}"), key=_page_num)
    if not pages:
        msg = f"לא נמצאו תמונות {args.ext} ב‑{in_dir}"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 1

    results: dict[Path, tuple[str | None, str | None]] = {}
    started = time.time()
    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {
            ex.submit(ocr_one, page, url, api_key, args.retries, args.timeout): page
            for page in pages
        }
        done = 0
        for fut in cf.as_completed(futures):
            page = futures[fut]
            try:
                _, text, err = fut.result()
            except Exception as e:
                text, err = None, str(e)
            results[page] = (text, err)
            done += 1
            if not args.json:
                status = "OK" if text else f"FAIL ({err})"
                print(f"  [{done}/{len(pages)}] {page.name}  {status}", file=sys.stderr)

    elapsed = time.time() - started

    # כתיבה
    parts: list[str] = []
    errors: list[dict] = []
    for page in pages:
        text, err = results.get(page, (None, "missing"))
        n = _page_num(page)
        parts.append(PAGE_SEP.format(n=n))
        if text:
            parts.append(text)
        else:
            parts.append(f"[OCR ERROR: {err}]")
            errors.append({"page": str(page), "error": err})
    out_file.write_text("".join(parts), encoding="utf-8")

    summary = {
        "ok": True,
        "out": str(out_file),
        "pages_total": len(pages),
        "pages_failed": len(errors),
        "elapsed_seconds": round(elapsed, 1),
        "errors": errors,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(
            f"\nסיכום: {len(pages) - len(errors)}/{len(pages)} עמודים | "
            f"{elapsed:.0f}ש' | פלט: {out_file}"
        )
        if errors:
            print(f"שגיאות: {len(errors)} עמודים נכשלו (ראה {out_file})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
