#!/usr/bin/env python3
"""השוואה fuzzy בין טקסט דיקטה לטקסט OCR.

הסקריפט:
  1. מנקה תגי HTML מהדיקטה (השוואה תוכנית בלבד).
  2. מנרמל את שני הצדדים (ניקוד, גרשיים, רווחים).
  3. מפצל לחלונות (sliding windows על שורות).
  4. עבור כל חלון בדיקטה — מאתר את החלון הקרוב ביותר ב‑OCR.
  5. מחזיר את הקטעים שדמיון פאזי מתחת ל‑threshold.

תלות אופציונלית: rapidfuzz (אם לא מותקן — נופל ל‑difflib).

שימוש:
    python diff_texts.py --dicta dicta.txt --ocr ocr.txt --out diff.json --threshold 0.85
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    from rapidfuzz import fuzz
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False
    import difflib


HTML_TAG = re.compile(r"<[^>]+>")
NIQQUD = re.compile(r"[֑-ׇ]")
WS = re.compile(r"\s+")
QUOTES = re.compile(r'["\'`]')


def normalize(s: str) -> str:
    s = HTML_TAG.sub("", s)
    s = NIQQUD.sub("", s)
    s = QUOTES.sub("", s)
    s = WS.sub(" ", s).strip()
    return s


def similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if HAS_RAPIDFUZZ:
        return fuzz.ratio(a, b) / 100.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def chunk_lines(lines: list[str], window: int = 5) -> list[tuple[int, str]]:
    """מחזיר רשימה של (start_line, joined_text) לחלונות בגודל window."""
    out: list[tuple[int, str]] = []
    for i in range(0, len(lines), window):
        chunk = " ".join(lines[i:i + window])
        out.append((i + 1, chunk))
    return out


def find_best_match(target: str, ocr_chunks: list[tuple[int, str]],
                    radius: int = 50, expected_idx: int = 0) -> tuple[float, int]:
    """מחזיר (score, ocr_start_line) של ההתאמה הטובה ביותר ב‑OCR."""
    lo = max(0, expected_idx - radius)
    hi = min(len(ocr_chunks), expected_idx + radius)
    if hi <= lo:
        lo, hi = 0, len(ocr_chunks)
    best_score = 0.0
    best_line = 0
    for i in range(lo, hi):
        s = similarity(target, ocr_chunks[i][1])
        if s > best_score:
            best_score = s
            best_line = ocr_chunks[i][0]
            if best_score > 0.98:
                break
    return best_score, best_line


def main() -> int:
    p = argparse.ArgumentParser(description="diff fuzzy בין דיקטה ל‑OCR")
    p.add_argument("--dicta", required=True)
    p.add_argument("--ocr", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--threshold", type=float, default=0.85,
                   help="מתחת לזה מסומן כפער (0..1)")
    p.add_argument("--window", type=int, default=5,
                   help="גודל חלון בשורות")
    p.add_argument("--context", type=int, default=2,
                   help="כמה שורות הקשר להחזיר לכל פער")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    dicta_path = Path(args.dicta).expanduser()
    ocr_path = Path(args.ocr).expanduser()

    if not dicta_path.exists():
        print(f"ERROR: dicta not found: {dicta_path}", file=sys.stderr)
        return 2
    if not ocr_path.exists():
        print(f"ERROR: ocr not found: {ocr_path}", file=sys.stderr)
        return 2

    dicta_raw = dicta_path.read_text(encoding="utf-8").splitlines()
    ocr_raw = ocr_path.read_text(encoding="utf-8").splitlines()

    dicta_norm = [normalize(line) for line in dicta_raw]
    ocr_norm = [normalize(line) for line in ocr_raw]

    dicta_norm_filt = [(i, t) for i, t in enumerate(dicta_norm) if t]
    ocr_norm_filt = [(i, t) for i, t in enumerate(ocr_norm) if t and not t.startswith("=== PAGE")]

    dicta_chunks = chunk_lines([t for _, t in dicta_norm_filt], args.window)
    ocr_chunks = chunk_lines([t for _, t in ocr_norm_filt], args.window)

    issues: list[dict] = []
    ratio = len(ocr_chunks) / max(1, len(dicta_chunks))

    for chunk_idx, (start_idx, text) in enumerate(dicta_chunks):
        if not text.strip():
            continue
        expected = int(chunk_idx * ratio)
        score, ocr_line = find_best_match(text, ocr_chunks, radius=50, expected_idx=expected)
        if score >= args.threshold:
            continue

        # Map start_idx (חלון בתוך filt) לשורה ב-RAW
        raw_idx = start_idx - 1
        if raw_idx < len(dicta_norm_filt):
            real_line = dicta_norm_filt[raw_idx][0] + 1
        else:
            real_line = 0

        ctx_lo = max(0, real_line - 1 - args.context)
        ctx_hi = min(len(dicta_raw), real_line - 1 + args.window + args.context)

        issues.append({
            "dicta_line": real_line,
            "score": round(score, 3),
            "dicta_excerpt": "\n".join(dicta_raw[ctx_lo:ctx_hi]),
            "ocr_best_line": ocr_line,
            "ocr_excerpt": ocr_chunks[ocr_line - 1][1] if 0 < ocr_line <= len(ocr_chunks) else "",
        })

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps({"threshold": args.threshold, "count": len(issues), "issues": issues},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    summary = {
        "ok": True,
        "out": args.out,
        "dicta_chunks": len(dicta_chunks),
        "ocr_chunks": len(ocr_chunks),
        "issues": len(issues),
        "threshold": args.threshold,
        "rapidfuzz": HAS_RAPIDFUZZ,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"חלונות: {len(dicta_chunks)} (דיקטה) / {len(ocr_chunks)} (OCR)")
        print(f"פערים מתחת ל‑{args.threshold}: {len(issues)} → {args.out}")
        if not HAS_RAPIDFUZZ:
            print("  (rapidfuzz לא מותקן — שימוש ב‑difflib האטי)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
