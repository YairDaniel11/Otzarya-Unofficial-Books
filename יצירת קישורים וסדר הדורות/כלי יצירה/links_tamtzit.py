"""
יצירת קישורים לסדרת "תמצית הסוגיות" (הרב ח.ז. קרלינשטיין): כל ספר מקושר לדפי המסכת שלו בגמרא.

עיקרון: כותרת דף (<h2>-<h6> מהצורה "ב." / "סב:") קובעת את הדף לכל שורות הגוף שאחריה, עד הכותרת הבאה.
מצב קפדני (עדיף קישור חסר על קישור שגוי):
  - כותרת פרק (<h2> שאינה דף) מאפסת את הדף הנוכחי, כך שפסקאות לפני כותרת הדף הראשונה של הפרק לא מקושרות.
  - כותרת טווח דפים ("ד. - ד:") לא מקושרת לדף כלשהו: השורות שאחריה אינן מקושרות עד כותרת דף בודדת.
  - דף שאינו קיים ב-tocEntry של המסכת ב-seforim.db לא מקושר (מדווח).
ספרי סדר טהרות (משניות) מקושרים לפי פרק: כותרת h2 שהיא מספר פרק ("א", "י"א") קובעת את "פרק X" במסכת "משנה <מסכת>".
הפלט נכתב ל-קישורים/17/links.csv (UTF-8 עם BOM, CRLF) והכלי אידמפוטנטי: מסיר קודם שורות קיימות של אותם ספרי מקור.

הרצה:
  python -X utf8 links_tamtzit.py --base "<תיקיית ספרים>"           # ניסוי יבש: סיכום ודוח
  python -X utf8 links_tamtzit.py --base "<תיקיית ספרים>" --apply   # כתיבה
"""
import csv, io, os, re, sqlite3, sys, glob, collections

sys.stdout.reconfigure(encoding='utf-8')

import config

BASE = config.BASE
LINKS_DIR = config.LINKS_DIR
OUT_DIR = os.path.join(LINKS_DIR, 'קישורים', '17')
REPORT = os.path.join(config.OUT_DIR, 'links_tamtzit_report.csv')
SEF = config.SEF_URI

# מסכת (שם ב-seforim.db) -> שם ספר המקור
MASECHTOT = ['ברכות', 'עירובין', 'פסחים', 'סוכה', 'יבמות', 'כתובות', 'גיטין', 'בבא מציעא', 'בבא בתרא',
             'שבועות', 'זבחים', 'חולין', 'בכורות', 'תמורה', 'נדה']
JOBS = [{'source': 'תמצית הסוגיות - ' + m, 'target': m, 'type': 'פירוש', 'mode': 'daf'} for m in MASECHTOT]
# ספרי סדר טהרות: מסכת -> שם משנה ב-seforim.db (קישור ברמת פרק)
MISHNAH = {'אהלות': 'משנה אהלות', 'נגעים': 'משנה נגעים', 'פרה': 'משנה פרה', 'טהרות': 'משנה טהרות',
           'מקוואות': 'משנה מקואות', 'מכשירין': 'משנה מכשירין', 'זבין': 'משנה זבים',
           'טבול יום': 'משנה טבול יום', 'ידיים': 'משנה ידים', 'עוקצין': 'משנה עוקצים'}
JOBS += [{'source': 'תמצית הסוגיות - ' + m, 'target': t, 'type': 'פירוש', 'mode': 'perek'} for m, t in MISHNAH.items()]
PEREK_HEAD = re.compile(r"^[א-ת\"״'׳]{1,4}$")

HEADER = ['מקור', 'ספר_מקור', 'מקור_אישי', 'ספר_יעד', 'מיקום_יעד', 'סוג', 'יעד_אישי', 'קטגוריית_מקור']
HEAD = re.compile(r'^<h([1-6])[^>]*>(.*?)</h[1-6]>\s*$')
TAGS = re.compile(r'<[^>]+>')
HEAD_DAF = re.compile(r'^\[?(?:דף\s+)?([א-ת]{1,3})([.:])\]?$')
HEAD_RANGE = re.compile(r'^\[?(?:דף\s+)?[א-ת]{1,3}[.:]\s*[-–]\s*[א-ת]{1,3}[.:]\]?$|^\[?(?:דף\s+)?[א-ת]{1,3}[.:]\s+[א-ת]{1,3}[.:]\]?$')


def clean_num(s):
    return re.sub(r'[\'"״׳]', '', s)


def target_dafs(sc, title):
    r = sc.execute('select id from book where title=?', (title,)).fetchone()
    if not r:
        return None
    s = set()
    for (t,) in sc.execute('select t.text from tocEntry e join tocText t on t.id=e.textId where e.bookId=?', (r[0],)):
        m = re.match(r'דף ([א-ת"״\']+)([.:])', t.strip())
        if m:
            s.add(clean_num(m.group(1)) + m.group(2))
    return s


