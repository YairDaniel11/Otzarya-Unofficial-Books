import csv,sqlite3,shutil
u=sqlite3.connect(r'file:C:/Users/HP/AppData/Roaming/otzaria/databases/user_books.db?mode=ro',uri=True)
have={(t,c) for t,c in u.execute("select title,categoryId from book")}
titles={t for t,_ in have}
src=r'C:\Users\HP\Desktop\דורות.csv'
shutil.copy(src,'dorot_full_backup.csv')
rows=list(csv.reader(open(src,encoding='utf-8-sig',newline='')))
hdr,body=rows[0],rows[1:]
keep=[];drop=[]
for r in body:
    t=r[0]; c=int(r[3]) if len(r)>3 and r[3] else None
    ok=(t,c) in have if c else t in titles
    (keep if ok else drop).append(r)
print(len(body),'keep',len(keep),'drop',len(drop))
for r in drop[:12]: print(' ',r[0],r[3] if len(r)>3 else '')
with open(src,'w',encoding='utf-8-sig',newline='') as f:
    w=csv.writer(f,lineterminator='\r\n'); w.writerow(hdr); w.writerows(keep)
