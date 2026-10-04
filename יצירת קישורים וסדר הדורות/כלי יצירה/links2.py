import csv, os, re, sqlite3, sys, collections, pickle
sys.stdout.reconfigure(encoding='utf-8')
ROOT = r'C:\Users\HP\Desktop\מאגרי גיטאב\אוצריא\מאגר ספרים - גיטאב\ספרים'
SEF = 'file:C:/Users/HP/AppData/Roaming/io.github.kdroidfilter.seforimapp/databases/seforim.db?mode=ro'
UB = 'file:C:/Users/HP/AppData/Roaming/otzaria/databases/user_books.db?mode=ro'
sc = sqlite3.connect(SEF, uri=True)
uc = sqlite3.connect(UB, uri=True)
user_titles = {r[0] for r in uc.execute("select title from book")}
cats = {r[0]: (r[1], r[2]) for r in uc.execute("select id,parentId,title from category")}


def cpath(i):
    p = []
    while i:
        par, t = cats[i]
        p.append(t)
        i = par
    return tuple(p[::-1])


path2id = {cpath(i): i for i in cats}
dup = {t for t, n in uc.execute("select title,count(*) from book group by title") if n > 1}
toc = {}


def tocs(title):
    if title in toc:
        return toc[title]
    r = sc.execute("select id from book where title=?", (title,)).fetchone()
    if not r:
        toc[title] = None
        return None
    ents = sc.execute(
        "select e.id,e.parentId,e.level,t.text from tocEntry e join tocText t on t.id=e.textId where e.bookId=? order by e.id",
        (r[0],)).fetchall()
    toc[title] = ents
    return ents


def dafset(title):
    e = tocs(title)
    if not e:
        return None
    s = set()
    for _, _, _, t in e:
        m = re.match(r'דף ([א-ת"״\']+)([.:])', t.strip())
        if m:
            s.add(m.group(1) + m.group(2))
    return s


def hset(title):
    e = tocs(title)
    if not e:
        return None
    byid = {x[0]: x for x in e}
    s = set()
    for i, p, l, t in e:
        if l == 2 and p in byid:
            s.add((byid[p][3].strip(), t.strip()))
    return s


def chset(title):
    e = tocs(title)
    return {t.strip() for _, _, l, t in e if l == 1} if e else None


MAS = ['בבא בתרא', 'בבא קמא', 'בבא מציעא', 'ראש השנה', 'עבודה זרה', 'מועד קטן', 'פסחים', 'קידושין', 'קדושין',
       'מגילה', 'סנהדרין', 'סוכה', 'מכות', 'ביצה', 'יומא', 'ברכות', 'חגיגה', 'נדרים', 'עירובין', 'סוטה', 'זבחים',
       'תענית', 'נדה', 'נזיר', 'מנחות', 'כריתות', 'הוריות', 'בכורות', 'ערכין', 'תמורה', 'מעילה', 'תמיד', 'כתובות',
       'שבועות', 'יבמות', 'גיטין', 'שבת', 'חולין']
ALIAS = {'קדושין': 'קידושין'}
HEAD = re.compile(r'^<h([2-6])>(.*?)</h[2-6]>\s*$')
DAF = re.compile(r'^\[?(?:דף\s+)?([א-ת]{1,3})([.:])\]?$')


def strip(t):
    return re.sub(r'<[^>]+>', '', t).strip()


def find_mas(text):
    best = None
    for m in MAS:
        if re.search(r'(?<![א-ת])' + re.escape(m) + r'(?![א-ת])', text):
            if best is None or len(m) > len(best):
                best = m
    return best


def lines(path):
    return open(path, encoding='utf-8-sig', errors='replace').read().split('\n')


rows = collections.defaultdict(list)
rep = collections.Counter()
notes = []


def emit(grp, i, b, cid, target, ref, typ):
    rows[grp].append((i, b, 'כן', target, ref, typ, 'לא', cid if cid else ''))


