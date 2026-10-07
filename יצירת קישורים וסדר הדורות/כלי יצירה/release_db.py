#!/usr/bin/env python3
"""אריזת מסד אישי לעדכון מובנה באוצריא (מקביל ל-tool/personal_db_update.dart).

פקודות: keygen | inject | pack | sign | verify | release (pack+sign+verify).
תלויות: pynacl (חתימה), zstandard או zstd בנתיב (דחיסה); דלתא דורשת zstd בנתיב.
"""
import argparse, base64, hashlib, json, os, re, shutil, subprocess, sqlite3, sys, tempfile
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

LIBRARY_ID = "otzarya-unofficial-books"
PART_SIZE = 1900 * 1024 * 1024        # מתחת למגבלת 2GB של GitHub Releases
MAX_DELTAS = 4
MAX_BASE = 2 * 1024 ** 3              # אוצריא מחילה דלתא רק על בסיס < 2GiB
MAX_NOTES = 20000
MAX_PARTS = 1000


class ReleaseError(Exception):
    pass


# ---------- כלים כלליים ----------
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_zstd(explicit=None):
    return shutil.which(explicit or "zstd")


def run_zstd(exe, args):
    r = subprocess.run([exe, "-q", "-f", *map(str, args)], capture_output=True)
    if r.returncode:
        raise ReleaseError("zstd נכשל: " + r.stderr.decode(errors="replace"))


def read_meta(db):
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute("SELECT key, value FROM schema_meta").fetchall()
    except sqlite3.Error as e:
        raise ReleaseError(f"לא ניתן לקרוא schema_meta מ-{db}: {e}")
    finally:
        con.close()
    return {k: (str(v).strip() if v is not None else None) for k, v in rows}


def read_version(meta, where):
    try:
        v = int(meta.get("db_version") or "")
    except ValueError:
        v = 0
    if v < 1:
        raise ReleaseError(f"{where}: schema_meta.db_version חייב להיות מספר שלם חיובי")
    return v


def check_url_prefix(prefix):
    u = urlparse(prefix + "x")
    if u.scheme != "https" or not u.hostname or u.username or u.fragment:
        raise ReleaseError("--url-prefix חייב להיות כתובת https")
    return prefix if prefix.endswith("/") else prefix + "/"


# ---------- מפתחות וחתימה ----------
def _signing_key(key_text):
    from nacl.signing import SigningKey
    seed = base64.b64decode(key_text.strip())
    if len(seed) != 32:
        raise ReleaseError("המפתח הפרטי חייב להיות 32 בתים (base64)")
    return SigningKey(seed)


def public_key_of(key_path):
    return base64.b64encode(bytes(_signing_key(Path(key_path).read_text()).verify_key)).decode()


def keygen(path, force=False):
    from nacl.signing import SigningKey
    p = Path(path)
    if p.exists() and not force:
        raise ReleaseError(f"{p} כבר קיים; אובדן או החלפת מפתח מחייבים כל משתמש לצרף מחדש. --force לדריסה")
    sk = SigningKey.generate()
    p.write_text(base64.b64encode(bytes(sk)).decode() + "\n")
    return base64.b64encode(bytes(sk.verify_key)).decode()


def parse_manifest(data):
    """אימות קפדני בסגנון AttachedUpdateManifest.parse (התת-קבוצה הנחוצה לפרסום)."""
    if len(data) > 1024 * 1024:
        raise ReleaseError("המניפסט גדול מ-1MB")
    try:
        m = json.loads(data.decode("utf-8"))
    except ValueError:
        raise ReleaseError("המניפסט אינו JSON תקין")
    if not isinstance(m, dict) or not isinstance(m.get("format"), int) or not 1 <= m["format"] <= 1:
        raise ReleaseError("format לא נתמך")
    if not isinstance(m.get("library_id"), str) or not m["library_id"] or len(m["library_id"]) > 256:
        raise ReleaseError("library_id חסר")
    if not isinstance(m.get("db_version"), int) or m["db_version"] < 1:
        raise ReleaseError("db_version חסר")
    if len(m.get("release_notes") or "") > MAX_NOTES:
        raise ReleaseError("release_notes ארוך מדי")

    def artifact(a, patch):
        ok = ("zstd", "none") + (("zstd-patch",) if patch else ())
        if not isinstance(a, dict) or a.get("compression") not in ok:
            raise ReleaseError("compression לא תקין")
        parts = a.get("parts")
        if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS:
            raise ReleaseError("parts לא תקין")
        for p in parts:
            u = urlparse(p.get("url", ""))
            if u.scheme != "https" or not u.hostname or len(p["url"]) > 2048:
                raise ReleaseError("כתובת חלק חייבת להיות https")
            if not isinstance(p.get("size"), int) or len(p.get("sha256", "")) != 64:
                raise ReleaseError("חלק לא תקין")
        if not isinstance(a.get("size"), int) or a["size"] < 1 or len(a.get("sha256", "")) != 64:
            raise ReleaseError("size/sha256 לא תקינים")
        if a["compression"] == "none" and sum(p["size"] for p in parts) != a["size"]:
            raise ReleaseError("חלקים לא דחוסים חייבים להסתכם ל-size")

    artifact(m.get("full"), False)
    for d in m.get("delta") or []:
        artifact(d, True)
    return m


