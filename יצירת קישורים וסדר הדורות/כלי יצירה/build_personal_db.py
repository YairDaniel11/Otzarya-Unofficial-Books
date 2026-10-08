"""בונה DB אישי של אוצריא (SQLite) מתיקייה אחת במאגר: ספרים + דורות + מחברים + קישורים.

שימוש (ניסוי על תיקייה אחת):
    python -X utf8 build_personal_db.py "תלמוד ירושלמי" --out poc.db

- הספרים נכנסים כמות שהם: כל שורת קובץ = שורה ב-DB (lineIndex = מספר שורה בקובץ פחות 1).
- הדורות והמחברים נלקחים מ-דורות.csv.
- קישורים מ-links.csv שמקורם בספרי התיקייה נכנסים לטבלה external_link אל הספרייה הרשמית.
- קישורי <ספר>_links.json (פורמט אוצריא: line_index_1 בבסיס, path_2/line_index_2 במפרש) בין שני ספרים
  שבתיקייה עצמה נכנסים לאותה טבלה, עם targetSource = ה-library_id של המסד עצמו (למשל אנציקלופדיה תלמודית והערות עליה).
  היעד נפתר מול seforim.db המקומי (לקריאה בלבד): targetRef = heRef מדויק, ו-targetLineIndex כגיבוי.
"""
import argparse
import csv
import glob
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'ספרים'))
LINKS_DIR = os.path.join(ROOT, 'קבצי קישורים וסדר הדורות')
OFFICIAL_DB = r'C:\ProgramData\otzaria\books\seforim.db'
BOOK_EXTS = {'.txt'}  # בניסוי: טקסט בלבד (PDF/DOCX דורשים filePath)
LIBRARY_ID = 'otzarya-unofficial-books'
ROW_STRIDE = 1 << 20  # שורות/רשומות תוכן-עניינים לספר: מזהה = bookId*ROW_STRIDE + מונה, בלי תלות בספרים אחרים


class Ids:
    """מקצה מזהים יציבים: מפתח שכבר היה ב-DB הקודם (--stable-from) שומר id, חדשים מקבלים max+1."""

    def __init__(self, prev=None):
        self.map = dict(prev or {})
        self.next = max(self.map.values(), default=0) + 1

    def get(self, key):
        if key not in self.map:
            self.map[key] = self.next
            self.next += 1
        return self.map[key]


def load_prev(path):
    """קורא מ-DB קודם את מזהי הקטגוריות, הספרים, tocText, המחברים והדורות."""
    p = {'category': {}, 'book': {}, 'tocText': {}, 'author': {}, 'generation': {}}
    if not path:
        return p
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    rows = {i: (par, t) for i, par, t in db.execute('SELECT id,parentId,title FROM category')}

    def full(i):
        par, t = rows[i]
        return (full(par) if par else ()) + (t,)
    p['category'] = {full(i): i for i in rows}
    p['book'] = {t: i for i, t in db.execute('SELECT id,title FROM book')}
    p['tocText'] = {t: i for i, t in db.execute('SELECT id,text FROM tocText')}
    p['author'] = {t: i for i, t in db.execute('SELECT id,name FROM author')}
    p['generation'] = {t: i for i, t in db.execute('SELECT id,name FROM generation')}
    db.close()
    return p

SCHEMA = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE category (
  id INTEGER PRIMARY KEY, parentId INTEGER, title TEXT NOT NULL,
  level INTEGER NOT NULL DEFAULT 0, orderIndex INTEGER NOT NULL DEFAULT 999);
