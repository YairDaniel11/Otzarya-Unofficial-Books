"""התאמת דיבור מתחיל (ד"ה) בפירוש על הש"ס לשורת הגמרא המדויקת ב-seforim.db (לקריאה בלבד).

מבנה seforim.db (נחקר): line(id,bookId,lineIndex,heRef) כש-heRef ברמת שורה: 'ברכות, ב., ד' = עמוד ב. שורה ד.
טקסט השורה נמצא ב-line_content(id,content) כ-blob דחוס zstd עם מילון מהטבלה zstd_dict (מזהה המילון בכותרת המסגרת).
מתוך: gemara_page_lines -> שורות עמוד, normalize -> טקסט מנורמל.

שימוש מהבונה:
    m = DhMatcher(official_db_connection)
    dh = extract_dh(source_line_content)           # רשימת מילים מנורמלות, או None
    hit = m.match('ברכות', 'ב:', dh, cursor)       # (heRef, lineIndex, confidence) או None
"""
import re
import sqlite3
from functools import lru_cache

try:
    import zstandard
except ImportError:  # בלי הספרייה אי אפשר לקרוא את טקסט הגמרא
    zstandard = None

NIKUD = re.compile(r'[֑-ׇ]')
TAG = re.compile(r'<[^>]*>')
QUOTES = re.compile(r'[\"\'׳״“”‘’`]')
PUNCT = re.compile(r'[^א-ת\s]')
BOLD_START = re.compile(r'^\s*(?:<[^>]+>\s*)*?<b>(.*?)</b>')

# קיצורי גמרא נפוצים בד"ה: מורחבים בשני הצדדים (גמרא ופירוש) לפני ההשוואה
ABBR = {
    'אר': 'אמר רבי', 'תר': 'תנו רבנן', 'שמ': 'שמע מינה', 'מט': 'מאי טעמא', 'הק': 'הכי קאמר',
    'סד': 'סלקא דעתך', 'אעג': 'אף על גב', 'אעפ': 'אף על פי', 'כש': 'כל שכן', 'תש': 'תא שמע',
    'תק': 'תנא קמא', 'אאכ': 'אלא אם כן', 'אכ': 'אם כן', 'וכ': 'וכן', 'עכ': 'על כן', 'מנל': 'מנא לן', 'ר': 'רבי', 'רא': 'רבי אליעזר', 'רע': 'רבי עקיבא',
    'רש': 'רבי שמעון', 'רמ': 'רבי מאיר', 'בש': 'בית שמאי', 'בה': 'בית הלל',
}
# מילים שאינן מאפיינות: סוף ד"ה ('וכו'')
CUT = {'וכו', 'וכולי', 'כולי', 'כו', 'וגו', 'וגומר', 'ועוד', 'עכ'}
# סימוני פתיחה שאינם חלק מטקסט הגמרא
LEAD = {'מתני', 'מתניתין', 'גמ', 'גמרא', 'בגמרא', 'הג', 'בא', 'שם', 'במשנה', 'משנה'}
STOP_FIRST = {'את', 'של'}


def words(text):
    """מילים מנורמלות: בלי תגים, ניקוד, גרשיים וסימני פיסוק; מקף = רווח; קיצורים מורחבים."""
    t = TAG.sub(' ', text or '')
    t = NIKUD.sub('', t).replace('־', ' ').replace('—', ' ').replace('-', ' ').replace('׃', ' ')
    out = []
    for raw in t.split():
        has_q = bool(QUOTES.search(raw))
        w = PUNCT.sub('', QUOTES.sub('', raw))
        if not w:
            continue
        if w in ABBR and (has_q or w in ('ר',)) and len(w) <= 3:
            out.extend(ABBR[w].split())
        else:
            out.append(w)
    return out


def normalize(text):
    """טקסט שורה מנורמל כמחרוזת (בלי ניקוד, תגי HTML, גרשיים וסימני פיסוק)."""
    return ' '.join(words(text))


