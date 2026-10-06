#!/usr/bin/env python3
"""מעבד-אחרי: דוחס את טקסט השורות של מסד אישי שנבנה ע"י build_personal_db.py.

קלט:  DB עם line.content (TEXT).
פלט:  DB חדש בסכמה 6 של אוצריא (DbCapabilities.hasSplitLineContent):
        line_content(id, content BLOB)  - כל שורה מסגרת zstd עם מילון
        zstd_dict(id, dict BLOB)        - המילון (ה-id = dictID שבמסגרות)
        line בלי עמודת content
      שאר הטבלאות ללא שינוי. VACUUM בסוף. דטרמיניסטי: אותו קלט + אותם
      פרמטרים => אותו קובץ.

שימוש:  python compress_personal_db.py IN.db OUT.db [--level 19] [--dict-size 112640]
דורש: pip install zstandard
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time

import zstandard as zstd

DICT_ID = 32768          # כמו במסד הרשמי; אוצריא בוחרת מילון לפי dictID שבמסגרת
SAMPLE_BYTES = 64 * 1024 * 1024
MAX_SAMPLE_LEN = 8192
MIN_SAMPLE_LEN = 16
BATCH = 20000


def pick_samples(db):
    """מדגם דטרמיניסטי: כל k-ית שורה (לפי id), עד SAMPLE_BYTES."""
    total, nbytes = db.execute(
        "SELECT COUNT(*), SUM(LENGTH(CAST(content AS BLOB))) FROM line").fetchone()
    nbytes = nbytes or 0
    step = max(1, nbytes // SAMPLE_BYTES + 1)
    samples, used = [], 0
    for i, (content,) in enumerate(db.execute("SELECT content FROM line ORDER BY id")):
        if i % step:
            continue
        b = content.encode("utf-8")[:MAX_SAMPLE_LEN]
        if len(b) < MIN_SAMPLE_LEN:
            continue
        samples.append(b)
        used += len(b)
    return samples, used, step


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--level", type=int, default=19)
    ap.add_argument("--dict-size", type=int, default=112640)
    a = ap.parse_args()
    if os.path.exists(a.dst):
        sys.exit(f"הקובץ {a.dst} כבר קיים; מחק אותו ידנית")
    t0 = time.time()
    tmp = a.dst + ".part"
    if os.path.exists(tmp):
        os.remove(tmp)
    shutil.copyfile(a.src, tmp)
    db = sqlite3.connect(tmp)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    cols = [r[1] for r in db.execute("PRAGMA table_info(line)")]
    if "content" not in cols:
        sys.exit("בטבלת line אין עמודת content - הקלט כבר דחוס או לא תקין")
    if db.execute("SELECT 1 FROM sqlite_master WHERE name IN ('line_content','zstd_dict')").fetchone():
        sys.exit("הקלט כבר מכיל line_content/zstd_dict")

    samples, used, step = pick_samples(db)
    print(f"מדגם: {len(samples)} שורות, {used/1e6:.1f}MB (כל {step}-ית)", flush=True)
    dict_data = zstd.train_dictionary(a.dict_size, samples, dict_id=DICT_ID, threads=1)
    dbytes = dict_data.as_bytes()
    print(f"מילון: {len(dbytes)} בייט, dictID={dict_data.dict_id()}, {time.time()-t0:.0f}s", flush=True)
    del samples

    cctx = zstd.ZstdCompressor(level=a.level, dict_data=dict_data,
                               write_content_size=True, write_checksum=False,
                               write_dict_id=True, threads=0)
    db.execute("CREATE TABLE zstd_dict (id INTEGER PRIMARY KEY NOT NULL, dict BLOB NOT NULL)")
    db.execute("INSERT INTO zstd_dict VALUES (?,?)", (DICT_ID, dbytes))
    db.execute("CREATE TABLE line_content (id INTEGER PRIMARY KEY NOT NULL, content BLOB NOT NULL)")

    n = 0
    cur = db.execute("SELECT id, content FROM line ORDER BY id")
    while True:
        rows = cur.fetchmany(BATCH)
        if not rows:
            break
        db.executemany("INSERT INTO line_content VALUES (?,?)",
                       [(i, cctx.compress(c.encode("utf-8"))) for i, c in rows])
        n += len(rows)
        if n % (BATCH * 10) == 0:
            print(f"  {n} שורות, {time.time()-t0:.0f}s", flush=True)
    db.commit()
    db.execute("ALTER TABLE line DROP COLUMN content")
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM line").fetchone()[0] == n
    db.execute("VACUUM")
    ok = db.execute("PRAGMA integrity_check").fetchone()[0]
    db.close()
    os.replace(tmp, a.dst)
    s, d = os.path.getsize(a.src), os.path.getsize(a.dst)
    print(f"integrity={ok}; {n} שורות; {s/1e6:.1f}MB -> {d/1e6:.1f}MB "
          f"(יחס {s/d:.2f}x, {d/2**30:.3f}GiB); {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
