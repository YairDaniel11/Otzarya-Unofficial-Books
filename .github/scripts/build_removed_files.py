#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
בונה את removed_files.json: הנתיבים (יחסית לתיקיית `ספרים`) שהוסרו או הועברו מהמאגר ולא קיימים היום.

התוסף "הורדת ספרים" מחלץ zip מעל התיקייה של המשתמש ולא מוחק קבצים ישנים, ולכן אחרי שינוי שם, העברה או
מחיקה של ספר במאגר נשארים אצל המשתמש הקבצים הישנים לצד החדשים. התוסף קורא את הקובץ הזה ומוחק אותם.

הקובץ נגזר מההיסטוריה של git (בלי מצב שמור), ולכן אפשר להריץ אותו מחדש בכל פעם:
  {"version": 1, "generated": <epoch>,
   "removed": [{"t": <epoch של הקומיט האחרון שהשפיע>, "path": "<נתיב ישן>", "to": "<נתיב חדש, רק בהעברה>"}]}

* שרשרת שינויי שם (A -> B -> C) מצטמצמת ל-A -> C.
* נתיב שחזר להתקיים (נמחק ונוסף שוב בשם זהה) לא מופיע.
* "to" מופיע רק אם היעד קיים היום. התוסף משתמש בו כדי למחוק את הקובץ הישן רק אחרי שהחדש ירד.

שימוש:  python build_removed_files.py [קובץ פלט] [תיקיית המאגר]
"""
import json
import os
import subprocess
import sys
import time

BOOKS_DIR = 'ספרים'


def git_log(repo):
    out = subprocess.run(
        ['git', '-c', 'core.quotePath=false', 'log', '--reverse', '--name-status', '-M', '--format=@@%ct', '--', BOOKS_DIR],
        capture_output=True, encoding='utf-8', cwd=repo, check=True).stdout
    return out


def compute(repo):
    """מחזיר רשימת רשומות {t, path[, to]} ממוינת לפי זמן ונתיב."""
    entries = {}   # נתיב מקורי -> {'to': מיקום נוכחי או None, 't': epoch}
    t = 0
    for line in git_log(repo).split('\n'):
        if line.startswith('@@'):
            t = int(line[2:])
            continue
        parts = line.split('\t')
        if len(parts) < 2 or not parts[0]:
            continue
        st = parts[0][:1]
        if st == 'R' and len(parts) >= 3:
            old, new = parts[1], parts[2]
            for e in entries.values():
                if e['to'] == old:
                    e['to'], e['t'] = new, t
            entries.pop(new, None)                    # היעד קיים שוב
            entries[old] = {'to': new, 't': t}
        elif st == 'D':
            p = parts[1]
            for e in entries.values():
                if e['to'] == p:
                    e['to'], e['t'] = None, t
            entries[p] = {'to': None, 't': t}
        elif st in ('A', 'C'):
            entries.pop(parts[1], None)               # הנתיב קיים שוב
    prefix = BOOKS_DIR + '/'
    result = []
    for orig, e in entries.items():
        if not orig.startswith(prefix) or os.path.exists(os.path.join(repo, orig)):
            continue
        to = e['to']
        if to and (not to.startswith(prefix) or not os.path.exists(os.path.join(repo, to))):
            to = None
        rec = {'t': e['t'], 'path': orig[len(prefix):]}
        if to:
            rec['to'] = to[len(prefix):]
        result.append(rec)
    result.sort(key=lambda r: (r['t'], r['path']))
    return result


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else 'removed_files.json'
    repo = sys.argv[2] if len(sys.argv) > 2 else '.'
    removed = compute(repo)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump({'version': 1, 'generated': int(time.time()), 'removed': removed}, f, ensure_ascii=False, separators=(',', ':'))
        f.write('\n')
    moved = sum(1 for r in removed if 'to' in r)
    print(f'removed_files.json: {len(removed)} נתיבים ({moved} הועברו, {len(removed) - moved} נמחקו)')


if __name__ == '__main__':
    main()