@lru_cache(maxsize=None)
def skeleton(w):
    s = w.replace('ו', '').replace('י', '')  # כתיב מלא/חסר: בלי ו' וי'
    s = s.replace('ך', 'כ').replace('ם', 'מ').replace('ן', 'נ') \
         .replace('ף', 'פ').replace('ץ', 'צ')
    return s or w


@lru_cache(maxsize=1 << 20)
def _same(a, b):
    """שוויון מילים סביל: שלד (כתיב מלא/חסר), או הבדל בקידומת אחת (ו/ד/ה/ב/ל/מ/כ/ש), או טעות אות אחת במילה ארוכה."""
    if a == b:
        return True
    sa, sb = skeleton(a), skeleton(b)
    if sa == sb:
        return True
    pre = 'ודהבלמכש'
    if len(sa) > 3 and len(sb) > 3:
        if sa[0] in pre and sa[1:] == sb:
            return True
        if sb[0] in pre and sb[1:] == sa:
            return True
        if len(sa) == len(sb) and len(sa) >= 5 and sum(x != y for x, y in zip(sa, sb)) <= 1:
            return True
    return False


def extract_dh(content):
    """ד"ה מודגש בתחילת שורת פירוש: רשימת מילים מנורמלות, או None. עד 8 מילים; נחתך בראשית 'וכו''."""
    m = BOLD_START.match(content or '')
    if not m:
        return None
    raw = m.group(1)
    if len(raw) > 160:
        return None
    ws = words(raw)
    if not ws or len(ws) > 12:
        return None
    return clean_dh(ws)


def clean_dh(ws):
    ws = list(ws)
    for i, w in enumerate(ws):
        if w in CUT and i > 0:
            ws = ws[:i]
            break
    else:
        while ws and ws[-1] in CUT:
            ws.pop()
    return ws


def dh_variants(ws):
    """הד"ה כמות שהוא, ובלי סימון פתיחה ('גמרא', 'מתני'')."""
    out = [ws]
    if len(ws) > 1 and ws[0] in LEAD:
        out.append(ws[1:])
    return out


def match_in_line(dh, toks, nxt):
    """(k, p): כמה מילות ד"ה (מהתחלה) תואמות ברצף החל מעמדה p בשורה; ממשיך לשורה הבאה (nxt) אם הד"ה גולש."""
    best = (0, -1, 0)
    n = len(dh)
    for p in range(len(toks)):
        k = 0
        chars = 0
        seq = toks[p:] + nxt
        while k < n and k < len(seq) and _same(dh[k], seq[k]):
            chars += len(dh[k])
            k += 1
        if (k, chars) > (best[0], best[2]):
            best = (k, p, chars)
    return best[0], best[2]


def lcs_in_line(dh, toks):
    """אורך תת-סדרה משותפת (לפי הסדר, עם דילוגים) בין הד"ה לטקסט, ומספר התווים המותאמים."""
    n, m = len(dh), len(toks)
    if not n or not m:
        return 0, 0
    L = [[0] * (m + 1) for _ in range(n + 1)]
    C = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n):
        for j in range(m):
            if _same(dh[i], toks[j]):
                L[i + 1][j + 1] = L[i][j] + 1
                C[i + 1][j + 1] = C[i][j] + len(dh[i])
            else:
                if L[i][j + 1] >= L[i + 1][j]:
                    L[i + 1][j + 1], C[i + 1][j + 1] = L[i][j + 1], C[i][j + 1]
                else:
                    L[i + 1][j + 1], C[i + 1][j + 1] = L[i + 1][j], C[i + 1][j]
    return L[n][m], C[n][m]


