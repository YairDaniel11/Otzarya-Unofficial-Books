import csv, os, re, sqlite3, sys, collections
sys.stdout.reconfigure(encoding='utf-8')
ROOT=r'C:\Users\HP\Desktop\מאגרי גיטאב\אוצריא\מאגר ספרים - גיטאב\ספרים'
SEF=r'file:C:\Users\HP\AppData\Roaming\io.github.kdroidfilter.seforimapp\databases\seforim.db?mode=ro'
UB=r'file:C:\Users\HP\AppData\Roaming\otzaria\databases\user_books.db?mode=ro'
OUT=r'C:\Users\HP\Desktop\קישורים.csv'
sc=sqlite3.connect(SEF,uri=True); uc=sqlite3.connect(UB,uri=True)
user_titles={r[0] for r in uc.execute("select title from book")}
def dafs(title):
    r=sc.execute("select id from book where title=?",(title,)).fetchone()
    if not r: return None
    s=set()
    for (t,) in sc.execute("select t.text from tocEntry e join tocText t on t.id=e.textId where e.bookId=? and e.level=1",(r[0],)):
        m=re.match(r'דף ([א-ת"״\']+)([.:])',t.strip())
        if m: s.add(m.group(1)+m.group(2))
    return s
ALIAS={'קדושין':'קידושין'}
cache={}
def bavli(m):
    m=ALIAS.get(m,m)
    if m not in cache: cache[m]=dafs(m)
    return m,cache[m]
HEAD=re.compile(r'^<h[2-6]>(.*?)</h[2-6]>\s*$')
DAF=re.compile(r'^\[?(?:דף\s+)?([א-ת]{1,3})([.:])\]?$')
rows=[];stat=collections.Counter();skipped=[];notitle=[]
for dp,dn,fn in os.walk(ROOT):
    rel=os.path.relpath(dp,ROOT).split(os.sep)
    if rel[:2]==['תלמוד בבלי','שס וגשל']: continue
    for n in sorted(fn):
        b,e=os.path.splitext(n)
        if e!='.txt': continue
        m=re.search(r'מסכת (.+?)\s*(?:\(\d+\)|\d+)?\s*$',b)
        if not m: continue
        mas=re.sub(r"\s*\(.*?\)\s*$",'',m.group(1)).strip()
        mas=re.sub(r'\s+ועוד$','',mas)
        title,ds=bavli(mas)
        if not ds: skipped.append((b,mas)); continue
        if b not in user_titles: notitle.append(b); continue
        cur=None;cnt=0;bad=set()
        for i,l in enumerate(open(os.path.join(dp,n),encoding='utf-8-sig').read().split('\n'),1):
            l=l.rstrip('\r'); s=l.strip()
            hm=HEAD.match(s)
            if hm:
                t=re.sub(r'<[^>]+>','',hm.group(1)).strip()
                dm=DAF.match(t)
                if dm and s.startswith('<h3'):
                    cur=dm.group(1)+dm.group(2)
                    if cur not in ds: bad.add(cur)
                continue
            if i==1 or not s or s.startswith('<h1'): continue
            if cur and cur in ds:
                rows.append((i,b,title,cur)); cnt+=1
        stat['files']+=1; stat['rows']+=cnt
        if bad: stat['baddafs']+=len(bad); print('daf לא נמצא',b,sorted(bad)[:8])
print(stat); print('skipped (no target book):',skipped); print('not in user lib:',notitle)
with open(OUT,'w',encoding='utf-8-sig',newline='') as f:
    w=csv.writer(f,lineterminator='\r\n'); w.writerow(['מקור','ספר_מקור','מקור_אישי','ספר_יעד','מיקום_יעד','סוג','יעד_אישי'])
    for i,b,t,d in rows: w.writerow([i,b,'כן',t,d,'פירוש','לא'])
print(os.path.getsize(OUT)/1e6,'MB')
