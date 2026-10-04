"""
ביקורת קישורים: לכל ספר במאגר בודק שיש לו קישורים (אם הוא אמור) ושהקישורים עדיין מצביעים נכון.

השיטה: משחזרים מהקבצים הנוכחיים את הקישורים שהיו נוצרים היום (אותה לוגיקה של links2.py ו-halacha.py),
ומשווים לכל קבצי links.csv הקיימים (קישורים/*, קישורים - קבצי קובץ (גדולים)/*).

לכל ספר מקור מחושב:
  expected  קישורים שהיו נוצרים היום
  actual    קישורים שקיימים בקבצים
  ok        אותה שורה ואותו יעד
  wrong     אותה שורה, יעד אחר (הקישור מצביע למקום שגוי)
  stale     שורה בקישורים שאין לה יעד היום (שורה שנעלמה, שהפכה לכותרת או לריקה, או חורגת מסוף הקובץ)
  missing   שורה שהייתה צריכה להיות מקושרת ואין לה קישור

הרצה:  python -X utf8 audit_links.py --base "<תיקיית ספרים>" [--seforim "<seforim.db>"] [--out "<תיקיית דוחות>"]
פלט:   audit_links_details.csv (שורה לכל ספר) בתיקיית הדוחות.
תנאי:  seforim.db זמין לקריאה. קבצי git-lfs (מצביעים) מסומנים ולא נבדקים. לפירוט ההגדרות: config.py.
"""
import collections, csv, glob, os, re, sqlite3, sys

sys.stdout.reconfigure(encoding='utf-8')

import config

BASE = config.BASE
LINKS_DIR = config.LINKS_DIR
HERE = config.OUT_DIR  # תיקיית הדוחות
sc = sqlite3.connect(config.SEF_URI, uri=True)

MAS = ['בבא בתרא', 'בבא קמא', 'בבא מציעא', 'ראש השנה', 'עבודה זרה', 'מועד קטן', 'פסחים', 'קידושין', 'קדושין',
       'מגילה', 'סנהדרין', 'סוכה', 'מכות', 'ביצה', 'יומא', 'ברכות', 'חגיגה', 'נדרים', 'עירובין', 'סוטה', 'זבחים',
       'תענית', 'נדה', 'נזיר', 'מנחות', 'כריתות', 'הוריות', 'בכורות', 'ערכין', 'תמורה', 'מעילה', 'תמיד', 'כתובות',
       'שבועות', 'יבמות', 'גיטין', 'שבת', 'חולין']
ALIAS = {'קדושין': 'קידושין'}
HEAD = re.compile(r'^<h([2-6])>(.*?)</h[2-6]>\s*$')
DAF = re.compile(r'^\[?(?:דף\s+)?([א-ת]{1,3})([.:])\]?$')
_toc = {}


def strip(t):
    return re.sub(r'<[^>]+>', '', t).strip()


def tocs(title):
    if title not in _toc:
        r = sc.execute('select id from book where title=?', (title,)).fetchone()
        _toc[title] = None if not r else sc.execute(
            'select e.id,e.parentId,e.level,t.text from tocEntry e join tocText t on t.id=e.textId '
            'where e.bookId=? order by e.id', (r[0],)).fetchall()
    return _toc[title]


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
    return {(byid[p][3].strip(), t.strip()) for i, p, l, t in e if l == 2 and p in byid}


def chset(title):
    e = tocs(title)
    return {t.strip() for _, _, l, t in e if l == 1} if e else None


def parse_daf_ext(t):
    # כותרות דף בפורמטים נוספים: "יד, ב", "דף ב ע"א", "כא ע״ב" -> "יד:" / "ב." / "כא:"
    q = '"״\'׳'  # גרשיים, גרשיים עבריים וגרש
    t = t.strip().strip('[]').strip()
    m = re.match(r'^(?:דף\s+)?([א-ת' + q + r']{1,5}?)\s*(?:,|ע[' + q + r']?)\s*([אב])$', t)
    if not m:
        return None
    return re.sub('[' + q + ']', '', m.group(1)) + ('.' if m.group(2) == 'א' else ':')