def sign(manifest_path, key_path):
    data = Path(manifest_path).read_bytes()
    parse_manifest(data)
    sig = _signing_key(Path(key_path).read_text()).sign(data).signature
    out = Path(str(manifest_path) + ".sig")
    out.write_text(base64.b64encode(sig).decode() + "\n")
    return out


def verify_signature(data, sig_text, public_key):
    from nacl.signing import VerifyKey
    try:
        key = base64.b64decode(public_key.strip(), validate=True)
        sig = base64.b64decode(sig_text.strip(), validate=True)
        if len(key) != 32 or len(sig) != 64:
            return False
        VerifyKey(key).verify(data, sig)
        return True
    except Exception:
        return False


# ---------- הזרקה ל-schema_meta ----------
def inject(db, db_version=None, manifest_url=None, public_key=None, library_id=LIBRARY_ID):
    """מעדכן רק מפתחות ב-schema_meta; שאר המסד לא נוגע."""
    if manifest_url and not manifest_url.startswith("https://"):
        raise ReleaseError("update_manifest_url חייב להיות https")
    con = sqlite3.connect(str(db))
    try:
        cur = con.execute("SELECT value FROM schema_meta WHERE key='library_id'").fetchone()
        if cur and cur[0] != library_id:
            raise ReleaseError(f"library_id במסד הוא {cur[0]!r}, לא {library_id!r}")
        items = {"library_id": library_id, "db_version": db_version,
                 "update_manifest_url": manifest_url, "update_public_key": public_key}
        for k, v in items.items():
            if v is not None:
                con.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)", (k, str(v)))
        con.commit()
    except sqlite3.Error as e:
        raise ReleaseError(f"הזרקה נכשלה: {e}")
    finally:
        con.close()


# ---------- דחיסה ופיצול ----------
def compress_full(src, dst, zstd=None):
    exe = find_zstd(zstd)
    if exe:
        run_zstd(exe, ["-19", "-T0", "--long=27", src, "-o", dst])
        return
    import zstandard
    params = zstandard.ZstdCompressionParameters.from_level(19, window_log=27, enable_ldm=True, threads=-1)
    cctx = zstandard.ZstdCompressor(compression_params=params, write_content_size=True)
    with open(src, "rb") as i, open(dst, "wb") as o:
        cctx.copy_stream(i, o, size=os.path.getsize(src))


