#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""מתאם CI לבניית וקטורי החיפוש הסמנטי של אוצריא מספרי המאגר (build-vectors.yml).

כל ההיגיון של אוצריא (חיתוך לפסקאות, מפתחות, קידוד rot192-i8i4, כתיבת מקטע .oxv ופרסום דור)
נמצא בסקריפט של קרית חנה, otzaria_personal_vectors.py (אותה תיקייה), והמתאם הזה רק מזין אותו:
  - אין עריכה בסקריפט: הוא נטען כמודול (import) ופונקציה אחת בו מוחלפת בזמן ריצה בתהליך הזה
    (user_books: רשימת הספרים), ופעם נוספת plan_segment (עצירה אחרי ההטמעה, במצב embed).
    החלפת הסקריפט בגרסה מתוקנת = החלפת קובץ אחד. הממשקים שבהם המתאם נוגע (run, Cache, LiveSet,
    publish_generation, write_segment, self_test, CANCEL, Cancelled, recipe_identity) נבדקים בבדיקות.
  - מקור הקלט: המסד האישי שפורסם בפועל (release db-v<N>), כספרייה מצורפת (attached): הפסקאות נקראות משורות
    המסד, ושמות הספרים במקטע הם db:<slug>:<book.id> (slug = otzarya-unofficial-books). לא uid:<id> (ספרים אישיים של
    המשתמש) ולא id:<id> (הרשמי). אזהרה: ה-hint של כל פסקה במקטע הוא מיקום השורה בספר, כפי שהוא במסד; כשהשורות
    במסד זזות (ספר שנערך) ה-hint מתיישן, ולכן המקטע קשור לגרסת המסד (db.version במניפסט) ו-install מסרב אחרת. ה-anchor הוא
    מפתח (hash) הפסקה הראשונה של השורה, ולכן יציב לתוכן.
  - אין ספרייה של אוצריא ב-CI: מרכיבים "פריסה מינימלית" (vectors/CURRENT + gen-000001 + המקטע הבסיסי
    הרשמי) מהוצאת הווקטורים הרשמית, והסקריפט רץ עליה כאילו היא ספריית אוצריא.

תת-פקודות (מידע ב---help של כל אחת):
  pick-official   בחירת ההוצאה הרשמית האחרונה של הווקטורים (בסיס, משפחת windows)
  books           רשימת ספרי המאגר ושיוך לרסיסים (shards)
  prepare-layout  פריסה מינימלית מהוצאה רשמית (אימות sha256, פענוח zstd)
  embed           הטמעה בתקציב זמן (עצירה מסודרת, שמירת cache) או הרכבת המקטע (assemble)
  validate        אימות מקטע שנוצר (כותרת, CRC לבלוקים, כתיבה מחדש זהה בבייטים)
  pack            דחיסה, פיצול למקטעי <2GiB ומניפסט
  install         התקנה אצל משתמש בסט הווקטורים של אוצריא שלו (לא רשמי; ראו תיעוד בקובץ ה-workflow)
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import tarfile
import threading
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

FAMILY_TAG_RE = re.compile(r"^vectors-qa491-windows-v(\d+)-(\d+)$")
MANIFEST_RE = re.compile(r"^otzaria-vectors-([0-9a-f]{8})-v(\d+)-base\.manifest\.json$")
PART_LIMIT = 1900 * 1024 * 1024   # מגבלת נכס ב-GitHub: 2GiB
DEFAULT_SLUG = "otzarya-unofficial-books"   # sanitizeSlug(library_id) של מסד המאגר


class CiError(Exception):
    pass


def load_opv():
    """הסקריפט של קרית חנה, כמודול רגיל (שם אמיתי: חיוני ל-multiprocessing ב-spawn, כמו ב-Windows)."""
    import otzaria_personal_vectors as opv
    return opv


