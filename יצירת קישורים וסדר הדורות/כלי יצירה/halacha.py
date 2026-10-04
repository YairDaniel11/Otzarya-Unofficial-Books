import csv, os, re, sqlite3, sys
sys.stdout.reconfigure(encoding='utf-8')
ROOT = r'C:\Users\HP\Desktop\מאגרי גיטאב\אוצריא\מאגר ספרים - גיטאב\ספרים'
sc = sqlite3.connect('file:C:/Users/HP/AppData/Roaming/io.github.kdroidfilter.seforimapp/databases/seforim.db?mode=ro', uri=True)
uc = sqlite3.connect('file:C:/Users/HP/AppData/Roaming/otzaria/databases/user_books.db?mode=ro', uri=True)
users = {r[0] for r in uc.execute('select title from book')}


def chapters(title):
    r = sc.execute('select id from book where title=?', (title,)).fetchone()
    return {t.strip() for (t,) in sc.execute(
        'select t.text from tocEntry e join tocText t on t.id=e.textId where e.bookId=? and e.level=1', (r[0],))}


def lines(p):
    return open(p, encoding='utf-8-sig', errors='replace').read().split('\n')


HEAD = re.compile(r'^<h2>(.*?)</h2>\s*$')
out = []
# (נתיב יחסי, שם ספר, יעד, תבנית כותרת)
jobs = [
    (r'הלכה\שולחן ערוך\מפרשים', "פרי חדש או''ח", 'שולחן ערוך, אורח חיים', r'^(סימן [א-ת]+)$'),
    (r'הלכה\משנה תורה\מפרשים', 'ר״י קורקוס שבת', 'משנה תורה, הלכות שבת', r'^(פרק [א-ת]+)$'),
]
for rel, b, target, pat in jobs:
    ch = chapters(target)
    assert b in users
    cur = None
    n = 0
    for i, l in enumerate(lines(os.path.join(ROOT, rel, b + '.txt')), 1):
        s = l.rstrip('\r').strip()
        hm = HEAD.match(s)
        if hm:
            m = re.match(pat, re.sub(r'<[^>]+>', '', hm.group(1)).strip())
            cur = m.group(1) if m and m.group(1) in ch else None
            continue
        if i == 1 or not s or s.startswith('<h'):
            continue
        if cur:
            out.append((i, b, 'כן', target, cur, 'פירוש', 'לא', 'x'))
            n += 1
    print(b, n)
# דגול מרבבה - אבן העזר, כותרת טקסט רגילה
b = "דגול מרבבה אהע''ז"
target = 'שולחן ערוך, אבן העזר'
ch = chapters(target)
cur = None
n = 0
for i, l in enumerate(lines(os.path.join(ROOT, r'ספרים שאינם מותאמים לאוצריא\הלכה\שו״ע\מפרשים', b + '.txt')), 1):
    s = l.rstrip('\r').strip()
    m = re.match(r'^דגול מרבבה אבן העזר (סימן [א-ת]+)$', s)
    if m:
        cur = m.group(1) if m.group(1) in ch else None
        continue
    if not s or s.startswith('====') or s.startswith('<h1'):
        continue
    if cur:
        out.append((i, b, 'כן', target, cur, 'פירוש', 'לא', 'x'))
        n += 1
print(b, n)
# קטגוריה רק לשמות כפולים
dupt = {t for t, c in uc.execute('select title,count(*) from book group by title') if c > 1}
os.makedirs(r'C:\Users\HP\Desktop\קישורים\12', exist_ok=True)
with open(r'C:\Users\HP\Desktop\קישורים\12\links.csv', 'w', encoding='utf-8-sig', newline='') as f:
    w = csv.writer(f, lineterminator='\r\n')
    w.writerow(['מקור', 'ספר_מקור', 'מקור_אישי', 'ספר_יעד', 'מיקום_יעד', 'סוג', 'יעד_אישי', 'קטגוריית_מקור'])
    for r in out:
        assert r[1] not in dupt
        w.writerow(list(r[:7]) + [''])
print(len(out))
