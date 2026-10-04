"""
עדכון קישורים קיימים לפי הקבצים הנוכחיים, והוספת קישורים לספרים שאין להם.

למה: מספרי השורות בקישורים נוצרו מגרסה ישנה של הקבצים. מאז נוספה שורת תמונה בשורה 1 בכ-500 ספרים,
ולכן כל מספרי השורות שלהם קטנים ב-1. הכלי משחזר מהקבצים הנוכחיים את הקישורים הנכונים (אותה לוגיקה של
audit_links.py, שהיא הלוגיקה של links2.py/halacha.py) ומחליף בהם את השורות הישנות.

אלגוריתם:
 1. קבוצה = (ספר_מקור, קטגוריית_מקור). שמות כפולים בתיקיות שונות (למשל "בראשית (כתאב אלתאג')" ברש"י, ברמב"ן ובתרגום)
    נבדלים רק לפי הקטגוריה, ולכן כל קבוצה מותאמת לקובץ המועמד שמתאים לה ביותר (חפיפה של שורות, עם הזזה של -2..+3).
 2. קבוצה שנתפסת (חפיפה >= 90%) מוחלפת בקישורים הנכונים לקובץ. עמודת הקטגוריה והסוג נשמרות.
 3. קבוצה שלא נתפסת, ספר שהוא מצביע git-lfs וספרים שמופיעים ב-links_new_books.py נשארים כמו שהם ומדווחים.
 4. ספר לא-כפול שיש לו קישורים צפויים ואין לו שום קבוצה מקבל קישורים חדשים ב-קישורים/13/links.csv.

הרצה:
  python -X utf8 fix_links.py            # ניסוי יבש + דוח בתיקיית הדוחות (fix_links_report.csv)
  python -X utf8 fix_links.py --apply    # כותב את הקבצים (שומר BOM ו-CRLF, ומשאיר את סדר השורות)
  python -X utf8 fix_links.py --verify   # בדיקה אחרי כתיבה: כל קבוצה תואמת בדיוק לקובץ
לפני --apply לגבות את תיקיית קבצי קישורים וסדר הדורות.
"""
import collections, csv, glob, io, os, sys

import audit_links as A

sys.stdout.reconfigure(encoding='utf-8')
LINKS = A.LINKS_DIR
HEADER = ['מקור', 'ספר_מקור', 'מקור_אישי', 'ספר_יעד', 'מיקום_יעד', 'סוג', 'יעד_אישי', 'קטגוריית_מקור']
NEW_DIR = os.path.join(LINKS, 'קישורים', '13')
REPORT = os.path.join(A.HERE, 'fix_links_report.csv')
SHIFTS = range(-2, 4)
MANUAL = {'ברכת שמואל על בבא מציעא'}  # נוצרו ב-links_new_books.py


def candidates():
    c = collections.defaultdict(list)
    for dp, _, fs in os.walk(A.BASE):
        if 'קבצי קישורים' in dp:
            continue
        rel = tuple(os.path.relpath(dp, A.BASE).split(os.sep))
        for f in sorted(fs):
            if f.endswith('.txt'):
                c[f[:-4]].append((rel, os.path.join(dp, f)))
    return c


_exp_cache = {}


def expected(stem, rel, path):
    key = path
    if key not in _exp_cache:
        L = A.read_lines(path)
        if A.is_lfs(L):
            _exp_cache[key] = None
        else:
            cat, exp, _ = A.expected_for(rel, stem, L)
            h = A.halacha_expected(stem, L)
            if h is not None:
                exp = h
            _exp_cache[key] = exp or {}
    return _exp_cache[key]


def read_files():
    files = {}
    for p in sorted(glob.glob(os.path.join(LINKS, 'קישורים*', '*', 'links.csv'))):
        with open(p, encoding='utf-8-sig', newline='') as f:
            rd = list(csv.reader(f))
        files[p] = rd
    return files


def write_file(p, rows):
    out = io.StringIO()
    csv.writer(out, lineterminator='\r\n').writerows(rows)
    with open(p, 'wb') as f:
        f.write(('﻿' + out.getvalue()).encode('utf-8'))