CREATE TABLE source (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE generation (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE author (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE book (
  id INTEGER PRIMARY KEY, categoryId INTEGER NOT NULL, sourceId INTEGER NOT NULL,
  title TEXT NOT NULL, heShortDesc TEXT, orderIndex INTEGER NOT NULL DEFAULT 999,
  totalLines INTEGER NOT NULL DEFAULT 0, isBaseBook INTEGER NOT NULL DEFAULT 0,
  hasSourceConnection INTEGER NOT NULL DEFAULT 0, hasAltStructures INTEGER NOT NULL DEFAULT 0,
  fileType TEXT DEFAULT 'txt');
CREATE TABLE book_author (bookId INTEGER NOT NULL, authorId INTEGER NOT NULL, PRIMARY KEY (bookId, authorId));
CREATE TABLE book_generation (bookId INTEGER NOT NULL, generationId INTEGER NOT NULL, PRIMARY KEY (bookId, generationId));
CREATE TABLE line (
  id INTEGER PRIMARY KEY, bookId INTEGER NOT NULL, lineIndex INTEGER NOT NULL,
  content TEXT NOT NULL, heRef TEXT, tocEntryId INTEGER);
CREATE INDEX idx_line_book_index ON line(bookId, lineIndex);
CREATE TABLE tocText (id INTEGER PRIMARY KEY, text TEXT NOT NULL UNIQUE);
CREATE TABLE tocEntry (
  id INTEGER PRIMARY KEY, bookId INTEGER NOT NULL, parentId INTEGER, textId INTEGER NOT NULL,
  level INTEGER NOT NULL, lineId INTEGER, isLastChild INTEGER NOT NULL DEFAULT 0,
  hasChildren INTEGER NOT NULL DEFAULT 0);
CREATE INDEX idx_toc_book ON tocEntry(bookId);
CREATE TABLE line_toc (lineId INTEGER PRIMARY KEY, tocEntryId INTEGER NOT NULL);
CREATE TABLE connection_type (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE external_link (
  sourceBookId INTEGER NOT NULL, sourceLineIndex INTEGER NOT NULL,
  targetSource TEXT, targetTitle TEXT NOT NULL, targetRef TEXT,
  targetLineIndex INTEGER, connectionType TEXT);
CREATE INDEX idx_external_link_source ON external_link(sourceBookId, sourceLineIndex);
"""

HEADING = re.compile(r'^<h([1-6])[^>]*>(.*?)</h\1>\s*$')
TAGS = re.compile(r'<[^>]+>')


def read_lines(path):
    with open(path, encoding='utf-8', errors='replace') as f:
        lines = f.read().replace('\r\n', '\n').split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    return lines


def load_dorot():
    rows = {}
    with open(os.path.join(LINKS_DIR, 'דורות.csv'), encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            # שם כפול עם קטגוריה: ברירת מחדל לשורה בלי קטגוריה
            if r['ספר'] not in rows or not r['קטגוריה']:
                rows[r['ספר']] = r
    return rows


class Official:
    """פותר יעד 'ספר + פרק [+ הלכה]' או 'מסכת + דף' (ב. / ב:) ל-heRef ושורה ב-seforim.db הרשמי."""

    def __init__(self, path):
        self.db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        self.cache = {}
        self.toc = {}  # bookId -> רשומות תוכן עניינים (נטענות פעם אחת לספר)

    def resolve(self, title, location):
        key = (title, location)
        if key in self.cache:
            return self.cache[key]
        res = None
        b = self.db.execute('SELECT id FROM book WHERE title=?', (title,)).fetchall()
        # פרק/הלכה, סימן (שו"ע), או דף בתלמוד ('ב.' = עמוד א, 'ב:' = עמוד ב; בתוכן העניינים הרשמי: 'דף ב.')
        m = re.fullmatch(r'(פרק \S+|סימן \S+|\S+[.:])(?: (הלכה \S+))?', location)
        if len(b) == 1 and m:
            bid = b[0][0]
            chap, hal = m.group(1), m.group(2)
            if chap[-1] in '.:':
                chap = 'דף ' + chap
            if bid not in self.toc:
                self.toc[bid] = self.db.execute(
                    'SELECT e.id,e.parentId,e.level,t.text,e.lineId FROM tocEntry e '
                    'JOIN tocText t ON t.id=e.textId WHERE e.bookId=? ORDER BY e.id', (bid,)).fetchall()
            ents = self.toc[bid]
            cid = next((e[0] for e in ents if e[2] == 1 and e[3] == chap), None)
            target = None
            if cid is not None:
                target = next((e for e in ents if e[0] == cid), None) if not hal else \
                    next((e for e in ents if e[1] == cid and e[3] == hal), None)
            if target and target[4] is not None:
                li = self.db.execute('SELECT lineIndex FROM line WHERE id=?', (target[4],)).fetchone()[0]
                row = self.db.execute(
                    'SELECT lineIndex,heRef FROM line WHERE bookId=? AND lineIndex>=? AND heRef IS NOT NULL '
                    'ORDER BY lineIndex LIMIT 1', (bid, li)).fetchone()
                if row:
                    res = (row[1], row[0])
        self.cache[key] = res
        return res


    def verses(self, title, location):
        """רשימת (heRef, lineIndex) של כל פסוקי הפרק ביעד, לפי הסדר. ריק אם לא תנ"ך ברמת פרק."""
        m = re.fullmatch(r'פרק (\S+)', location)
        b = self.db.execute('SELECT id FROM book WHERE title=?', (title,)).fetchall()
        if not m or len(b) != 1:
            return None
        res = self.resolve(title, location)
        if not res or res[0].count(', ') != 2:
            return None
        chapter = res[0].rsplit(', ', 1)[0]
        return [(h, i) for i, h in self.db.execute(
            "SELECT lineIndex,heRef FROM line WHERE bookId=? AND heRef LIKE ? ORDER BY lineIndex",
            (b[0][0], chapter + ', %'))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('folder', help='תיקייה (יחסית ל-ספרים) לבנייה')
    ap.add_argument('--out', default='poc.db')
    ap.add_argument('--official', default=OFFICIAL_DB)
    ap.add_argument('--library-id', default=LIBRARY_ID)
    ap.add_argument('--db-version', default='1')
    ap.add_argument('--display-name', default='מאגר ספרים לא רשמי (אוצריא)')
    ap.add_argument('--stable-from', help='DB קודם: ספרים/קטגוריות/מחברים קיימים שומרים id, חדשים בסוף')
    ap.add_argument('--root', default=ROOT, help='תיקיית ספרים (ברירת מחדל: ספרים של המאגר)')
    a = ap.parse_args()
    root = os.path.normpath(a.root)

    base = os.path.join(root, a.folder)
    files = sorted(os.path.join(d, f) for d, _, fs in os.walk(base) for f in fs
                   if os.path.splitext(f)[1] in BOOK_EXTS)
    if os.path.exists(a.out):
        os.remove(a.out)
    db = sqlite3.connect(a.out)
    db.executescript(SCHEMA)

    dorot = load_dorot()
    official = Official(a.official)

    meta = {'library_id': a.library_id, 'db_version': a.db_version,
            'display_name': a.display_name,
            'library_name': a.display_name,  # המפתח שאוצריא קוראת בפועל (התיעוד שלה כותב display_name)
            'description': 'ספרים מותאמים לאוצריא, עם קישורים לספרייה הרשמית',
            'author': 'YairDaniel11'}
    db.executemany('INSERT INTO schema_meta VALUES (?,?)', meta.items())
    db.execute("INSERT INTO source(id,name) VALUES (1,'Otzarya-Unofficial-Books')")
    db.execute("INSERT INTO connection_type(name) VALUES ('SOURCE')")

    prev = load_prev(a.stable_from)
    cat_ids, book_id_alloc, toc_text_ids = Ids(prev['category']), Ids(prev['book']), Ids(prev['tocText'])
    gen_ids, auth_ids = Ids(prev['generation']), Ids(prev['author'])
    cats, gens, auths = {}, {}, {}

    def cat_id(parts):
        parent = None
        for i in range(len(parts)):
            k = tuple(parts[:i + 1])
            if k not in cats:
                cats[k] = cat_ids.get(k)
                db.execute('INSERT INTO category(id,parentId,title,level,orderIndex) VALUES (?,?,?,?,?)',
                           (cats[k], parent, parts[i], i, len(cats)))
            parent = cats[k]
        return parent

    def lookup(table, d, alloc, name):
        if name not in d:
            d[name] = alloc.get(name)
            db.execute(f'INSERT INTO {table}(id,name) VALUES (?,?)', (d[name], name))
        return d[name]

    book_ids, stats, toc_written = {}, defaultdict(int), set()
    for order, path in enumerate(files, 1):
        title = os.path.splitext(os.path.basename(path))[0]
        rel = os.path.relpath(os.path.dirname(path), root).split(os.sep)
        cid = cat_id(rel)
        lines = read_lines(path)
        if len(lines) >= ROW_STRIDE:
            raise SystemExit(f'ספר ארוך מדי למזהים יציבים: {title}')
        bid = book_id_alloc.get(title)
        db.execute(
            'INSERT INTO book(id,categoryId,sourceId,title,orderIndex,totalLines) VALUES (?,?,?,?,?,?)',
            (bid, cid, 1, title, order, len(lines)))
        book_ids[title] = bid
        d = dorot.get(title)
        if d:
            if d['דור']:
                db.execute('INSERT INTO book_generation VALUES (?,?)', (bid, lookup('generation', gens, gen_ids, d['דור'])))
            if d['מחבר']:
                db.execute('INSERT INTO book_author VALUES (?,?)', (bid, lookup('author', auths, auth_ids, d['מחבר'])))
        else:
            stats['no_dorot'] += 1

        toc_texts, stack, line_ids = {}, [], []
        entries = []  # (entry_id, line_index)
        cur_entry = None
        for i, content in enumerate(lines):
            lid = bid * ROW_STRIDE + i + 1
            db.execute('INSERT INTO line(id,bookId,lineIndex,content) VALUES (?,?,?,?)', (lid, bid, i, content))
            line_ids.append(lid)
            m = HEADING.match(content)
            if m:
                level = int(m.group(1)) - 1
                text = TAGS.sub('', m.group(2)).strip() or '-'
                tid = toc_texts.get(text)
                if tid is None:
                    tid = toc_text_ids.get(text)
                    if text not in toc_written:
                        db.execute('INSERT INTO tocText(id,text) VALUES (?,?)', (tid, text))
                        toc_written.add(text)
                    toc_texts[text] = tid
                while stack and stack[-1][1] >= level:
                    stack.pop()
                parent = stack[-1][0] if stack else None
                cur_entry = bid * ROW_STRIDE + len(entries) + 1
                db.execute('INSERT INTO tocEntry(id,bookId,parentId,textId,level,lineId) VALUES (?,?,?,?,?,?)',
                           (cur_entry, bid, parent, tid, level, lid))
                stack.append((cur_entry, level))
                entries.append(cur_entry)
            if cur_entry is not None:
                db.execute('UPDATE line SET tocEntryId=? WHERE id=?', (cur_entry, lid))
                db.execute('INSERT INTO line_toc VALUES (?,?)', (lid, cur_entry))
        db.execute('UPDATE tocEntry SET hasChildren=1 WHERE bookId=? AND id IN '
                   '(SELECT parentId FROM tocEntry WHERE bookId=? AND parentId IS NOT NULL)', (bid, bid))
        stats['books'] += 1
        stats['lines'] += len(lines)

    # קישורים: קודם נאספים ומתוקנים, ורק אחר כך נכתבים
    LABEL = re.compile(r'^\s*(\{[^}]*\}|<h[1-6][^>]*>.*</h[1-6]>)?\s*$')  # תווית, כותרת או שורה ריקה
    pending = defaultdict(list)  # (מקור, יעד, פרק-ביעד, סוג) -> [(שורה, ref, li)]
    seen = set()
    for f in sorted(glob.glob(os.path.join(LINKS_DIR, 'קישורים*', '*', 'links.csv'))):
        with open(f, encoding='utf-8-sig') as fh:
            for r in csv.DictReader(fh):
                src = r['ספר_מקור']
                if src not in book_ids:
                    continue
                stats['links_in_csv'] += 1
                res = official.resolve(r['ספר_יעד'], r['מיקום_יעד'])
                if not res:
                    stats['unresolved'] += 1
                    continue
                ref, li = res
                line0 = int(r['מקור']) - 1
                content = db.execute('SELECT content FROM line WHERE bookId=? AND lineIndex=?',
                                     (book_ids[src], line0)).fetchone()
                if content is None or LABEL.match(content[0]):
                    stats['skipped_label'] += 1  # לא מקשרים תוויות כמו {פרשת בראשית}
                    continue
                key = (src, line0, ref)
                if key in seen:
                    stats['dup'] += 1
                    continue
                seen.add(key)
                pending[(src, r['ספר_יעד'], r['מיקום_יעד'], r['סוג'])].append((line0, ref, li))

    for (src, target, loc, kind), items in pending.items():
        items.sort()
        # כתאב אלתאג' (מקרא): שורה אחת לפסוק, גם כשחלוקת הפסוקים בפרק שונה במעט מהרשמי
        taj_text = src.endswith(" (כתאב אלתאג')") and not src.startswith('תרגום')
        verses = official.verses(target, loc) if kind == 'תרגום' or taj_text else None
        if verses and len(verses) == len(items):
            # תרגום שורה-לפסוק: השורה ה-i מקושרת לפסוק ה-i
            items = [(it[0], v[0], v[1]) for it, v in zip(items, verses)]
            stats['verse_aligned'] += len(items)
        elif verses and taj_text:
            # מספר פסוקים שונה: השורה ה-i לפסוק ה-i, ועודף שורות נצמד לפסוק האחרון
            items = [(it[0],) + verses[min(i, len(verses) - 1)] for i, it in enumerate(items)]
            stats['verse_aligned_clamped'] += len(items)
        for line0, ref, li in items:
            db.execute('INSERT INTO external_link VALUES (?,?,?,?,?,?,?)',
                       (book_ids[src], line0, 'official', target, ref, li, 'SOURCE'))
            stats['links'] += 1
    # קישורים בין ספרים באותו מסד (<ספר>_links.json): המפרש הוא המקור והבסיס הוא היעד, כמו בקישורי links.csv
    json_seen = set()
    for jf in sorted(glob.glob(os.path.join(base, '**', '*_links.json'), recursive=True)):
        base_title = os.path.basename(jf)[:-len('_links.json')]
        if base_title not in book_ids:
            continue
        with open(jf, encoding='utf-8') as fh:
            entries = json.load(fh)
        for e in entries:
            src = os.path.splitext(os.path.basename(e['path_2']))[0]
            if src not in book_ids:
                stats['json_missing_book'] += 1
                continue
            sl, tl = int(e['line_index_2']) - 1, int(e['line_index_1']) - 1
            key = (src, sl, base_title, tl)
            if sl < 0 or tl < 0 or key in json_seen:
                stats['json_dup_or_bad'] += 1
                continue
            json_seen.add(key)
            db.execute('INSERT INTO external_link VALUES (?,?,?,?,?,?,?)',
                       (book_ids[src], sl, a.library_id, base_title, None, tl, 'SOURCE'))
            stats['json_links'] += 1
    db.execute('UPDATE book SET hasSourceConnection=1 WHERE id IN (SELECT DISTINCT sourceBookId FROM external_link)')
    db.commit()
    db.execute('VACUUM')
    db.close()
    print(dict(stats))
    print('size MB:', round(os.path.getsize(a.out) / 1e6, 2))


if __name__ == '__main__':
    sys.exit(main())
