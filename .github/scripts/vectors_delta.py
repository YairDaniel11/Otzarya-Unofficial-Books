#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""עדכוני דלתא לווקטורי החיפוש של המאגר (build-vectors.yml, vectors_ci.py). תיעוד ונימוקים: vectors_delta.md באותה תיקייה.

העיקרון: הדלתא אינה "מקטע של אוצריא" אלא קובץ פנימי של המאגר (.oxd), והמתקין מחיל אותה על המקטע האישי המותקן
וכותב מקטע אישי יחיד, בדיוק כמו התקנה מלאה. לכן אוצריא רואה תמיד את אותו מבנה (מקטע "שלנו" אחד שמוחלף כולו ב-
publish_generation), בלי להסתמך על דגלי delta/.del/tombstones של אוצריא שאי אפשר לאמת כאן.

תוכן הדלתא (בין מקטע ישן S1 למקטע חדש S2, שניהם בנויים בידי הסקריפט של קרית חנה):
  removed  רשומות occurrence של S1 שאינן ב-S2 (key, ספר, hint, anchor, window): מה שירד או שה-hint שלו השתנה
  added    רשומות occurrence של S2 שאינן ב-S1
  keys     המפתחות ש-S2 מכיל ו-S1 לא, עם הווקטורים המקודדים שלהם (codes + scales): המידע היחיד שאי אפשר לשחזר
