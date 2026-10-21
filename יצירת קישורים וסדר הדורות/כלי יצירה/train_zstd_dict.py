#!/usr/bin/env python3
"""אימון חד-פעמי של המילון הקבוע (zstd_dict_32768.bin) ישירות מקבצי ה-TXT של הספרים.

המילון נשמר במאגר ונטען ע"י compress_personal_db.py בכל בנייה. אין לאמן מחדש בלי סיבה:
כל שינוי במילון משנה את כל בלוקי line_content ומנפח את הדלתא לגרסה הבאה פי כמה.

שימוש:  python -X utf8 train_zstd_dict.py [--root ספרים] [--out zstd_dict_32768.bin] [--dict-size 112640]
"""
import argparse
import os
import sys

import zstandard as zstd

DICT_ID = 32768
SAMPLE_BYTES = 64 * 1024 * 1024
MAX_SAMPLE_LEN = 8192
MIN_SAMPLE_LEN = 16
HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.normpath(os.path.join(HERE, "..", "..", "ספרים")))
    ap.add_argument("--out", default=os.path.join(HERE, "zstd_dict_32768.bin"))
    ap.add_argument("--dict-size", type=int, default=112640)
    a = ap.parse_args()
    files = sorted(os.path.join(d, f) for d, _, fs in os.walk(a.root) for f in fs if f.endswith(".txt"))
    total = sum(os.path.getsize(f) for f in files)
    step = max(1, total // SAMPLE_BYTES + 1)
    samples, used, i = [], 0, 0
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh.read().replace("\r\n", "\n").split("\n"):
                i += 1
                if i % step:
                    continue
                b = line.encode("utf-8")[:MAX_SAMPLE_LEN]
                if len(b) >= MIN_SAMPLE_LEN:
                    samples.append(b)
                    used += len(b)
    print(f"{len(files)} קבצים, מדגם {len(samples)} שורות, {used/1e6:.1f}MB (כל {step}-ית)")
    d = zstd.train_dictionary(a.dict_size, samples, dict_id=DICT_ID, threads=1)
    with open(a.out, "wb") as o:
        o.write(d.as_bytes())
    print(f"נכתב {a.out}: {len(d.as_bytes())} בייט, dictID={d.dict_id()}")


if __name__ == "__main__":
    sys.exit(main())