def main():
    apply_, verify = '--apply' in sys.argv, '--verify' in sys.argv
    cands = candidates()
    files = read_files()
    groups = collections.OrderedDict()  # (stem,cat) -> rows
    for p, rows in files.items():
        for r in rows[1:]:
            if len(r) >= 8:
                groups.setdefault((r[1], r[7]), []).append(r)
    print(len(files), 'קבצים,', len(groups), 'קבוצות (ספר, קטגוריה)')

    new_for, report, stats = {}, [], collections.Counter()
    by_stem = collections.OrderedDict()
    for (stem, cat), rows in groups.items():
        by_stem.setdefault(stem, []).append((cat, rows))
    for stem, glist in by_stem.items():
        olds = []
        for cat, rows in glist:
            old = collections.defaultdict(set)
            for r in rows:
                old[int(r[0])].add((r[3], r[4]))
            olds.append(old)
        if stem in MANUAL:
            for cat, rows in glist:
                report.append((stem, cat, 'ידני', len(rows), len(rows), '', 'נוצר ב-links_new_books.py'))
                stats['ידני'] += 1
            continue
        cl = [(rel, path, expected(stem, rel, path)) for rel, path in cands.get(stem, [])]
        any_lfs = any(e is None for _, _, e in cl)
        cl = [(rel, path, e) for rel, path, e in cl if e]
        # ציון חפיפה (Jaccard) לכל זוג (קבוצה, קובץ), עם ההזזה הטובה ביותר
        pairs = []
        for gi, old in enumerate(olds):
            n_old = sum(len(v) for v in old.values())
            for ci, (rel, path, exp) in enumerate(cl):
                bj, bs = 0.0, 0
                for s in SHIFTS:
                    m = sum(1 for ln, ts in old.items() if exp.get(ln + s) in ts)
                    j = m / (n_old + len(exp) - m) if (n_old + len(exp) - m) else 0
                    if j > bj:
                        bj, bs = j, s
                pairs.append((bj, gi, ci, bs))
        pairs.sort(reverse=True)
        gdone, cdone, assign = set(), set(), {}
        for j, gi, ci, s in pairs:
            if j < 0.5 or gi in gdone or ci in cdone:
                continue
            gdone.add(gi)
            cdone.add(ci)
            assign[gi] = (ci, s, j)
        for gi, (cat, rows) in enumerate(glist):
            n_old = len(rows)
            if gi not in assign:
                why = ('מצביע git-lfs, לא נבדק' if any_lfs else ('אין קובץ' if not cands.get(stem)
                       else 'אין קובץ מתאים לקבוצה (חפיפה < 50% או אין קישורים צפויים)'))
                report.append((stem, cat, 'לא נתפס' if cl else 'לא נבדק', n_old, n_old, '', why))
                stats['לא נתפס' if cl else 'לא נבדק'] += 1
                continue
            ci, s, j = assign[gi]
            rel, path, exp = cl[ci]
            new = [[str(ln), stem, rows[0][2], t_, ref, rows[0][5], rows[0][6], cat]
                   for ln, (t_, ref) in sorted(exp.items())]
            new_for[(stem, cat)] = new
            status = 'תקין' if s == 0 and j == 1.0 else 'עודכן'
            stats[status] += 1
            report.append((stem, cat, status, n_old, len(new), s, f'{"/".join(rel)} (התאמה {j:.0%})'))

    # ספרים חדשים: אין להם אף קבוצה, אינם כפולים, ויש להם קישורים צפויים
    additions = []
    for stem, lst in cands.items():
        if stem in MANUAL or any((stem, c) in groups for c in {k[1] for k in groups if k[0] == stem}):
            continue
        if len(lst) != 1:
            continue
        rel, path = lst[0]
        exp = expected(stem, rel, path)
        if exp:
            first = next(iter(exp.values()))
            typ = 'תרגום' if rel[:2] == ('תנ״ך', 'תרגומים') else 'פירוש'
            additions.append(([[str(ln), stem, 'כן', t, ref, typ, 'לא', ''] for ln, (t, ref) in sorted(exp.items())], stem, rel))
    for rows, stem, rel in additions:
        report.append((stem, '', 'חדש', 0, len(rows), '', '/'.join(rel)))
        stats['חדש'] += 1

    print(dict(stats))
    tot_old = sum(r[3] for r in report)
    tot_new = sum(r[4] for r in report)
    print('שורות לפני:', tot_old, ' אחרי:', tot_new)
    with open(REPORT, 'wb') as f:
        out = io.StringIO()
        w = csv.writer(out, lineterminator='\r\n')
        w.writerow(['ספר', 'קטגוריה', 'סטטוס', 'שורות_לפני', 'שורות_אחרי', 'הזזה', 'הערה'])
        w.writerows(report)
        f.write(('﻿' + out.getvalue()).encode('utf-8'))
    print('דוח:', REPORT)
    if verify:
        bad = [r for r in report if r[2] in ('עודכן', 'לא נתפס')]
        print('בדיקה:', 'הכל תואם' if not bad else f'{len(bad)} קבוצות שלא תואמות', bad[:10])
        return
    if not apply_:
        print('ניסוי יבש. לכתיבה: --apply')
        return
    # כתיבה: החלפת כל קבוצה במקומה, בלי לשנות את סדר הקבוצות
    for p, rows in files.items():
        out_rows, emitted = [rows[0]], set()
        changed = False
        for r in rows[1:]:
            key = (r[1], r[7]) if len(r) >= 8 else None
            if key in new_for:
                changed = True
                if key not in emitted:
                    out_rows.extend(new_for[key])
                    emitted.add(key)
                continue
            out_rows.append(r)
        if changed:
            write_file(p, out_rows)
            print('נכתב', p, len(rows) - 1, '->', len(out_rows) - 1)
    if additions:
        existing = files.get(os.path.join(NEW_DIR, 'links.csv'))
        base = existing if existing else [HEADER]
        names = {a[1] for a in additions}
        rows = [base[0]] + [r for r in base[1:] if r[1] not in names]
        for adds, stem, rel in additions:
            rows.extend(adds)
        write_file(os.path.join(NEW_DIR, 'links.csv'), rows)
        print('קישורים חדשים ל', len(additions), 'ספרים ב', NEW_DIR)


if __name__ == '__main__':
    main()