class GemaraText:
    """קורא שורות גמרא מ-seforim.db: לפי מסכת -> עמודים (לפי heRef) -> שורות (lineIndex, heRef, מילים)."""

    def __init__(self, db):
        self.db = db
        self.cache = {}
        self._dicts = None
        self._dec = {}

    def _decompress(self, blob):
        if not isinstance(blob, bytes):
            return blob
        if self._dicts is None:
            self._dicts = {}
            for _, d in self.db.execute('SELECT id,dict FROM zstd_dict'):
                zd = zstandard.ZstdCompressionDict(d)
                self._dicts[zd.dict_id()] = zd
        did = zstandard.get_frame_parameters(blob).dict_id
        if did not in self._dec:
            self._dec[did] = zstandard.ZstdDecompressor(dict_data=self._dicts[did]) if did else zstandard.ZstdDecompressor()
        return self._dec[did].decompress(blob).decode('utf-8')

    def is_gemara(self, title):
        return self.masechet(title) is not None

    def masechet(self, title):
        """(pages, order): pages = {prefix: [(lineIndex, heRef, content)]}, order = רשימת prefix לפי סדר הספר. None אם לא בבלי."""
        if title in self.cache:
            return self.cache[title]
        res = None
        rows = self.db.execute(
            'SELECT b.id,c.parentId FROM book b JOIN category c ON c.id=b.categoryId WHERE b.title=?', (title,)).fetchall()
        if len(rows) == 1 and zstandard is not None:
            bid = rows[0][0]
            top = self.db.execute(
                'WITH RECURSIVE up(id,parentId,title) AS (SELECT c.id,c.parentId,c.title FROM book b '
                'JOIN category c ON c.id=b.categoryId WHERE b.id=? UNION ALL SELECT c.id,c.parentId,c.title '
                'FROM category c JOIN up ON c.id=up.parentId) SELECT group_concat(title,"/") FROM up', (bid,)).fetchone()[0]
            # base text: תלמוד בבלי > סדר X > מסכת (ולא תלמוד בבלי > ראשונים/אחרונים)
            if top and 'תלמוד בבלי' in top.split('/') and any(s.startswith('סדר ') for s in top.split('/')):
                pages, order = {}, []
                q = ('SELECT l.lineIndex,l.heRef,lc.content FROM line l JOIN line_content lc ON lc.id=l.id '
                     'WHERE l.bookId=? AND l.heRef IS NOT NULL ORDER BY l.lineIndex')
                for li, he, blob in self.db.execute(q, (bid,)):
                    parts = he.rsplit(', ', 1)
                    if len(parts) != 2:
                        continue
                    pre = parts[0]
                    if pre not in pages:
                        pages[pre] = []
                        order.append(pre)
                    pages[pre].append((li, he, words(self._decompress(blob))))
                if pages:
                    res = (pages, order)
        self.cache[title] = res
        return res

    def page_lines(self, title, page):
        """שורות עמוד ('ב.' או 'ב:') -> [(lineIndex, heRef, מילים)], ובנוסף שורות העמוד הבא."""
        m = self.masechet(title)
        if not m:
            return None
        pages, order = m
        pre = f'{title}, {page}'
        if pre not in pages:
            return None
        i = order.index(pre)
        nxt = pages[order[i + 1]] if i + 1 < len(order) else []
        return pages[pre], nxt


def gemara_line_text(db, title, line_index):
    """טקסט מנורמל של שורת גמרא (עזר לבדיקה ידנית)."""
    g = GemaraText(db)
    for pre in (g.masechet(title) or ({}, []))[0].values():
        for li, he, ws in pre:
            if li == line_index:
                return ' '.join(ws)
    return None


