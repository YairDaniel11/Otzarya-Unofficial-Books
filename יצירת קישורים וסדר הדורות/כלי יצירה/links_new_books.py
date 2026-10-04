"""
יצירת קישורים לספרים חדשים (בלי להריץ מחדש את כל הקישורים).

הכלי מוסיף שורות ל-קישורים/13/links.csv עבור ספרי מקור שמוגדרים ב-JOBS. הוא אידמפוטנטי:
לפני הכתיבה הוא מסיר מכל קבצי links.csv שורות קיימות של אותם ספרי מקור, כך שאפשר להריץ שוב
אחרי עדכון הספר.

מצבים (mode):
  daf-anchor  שורה שפותחת ב"בדף X." (עוגן מפורש) מקשרת לדף X במסכת היעד, וכל השורות שאחריה
              עד העוגן הבא או עד כותרת <h2> חדשה מקבלות את אותו יעד. שורות לפני העוגן הראשון
              בסימן לא מקושרות. זה מצב שמרני: קישור נוצר רק כשיש עוגן מפורש בטקסט.
  daf-heading כותרת <h2>-<h6> מהצורה "ב." או "דף ב:" קובעת את הדף לשורות שאחריה
              (כמו שנעשה לפירושים על הש"ס בהרצה המקורית).

הרצה:
  python -X utf8 links_new_books.py --base "<תיקיית ספרים>"            # ניסוי יבש: סיכום ודוח בדיקה, לא נוגע בקבצים
  python -X utf8 links_new_books.py --base "<תיקיית ספרים>" --apply    # כותב את links.csv (שומר BOM ו-CRLF)

תנאים: seforim.db זמין לקריאה (רק לבדיקת קיום הדפים במסכת היעד). לפירוט ההגדרות: config.py.
"""
import csv, io, os, re, sqlite3, sys, glob, collections

sys.stdout.reconfigure(encoding='utf-8')

import config

BASE = config.BASE
LINKS_DIR = config.LINKS_DIR
OUT_DIR = os.path.join(LINKS_DIR, 'קישורים', '13')  # תיקיית הקישורים שהכלי כותב אליה
REPORT = os.path.join(config.OUT_DIR, 'links_new_books_report.csv')
SEF = config.SEF_URI

# ספר מקור (שם קובץ בלי סיומת) -> יעד
JOBS = [
    {'source': 'ברכת שמואל על בבא מציעא', 'target': 'בבא מציעא', 'mode': 'daf-anchor', 'type': 'פירוש'},
]

HEADER = ['מקור', 'ספר_מקור', 'מקור_אישי', 'ספר_יעד', 'מיקום_יעד', 'סוג', 'יעד_אישי', 'קטגוריית_מקור']
HEAD = re.compile(r'^<h([1-6])[^>]*>(.*?)</h[1-6]>\s*$')
TAGS = re.compile(r'<[^>]+>')
# "בדף כ"ו:" / "בדף מ״ז ע״א" / "בדף ב." בתחילת שורה, עם <b> אופציונלי
ANCHOR = re.compile(r'^(?:<[^>]+>)*\s*(?:ו)?בדף\s+([א-ת]{1,3}[\'"״׳]?[א-ת]?)\s*(?:([.:])|ע[\'"״׳]?([אב]))')
HEAD_DAF = re.compile(r'^\[?(?:דף\s+)?([א-ת]{1,3})([.:])\]?$')


def clean_num(s):
    return re.sub(r'[\'"״׳]', '', s)


def to_ref(num, dot, amud):
    side = dot if dot else ('.' if amud == 'א' else ':')
    return clean_num(num) + side


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
    skipped = collections.Counter()
    for i, l in enumerate(L, 1):
        s = l.strip()
        hm = HEAD.match(s)
        if hm:
            lvl = int(hm.group(1))
            if job['mode'] == 'daf-anchor' and lvl == 2:
                cur = None  # סימן חדש: העוגן מתאפס
            if job['mode'] == 'daf-heading' and lvl >= 2:
                m = HEAD_DAF.match(TAGS.sub('', hm.group(2)).strip())
                if m:
                    cur = clean_num(m.group(1)) + m.group(2)
            continue
        if i == 1 or not s:
            continue
        if job['mode'] == 'daf-anchor':
            m = ANCHOR.match(s)
            if m:
                ref = to_ref(m.group(1), m.group(2), m.group(3))
                if ref in dafs:
                    cur = ref
                    report.append((job['source'], i, ref, 'עוגן', TAGS.sub('', s)[:70]))
                else:
                    skipped['דף לא קיים ביעד: ' + ref] += 1
                    cur = None
                    report.append((job['source'], i, ref, 'דף לא קיים ביעד', TAGS.sub('', s)[:70]))
        if cur:
            rows.append((i, job['source'], 'כן', job['target'], cur, job['type'], 'לא', ''))
    print(f"{job['source']}: {len(rows)} שורות מקושרות מתוך {len(L)}; דילוגים: {dict(skipped)}")
    return rows


def read_csv(path):
    return list(csv.reader(open(path, encoding='utf-8-sig', newline='')))


def write_csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    out = io.StringIO()
    csv.writer(out, lineterminator='\r\n').writerows(rows)
    with open(path, 'wb') as f:
        f.write(('\ufeff' + out.getvalue()).encode('utf-8'))


def main():
    apply = '--apply' in sys.argv
    sc = sqlite3.connect(SEF, uri=True)
    report, new_rows, sources = [], [], set()
    for job in JOBS:
        dafs = target_dafs(sc, job['target'])
        if not dafs:
            print('אין יעד ב-seforim.db:', job['target'])
            continue
        new_rows += gen(job, dafs, report)
        sources.add(job['source'])
    with open(REPORT, 'wb') as f:
        out = io.StringIO()
        w = csv.writer(out, lineterminator='\r\n')
        w.writerow(['ספר', 'שורה', 'דף', 'סוג', 'טקסט'])
        w.writerows(report)
        f.write(('\ufeff' + out.getvalue()).encode('utf-8'))
    print('דוח בדיקה:', REPORT)
    if not apply:
        print('ניסוי יבש. לכתיבה: --apply')
        return
    # הסרת שורות קיימות של אותם מקורות מכל קבצי הקישורים
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