NEWDIR = 'ספרים חדשים מותאמים לאוצריא - לבדיקה'
for dp, dn, fn in os.walk(ROOT):
    rel = tuple(os.path.relpath(dp, ROOT).split(os.sep))
    if rel[:2] == ('תלמוד בבלי', 'שס וגשל') or rel == ('.',):
        continue
    for n in sorted(fn):
        b, e = os.path.splitext(n)
        if e != '.txt' or b not in user_titles:
            continue
        cid = path2id.get(('ספרים אישיים', 'ספרים') + rel) if b in dup else None
        L = lines(os.path.join(dp, n))
        # תנ"ך
        if rel[:1] == ('תנ״ך',) and len(rel) > 1 and rel[1] in ('ראשונים', 'תרגומים'):
            book = re.sub(r"\s*\(כתאב אלתאג'\)$", '', b)
            book = re.sub(r'^תרגום (שני )?(על )?', '', book)
            book = {'תהלים': 'תהילים'}.get(book, book)
            ch = chset(book)
            if not ch:
                notes.append(('תנך ללא יעד', b))
                continue
            typ = 'תרגום' if rel[1] == 'תרגומים' else 'פירוש'
            cur = None
            cnt = 0
            for i, l in enumerate(L, 1):
                s = l.rstrip('\r').strip()
                hm = HEAD.match(s)
                if hm:
                    if hm.group(1) == '2':
                        t = strip(hm.group(2))
                        cur = t if t in ch else None
                    continue
                if i == 1 or not s or s.startswith('<h1'):
                    continue
                if cur:
                    emit('תנך', i, b, cid, book, cur, typ)
                    cnt += 1
            rep['tanach_rows'] += cnt
            rep['tanach_files'] += 1
            if not cnt:
                notes.append(('תנך 0 שורות', b))
            continue
        # ירושלמי שקלים
        if re.search(r'(?<![א-ת])שקלים(?![א-ת])', b):
            target = 'תלמוד ירושלמי שקלים'
            hs = hset(target)
            pm = re.search(r'פרק ([א-ת]+)$', b)
            per = 'פרק ' + pm.group(1) if pm else None
            hal = None
            cnt = 0
            for i, l in enumerate(L, 1):
                s = l.rstrip('\r').strip()
                hm = HEAD.match(s)
                if hm:
                    t = strip(hm.group(2))
                    m = re.search(r'פרק ([א-ת]+)$', t)
                    if hm.group(1) == '2' and m:
                        per = 'פרק ' + m.group(1)
                        hal = None
                    elif t.startswith('הלכה '):
                        hal = t
                    continue
                if i == 1 or not s or s.startswith('<h1'):
                    continue
                if per and hal and (per, hal) in hs:
                    emit('ירושלמי', i, b, cid, target, per + ' ' + hal, 'פירוש')
                    cnt += 1
            rep['yer_rows'] += cnt
            rep['yer_files'] += 1
            if not cnt:
                notes.append(('ירושלמי 0', b))
            continue
        # בבלי
        if rel[:1] == ('תלמוד בבלי',) or rel[:2] == ('ספרים שאינם מותאמים לאוצריא', 'תלמוד בבלי') or rel[:1] == (NEWDIR,):
            m = re.search(r'מסכת (.+?)\s*(?:\(\d+\)|\d+)?\s*$', b)
            mas = None
            if m:
                mas = re.sub(r"\s*\(.*?\)\s*$", '', m.group(1)).strip()
                mas = re.sub(r'\s+ועוד$', '', mas)
            elif rel[:1] == (NEWDIR,) or (rel[:1] == ('תלמוד בבלי',) and 'מחברי זמננו' not in rel):
                mas = find_mas(b.replace("''", '').replace("'", ''))
            mas = ALIAS.get(mas, mas) if mas else None
            ds = dafset(mas) if mas else None
            cur = None
            cnt = 0
            bad = set()
            for i, l in enumerate(L, 1):
                s = l.rstrip('\r').strip()
                hm = HEAD.match(s)
                if hm:
                    t = strip(hm.group(2))
                    mm = re.match(r'^מסכת (.+?)$', t)
                    if hm.group(1) == '2' and mm:
                        nm = ALIAS.get(mm.group(1).strip(), mm.group(1).strip())
                        nd = dafset(nm)
                        if nd:
                            mas, ds, cur = nm, nd, None
                        continue
                    dm = DAF.match(t)
                    if dm:
                        cur = dm.group(1) + dm.group(2)
                        if ds is not None and cur not in ds:
                            bad.add(cur)
                    continue
                if i == 1 or not s or s.startswith('<h1'):
                    continue
                if ds and cur and cur in ds:
                    emit('בבלי', i, b, cid, mas, cur, 'פירוש')
                    cnt += 1
            rep['bav_rows'] += cnt
            rep['bav_files'] += 1
            rep['bav_baddaf'] += len(bad)
            if not cnt:
                notes.append(('בבלי 0 שורות', b))
print(rep)
for x in notes:
    print(x)
print({k: len(v) for k, v in rows.items()})
pickle.dump(dict(rows), open('rows.pkl', 'wb'))