def find_mas(text):
    best = None
    for m in MAS:
        if re.search(r'(?<![א-ת])' + re.escape(m) + r'(?![א-ת])', text):
            if best is None or len(m) > len(best):
                best = m
    return best


def read_lines(path):
    return open(path, encoding='utf-8-sig', errors='replace').read().split('\n')


def is_lfs(L):
    return bool(L) and L[0].startswith('version https://git-lfs')


def expected_for(rel, b, L):
    """מחזיר (קטגוריה, {שורה: (יעד, מיקום)}) או (None, None) אם הספר לא אמור להיות מקושר."""
    # תנ"ך
    if rel[:1] == ('תנ״ך',) and len(rel) > 1 and rel[1] in ('ראשונים', 'תרגומים'):
        book = re.sub(r"\s*\(כתאב אלתאג'\)$", '', b)
        book = re.sub(r'^תרגום (שני )?(על )?', '', book)
        book = {'תהלים': 'תהילים'}.get(book, book)
        ch = chset(book)
        if not ch:
            return 'תנ"ך', {}, 'אין יעד ב-seforim.db: ' + book
        out, cur = {}, None
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
                out[i] = (book, cur)
        return 'תנ"ך', out, ''
    # ירושלמי שקלים
    if re.search(r'(?<![א-ת])שקלים(?![א-ת])', b):
        target = 'תלמוד ירושלמי שקלים'
        hs = hset(target) or set()
        pm = re.search(r'פרק ([א-ת]+)$', b)
        per = 'פרק ' + pm.group(1) if pm else None
        hal, out = None, {}
        for i, l in enumerate(L, 1):
            s = l.rstrip('\r').strip()
            hm = HEAD.match(s)
            if hm:
                t = strip(hm.group(2))
                m = re.search(r'פרק ([א-ת]+)$', t)
                if hm.group(1) == '2' and m:
                    per, hal = 'פרק ' + m.group(1), None
                elif t.startswith('הלכה '):
                    hal = t
                continue
            if i == 1 or not s or s.startswith('<h1'):
                continue
            if per and hal and (per, hal) in hs:
                out[i] = (target, per + ' ' + hal)
        return 'ירושלמי', out, ''
    # הגהות אשרי: פרק (h2) וסימן (h3) -> פסקי הרא"ש על המסכת, "פרק X הלכה Y" (הסימן בהגהות הוא ההלכה בפסקי הרא"ש)
    if b.startswith('הגהות אשרי - '):
        mas = ALIAS.get(b.split(' - ', 1)[1].strip(), b.split(' - ', 1)[1].strip())
        target = 'פסקי הרא"ש על ' + mas
        hs = hset(target)
        if not hs:
            return 'הגהות אשרי', {}, 'אין יעד ב-seforim.db: ' + target
        per, out = None, {}
        for i, l in enumerate(L, 1):
            s = l.rstrip('\r').strip()
            hm = HEAD.match(s)
            if hm:
                t = strip(hm.group(2))
                if hm.group(1) == '2' and re.match(r'^פרק [א-ת]+$', t):
                    per = t
                elif t.startswith('סימן ') and per:
                    hal = 'הלכה ' + t[len('סימן '):]
                    cur = (per, hal)
                    out['_cur'] = cur if cur in hs else None
                continue
            if i == 1 or not s or s.startswith('<h1'):
                continue
            if out.get('_cur'):
                out[i] = (target, out['_cur'][0] + ' ' + out['_cur'][1])
        out.pop('_cur', None)
        return 'הגהות אשרי', out, ''
    # בבלי
    if rel[:1] == ('תלמוד בבלי',) or rel[:2] == ('ספרים שאינם מותאמים לאוצריא', 'תלמוד בבלי'):
        if rel[:2] == ('תלמוד בבלי', 'שס וגשל'):
            return None, None, ''
        m = re.search(r'מסכת (.+?)\s*(?:\(\d+\)|\d+)?\s*$', b)
        mas = None
        if m:
            mas = re.sub(r"\s*\(.*?\)\s*$", '', m.group(1)).strip()
            mas = re.sub(r'\s+ועוד$', '', mas)
        elif rel[:1] == ('תלמוד בבלי',) and 'מחברי זמננו' not in rel:
            mas = find_mas(b.replace("''", '').replace("'", ''))
        mas = ALIAS.get(mas, mas) if mas else None
        mas0 = mas

        def run(ext):
            mas, ds = mas0, (dafset(mas0) if mas0 else None)
            cur, out = None, {}
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
                    elif ext:
                        r_ = parse_daf_ext(t)
                        if r_:
                            cur = r_
                    continue
                if i == 1 or not s or s.startswith('<h1'):
                    continue
                if ds and cur and cur in ds:
                    out[i] = (mas, cur)
            return ds, out

        ds, out = run(False)
        if not out:  # כותרות בפורמט אחר ("יד, ב", "דף ב ע"א")
            ds, out = run(True)
        reason = '' if ds else ('לא זוהתה מסכת' if not mas else 'אין יעד ב-seforim.db: ' + mas)
        return 'בבלי', out, reason
    return None, None, ''