def target_chapters(sc, title):
    r = sc.execute('select id from book where title=?', (title,)).fetchone()
    if not r:
        return None
    s = set()
    for (t,) in sc.execute('select t.text from tocEntry e join tocText t on t.id=e.textId where e.bookId=? and e.level=1', (r[0],)):
        s.add(t.strip())
    return s


def find_source(stem):
    for dp, _, fs in os.walk(BASE):
        if 'קבצי קישורים' in dp:
            continue
        if stem + '.txt' in fs:
            return os.path.join(dp, stem + '.txt')
    return None


def gen(job, dafs, report):
    path = find_source(job['source'])
    if not path:
        print('לא נמצא קובץ:', job['source'])
        return []
    L = open(path, encoding='utf-8-sig', errors='replace').read().replace('\r\n', '\n').split('\n')
    rows, cur = [], None
    stat = collections.Counter()
    for i, l in enumerate(L, 1):
        s = l.strip()
        hm = HEAD.match(s)
        if hm:
            lvl = int(hm.group(1))
            text = TAGS.sub('', hm.group(2)).strip()
            if job['mode'] == 'perek':
                if lvl == 2:
                    if PEREK_HEAD.match(text):
                        ref = 'פרק ' + clean_num(text)
                        if ref in dafs:
                            cur = ref
                        else:
                            cur = None
                            stat['פרק שאינו ביעד'] += 1
                            report.append((job['source'], i, ref, 'פרק לא קיים ביעד', text))
                    else:
                        cur = None
                continue
            if lvl >= 2:
                m = HEAD_DAF.match(text)
                if m:
                    ref = clean_num(m.group(1)) + m.group(2)
                    if ref in dafs:
                        cur = ref
                    else:
                        cur = None
                        stat['דף שאינו ביעד'] += 1
                        report.append((job['source'], i, ref, 'דף לא קיים ביעד', text))
                elif HEAD_RANGE.match(text):
                    cur = None
                    stat['כותרת טווח'] += 1
                    report.append((job['source'], i, text, 'טווח דפים - לא מקושר', text))
                elif lvl == 2:
                    cur = None   # כותרת פרק: איפוס
            continue
        if i == 1 or not s:
            continue
        if cur:
            rows.append((i, job['source'], 'כן', job['target'], cur, job['type'], 'לא', ''))
        else:
            stat['שורות ללא דף'] += 1
    print(f"{job['source']}: {len(rows)} שורות מקושרות מתוך {len(L)}; {dict(stat)}")
    return rows


def read_csv(path):
    return list(csv.reader(open(path, encoding='utf-8-sig', newline='')))


def write_csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    out = io.StringIO()
    csv.writer(out, lineterminator='\r\n').writerows(rows)
    with open(path, 'wb') as f:
        f.write(('﻿' + out.getvalue()).encode('utf-8'))


def main():
    apply = '--apply' in sys.argv
    sc = sqlite3.connect(SEF, uri=True)
    report, new_rows, sources = [], [], set()
    for job in JOBS:
        dafs = target_chapters(sc, job['target']) if job['mode'] == 'perek' else target_dafs(sc, job['target'])
        if not dafs:
            print('אין יעד ב-seforim.db:', job['target'])
            continue
        new_rows += gen(job, dafs, report)
        sources.add(job['source'])
    os.makedirs(config.OUT_DIR, exist_ok=True)
    out = io.StringIO()
    w = csv.writer(out, lineterminator='\r\n')
    w.writerow(['ספר', 'שורה', 'דף/כותרת', 'סוג', 'טקסט'])
    w.writerows(report)
    with open(REPORT, 'wb') as f:
        f.write(('﻿' + out.getvalue()).encode('utf-8'))
    print('סה"כ שורות קישורים:', len(new_rows), '| דוח:', REPORT)
    if not apply:
        print('ניסוי יבש. לכתיבה: --apply')
        return
    for p in glob.glob(os.path.join(LINKS_DIR, 'קישורים*', '*', 'links.csv')):
        if os.path.abspath(p) == os.path.abspath(os.path.join(OUT_DIR, 'links.csv')):
            continue
        rows = read_csv(p)
        keep = [rows[0]] + [r for r in rows[1:] if len(r) < 2 or r[1] not in sources]
        if len(keep) != len(rows):
            print('הוסרו', len(rows) - len(keep), 'שורות ישנות מ', p)
            write_csv(p, keep)
    out_path = os.path.join(OUT_DIR, 'links.csv')
    existing = read_csv(out_path) if os.path.exists(out_path) else [HEADER]
    kept = [existing[0]] + [r for r in existing[1:] if r[1] not in sources]
    write_csv(out_path, kept + [list(map(str, r)) for r in new_rows])
    print('נכתבו', len(new_rows), 'שורות ל', out_path)


if __name__ == '__main__':
    main()
