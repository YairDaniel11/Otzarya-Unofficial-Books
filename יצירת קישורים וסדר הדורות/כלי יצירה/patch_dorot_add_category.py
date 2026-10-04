s=open('gen.py',encoding='utf-8').read()
a="rows=[];un=[];src={}"
b='''import sqlite3
uc=sqlite3.connect(r"file:C:/Users/HP/AppData/Roaming/otzaria/databases/user_books.db?mode=ro",uri=True)
cats={r[0]:(r[1],r[2]) for r in uc.execute("select id,parentId,title from category")}
def cpath(i):
    p=[]
    while i:
        par,t=cats[i]; p.append(t); i=par
    return tuple(p[::-1])
path2id={cpath(i):i for i in cats}
dup={t for t,n in uc.execute("select title,count(*) from book group by title") if n>1}
rows=[];un=[];src={}'''
assert a in s; s=s.replace(a,b)
a="        rows.append((b,e)); src[s]=src.get(s,0)+1"
b='''        cid=path2id.get(("ספרים אישיים","ספרים")+tuple(parts)) if b in dup else None
        if b in dup and cid is None: print("NOCAT",parts,b)
        rows.append(((b,cid),e)); src[s]=src.get(s,0)+1'''
assert a in s; s=s.replace(a,b)
a="w.writerow(['ספר','דור'])\n    for b,e in sorted(seen.items()): w.writerow([b,e])"
b="w.writerow(['ספר','דור','מחבר','קטגוריה'])\n    for (b,cid),e in sorted(seen.items(),key=lambda x:(x[0][0],x[0][1] or 0)): w.writerow([b,e,'',cid if cid else ''])"
assert a in s; s=s.replace(a,b)
open('gen.py','w',encoding='utf-8').write(s)
