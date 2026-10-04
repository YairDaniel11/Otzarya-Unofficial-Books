import csv, os, pickle, shutil, sys, collections
sys.stdout.reconfigure(encoding='utf-8')
rows = pickle.load(open('rows.pkl', 'rb'))
HDR = ['מקור', 'ספר_מקור', 'מקור_אישי', 'ספר_יעד', 'מיקום_יעד', 'סוג', 'יעד_אישי', 'קטגוריית_מקור']
DESK = r'C:\Users\HP\Desktop'
main = rows['תנך'] + rows['ירושלמי'] + [r for r in rows['בבלי'] if not r[1].startswith('קובץ')]
big = [r for r in rows['בבלי'] if r[1].startswith('קובץ')]


def write(name, data, limit):
    base = os.path.join(DESK, name)
    if os.path.exists(base):
        shutil.rmtree(base)
    byfile = collections.OrderedDict()
    for r in data:
        byfile.setdefault((r[1], r[7]), []).append(r)
    chunks = [[]]
    for k, v in byfile.items():
        if chunks[-1] and len(chunks[-1]) + len(v) > limit:
            chunks.append([])
        chunks[-1].extend(v)
    for n, ch in enumerate(chunks, 1):
        d = os.path.join(base, '%02d' % n)
        os.makedirs(d)
        with open(os.path.join(d, 'links.csv'), 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f, lineterminator='\r\n')
            w.writerow(HDR)
            w.writerows(ch)
    print(name, len(data), 'שורות', len(chunks), 'קבצים')


write('קישורים', main, 25000)
write('קישורים - קבצי קובץ (גדולים)', big, 60000)