def split(payload, single, part_size):
    """קובץ אחד נשאר בשם [single]; כמה חלקים מקבלים .001, .002..."""
    total = os.path.getsize(payload)
    if total <= part_size:
        if Path(payload).resolve() != Path(single).resolve():
            shutil.copyfile(payload, single)
        return [Path(single)]
    paths = []
    with open(payload, "rb") as f:
        for i in range((total + part_size - 1) // part_size):
            p = Path(f"{single}.{i + 1:03d}")
            with open(p, "wb") as o:
                left = part_size
                while left > 0:
                    chunk = f.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    o.write(chunk)
                    left -= len(chunk)
            paths.append(p)
    return paths


def parts_of(paths, prefix):
    return [{"url": prefix + quote(p.name, safe=""), "size": p.stat().st_size,
             "sha256": sha256_file(p)} for p in paths]


def pack(db, out, url_prefix, notes=None, prev_dbs=(), part_size=PART_SIZE, zstd=None,
         compression="zstd", warn=print):
    db, out = Path(db), Path(out)
    if not db.exists():
        raise ReleaseError(f"אין קובץ: {db}")
    if len(prev_dbs) > MAX_DELTAS:
        raise ReleaseError(f"--prev-dbs עד {MAX_DELTAS} קבצים")
    if part_size < 1:
        raise ReleaseError("--part-size חייב להיות חיובי")
    prefix = check_url_prefix(url_prefix)
    meta = read_meta(db)
    lib_id = meta.get("library_id")
    if not lib_id:
        raise ReleaseError("חסר schema_meta.library_id")
    version = read_version(meta, str(db))
    if not (meta.get("update_manifest_url") and meta.get("update_public_key")):
        warn("אזהרה: חסרים update_manifest_url/update_public_key; המסד לא יקבל עדכונים נוספים")
    out.mkdir(parents=True, exist_ok=True)
    base = f"{re.sub(r'[^A-Za-z0-9._-]+', '-', lib_id)}-{version}.db"
    if compression == "zstd":
        single, tmp = out / f"{base}.zst", out / f"{base}.zst.tmp"
        compress_full(db, tmp, zstd)
        paths = split(tmp, single, part_size)
        tmp.unlink()
    else:
        paths = split(db, out / base, part_size)
    full = {"compression": compression, "size": db.stat().st_size, "sha256": sha256_file(db),
            "parts": parts_of(paths, prefix)}
    full_size = sum(p.stat().st_size for p in paths)

    deltas, seen = [], set()
    for old in map(Path, prev_dbs):
        d = _pack_delta(old, db, out, base, prefix, lib_id, version, part_size, zstd, full_size, warn, seen)
        if d:
            deltas.append(d)
    manifest = {"format": 1, "library_id": lib_id, "db_version": version}
    if notes:
        manifest["release_notes"] = notes
    manifest["full"] = full
    if deltas:
        manifest["delta"] = deltas
    data = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    parse_manifest(data)
    mpath = out / "manifest.json"
    mpath.write_bytes(data)
    return mpath


def _pack_delta(old, new, out, base, prefix, lib_id, version, part_size, zstd, full_size, warn, seen):
    if not old.exists():
        raise ReleaseError(f"אין קובץ: {old}")
    meta = read_meta(old)
    if meta.get("library_id") != lib_id:
        raise ReleaseError(f"{old}: library_id {meta.get('library_id')!r} שונה מ-{lib_id!r}")
    try:
        from_v = int(meta.get("db_version") or "")
    except ValueError:
        from_v = 0
    if not 1 <= from_v < version:
        raise ReleaseError(f"{old}: db_version חייב להיות מספר שלם קטן מ-{version}")
    if from_v in seen:
        raise ReleaseError(f"שתי גרסאות קודמות עם db_version {from_v}")
    seen.add(from_v)
    if old.stat().st_size >= MAX_BASE:
        warn(f"אזהרה: {old} גדול מ-2GiB; אוצריא לא תוכל להשתמש בו כבסיס, לא נוצרה דלתא ל-{from_v}")
        return None
    exe = find_zstd(zstd)
    if not exe:
        raise ReleaseError("דלתא דורשת zstd בנתיב (או --zstd)")
    patch = out / f"{base}.from-{from_v}.patch.zst"
    run_zstd(exe, [f"--patch-from={old}", "--long=31", "--ultra", "-19", new, "-o", patch])
    if patch.stat().st_size >= full_size:
        warn(f"אזהרה: הדלתא מגרסה {from_v} ({patch.stat().st_size}) אינה קטנה מהמלא ({full_size}); נזרקה")
        patch.unlink()
        return None
    paths = split(patch, patch, part_size)
    if len(paths) > 1:
        patch.unlink()
    return {"from_db_version": from_v, "from_sha256": sha256_file(old), "compression": "zstd-patch",
            "size": new.stat().st_size, "sha256": sha256_file(new), "parts": parts_of(paths, prefix)}


# ---------- אימות ----------
def _concat(parts, parts_dir, dst, label):
    with open(dst, "wb") as o:
        for i, p in enumerate(parts, 1):
            f = Path(parts_dir) / unquote(urlparse(p["url"]).path.rsplit("/", 1)[-1])
            if not f.exists():
                raise ReleaseError(f"{label}: חלק {i} חסר: {f}")
            if f.stat().st_size != p["size"] or sha256_file(f) != p["sha256"]:
                raise ReleaseError(f"{label}: חלק {i} אינו תואם: {f}")
            with open(f, "rb") as s:
                shutil.copyfileobj(s, o, 1 << 20)


def _check_result(path, art, label):
    if os.path.getsize(path) != art["size"] or sha256_file(path) != art["sha256"]:
        raise ReleaseError(f"{label}: הקובץ המפוענח אינו תואם ל-size/sha256 שבמניפסט")


def verify(manifest_path, public_key, parts_dir=None, prev_dbs=(), zstd=None, db=None):
    mp = Path(manifest_path)
    data = mp.read_bytes()
    sig = Path(str(mp) + ".sig")
    if not sig.exists():
        raise ReleaseError(f"אין קובץ חתימה: {sig}")
    if not verify_signature(data, sig.read_text(), public_key):
        raise ReleaseError("החתימה אינה תואמת למפתח הציבורי")
    m = parse_manifest(data)
    if db:
        meta = read_meta(db)
        if m["library_id"] != meta.get("library_id"):
            raise ReleaseError("library_id במניפסט שונה מזה שבמסד")
        if m["db_version"] != read_version(meta, str(db)):
            raise ReleaseError("db_version במניפסט שונה מזה שבמסד")
        if sha256_file(db) != m["full"]["sha256"]:
            raise ReleaseError("sha256 של המסד שונה מ-full.sha256")
    if not parts_dir:
        return m
    exe = find_zstd(zstd)
    with tempfile.TemporaryDirectory() as t:
        raw, res = Path(t) / "raw", Path(t) / "res.db"
        _concat(m["full"]["parts"], parts_dir, raw, "full")
        if m["full"]["compression"] == "none":
            res = raw
        elif exe:
            run_zstd(exe, ["-d", "--long=27", raw, "-o", res])
        else:
            import zstandard
            with open(raw, "rb") as i, open(res, "wb") as o:
                zstandard.ZstdDecompressor(max_window_size=1 << 31).copy_stream(i, o)
        _check_result(res, m["full"], "full")
        res = Path(t) / "patched.db"
        for d in m.get("delta") or []:
            label = f"delta מגרסה {d['from_db_version']}"
            base = next((b for b in prev_dbs if sha256_file(b) == d["from_sha256"]), None)
            if not base:
                raise ReleaseError(f"{label}: חסר ב---prev-dbs מסד עם sha256 {d['from_sha256']}")
            if not exe:
                raise ReleaseError("אימות דלתא דורש zstd בנתיב")
            _concat(d["parts"], parts_dir, raw, label)
            run_zstd(exe, ["-d", "--memory=2048MB", f"--patch-from={base}", raw, "-o", res])
            _check_result(res, d, label)
    return m


# ---------- שורת פקודה ----------
def _notes(v):
    if not v:
        return None
    p = Path(v)
    return (p.read_text(encoding="utf-8") if p.is_file() else v).strip()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("keygen"); a.add_argument("--out", required=True); a.add_argument("--force", action="store_true")
    a = sub.add_parser("inject"); a.add_argument("--db", required=True)
    a.add_argument("--db-version", type=int); a.add_argument("--manifest-url")
    a.add_argument("--key", help="קובץ מפתח פרטי; ממנו נגזר update_public_key")
    a.add_argument("--public-key"); a.add_argument("--library-id", default=LIBRARY_ID)
    for name in ("pack", "release"):
        a = sub.add_parser(name); a.add_argument("--db", required=True); a.add_argument("--out", required=True)
        a.add_argument("--url-prefix", required=True); a.add_argument("--prev-dbs", nargs="*", default=[])
        a.add_argument("--notes", help="טקסט או נתיב לקובץ"); a.add_argument("--zstd")
        a.add_argument("--part-size", type=int, default=PART_SIZE)
        a.add_argument("--compression", choices=["zstd", "none"], default="zstd")
        a.add_argument("--key", required=name == "release")
    a = sub.add_parser("sign"); a.add_argument("manifest"); a.add_argument("--key", required=True)
    a = sub.add_parser("verify"); a.add_argument("manifest"); a.add_argument("--db"); a.add_argument("--public-key")
    a.add_argument("--parts"); a.add_argument("--prev-dbs", nargs="*", default=[]); a.add_argument("--zstd")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "keygen":
            print("מפתח ציבורי:", keygen(args.out, args.force))
        elif args.cmd == "inject":
            pub = args.public_key or (public_key_of(args.key) if args.key else None)
            inject(args.db, args.db_version, args.manifest_url, pub, args.library_id)
            print("schema_meta עודכן")
        elif args.cmd in ("pack", "release"):
            mp = pack(args.db, args.out, args.url_prefix, _notes(args.notes), args.prev_dbs,
                      args.part_size, args.zstd, args.compression, warn=lambda s: print(s, file=sys.stderr))
            print("מניפסט:", mp)
            if args.cmd == "release":
                sign(mp, args.key)
                verify(mp, public_key_of(args.key), args.out, args.prev_dbs, args.zstd, args.db)
                print("נחתם ואומת")
        elif args.cmd == "sign":
            print("חתימה:", sign(args.manifest, args.key))
        elif args.cmd == "verify":
            pub = args.public_key or (read_meta(args.db).get("update_public_key") if args.db else None)
            if not pub:
                raise ReleaseError("נדרש --public-key או --db שמצהיר update_public_key")
            m = verify(args.manifest, pub, args.parts, args.prev_dbs, args.zstd, args.db)
            print(f"OK: {m['library_id']} db_version {m['db_version']}")
    except ReleaseError as e:
        print("שגיאה:", e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