class DhMatcher:
    def __init__(self, db):
        self.g = GemaraText(db)
        self.stats = {}

    def _count(self, k):
        self.stats[k] = self.stats.get(k, 0) + 1

    def _cands(self, lines, dh_vars, fuzzy=False, skip=()):
        """מועמדות: [(lineIndex, heRef, kind, k, chars)], kind: 2 = התאמה מלאה, 1 = חלקית (רצף מתחילת הד"ה),
        0 = מטושטשת (תת-סדרה; רק כש-fuzzy=True ורק לשורות שאינן ב-skip: יקרה, ולכן נבדקת אחרונה)."""
        out = []
        for idx, (li, he, toks) in enumerate(lines):
            if fuzzy and li in skip:
                continue
            nxt = lines[idx + 1][2] if idx + 1 < len(lines) else []
            best = None
            for dh in dh_vars:
                n = len(dh)
                if not n:
                    continue
                if fuzzy:
                    if n < 3:
                        continue
                    seq = toks + nxt[:n]
                    lc, ch = lcs_in_line(dh, seq)
                    cand = (li, he, 0, lc, ch) if lc >= 3 and lc * 4 >= n * 3 and ch >= 10 else None
                else:
                    k, chars = match_in_line(dh, toks, nxt[:n])
                    if k == n and chars >= 3:
                        cand = (li, he, 2, k, chars)
                    elif k >= 2 and chars >= 8 and k * 2 >= n:
                        cand = (li, he, 1, k, chars)
                    else:
                        cand = None
                if cand and (best is None or cand[2:] > best[2:]):
                    best = cand
            if best:
                out.append(best)
        return out

    def match(self, title, page, dh, cursor=0):
        """(heRef, lineIndex, סוג) של שורת הגמרא לד"ה בעמוד (או בבא אחריו), או None. cursor = שורה מינימלית (שמירת סדר).
        סוג: full / full_next / partial / fuzzy / reorder (התאמה מלאה אך לפני הסמן: הסדר בפירוש אינו עוקב).
        הבדיקות היקרות נעשות בעצלתיים: הבא אחרי הנוכחי, והמטושטשת רק כשאין התאמה אחרת."""
        if not dh:
            return None
        pl = self.g.page_lines(title, page)
        if pl is None:
            self._count('no_page')
            return None
        cur, nxt = pl
        vars_ = dh_variants(dh)
        c_cur = self._cands(cur, vars_)
        after = [c for c in c_cur if c[2] == 2 and c[0] >= cursor]
        if after:
            return after[0][1], after[0][0], 'full'
        c_nxt = self._cands(nxt[:60], vars_) if nxt else []
        after = [c for c in c_nxt if c[2] == 2 and c[0] >= cursor]
        if after:
            return after[0][1], after[0][0], 'full_next'
        part = [c for c in c_cur if c[2] == 1 and c[0] >= cursor]
        if part:
            b = max(part, key=lambda c: (c[3], c[4], -c[0]))
            return b[1], b[0], 'partial'
        fz = [c for c in self._cands(cur, vars_, fuzzy=True, skip={c[0] for c in c_cur}) if c[0] >= cursor]
        if fz:
            b = max(fz, key=lambda c: (c[3], c[4], -c[0]))
            return b[1], b[0], 'fuzzy'
        before = [c for c in c_cur + c_nxt if c[2] == 2]
        if before:
            self._count('only_before_cursor')
            return before[0][1], before[0][0], 'reorder'
        return None
        pl = self.g.page_lines(title, page)
        if pl is None:
            self._count('no_page')
            return None
        cur, nxt = pl
        vars_ = dh_variants(dh)
        c_cur = self._cands(cur, vars_)
        c_nxt = self._cands(nxt[:60], vars_) if nxt else []
        for cs, name in ((c_cur, 'full'), (c_nxt, 'full_next')):
            after = [c for c in cs if c[2] == 2 and c[0] >= cursor]
            if after:
                return after[0][1], after[0][0], name
        for kind, name in ((1, 'partial'), (0, 'fuzzy')):
            part = [c for c in c_cur if c[2] == kind and c[0] >= cursor]
            if part:
                b = max(part, key=lambda c: (c[3], c[4], -c[0]))
                return b[1], b[0], name
        before = [c for c in c_cur + c_nxt if c[2] == 2]
        if before:
            self._count('only_before_cursor')
            return before[0][1], before[0][0], 'reorder'
        return None