def halacha_expected(b, L):
    """שלושת ספרי ההלכה המקושרים בנפרד (halacha.py)."""
    cfg = {
        "פרי חדש או״ח": ('שולחן ערוך, אורח חיים', r'^(סימן [א-ת]+)$', r'^<h2>(.*?)</h2>\s*$'),
        'ר״י קורקוס שבת': ('משנה תורה, הלכות שבת', r'^(פרק [א-ת]+)$', r'^<h2>(.*?)</h2>\s*$'),
        "דגול מרבבה אהע״ז": ('שולחן ערוך, אבן העזר', None, None),
        'חוות דעת ביאורים - הלכות ריבית': ('שולחן ערוך, יורה דעה', r'^(סימן [א-ת]+)$', r'^<h2>(.*?)</h2>\s*$'),
        'שער דעה - הלכות ריבית': ('שולחן ערוך, יורה דעה', r'^(סימן [א-ת]+)$', r'^<h2>(.*?)</h2>\s*$'),    }
    if b not in cfg:
        return None
    target, pat, hpat = cfg[b]
    ch = chset(target) or set()
    cur, out = None, {}
    for i, l in enumerate(L, 1):
        s = l.rstrip('\r').strip()
        if hpat:
            hm = re.match(hpat, s)
            if hm:
                m = re.match(pat, strip(hm.group(1)))
                cur = m.group(1) if m and m.group(1) in ch else None
                continue
            if i == 1 or not s or s.startswith('<h'):
                continue
        else:
            m = re.match(r'^דגול מרבבה אבן העזר (סימן [א-ת]+)$', s)
            if m:
                cur = m.group(1) if m.group(1) in ch else None
                continue
            if not s or s.startswith('====') or s.startswith('<h1'):
                continue
        if cur:
            out[i] = (target, cur)
    return out


def load_actual():
    act = collections.defaultdict(lambda: collections.defaultdict(set))
    files = glob.glob(os.path.join(LINKS_DIR, 'קישורים*', '*', 'links.csv'))
    for p in files:
        with open(p, encoding='utf-8-sig', newline='') as f:
            rd = csv.reader(f)
            next(rd)
            for r in rd:
                if len(r) >= 5:
                    act[r[1]][int(r[0])].add((r[3], r[4]))
    return act, len(files)