def sha256_file(path, chunk=8 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def crc32(b):
    return zlib.crc32(b) & 0xFFFFFFFF


def safe_family(family):
    """שם תיקיית ה-cache של הסקריפט (opv.Cache)."""
    return re.sub(r"[^\w.\-]+", "_", family)


# ------------------------------------------------------------------ ההוצאה הרשמית

def pick_official(releases):
    """ההוצאה הרשמית האחרונה (לפי מספר ספרייה, ואז published_at) של משפחת windows (הסקריפט כותב רק
    חיתוך עם חלונות). ההוצאות האלה מסומנות prerelease אצל הרשמי, ולכן אין מסננים לפי זה.
    -> (tag, library_version, manifest_asset, base_asset, size)"""
    best = None
    for r in releases:
        m = FAMILY_TAG_RE.match(r.get("tag_name") or "")
        if not m or r.get("draft"):
            continue
        assets = {a["name"]: a for a in r.get("assets", [])}
        mf = [n for n in assets if MANIFEST_RE.match(n)]
        if len(mf) != 1:
            continue
        base = mf[0][:-len(".manifest.json")] + ".oxv.zst"
        if base not in assets:
            continue
        key = (int(m.group(1)), r.get("published_at") or "")
        if best is None or key > best[0]:
            best = (key, r["tag_name"], int(m.group(1)), mf[0], base, assets[base].get("size", 0))
    if best is None:
        raise CiError("לא נמצאה הוצאת וקטורים רשמית (vectors-qa491-windows-v<N>-<ts>) עם מניפסט ובסיס")
    return best[1], best[2], best[3], best[4], best[5]


# ------------------------------------------------------------------ ספרים ורסיסים

def shard_of(key, shards):
    """רסיס של ספר לפי מזהה (מחרוזת). יציב: מזהי הספרים במסד יציבים לפי שם הספר בין בניות."""
    return int.from_bytes(hashlib.sha1(str(key).encode("utf-8")).digest()[:4], "big") % shards


def sanitize_slug(library_id):
    """slug של ספרייה מצורפת באוצריא (attached_library_probe.dart: sanitizeSlug, כפי שנמסר): תווים לא חוקיים
    ל-'-'. עבור otzarya-unofficial-books זה בדיוק אותו ערך."""
    s = re.sub(r"[^\w.\-]+", "-", str(library_id).strip().lower()).strip("-")
    return s[:64] or DEFAULT_SLUG


def db_info(db_path):
    """המסד האישי שפורסם (release db-v<N>): {"library_id", "db_version", "slug", "books": [(id, title)]}.
    נקרא דרך BookDB של הסקריפט (תומך בשורות דחוסות עם מילון)."""
    opv = load_opv()
    db = opv.BookDB(db_path)
    con = db.connect()
    try:
        meta = db.meta(con)
        cat = db.catalog(con)
        try:
            lines = {int(i): int(n) for i, n in con.execute("select id, totalLines from book")}
        except Exception:
            lines = {}
    finally:
        con.close()
    lib = meta.get("library_id") or DEFAULT_SLUG
    ver = meta.get("db_version")
    return {"library_id": lib, "db_version": int(ver) if ver not in (None, "") else None,
            "slug": sanitize_slug(lib), "books": sorted((int(i), t) for i, (t, _c) in cat.items()),
            "book_lines": lines}


# סף סבירות להתקנה כשגרסת המסד המותקן שונה מזו שהוקטורים נבנו לה: עד 20% מהספרים (התאמה של 80%+) שבוקטורים (ולא פחות מ-5 ספרים)
# רשאים להשתנות (מספר שורות שונה = ה-hint של הספר זז). שאר הספרים תקינים כמות שהם.
STALE_RATIO = 0.20
STALE_MIN = 5


def layout_compat(want_lines, have_lines):
    """השוואת פריסת השורות של הספרים שבוקטורים (want_lines: {id: n}) מול המסד המותקן (have_lines).
    מחזיר (ok, stale_ids). ספר שנעלם מהמסד אינו נחשב (אין לו מה להציג); ספר ששינה מספר שורות = מיושן."""
    stale = [i for i, n in want_lines.items() if i in have_lines and have_lines[i] != n]
    limit = max(STALE_MIN, int(len(want_lines) * STALE_RATIO))
    return len(stale) <= limit, stale


def book_names(slug, ids):
    """מפתחות הספרים כפי שאוצריא נותנת לספר ממסד מצורף: db:<slug>:<id>."""
    return [f"db:{slug}:{int(i)}" for i in ids]


# ------------------------------------------------------------------ פריסה מינימלית

def build_set_doc(pkg, generation=1, created_at=None):
    """set.json של דור שמכיל רק את המקטע הבסיסי שבמניפסט החבילה (כפי שאוצריא רושמת בסיס חדש).
    נבדק מול set.json אמיתי של אוצריא בבדיקות היחידה."""
    if pkg.get("kind") != "base" or len(pkg.get("files", [])) != 1:
        raise CiError("המניפסט אינו של מקטע בסיס בקובץ אחד")
    sid = pkg["segmentId"]
    delb = _del_bytes(pkg["counts"]["slots"])
    seg = {"id": sid, "file": f"segments/{sid}.oxv", "kind": "base", "from": pkg["fromLibraryVersion"],
           "to": pkg["toLibraryVersion"], "sha256": pkg["segment"]["sha256"], "size": pkg["segment"]["size"],
           "slots": pkg["counts"]["slots"], "package_digest": pkg["packageDigest"],
           "provenance": pkg.get("provenance", {}),
           "del": {"file": f"{sid}.del", "crc32": crc32(delb[:-4]), "count": 0}}
    return {"format": "otzaria-vector-set", "format_version": 1, "generation": generation,
            "identity": pkg["identity"], "identity_digest": pkg["identityDigest"],
            "codec_params_sha256": pkg["codecParamsSha256"], "library_version": pkg["toLibraryVersion"],
            "library_release_tag": pkg["libraryReleaseTag"], "segments": [seg],
            "stats": {"slots": seg["slots"], "slots_dead": 0, "foreign": pkg["counts"].get("foreign", 0),
                      "foreign_unresolved": 0, "bytes": seg["size"]},
            "created_at": created_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def _del_bytes(slots):
    b = b"OXVDEL1\n" + struct.pack("<QQ", slots, 0) + b"\0" * (8 * (-(-slots // 64)))
    return b + struct.pack("<I", crc32(b))


def write_layout(pkg, segment_path, layout, move=True):
    """vectors/ = CURRENT + gen-000001 + segments/<id>.oxv ... בתוך layout."""
    v = Path(layout) / "vectors"
    (v / "segments").mkdir(parents=True, exist_ok=True)
    sid = pkg["segmentId"]
    dst = v / "segments" / f"{sid}.oxv"
    if Path(segment_path).resolve() != dst.resolve():
        (shutil.move if move else shutil.copyfile)(str(segment_path), str(dst))
    doc = build_set_doc(pkg)
    g = v / "gen-000001"
    g.mkdir(exist_ok=True)
    (g / f"{sid}.del").write_bytes(_del_bytes(pkg["counts"]["slots"]))
    raw = json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
    (g / "set.json").write_bytes(raw)
    (v / "CURRENT").write_text(json.dumps({"generation": 1, "set": "gen-000001/set.json",
                                           "set_sha256": hashlib.sha256(raw).hexdigest()},
                                          separators=(",", ":")), encoding="utf-8")
    return v


def decompress_zst(src_files, dst, expect_sha=None, expect_size=None):
    """src_files: קבצי zstd (אחד, או מקטעים שמחוברים לפי הסדר). מחזיר sha256 של התוצאה."""
    import zstandard
    h, n = hashlib.sha256(), 0
    with open(dst, "wb") as out:
        cat = _Concat(src_files)
        with zstandard.ZstdDecompressor(max_window_size=1 << 31).stream_reader(cat) as r:
            for chunk in iter(lambda: r.read(8 << 20), b""):
                out.write(chunk)
                h.update(chunk)
                n += len(chunk)
    cat.close()
    if expect_sha and h.hexdigest() != expect_sha:
        raise CiError(f"sha256 אחרי פענוח אינו תואם: {h.hexdigest()} במקום {expect_sha}")
    if expect_size is not None and n != expect_size:
        raise CiError(f"גודל אחרי פענוח {n} במקום {expect_size}")
    return h.hexdigest()


class _Concat:
    """קובץ וירטואלי רציף ממקטעים (לקריאה בלבד)."""

    def __init__(self, files):
        self.files = [Path(f) for f in files]
        self.i, self.cur = 0, None

    def read(self, n=-1):
        n = 8 << 20 if n is None or n < 0 else n
        out = []
        while n > 0:
            if self.cur is None:
                if self.i >= len(self.files):
                    break
                self.cur = open(self.files[self.i], "rb")
                self.i += 1
            b = self.cur.read(n)
            if not b:
                self.cur.close()
                self.cur = None
                continue
            out.append(b)
            n -= len(b)
        return b"".join(out)

    def readable(self):
        return True

    def close(self):
        if self.cur:
            self.cur.close()


def cmd_prepare_layout(a):
    pkg = json.loads(Path(a.manifest).read_text(encoding="utf-8-sig"))
    f = pkg["files"][0]
    zst = Path(a.zst)
    if zst.stat().st_size != f["size"] or sha256_file(zst) != f["sha256"]:
        raise CiError("הארכיון הרשמי אינו תואם למניפסט (גודל/sha256)")
    tmp = Path(a.layout) / "base.oxv.tmp"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    decompress_zst([zst], tmp, f["uncompressedSha256"], f["uncompressedSize"])
    write_layout(pkg, tmp, a.layout)
    (Path(a.layout) / "data").mkdir(exist_ok=True)
    if a.delete_zst:
        zst.unlink()
    print(f"layout ok: {a.layout} (library v{pkg['toLibraryVersion']}, {pkg['counts']['slots']:,} slots, "
          f"identity {pkg['identityDigest'][:8]})")


# ------------------------------------------------------------------ cache (קובצי part של הסקריפט)

def family_dir(cache_root, family):
    return Path(cache_root) / safe_family(family)


def restore_cache(tars_dir, fdir):
    """פורס tar-ים של cache לתיקיית המשפחה: רק part-*.npz ו-codec_params.bin, בלי נתיבים."""
    fdir = Path(fdir)
    fdir.mkdir(parents=True, exist_ok=True)
    n = 0
    for t in sorted(Path(tars_dir).glob("cache-*.tar")):
        with tarfile.open(t) as tf:
            for m in tf.getmembers():
                name = os.path.basename(m.name)
                if not m.isfile() or not re.fullmatch(r"part-[\w.\-]+\.npz|codec_params\.bin", name):
                    raise CiError(f"{t.name}: חבר לא צפוי {m.name!r}")
                if name == "codec_params.bin" and (fdir / name).exists():
                    continue
                with tf.extractfile(m) as src, open(fdir / name, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                n += name != "codec_params.bin"
    return n


def pack_new_parts(fdir, before, out_tar, tag):
    """tar של ה-parts שנוספו מאז `before` (קבוצת שמות), בשמות ייחודיים לפי tag. -> מספר parts."""
    fdir = Path(fdir)
    new = sorted(p for p in fdir.glob("part-*.npz") if p.name not in before)
    if not new:
        return 0
    with tarfile.open(out_tar, "w") as tf:
        if (fdir / "codec_params.bin").exists():
            tf.add(fdir / "codec_params.bin", arcname="codec_params.bin")
        for i, p in enumerate(new, 1):
            tf.add(p, arcname=f"part-{tag}-{i:06d}.npz")
    return len(new)


def cache_vectors(fdir):
    import numpy as np
    n = 0
    for p in Path(fdir).glob("part-*.npz"):
        with np.load(p) as z:
            n += len(z["keys"])
    return n


# ------------------------------------------------------------------ הטמעה והרכבה

class EmbeddedAll(Exception):
    """נזרק (מתוך plan_segment המוחלף) כשההטמעה של הרסיס הסתיימה וה-cache נשמר."""


def make_args(a, layout, cache_dir):
    L = Path(layout)
    return argparse.Namespace(
        check=bool(a.check), remove=False, gpu=False, library_dir=str(L), otzaria_dir=str(L / "data"),
        vectors_dir=str(L / "vectors"), model_dir=str(a.model_dir), cache_dir=str(cache_dir),
        threads=a.threads, workers=a.workers, group=a.group, force=True, self_test=None)


def cmd_embed(a):
    opv = load_opv()
    layout = Path(a.layout)
    live = opv.LiveSet(layout / "vectors")
    family = live.doc["identity"]["model"]["family_id"]
    info = db_info(a.db)
    total = len(info["books"])
    slug = a.slug or info["slug"]
    if not opv.SLUG_RE.match(slug):
        raise CiError(f"slug לא חוקי: {slug}")
    # מקור הקלט: המסד האישי שפורסם, כספרייה מצורפת: הפסקאות מגיעות משורות המסד (מה שאוצריא מאנדקסת), והשמות הם
    # db:<slug>:<id>. אין ספרים אישיים (user_books): כך אין התנגשות עם uid:<id> של המשתמש.
    opv.user_books = lambda _databases_dir: ([], 0)
    opv.otzaria_settings = lambda _dir: {"custom_folders": [], "attached_folders": [], "error": None,
                                         "attached": [{"path": str(a.db), "slug": slug, "displayName": info["library_id"]}]}
    orig_attached = opv.attached_books

    def attached_books(settings):
        out, report = orig_attached(settings)
        if a.shard is not None:
            out = [b for b in out if shard_of(b["book_id"], a.shards) == a.shard]
        return out, report
    opv.attached_books = attached_books
    feed = [i for i, _t in info["books"] if a.shard is None or shard_of(i, a.shards) == a.shard]

    cache_root = Path(a.cache_dir)
    fdir = family_dir(cache_root, family)
    if a.restore_from:
        n = restore_cache(a.restore_from, fdir)
        print(f"cache restored: {n} parts", flush=True)
    before = {p.name for p in fdir.glob("part-*.npz")} if fdir.exists() else set()
    n_before = cache_vectors(fdir) if before else 0

    args = make_args(a, layout, cache_root)
    mode = "assemble" if a.assemble else "embed"
    if mode == "embed":
        def stop(order, rows):
            raise EmbeddedAll()
        opv.plan_segment = stop
    timer = None
    if a.budget_minutes and mode == "embed":
        timer = threading.Timer(a.budget_minutes * 60, opv.CANCEL.set)
        timer.daemon = True
        timer.start()
    t0 = time.time()
    status = {"mode": mode, "shard": a.shard, "round": a.round, "books": len(feed), "books_total": total,
              "db_version": info["db_version"], "slug": slug, "done": False, "error": None}
    code = 0
    try:
        report = opv.run(args)
        status["done"] = True
        status["report"] = report
    except EmbeddedAll:
        status["done"] = True
    except opv.Cancelled:
        status["done"] = False
    except RuntimeError as e:
        status["error"] = str(e)
        code = 2
    finally:
        if timer:
            timer.cancel()
        status["seconds"] = int(time.time() - t0)
        status["cache_vectors_before"] = n_before
        if fdir.exists():
            status["cache_vectors_after"] = cache_vectors(fdir)
            if a.new_parts_tar:
                tag = a.tag or time.strftime("%Y%m%d%H%M%S")
                status["new_parts"] = pack_new_parts(fdir, before, a.new_parts_tar, tag)
    if mode == "assemble" and code == 0:
        published = [s for s in opv.LiveSet(layout / "vectors").doc["segments"] if opv.is_ours(s)]
        status["segment"] = published[0] if published else None
        if not published:
            status["error"] = "לא נוצר מקטע (אין פסקאות להפצה?)"
            code = 3
    if a.status:
        Path(a.status).write_text(json.dumps(status, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in status.items() if k not in ("report", "segment")}, ensure_ascii=False),
          flush=True)
    return code


# ------------------------------------------------------------------ אימות מקטע

def verify_block_crcs(path):
    """בודק מחדש את ה-CRC של כל הבלוקים (1MiB) של הסקציות הגדולות, לפי סדר הכתיבה, מול BLOCK_CRCS."""
    opv = load_opv()
    h = opv.read_header(path)
    want = opv.section(path, h, opv.BLOCK_CRCS)
    got = []
    order = (opv.SLOT_HINTS, opv.SLOT_KEYS, opv.VECTORS, opv.SCALES, opv.SLOT_WINDOWS, opv.OCCURRENCES,
             opv.BOOK_HASHES)
    secs = {s["kind"]: s for s in h["sections"]}
    with open(path, "rb") as f:
        for k in order:
            s = secs.get(k)
            if not s:
                continue
            f.seek(s["off"])
            left = s["len"]
            while left:
                b = f.read(min(opv.BLOCK, left))
                got.append(crc32(b))
                left -= len(b)
    return struct.pack(f"<{len(got)}I", *got) == want, len(got)


def segment_book_names(path):
    """שמות הספרים שבמקטע (סקציות BOOKS ו-BOOK_NAMES)."""
    import numpy as np
    opv = load_opv()
    h = opv.read_header(path)
    names = opv.section(path, h, opv.BOOK_NAMES)
    books = np.frombuffer(opv.section(path, h, opv.BOOKS), opv.BOOK_DT)
    return [names[int(b["name_off"]):int(b["name_off"]) + int(b["name_len"])].decode("utf-8") for b in books]


def check_names(names, slug):
    """כל שם הוא בדיוק db:<slug>:<id> (לא uid:, id: או ext:, שעלולים להתנגש עם ספרים אחרים באוצריא)."""
    pat = re.compile(r"^db:" + re.escape(slug) + r":\d+$")
    bad = [n for n in names if not pat.match(n)]
    return not bad, len(bad)


def cmd_validate(a):
    opv = load_opv()
    p = Path(a.segment)
    h = opv.read_header(p)                                  # CRC של הכותרת
    ok_crc, nblocks = verify_block_crcs(p)
    ok_self = opv.self_test(p) if a.self_test else None     # כתיבה מחדש מכל הסקציות: זהה בבייטים
    res = {"slots": h["slots"], "books": h["books"], "block_crcs_ok": ok_crc, "blocks": nblocks,
           "rewrite_identical": ok_self, "tag": h["tag"], "id": h["id"].hex()}
    names = segment_book_names(p)
    names_ok, nbad = check_names(names, a.slug)
    res.update({"names_ok": names_ok, "names_bad": nbad, "names": len(names)})
    if a.layout:
        live = opv.LiveSet(Path(a.layout) / "vectors")      # CURRENT -> set.json (sha256) -> כותרות
        ours = [s for s in live.doc["segments"] if opv.is_ours(s)]
        res["layout_generation"] = live.doc["generation"]
        res["ours_in_set"] = len(ours)
        res["set_slots"] = live.doc["stats"]["slots"]
    print(json.dumps(res))
    bad = not ok_crc or ok_self is False or not names_ok or (a.layout and res["ours_in_set"] != 1)
    return 1 if bad else 0


# ------------------------------------------------------------------ אריזה

def compress_split(src, out_dir, base_name, part_size=PART_LIMIT, level=10):
    """src -> base_name.zst, ואם הוא גדול מ-part_size: base_name.zst.part-000 ... (אותו תוכן, חתוך)."""
    import zstandard
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    whole = out_dir / (base_name + ".zst")
    with open(src, "rb") as fi, open(whole, "wb") as fo:
        zstandard.ZstdCompressor(level=level, threads=-1).copy_stream(fi, fo, read_size=8 << 20, write_size=8 << 20)
    size = whole.stat().st_size
    if size <= part_size:
        return [whole]
    parts = []
    with open(whole, "rb") as f:
        i = 0
        while True:
            chunk_path = out_dir / f"{base_name}.zst.part-{i:03d}"
            left, wrote = part_size, 0
            with open(chunk_path, "wb") as o:
                while left:
                    b = f.read(min(left, 16 << 20))
                    if not b:
                        break
                    o.write(b)
                    left -= len(b)
                    wrote += len(b)
            if not wrote:
                chunk_path.unlink()
                break
            parts.append(chunk_path)
            i += 1
    whole.unlink()
    return parts


def cmd_pack(a):
    opv = load_opv()
    seg = Path(a.segment)
    h = opv.read_header(seg)
    sha = sha256_file(seg)
    name = f"otzarya-books-vectors-{h['identity_digest'].hex()[:8]}-{h['id'].hex()[:12]}.oxv"
    files = compress_split(seg, a.out, name, a.part_size)
    entries = [{"file": f.name, "size": f.stat().st_size, "sha256": sha256_file(f)} for f in files]
    dbi = db_info(a.db) if a.db else {}
    layout_pkg = {}
    if a.official_manifest:
        layout_pkg = json.loads(Path(a.official_manifest).read_text(encoding="utf-8-sig"))
    manifest = {
        "format": "otzarya-unofficial-books-vectors", "formatVersion": 1, "kind": "supplement",
        "identityDigest": h["identity_digest"].hex(), "codecParamsSha256": h["params_sha256"].hex(),
        "family": (layout_pkg.get("identity", {}).get("model", {}) or {}).get("family_id"),
        "requires": layout_pkg.get("requires"),
        "dedupAgainst": {"repository": "Otzaria/SeforimLibrary", "tag": a.official_tag,
                         "libraryVersion": layout_pkg.get("toLibraryVersion"),
                         "segmentId": layout_pkg.get("segmentId"),
                         "segmentSha256": (layout_pkg.get("segment") or {}).get("sha256")},
        "segment": {"id": h["id"].hex(), "sha256": sha, "size": seg.stat().st_size, "slots": h["slots"],
                    "books": h["books"], "tag": h["tag"]},
        "compression": "zstd", "parts": len(entries) if len(entries) > 1 else 0, "files": entries,
        # המקטע קשור לגרסת המסד: ה-hint של כל פסקה הוא מיקום השורה בספר במסד (ראו README בתחילת הקובץ)
        "db": {"version": dbi.get("db_version"), "libraryId": dbi.get("library_id"), "slug": dbi.get("slug"),
               "sha256": a.db_sha256 or None, "bookKeys": f"db:{dbi.get('slug')}:<book.id>",
               # פריסת השורות בעת הבנייה {book.id: totalLines}. מתקין מקבל גם מסד בגרסה חדשה יותר כל עוד הפריסה של
               # (כמעט) כל הספרים זהה; ראו layout_compat. מזהי הספרים יציבים בין גרסאות (build_personal_db --stable-from).
               "bookLines": {str(i): n for i, n in sorted(dbi.get("book_lines", {}).items())},
               "compat": "book-layout-v1"},
        "source": {"repository": a.repository, "commit": a.commit},
        "builtBy": {"workflow": "build-vectors", "runId": a.run_id,
                    "scriptSha256": sha256_file(HERE / "otzaria_personal_vectors.py"),
                    "adapterSha256": sha256_file(Path(__file__))},
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "notice": "וקטורים לספרי המאגר שאינם בסט הרשמי. נבנו במודל של אוצריא (רישיון אישי בלבד); ראו README.",
    }
    mp = Path(a.out) / "vectors-manifest.json"
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(mp), "files": [e["file"] for e in entries],
                      "bytes": sum(e["size"] for e in entries), "segment_bytes": manifest["segment"]["size"]}))


# ------------------------------------------------------------------ התקנה אצל משתמש

def installed_db_version(opv, otzaria_dir, slug):
    """גרסת המסד המצורף (schema_meta.db_version) שאוצריא רשמה ב-key-attached-libraries תחת ה-slug."""
    st = opv.otzaria_settings(otzaria_dir)
    for e in st.get("attached", []):
        if str(e.get("slug")) == slug and Path(e["path"]).exists():
            return db_info(e["path"])["db_version"]
    return None


def cmd_install(a):
    opv = load_opv()
    man = json.loads(Path(a.manifest).read_text(encoding="utf-8-sig"))
    d = Path(a.files_dir)
    parts = [d / e["file"] for e in man["files"]]
    for e, p in zip(man["files"], parts):
        if not p.exists() or p.stat().st_size != e["size"] or sha256_file(p) != e["sha256"]:
            raise CiError(f"{e['file']}: חסר או אינו תואם למניפסט")
    vdir = Path(a.vectors_dir)
    live = opv.LiveSet(vdir)
    doc = live.doc
    if doc["identity_digest"] != man["identityDigest"] or doc["codec_params_sha256"] != man["codecParamsSha256"]:
        raise CiError("הווקטורים נבנו למודל/קודק אחר מסט הווקטורים של אוצריא כאן; ההתקנה נעצרת")
    seg = man["segment"]
    dbm = man.get("db") or {}
    have = a.db_version
    if have is None and a.otzaria_dir:
        have = installed_db_version(opv, a.otzaria_dir, dbm.get("slug"))
    if not a.allow_db_mismatch and have != dbm.get("version"):
        # גרסת מסד שונה (חדשה או ישנה): מתקינים כל עוד לפחות 80% מהספרים שבוקטורים עדיין באותה פריסת שורות
        # (ראו layout_compat). אם לא ניתן לקרוא את המסד המותקן - מסד חדש יותר מותר עם אזהרה, ישן או לא ידוע נחסם.
        want = {int(k): int(v) for k, v in (dbm.get("bookLines") or {}).items()}
        path = None
        if a.otzaria_dir:
            for e in opv.otzaria_settings(a.otzaria_dir).get("attached", []):
                if str(e.get("slug")) == dbm.get("slug") and Path(e["path"]).exists():
                    path = e["path"]
        if want and path:
            ok, stale = layout_compat(want, db_info(path)["book_lines"])
            if not ok:
                raise CiError(f"המקטע נבנה למסד גרסה {dbm.get('version')}, והמותקן אצלך: {have}. בהם {len(stale)} מתוך "
                              f"{len(want)} ספרים שינו מספר שורות (מעל {int(STALE_RATIO * 100)}%). עדכן את המסד, "
                              f"או ציין --allow-db-mismatch על אחריותך")
        elif have is None or have < (dbm.get("version") or 0):
            raise CiError(f"המקטע נבנה למסד גרסה {dbm.get('version')}, והמותקן אצלך: {have}. עדכן את המסד, "
                          f"או ציין --allow-db-mismatch על אחריותך")
        else:
            stale = []
        print(f"גרסת המסד המותקן ({have}) שונה מזו שהוקטורים נבנו לה ({dbm.get('version')}); מתקינים בכל זאת. "
              f"{len(stale)} ספרים שינו מספר שורות (ה-hint שלהם עלול לזוז), ספרים חדשים עדיין לא בחיפוש.")
    if a.dry_run:
        print(f"dry-run: היה מותקן מקטע {seg['id']} ({seg['slots']:,} וקטורים) בדור {doc['generation'] + 1}")
        return 0
    if not a.force:
        opv.guard(vdir, False)
    tmp = vdir / "segments" / "personal-vectors.oxv.tmp"
    tmp.parent.mkdir(exist_ok=True)
    decompress_zst(parts, tmp, seg["sha256"], seg["size"])
    h = opv.read_header(tmp)
    if h["id"].hex() != seg["id"] or h["identity_digest"].hex() != doc["identity_digest"] or h["version"] != 3:
        tmp.unlink()
        raise CiError("כותרת המקטע אינה תואמת למניפסט או לסט")
    lock = opv.SetLock(vdir)
    try:
        live2 = opv.LiveSet(vdir)
        if live2.doc["generation"] != doc["generation"]:
            raise CiError("הסט השתנה במהלך ההתקנה; יש להריץ שוב")
        segs = live2.doc["segments"]
        pv = next((s["to"] for i, s in enumerate(segs) if opv.is_ours(s) and i < len(segs) - 1), doc["library_version"])
        final = vdir / "segments" / f"{seg['id']}.oxv"
        os.replace(tmp, final)
        package = next((p for p in doc["identity"]["model"].get("query_packages", []) if p.get("quantization") == "fp32"), None)
        w = {"id": seg["id"], "slots": h["slots"], "version": pv, "sha256": seg["sha256"], "size": seg["size"]}
        gen = opv.publish_generation(live2, w, package, "prebuilt: " + str(man.get("builtBy", {}).get("runId")))
    finally:
        lock.release()
    print(f"הותקן: דור {gen}, מקטע {seg['id']} ({seg['slots']:,} וקטורים). אוצריא תשתמש בו בהפעלה הבאה.")
    return 0


# ------------------------------------------------------------------ CLI

def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pick-official", help="releases.json (gh api repos/Otzaria/SeforimLibrary/releases) -> TSV")
    p.add_argument("releases")

    p = sub.add_parser("books", help="ספרי המסד האישי ושיוך לרסיסים")
    p.add_argument("--db", required=True)
    p.add_argument("--shards", type=int, default=1)

    p = sub.add_parser("prepare-layout")
    p.add_argument("--manifest", required=True)
    p.add_argument("--zst", required=True)
    p.add_argument("--layout", required=True)
    p.add_argument("--delete-zst", action="store_true")

    p = sub.add_parser("embed", help="הטמעת רסיס בתקציב זמן, או (--assemble) הרכבת המקטע מכל ה-cache")
    p.add_argument("--db", required=True, help="המסד האישי (otzarya-unofficial-books-<N>.db)")
    p.add_argument("--slug", default="", help="ברירת מחדל: sanitizeSlug של schema_meta.library_id")
    p.add_argument("--layout", required=True)
    p.add_argument("--model-dir", required=True)
    p.add_argument("--cache-dir", required=True)
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--shard", type=int, default=None)
    p.add_argument("--round", type=int, default=1)
    p.add_argument("--budget-minutes", type=float, default=0)
    p.add_argument("--restore-from", help="תיקייה עם cache-*.tar לפריסה לפני ההרצה")
    p.add_argument("--new-parts-tar", help="tar של ה-parts החדשים (נכתב גם אחרי עצירה)")
    p.add_argument("--tag", default="")
    p.add_argument("--status", help="קובץ JSON עם סיכום הריצה")
    p.add_argument("--assemble", action="store_true")
    p.add_argument("--check", action="store_true")
    p.add_argument("--threads", type=int, default=0)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--group", type=int, default=20000)

    p = sub.add_parser("validate")
    p.add_argument("segment")
    p.add_argument("--slug", default=DEFAULT_SLUG)
    p.add_argument("--layout")
    p.add_argument("--self-test", action="store_true")

    p = sub.add_parser("pack")
    p.add_argument("--segment", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--official-manifest")
    p.add_argument("--official-tag", default="")
    p.add_argument("--db", help="המסד שממנו נבנה (לגרסה ול-slug במניפסט)")
    p.add_argument("--db-sha256", default="")
    p.add_argument("--repository", default="")
    p.add_argument("--commit", default="")
    p.add_argument("--run-id", default="")
    p.add_argument("--part-size", type=int, default=PART_LIMIT)

    p = sub.add_parser("install")
    p.add_argument("--manifest", required=True)
    p.add_argument("--files-dir", required=True)
    p.add_argument("--vectors-dir", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--otzaria-dir", help="תיקיית ההגדרות של אוצריא (app_preferences.hive): לקריאת גרסת המסד המצורף")
    p.add_argument("--db-version", type=int, default=None, help="גרסת המסד המותקן, אם לא נקראת מההגדרות")
    p.add_argument("--allow-db-mismatch", action="store_true")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "pick-official":
            rel = json.loads(Path(a.releases).read_text(encoding="utf-8"))
            print("\t".join(str(x) for x in pick_official(rel)))
            return 0
        if a.cmd == "books":
            info = db_info(a.db)
            per = [sum(1 for i, _t in info["books"] if shard_of(i, a.shards) == k) for k in range(a.shards)]
            print(json.dumps({"db_version": info["db_version"], "slug": info["slug"], "books": len(info["books"]),
                              "per_shard": per}))
            return 0
        if a.cmd == "prepare-layout":
            cmd_prepare_layout(a)
            return 0
        if a.cmd == "embed":
            return cmd_embed(a)
        if a.cmd == "validate":
            return cmd_validate(a)
        if a.cmd == "pack":
            cmd_pack(a)
            return 0
        if a.cmd == "install":
            return cmd_install(a)
    except CiError as e:
        print(f"שגיאה: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