החלה: שחזור שורות S1 מסעיף OCCURRENCES, הסרת removed, הוספת added, plan_segment של הסקריפט (אותו אלגוריתם בדיוק
שבונה את S2) ו-write_segment, כשהווקטורים נלקחים מ-S1 (מפתח קיים) או מהדלתא (מפתח חדש). התוצאה זהה בבייטים ל-S2, וה-sha256
שלה נבדק מול המניפסט; שרשרת של דלתאות שקולה אפוא לבסיס מלא של הגרסה האחרונה.
"""
import hashlib
import json
import struct
from pathlib import Path

import numpy as np

MAGIC = b"OXDELTA1\n"
DELTA_FORMAT = "otzarya-unofficial-books-vectors-delta"
ROW_DT = np.dtype([("key", "<u8"), ("name", "<u4"), ("hint", "<u4"), ("anchor", "<u8"), ("window", "u1")])


class DeltaError(Exception):
    pass


def sha256_file(path, chunk=8 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


# ------------------------------------------------------------------ קריאת מקטע

class SegInfo:
    """מה שדלתא צריכה ממקטע: שמות הספרים, שורות ה-occurrence (key, ספר, hint, anchor, window), מפתחות הסלוטים."""

    def __init__(self, opv, path):
        self.opv, self.path = opv, Path(path)
        self.h = opv.read_header(self.path)
        if self.h["version"] != 3 or self.h["codec"] != opv.CODEC_ROT:
            raise DeltaError(f"{self.path.name}: אינו מקטע בפורמט 3 / rot192-i8i4")
        sec = lambda k: opv.section(self.path, self.h, k)
        self.params = sec(opv.CODEC_PARAMS)
        nb = sec(opv.BOOK_NAMES) or b""
        books = np.frombuffer(sec(opv.BOOKS) or b"", opv.BOOK_DT)
        self.names = [nb[int(b["name_off"]):int(b["name_off"]) + int(b["name_len"])].decode("utf-8") for b in books]
        self.occ = np.frombuffer(sec(opv.OCCURRENCES) or b"", opv.OCC_DT)
        self.slot_keys = np.frombuffer(sec(opv.SLOT_KEYS) or b"", ">u8").astype(np.uint64)
        vs = next(s for s in self.h["sections"] if s["kind"] == opv.VECTORS)
        self.vec_off, self.vec_bytes = vs["off"], vs["elem"]
        self.scale_off = next(s for s in self.h["sections"] if s["kind"] == opv.SCALES)["off"]
        order = np.argsort(self.slot_keys, kind="stable")
        self.sorted_keys, self.slot_of_sorted = self.slot_keys[order], order

    def slots_of(self, keys):
        """מספרי הסלוטים של keys (-1: אין)."""
        keys = np.asarray(keys, np.uint64)
        i = np.searchsorted(self.sorted_keys, keys)
        i = np.minimum(i, max(len(self.sorted_keys) - 1, 0))
        ok = (self.sorted_keys[i] == keys) if len(self.sorted_keys) else np.zeros(len(keys), bool)
        return np.where(ok, self.slot_of_sorted[i] if len(self.sorted_keys) else 0, -1).astype(np.int64)

    def rows(self, name_ids):
        """שורות ROW_DT, כשהשם הוא האינדקס ב-name_ids (מילון שם -> מספר)."""
        remap = np.array([name_ids[n] for n in self.names], np.uint32)
        r = np.empty(len(self.occ), ROW_DT)
        r["key"], r["hint"], r["anchor"], r["window"] = self.occ["key"], self.occ["hint"], self.occ["anchor"], self.occ["window"]
        r["name"] = remap[self.occ["book"]] if len(self.occ) else 0
        return r

    def vectors(self, slots):
        """(codes (n, vec_bytes) u8, scales (n,) f4) של הסלוטים, בסדר הנתון."""
        slots = np.asarray(slots, np.int64)
        n = self.h["slots"]
        vmap = np.memmap(self.path, np.uint8, "r", self.vec_off, (n, self.vec_bytes))
        smap = np.memmap(self.path, "<f4", "r", self.scale_off, (n,))
        out = (np.asarray(vmap[slots]), np.asarray(smap[slots]))
        del vmap, smap
        return out


def member(a, b):
    """לכל שורה ב-a: האם היא ב-b (שני המערכים ייחודיים בתוך עצמם)."""
    res = np.zeros(len(a), bool)
    if not len(a) or not len(b):
        return res
    both = np.concatenate([a, b])
    order = np.argsort(both, kind="stable")
    s = both[order]
    eq = s[1:] == s[:-1]
    p = np.flatnonzero(eq & (order[:-1] < len(a)) & (order[1:] >= len(a)))
    res[order[p]] = True
    return res


def _name_table(*name_lists):
    names = sorted({n for ns in name_lists for n in ns}, key=lambda s: s.encode("utf-8"))
    return names, {n: i for i, n in enumerate(names)}


# ------------------------------------------------------------------ בניית דלתא

def build_delta(opv, old_path, new_path, out_path, level=10):
    """דלתא S_old -> S_new אל out_path (.oxd). -> מילון תיאור (עבור המניפסט)."""
    import zstandard
    old, new = SegInfo(opv, old_path), SegInfo(opv, new_path)
    if old.params != new.params or old.h["identity_digest"] != new.h["identity_digest"] or old.vec_bytes != new.vec_bytes:
        raise DeltaError("המקטעים נבנו במודל/קודק שונה: אין דלתא")
    names, ids = _name_table(old.names, new.names)
    ro, rn = old.rows(ids), new.rows(ids)
    removed = ro[~member(ro, rn)]
    added = rn[~member(rn, ro)]
    new_keys = new.sorted_keys[~np.isin(new.sorted_keys, old.sorted_keys)]
    codes, scales = new.vectors(new.slots_of(new_keys))
    hn = new.h
    meta = {"format": DELTA_FORMAT, "version": 1,
            "from": {"id": old.h["id"].hex(), "sha256": sha256_file(old_path), "slots": int(old.h["slots"])},
            "to": {"id": hn["id"].hex(), "sha256": sha256_file(new_path), "size": Path(new_path).stat().st_size,
                   "slots": int(hn["slots"]), "kindFlag": int(hn["flags"] & 7), "fromVersion": int(hn["from"]),
                   "toVersion": int(hn["to"]), "tag": hn["tag"]},
            "identityDigest": hn["identity_digest"].hex(), "paramsSha256": hn["params_sha256"].hex(),
            "dim": int(hn["dim"]), "vectorBytes": int(new.vec_bytes),
            "counts": {"removed": len(removed), "added": len(added), "newKeys": len(new_keys), "names": len(names)}}
    mb, nb = json.dumps(meta, ensure_ascii=False, sort_keys=True).encode("utf-8"), "\n".join(names).encode("utf-8")
    parts = [MAGIC, struct.pack("<II", len(mb), len(nb)), mb, nb, removed.tobytes(), added.tobytes(),
             new_keys.astype("<u8").tobytes(), np.ascontiguousarray(codes).tobytes(), scales.astype("<f4").tobytes()]
    with open(out_path, "wb") as f:
        with zstandard.ZstdCompressor(level=level, threads=-1).stream_writer(f, closefd=False) as w:
            for p in parts:
                w.write(p)
    return meta


def read_delta(path):
    """-> (meta, names, removed, added, keys, codes, scales)"""
    import zstandard
    with open(path, "rb") as f:
        raw = zstandard.ZstdDecompressor().stream_reader(f).read()
    if raw[:len(MAGIC)] != MAGIC:
        raise DeltaError(f"{Path(path).name}: אינו קובץ דלתא")
    o = len(MAGIC)
    lm, ln = struct.unpack_from("<II", raw, o)
    o += 8
    meta = json.loads(raw[o:o + lm].decode("utf-8"))
    o += lm
    names = raw[o:o + ln].decode("utf-8").split("\n") if ln else []
    o += ln
    c = meta["counts"]
    take = lambda dt, n: np.frombuffer(raw, dt, n, o)
    removed = take(ROW_DT, c["removed"])
    o += removed.nbytes
    added = take(ROW_DT, c["added"])
    o += added.nbytes
    keys = take("<u8", c["newKeys"]).astype(np.uint64)
    o += c["newKeys"] * 8
    vb = meta["vectorBytes"]
    codes = take(np.uint8, c["newKeys"] * vb).reshape(c["newKeys"], vb)
    o += codes.nbytes
    scales = take("<f4", c["newKeys"])
    o += scales.nbytes
    if o != len(raw):
        raise DeltaError(f"{Path(path).name}: אורך לא צפוי")
    return meta, names, removed, added, keys, codes, scales


# ------------------------------------------------------------------ החלת דלתא

def apply_delta(opv, old_path, delta_path, out_path):
    """S_old + דלתא -> out_path (מקטע זהה בבייטים ל-S_new). מאמת מול ה-sha256 שבדלתא. -> תוצאת write_segment."""
    meta, dnames, removed, added, dkeys, dcodes, dscales = read_delta(delta_path)
    old = SegInfo(opv, old_path)
    if old.h["id"].hex() != meta["from"]["id"] or sha256_file(old_path) != meta["from"]["sha256"]:
        raise DeltaError("הדלתא אינה מתאימה למקטע המותקן (id/sha256)")
    if old.params.__len__() == 0 or hashlib.sha256(old.params).hexdigest() != meta["paramsSha256"] \
            or old.h["identity_digest"].hex() != meta["identityDigest"] or old.vec_bytes != meta["vectorBytes"]:
        raise DeltaError("הדלתא נבנתה למודל/קודק אחר")
    names, ids = _name_table(old.names, dnames)
    ro = old.rows(ids)
    dmap = np.array([ids[n] for n in dnames], np.uint32)
    rem = removed.copy()
    add = added.copy()
    rem["name"], add["name"] = (dmap[rem["name"]] if len(rem) else rem["name"]), (dmap[add["name"]] if len(add) else add["name"])
    keep = ro[~member(ro, rem)]
    if len(ro) - len(keep) != len(rem):
        raise DeltaError("הדלתא מסירה רשומות שאינן במקטע המותקן")
    rows = np.concatenate([keep, add])
    used = np.unique(rows["name"])
    order = [names[i] for i in used]               # סדר בייטים של השמות, כמו אצל הסקריפט (books ממוינים לפי שם)
    renum = np.zeros(len(names), np.int64)
    renum[used] = np.arange(len(used))
    cols = {"K": rows["key"].astype(np.uint64), "B": renum[rows["name"]], "H": rows["hint"].astype(np.int64),
            "W": rows["window"].astype(np.uint8), "A": rows["anchor"].astype(np.uint64)}
    plan = opv.plan_segment(order, cols)
    pkeys = plan["keys"].astype(np.uint64)
    from_old = old.slots_of(pkeys)
    dorder = np.argsort(dkeys, kind="stable")
    di = np.searchsorted(dkeys[dorder], pkeys) if len(dkeys) else np.zeros(len(pkeys), np.int64)
    di = np.minimum(di, max(len(dkeys) - 1, 0))
    in_delta = (dkeys[dorder][di] == pkeys) if len(dkeys) else np.zeros(len(pkeys), bool)
    if ((from_old < 0) & ~in_delta).any():
        raise DeltaError("לווקטור חסר מקור (לא במקטע המותקן ולא בדלתא)")

    def fill(vec, scales):
        step = 1 << 17
        for i in range(0, len(pkeys), step):
            sl = slice(i, i + step)
            fo = from_old[sl]
            c = np.empty((len(fo), meta["vectorBytes"]), np.uint8)
            s = np.empty(len(fo), "<f4")
            m = fo >= 0
            if m.any():
                c[m], s[m] = old.vectors(fo[m])
            if (~m).any():
                j = dorder[di[sl][~m]]
                c[~m], s[~m] = dcodes[j], dscales[j]
            vec[sl], scales[sl] = c, s

    t = meta["to"]
    w = opv.write_segment(out_path, kind_flag=t["kindFlag"], identity_digest=bytes.fromhex(meta["identityDigest"]),
                          params=old.params, from_version=t["fromVersion"], to_version=t["toVersion"], tag=t["tag"],
                          dim=meta["dim"], books=plan["books"], names=plan["names"], keys=plan["keys"],
                          hints=plan["hints"], extras=plan["extras"], extras_by_slot=plan["extras_by_slot"],
                          windows=plan["windows"], occurrences=plan["occurrences"],
                          book_hashes=np.zeros(len(plan["books"]), "<u8"), fill_vectors=fill,
                          vector_bytes=meta["vectorBytes"])
    if w["sha256"] != t["sha256"] or w["size"] != t["size"] or w["id"] != t["id"]:
        Path(out_path).unlink(missing_ok=True)
        raise DeltaError("המקטע שהתקבל מהחלת הדלתא אינו זהה למצופה (sha256)")
    return w


# ------------------------------------------------------------------ תכנון התקנה (טהור, ללא קבצים)

def chain_to(deltas, installed_sha, target_sha):
    """רשימת הדלתאות (לפי הסדר) מ-installed_sha ועד target_sha, או None. deltas: רשומות 'deltas' במניפסט."""
    cur, out, seen = installed_sha, [], set()
    while cur != target_sha:
        nxt = next((d for d in deltas if d["from"]["sha256"] == cur), None)
        if nxt is None or cur in seen:
            return None
        seen.add(cur)
        out.append(nxt)
        cur = nxt["to"]["sha256"]
    return out


def plan_install(man, installed_sha, available, allow_delta=True, max_ratio=0.5, max_chain=3):
    """מה להתקין: {"mode": "current"|"delta"|"base"|"missing", "chain": [...], "need": [שמות קבצים], "reason": str}.
    installed_sha: sha256 של המקטע האישי המותקן (None: אין, או שאינו יחיד), available: שמות הקבצים שבתיקיית ההורדה.
    דלתא רק כשהשרשרת מגיעה בדיוק למקטע המותקן, אינה ארוכה מ-max_chain, וגודלה הכולל קטן מ-max_ratio מהבסיס;
    אחרת, או כשחסר קובץ של דלתא והבסיס קיים, בסיס מלא."""
    seg = man["segment"]
    base_files = [e["file"] for e in man["files"]]
    base_size = sum(e["size"] for e in man["files"])
    if installed_sha is not None and installed_sha == seg["sha256"]:
        return {"mode": "current", "chain": [], "need": [], "reason": "המקטע המותקן זהה לגרסה במניפסט"}
    reason = "אין מקטע אישי מותקן יחיד מוכר"
    if allow_delta and installed_sha is not None:
        chain = chain_to(man.get("deltas") or [], installed_sha, seg["sha256"])
        if chain is None:
            reason = "המקטע המותקן אינו חלק משרשרת הדלתאות במניפסט"
        elif len(chain) > max_chain:
            reason = f"שרשרת ארוכה מדי ({len(chain)} דלתאות)"
        elif sum(e["size"] for d in chain for e in d["files"]) >= max_ratio * base_size:
            reason = "הדלתאות גדולות מחצי מהבסיס"
        else:
            need = [e["file"] for d in chain for e in d["files"]]
            if all(f in available for f in need):
                return {"mode": "delta", "chain": chain, "need": need, "reason": f"{len(chain)} דלתאות"}
            reason = "חסרים קבצי דלתא"
            if not all(f in available for f in base_files):
                return {"mode": "missing", "chain": chain, "need": need, "reason": reason}
    elif not allow_delta:
        reason = "דלתאות כבויות (--no-delta)"
    if all(f in available for f in base_files):
        return {"mode": "base", "chain": [], "need": base_files, "reason": reason}
    return {"mode": "missing", "chain": [], "need": base_files, "reason": reason}
