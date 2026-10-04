import csv, json, os, re, sys, unicodedata
sys.stdout.reconfigure(encoding='utf-8')
ROOT = r'C:\Users\HP\Desktop\מאגרי גיטאב\אוצריא\מאגר ספרים - גיטאב\ספרים'
def norm(s):
    s = s.replace('"','״').replace("''",'״').replace('׳׳','״')
    s = re.sub(r'[֑-ׇ]','',s)
    s = re.sub(r'[\\/:*?<>|]','',s)
    s = s.replace('_',' ').replace("'",'').replace('״','')
    return re.sub(r'\s+',' ',s).strip()
up = {}
with open('gen_up.csv',encoding='utf-8-sig',newline='') as f:
    r = csv.reader(f); next(r)
    for row in r:
        if len(row)>=2: up[norm(row[0])] = row[1].strip()
meta = {}
for m in json.load(open('meta.json',encoding='utf-8')):
    if m.get('heEra') and m.get('title'): meta[norm(m['title'])] = m['heEra']
files=[]
for dp,dn,fn in os.walk(ROOT):
    for n in fn:
        ext=os.path.splitext(n)[1].lower()
        if ext in('.txt','.pdf','.docx'):
            files.append((os.path.join(dp,n), os.path.relpath(dp,ROOT).split(os.sep), os.path.splitext(n)[0]))
print(len(files))
c={'up':0,'meta':0,'none':0}; none=[]
for p,parts,t in files:
    k=norm(t)
    if k in up: c['up']+=1
    elif k in meta: c['meta']+=1
    else: c['none']+=1; none.append((parts,t))
print(c)
from collections import Counter
print(Counter(tuple(x[0][:2]) for x in none).most_common(40))
json.dump([(a,b) for a,b in none],open('none.json','w',encoding='utf-8'),ensure_ascii=False)
print(Counter(meta.values()))