def main():
    print('טוען קישורים קיימים...')
    actual, nfiles = load_actual()
    print(f'{nfiles} קבצי links.csv, {len(actual)} ספרי מקור')

    books = {}  # stem -> (rel, path)
    dups = collections.defaultdict(list)
    for dp, _, fs in os.walk(BASE):
        if 'קבצי קישורים' in dp:
            continue
        rel = tuple(os.path.relpath(dp, BASE).split(os.sep))
        for f in sorted(fs):
            stem, ext = os.path.splitext(f)
            if ext == '.txt':
                dups[stem].append((rel, os.path.join(dp, f)))
    for stem, lst in dups.items():
        # מעדיפים את הנתיב שבו הספר מותאם (לא "שאינם מותאמים")
        lst.sort(key=lambda x: x[0][:1] == ('ספרים שאינם מותאמים לאוצריא',))
        books[stem] = lst[0]
    print(len(books), 'ספרי txt')

    rows, lfs = [], []
    for stem, (rel, path) in sorted(books.items()):
        L = read_lines(path)
        if is_lfs(L):
            lfs.append(stem)
            rows.append(dict(book=stem, path='/'.join(rel), cat='?', status='LFS', exp=0,
                             act=sum(len(v) for v in actual.get(stem, {}).values()), ok=0, wrong=0, stale=0,
                             missing=0, note='קובץ git-lfs: לא נבדק'))
            continue
        cat, exp, note = expected_for(rel, stem, L)
        h = halacha_expected(stem, L)
        if h is not None:
            cat, exp, note = 'הלכה', h, ''
        act = actual.get(stem, {})
        n_act = sum(len(v) for v in act.values())
        if cat is None:
            rows.append(dict(book=stem, path='/'.join(rel), cat='—', status='לא רלוונטי' if not n_act else 'קישורים ללא כלל',
                             exp=0, act=n_act, ok=0, wrong=0, stale=n_act, missing=0, note=''))
            continue
        ok = wrong = stale = missing = 0
        nlines = len(L)
        for ln, targets in act.items():
            e = exp.get(ln)
            if e is None:
                stale += len(targets)
            elif e in targets:
                ok += 1
                wrong += len(targets) - 1
            else:
                wrong += len(targets)
        missing = sum(1 for ln in exp if ln not in act)
        if not n_act and exp:
            status = 'חסרים קישורים'
        elif not n_act and not exp:
            status = 'אין מה לקשר' + (' (' + note + ')' if note else '')
        elif wrong or stale or missing:
            status = 'לא מעודכן'
        else:
            status = 'תקין'
        rows.append(dict(book=stem, path='/'.join(rel), cat=cat, status=status, exp=len(exp), act=n_act, ok=ok,
                         wrong=wrong, stale=stale, missing=missing, note=note))

    # ספרי מקור בקישורים שאין להם קובץ
    for s in actual:
        if s not in books:
            rows.append(dict(book=s, path='—', cat='?', status='מקור ללא קובץ', exp=0,
                             act=sum(len(v) for v in actual[s].values()), ok=0, wrong=0, stale=0, missing=0,
                             note='ספר מקור שאין לו קובץ במאגר'))

    keys = ['book', 'path', 'cat', 'status', 'exp', 'act', 'ok', 'wrong', 'stale', 'missing', 'note']
    with open(os.path.join(HERE, 'audit_links_details.csv'), 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, lineterminator='\r\n')
        w.writeheader()
        w.writerows(rows)

    st = collections.Counter(r['status'].split(' (')[0] for r in rows)
    print(dict(st))
    for s in ('לא מעודכן', 'חסרים קישורים', 'מקור ללא קובץ', 'קישורים ללא כלל'):
        sel = [r for r in rows if r['status'] == s]
        print('==', s, len(sel))
        for r in sorted(sel, key=lambda r: -(r['wrong'] + r['stale'] + r['missing'] + r['exp']))[:60]:
            print(f"  {r['book']} | {r['path'][:48]} | exp {r['exp']} act {r['act']} ok {r['ok']} wrong {r['wrong']} "
                  f"stale {r['stale']} missing {r['missing']} {r['note']}")
    print('LFS:', len(lfs))
    tot = collections.Counter()
    for r in rows:
        for k in ('exp', 'act', 'ok', 'wrong', 'stale', 'missing'):
            tot[k] += r[k]
    print(dict(tot))


if __name__ == '__main__':
    main()
