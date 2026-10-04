import os,sys,re
sys.stdout.reconfigure(encoding='utf-8')
P="קבצי קישורים וסדר הדורות/דורות.csv"
raw=open(P,'rb').read().decode('utf-8'); nl='\r\n' if '\r\n' in raw else '\n'
# 1) תיקון שמות חוב -> חו״ב
raw,c=re.subn(r'^חוב - ',"חו״ב - ",raw,flags=re.M); print("renamed חוב:",c)
have={l.split(',')[0].lstrip('\ufeff') for l in raw.split(nl)}
gen={}
for dp,_,fs in os.walk("."):
    if "קבצי קישורים" in dp or "ספרים שאינם מותאמים" in dp: continue
    for f in fs:
        if not f.endswith('.txt'): continue
        s=f[:-4]
        if s in have: continue
        p=dp
        g='מחברי זמננו' if 'מחברי זמננו' in p else 'אחרונים' if 'אחרונים' in p else 'ראשונים'
        gen[s]=g
add=sorted(gen.items())
raw=raw.rstrip('\r\n')+nl+nl.join(f"{s},{g},," for s,g in add)+nl
open(P,'wb').write(raw.encode('utf-8'))
print("added",len(add));print(add[:3],add[-4:])
