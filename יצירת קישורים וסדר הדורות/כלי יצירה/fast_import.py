import csv, glob, os, re, shutil, sqlite3, sys, time
sys.stdout.reconfigure(encoding='utf-8')
OFF = 'file:C:/ProgramData/otzaria/books/seforim.db?mode=ro'
UBP = r'C:\Users\HP\AppData\Roaming\otzaria\databases\user_books.db'
DRY = '--apply' not in sys.argv
off = sqlite3.connect(OFF, uri=True)
ub = sqlite3.connect(UBP)

# אינדקס כתובות לכל ספר רשמי
cache = {}


def index(title):
    if title in cache:
        return cache[title]
    r = off.execute('select id from book where title=?', (title,)).fetchone()
    d = {}
    if r:
        ents = off.execute(
            'select e.id,e.parentId,e.level,t.text,l.lineIndex from tocEntry e '
            'join tocText t on t.id=e.textId join line l on l.id=e.lineId where e.bookId=? order by e.id',
            (r[0],)).fetchall()
        byid = {e[0]: e for e in ents}
        for i, p, lv, t, li in ents:
            t = t.strip()
            m = re.match(r'דף ([א-ת"״\']+)([.:])$', t)
            if m:
                d.setdefault(m.group(1) + m.group(2), li)
            if lv == 1:
                d.setdefault(t, li)
            if lv == 2 and p in byid:
                d.setdefault(byid[p][3].strip() + ' ' + t, li)
    cache[title] = d
    return d


# ספרי משתמש: (כותרת, קטגוריה) -> categoryId ; כותרת -> [categoryId]
books = {}
bytitle = {}
for t, c in ub.execute('select title,categoryId from book'):
    books[(t, c)] = c
    bytitle.setdefault(t, []).append(c)

existing = set()
for r in ub.execute('select sourceTitle,sourceLineIndex,targetTitle,targetCategoryId,targetLineIndex,connectionType from user_link where sourceIsUserBook=0 and targetIsUserBook=1'):
    existing.add(r)

TYPES = {'פירוש': 'COMMENTARY', 'תרגום': 'TARGUM', 'הפניה': 'REFERENCE', 'מקור': 'SOURCE', 'אחר': 'OTHER'}
folders = [os.path.join(r'C:\Users\HP\Desktop\קישורים', '%02d' % n) for n in range(6, 13)]
folders += sorted(glob.glob(r'C:\Users\HP\Desktop\קישורים - קבצי קובץ (גדולים)\*'))
new = []
stat = {'rows': 0, 'dup': 0, 'noref': 0, 'nobook': 0}
unres = {}
for fd in folders:
    with open(os.path.join(fd, 'links.csv'), encoding='utf-8-sig', newline='') as f:
        rd = csv.DictReader(f)
        for r in rd:
            stat['rows'] += 1
            title = r['ספר_מקור']
            cid = int(r['קטגוריית_מקור']) if r['קטגוריית_מקור'] else None
            cats = bytitle.get(title)
            if not cats:
                stat['nobook'] += 1
                continue
            cat = cid if cid in cats else (cats[0] if len(cats) == 1 else None)
            if cat is None:
                stat['nobook'] += 1
                continue
            idx = index(r['ספר_יעד']).get(r['מיקום_יעד'])
            if idx is None:
                stat['noref'] += 1
                unres[(r['ספר_יעד'], r['מיקום_יעד'])] = unres.get((r['ספר_יעד'], r['מיקום_יעד']), 0) + 1
                continue
            typ = TYPES[r['סוג']]
            tl = int(r['מקור']) - 1
            key = (r['ספר_יעד'], idx, title, cat, tl, typ)
            if key in existing:
                stat['dup'] += 1
                continue
            existing.add(key)
            new.append((r['ספר_יעד'], None, 0, idx, title, cat, 1, None, tl, typ))
print(stat, 'to insert', len(new))
print('unresolved refs sample', sorted(unres.items(), key=lambda x: -x[1])[:10])
if DRY:
    print('dry run - nothing written')
    sys.exit()
bak = UBP + '.backup-before-links'
if not os.path.exists(bak):
    shutil.copy2(UBP, bak)
    print('backup ->', bak)
t0 = time.time()
ub.execute('begin')
ub.executemany(
    'insert into user_link (sourceTitle,sourceCategoryId,sourceIsUserBook,sourceLineIndex,targetTitle,targetCategoryId,'
    'targetIsUserBook,targetRef,targetLineIndex,connectionType) values (?,?,?,?,?,?,?,?,?,?)', new)
ub.commit()
print('inserted', len(new), 'in', round(time.time() - t0, 1), 's')
print(ub.execute('select count(*) from user_link').fetchone())
