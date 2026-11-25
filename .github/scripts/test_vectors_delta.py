# -*- coding: utf-8 -*-
"""בדיקות לעדכוני הדלתא של וקטורי החיפוש (vectors_delta.py, והפקודות delta/install ב-vectors_ci.py). הכול על מקטעי
.oxv סינתטיים וקטנים (ללא מודל): הווקטור של כל מפתח נגזר מהמפתח עצמו, ולכן אפשר לבדוק שהווקטורים עוברים נכון."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import numpy as np
    import zstandard  # noqa: F401
    import otzaria_personal_vectors as opv
    import vectors_ci as C
    import vectors_delta as D
    HAVE = True
except ImportError:  # pragma: no cover
    HAVE = False

IDENT_DIGEST = hashlib.sha256(b"synthetic identity").digest()
PARAMS = b"synthetic codec params " * 8
VB = 160
SLUG = "otzarya-unofficial-books"


def nm(i):
    return f"db:{SLUG}:{i}"


def vec_of(k):
    """160 בייט שנגזרים מהמפתח (SHA-256 משורשר): לא דחיסים, כמו וקטורים אמיתיים."""
    out, h = b"", int(k).to_bytes(8, "little")
    while len(out) < VB:
        h = hashlib.sha256(h).digest()
        out += h
    return out[:VB]


def fill_for(keys):
    def fill(vec, scales):
        for i, k in enumerate(keys.astype(np.uint64).tolist()):
            vec[i] = np.frombuffer(vec_of(k), np.uint8)
            scales[i] = float(k % 9973) / 7
    return fill


def make_segment(path, rows, flag=None, tag="v36-test", frm=36, to=36):
    """rows: [(key, book_id, hint, window)] (עם כפילויות, כמו אצל הסקריפט); anchor = key. -> תוצאת write_segment"""
    names = sorted({nm(b) for _k, b, _h, _w in rows}, key=lambda s: s.encode("utf-8"))
    idx = {n: i for i, n in enumerate(names)}
    cols = {"K": np.array([r[0] for r in rows], np.uint64), "B": np.array([idx[nm(r[1])] for r in rows], np.int64),
            "H": np.array([r[2] for r in rows], np.int64), "W": np.array([r[3] for r in rows], np.uint8),
            "A": np.array([r[0] for r in rows], np.uint64)}
    p = opv.plan_segment(names, cols)
    return opv.write_segment(path, kind_flag=flag or opv.FLAG_DELTA, identity_digest=IDENT_DIGEST, params=PARAMS,
                             from_version=frm, to_version=to, tag=tag, dim=256, books=p["books"], names=p["names"],
                             keys=p["keys"], hints=p["hints"], extras=p["extras"], extras_by_slot=p["extras_by_slot"],
                             windows=p["windows"], occurrences=p["occurrences"],
                             book_hashes=np.zeros(len(p["books"]), "<u8"), fill_vectors=fill_for(p["keys"]),
                             vector_bytes=VB)


def book_rows(book, keys, start=1, dup=True):
    """שורות ספר: keys[i] בשורה start+i; הפסקה הראשונה חוזרת בשורה נוספת וב-window אחר (כפילות כמו אצל הסקריפט)."""
    out = [(k, book, start + i, 0) for i, k in enumerate(keys)]
    if dup and keys:
        out.append((keys[0], book, start + len(keys) + 5, 0))
        out.append((keys[-1], book, start + len(keys) + 6, 1))
    return out


# שלוש גרסאות של "המסד". מפתחות 1-199 לספר 1, 200-399 לספר 2, וכו'; מפתח 50 משותף גם לספר 3 (extra)
def v1():
    return (book_rows(1, list(range(100, 160))) + book_rows(2, list(range(200, 240))) +
            book_rows(3, list(range(300, 320)) + [130]))


def v2():
    # ספר 1: הוכנסו 5 שורות בראש (כל ה-hint זז) ו-3 פסקאות נמחקו; ספר 2 נמחק; ספר 4 נוסף; ספר 3 ללא שינוי
    k1 = [900, 901, 902, 903, 904] + [k for k in range(100, 160) if k not in (110, 111, 112)]
    return (book_rows(1, k1) + book_rows(3, list(range(300, 320)) + [130]) + book_rows(4, list(range(400, 430)) + [105]))


def v3():
    # ספר 2 חוזר (עם מפתחות ישנים וחדשים), ספר 3 נמחק, ספר 4 עם פסקה אחת ששונתה, ספר 5 נוסף
    k4 = [k for k in list(range(400, 430)) if k != 410] + [777]
    return (book_rows(1, [900, 901, 902, 903, 904] + [k for k in range(100, 160) if k not in (110, 111, 112)]) +
            book_rows(2, list(range(200, 230)) + [5000, 5001]) + book_rows(4, k4 + [105]) +
            book_rows(5, [130, 131, 6000]))


def occ_set(rows):
    """קבוצת ה-occurrence כפי שהסקריפט שומר: (key, book, anchor=key, window) -> hint מינימלי."""
    d = {}
    for k, b, h, w in rows:
        t = (k, nm(b), w)
        d[t] = min(h, d.get(t, h))
    return {(k, b, h, w) for (k, b, w), h in d.items()}


@unittest.skipUnless(HAVE, "דורש numpy, zstandard והסקריפט של קרית חנה")
class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.segs = [make_segment(self.d / f"s{i}.oxv", f()) for i, f in enumerate((v1, v2, v3), 1)]

    def tearDown(self):
        self.tmp.cleanup()

    def sha(self, p):
        return D.sha256_file(p)

    def test_roundtrip_rewrite_is_identical(self):
        # בסיס הנכונות של ההחלה: קריאת השורות מהמקטע ו-plan_segment מחדש מייצרים את אותו קובץ בייט-בייט
        s = D.SegInfo(opv, self.d / "s1.oxv")
        names, ids = D._name_table(s.names)
        r = s.rows(ids)
        order = names
        cols = {"K": r["key"].astype(np.uint64), "B": r["name"].astype(np.int64), "H": r["hint"].astype(np.int64),
                "W": r["window"].astype(np.uint8), "A": r["anchor"].astype(np.uint64)}
        p = opv.plan_segment(order, cols)
        w = opv.write_segment(self.d / "re.oxv", kind_flag=opv.FLAG_DELTA, identity_digest=IDENT_DIGEST, params=PARAMS,
                              from_version=36, to_version=36, tag="v36-test", dim=256, books=p["books"], names=p["names"],
                              keys=p["keys"], hints=p["hints"], extras=p["extras"], extras_by_slot=p["extras_by_slot"],
                              windows=p["windows"], occurrences=p["occurrences"],
                              book_hashes=np.zeros(len(p["books"]), "<u8"), fill_vectors=fill_for(p["keys"]),
                              vector_bytes=VB)
        self.assertEqual(w["sha256"], self.segs[0]["sha256"])

    def test_delta_contents_match_independent_computation(self):
        meta = D.build_delta(opv, self.d / "s1.oxv", self.d / "s2.oxv", self.d / "d12.oxd")
        o1, o2 = occ_set(v1()), occ_set(v2())
        self.assertEqual(meta["counts"]["removed"], len(o1 - o2))
        self.assertEqual(meta["counts"]["added"], len(o2 - o1))
        k1, k2 = {r[0] for r in o1}, {r[0] for r in o2}
        self.assertEqual(meta["counts"]["newKeys"], len(k2 - k1))
        self.assertGreater(len(o1 - o2), 0)
        self.assertGreater(meta["counts"]["newKeys"], 0)
        # הדלתא קטנה מהבסיס המלא של הגרסה החדשה, וגם נמדדת: המפתחות שלא השתנו אינם בה
        self.assertLess(meta["counts"]["newKeys"], self.segs[1]["slots"])
        # וקטורי המפתחות החדשים בדלתא הם אלה שבמקטע החדש
        _m, _n, _r, _a, keys, codes, scales = D.read_delta(self.d / "d12.oxd")
        self.assertEqual(sorted(keys.tolist()), sorted(k2 - k1))
        for k, c, s in zip(keys.tolist(), codes, scales):
            self.assertEqual(bytes(c), vec_of(k))
            self.assertAlmostEqual(float(s), float(k % 9973) / 7, places=3)

    def test_apply_equals_new_segment(self):
        D.build_delta(opv, self.d / "s1.oxv", self.d / "s2.oxv", self.d / "d12.oxd")
        w = D.apply_delta(opv, self.d / "s1.oxv", self.d / "d12.oxd", self.d / "out2.oxv")
        self.assertEqual(w["sha256"], self.segs[1]["sha256"])
        self.assertEqual(self.sha(self.d / "out2.oxv"), self.sha(self.d / "s2.oxv"))
        self.assertEqual(w["slots"], self.segs[1]["slots"])

    def test_chain_equals_full_base(self):
        D.build_delta(opv, self.d / "s1.oxv", self.d / "s2.oxv", self.d / "d12.oxd")
        D.build_delta(opv, self.d / "s2.oxv", self.d / "s3.oxv", self.d / "d23.oxd")
        D.apply_delta(opv, self.d / "s1.oxv", self.d / "d12.oxd", self.d / "m2.oxv")
        w = D.apply_delta(opv, self.d / "m2.oxv", self.d / "d23.oxd", self.d / "m3.oxv")
        self.assertEqual(w["sha256"], self.segs[2]["sha256"])
        self.assertEqual((self.d / "m3.oxv").read_bytes(), (self.d / "s3.oxv").read_bytes())
        # הווקטורים של כל המפתחות בתוצאה זהים לבסיס המלא
        a, b = D.SegInfo(opv, self.d / "m3.oxv"), D.SegInfo(opv, self.d / "s3.oxv")
        self.assertEqual(a.slot_keys.tolist(), b.slot_keys.tolist())
        self.assertTrue(np.array_equal(a.vectors(np.arange(a.h["slots"]))[0], b.vectors(np.arange(b.h["slots"]))[0]))
        # מפתח שנמחק ב-v2 וחזר ב-v3 (ספר 2 שחזר) מגיע מהדלתא, לא מהמקטע הישן
        self.assertIn(220, a.slot_keys.tolist())

    def test_wrong_base_is_rejected(self):
        D.build_delta(opv, self.d / "s2.oxv", self.d / "s3.oxv", self.d / "d23.oxd")
        with self.assertRaises(D.DeltaError):
            D.apply_delta(opv, self.d / "s1.oxv", self.d / "d23.oxd", self.d / "bad.oxv")
        self.assertFalse((self.d / "bad.oxv").exists())

    def test_corrupt_delta_is_rejected(self):
        D.build_delta(opv, self.d / "s1.oxv", self.d / "s2.oxv", self.d / "d12.oxd")
        meta, names, rem, add, keys, codes, scales = D.read_delta(self.d / "d12.oxd")
        # וקטור שהשתנה בדלתא -> sha256 של התוצאה שונה, והקובץ לא נשאר
        raw = bytearray(zstd_read(self.d / "d12.oxd"))
        raw[-5] ^= 0xFF
        (self.d / "bad.oxd").write_bytes(zstd_write(bytes(raw)))
        with self.assertRaises(D.DeltaError):
            D.apply_delta(opv, self.d / "s1.oxv", self.d / "bad.oxd", self.d / "bad.oxv")
        self.assertFalse((self.d / "bad.oxv").exists())


def zstd_read(p):
    import zstandard
    with open(p, "rb") as f:
        return zstandard.ZstdDecompressor().stream_reader(f).read()


def zstd_write(b):
    import zstandard
    return zstandard.ZstdCompressor().compress(b)


def entry(frm, to, size=10, tag="vectors-vX", name=None):
    return {"fromDb": frm[0], "toDb": to[0], "tag": tag, "from": {"segmentId": "i" + frm[1], "sha256": frm[1]},
            "to": {"segmentId": "i" + to[1], "sha256": to[1], "size": 1},
            "files": [{"file": name or f"d-{frm[1]}-{to[1]}.oxd", "size": size, "sha256": "x"}]}


def manifest(target=("a3", "S3"), deltas=(), base_size=100):
    return {"segment": {"id": "i" + target[1], "sha256": target[1]},
            "files": [{"file": "base.zst", "size": base_size, "sha256": "y"}], "deltas": list(deltas)}


@unittest.skipUnless(HAVE, "דורש numpy, zstandard והסקריפט של קרית חנה")
class PlanInstall(unittest.TestCase):
    ALL = {"base.zst", "d-S1-S2.oxd", "d-S2-S3.oxd"}
    CH = [entry((1, "S1"), (2, "S2")), entry((2, "S2"), (3, "S3"))]

    def test_current(self):
        p = D.plan_install(manifest(deltas=self.CH), "S3", self.ALL)
        self.assertEqual(p["mode"], "current")

    def test_one_and_two_step_chain(self):
        m = manifest(deltas=self.CH)
        p = D.plan_install(m, "S2", self.ALL)
        self.assertEqual((p["mode"], len(p["chain"]), p["need"]), ("delta", 1, ["d-S2-S3.oxd"]))
        p = D.plan_install(m, "S1", self.ALL)
        self.assertEqual((p["mode"], len(p["chain"])), ("delta", 2))

    def test_fallback_unknown_installed(self):
        p = D.plan_install(manifest(deltas=self.CH), "OTHER", self.ALL)
        self.assertEqual((p["mode"], p["need"]), ("base", ["base.zst"]))
        self.assertEqual(D.plan_install(manifest(deltas=self.CH), None, self.ALL)["mode"], "base")

    def test_fallback_missing_delta_file(self):
        p = D.plan_install(manifest(deltas=self.CH), "S1", {"base.zst", "d-S2-S3.oxd"})
        self.assertEqual(p["mode"], "base")
        self.assertIn("חסרים", p["reason"])

    def test_missing_everything(self):
        p = D.plan_install(manifest(deltas=self.CH), "S1", {"d-S2-S3.oxd"})
        self.assertEqual(p["mode"], "missing")
        self.assertEqual(set(p["need"]), {"d-S1-S2.oxd", "d-S2-S3.oxd"})
        p = D.plan_install(manifest(deltas=self.CH), "OTHER", set())
        self.assertEqual((p["mode"], p["need"]), ("missing", ["base.zst"]))

    def test_chain_too_long_or_too_big(self):
        m = manifest(deltas=self.CH)
        self.assertEqual(D.plan_install(m, "S1", self.ALL, max_chain=1)["mode"], "base")
        big = manifest(deltas=[entry((1, "S1"), (2, "S2"), size=30), entry((2, "S2"), (3, "S3"), size=30)])
        self.assertEqual(D.plan_install(big, "S1", self.ALL)["mode"], "base")     # 60 >= 50% מ-100
        self.assertEqual(D.plan_install(big, "S2", self.ALL)["mode"], "delta")    # 30 < 50

    def test_no_delta_flag_and_broken_chain(self):
        self.assertEqual(D.plan_install(manifest(deltas=self.CH), "S2", self.ALL, allow_delta=False)["mode"], "base")
        gap = manifest(deltas=[entry((2, "S2"), (3, "S3"))])
        self.assertEqual(D.plan_install(gap, "S1", self.ALL)["mode"], "base")
        loop = manifest(deltas=[entry((1, "S1"), (2, "S2")), entry((2, "S2"), (1, "S1"))])
        self.assertEqual(D.plan_install(loop, "S1", self.ALL)["mode"], "base")


IDENT = {"text": {"line_text_version": 4, "key_version": 1},
         "model": {"family_id": "otzaria/synthetic", "embedding_dim": 256},
         "store": {"backend_id": "otzaria-oxv", "store_format_version": 3, "vector_precision": "rot192-i8i4"}}


def argv_ns(**kw):
    import argparse
    return argparse.Namespace(**kw)


def capture(fn, *a, **kw):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **kw)
    return r, buf.getvalue()


@unittest.skipUnless(HAVE, "דורש numpy, zstandard והסקריפט של קרית חנה")
class EndToEnd(unittest.TestCase):
    """pack (עם --prev-manifest) -> install, על סט וקטורים סינתטי של "אוצריא" (בסיס רשמי + מקטע אישי)."""

    def pack(self, i, pth, out, prev_m, prev_s, tag, max_chain=2):
        C.cmd_pack(argv_ns(segment=str(pth), out=str(out), official_manifest=None, official_tag="", db=None,
                           db_sha256="", repository="r", commit="c", run_id=str(i), part_size=C.PART_LIMIT,
                           release_tag=tag, prev_manifest=prev_m, prev_segment=prev_s,
                           max_chain=max_chain, no_verify_delta=False))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.seg = [self.d / f"s{i}.oxv" for i in (1, 2, 3)]
        for pth, f in zip(self.seg, (v1, v2, v3)):
            make_segment(pth, f())
        self.rel = {}
        prev_m = prev_s = None
        for i, pth in enumerate(self.seg, 1):
            out = self.d / f"rel{i}"
            out.mkdir()
            capture(self.pack, i, pth, out, prev_m, prev_s, f"vectors-v{50 + i}")
            prev_m, prev_s = str(out / "vectors-manifest.json"), str(pth)
            self.rel[i] = out
        self.files = self.d / "dl"            # כל הקבצים של שלוש ההוצאות, כפי שהמשתמש מוריד מה-releases
        self.files.mkdir()
        for out in self.rel.values():
            for f in out.glob("*"):
                if f.name != "vectors-manifest.json":
                    (self.files / f.name).write_bytes(f.read_bytes())
        self.m = {i: json.loads((self.rel[i] / "vectors-manifest.json").read_text(encoding="utf-8")) for i in (1, 2, 3)}

    def tearDown(self):
        self.tmp.cleanup()

    def layout(self, name, ours_from=None):
        """סט של אוצריא: מקטע בסיס "רשמי" סינתטי, ואם ours_from: מותקן בו גם מקטע אישי (בסיס מלא של אותה גרסה)."""
        official = self.d / f"official-{name}.oxv"
        w = make_segment(official, [(7000 + i, 9, i + 1, 0) for i in range(50)], flag=opv.FLAG_BASE, tag="v36-x",
                         frm=0, to=36)
        pkg = {"kind": "base", "identity": IDENT, "identityDigest": IDENT_DIGEST.hex(), "toLibraryVersion": 36,
               "fromLibraryVersion": 0, "libraryReleaseTag": "v36-x", "segmentId": w["id"],
               "counts": {"slots": w["slots"]}, "codecParamsSha256": hashlib.sha256(PARAMS).hexdigest(),
               "segment": {"sha256": w["sha256"], "size": w["size"]}, "packageDigest": "p",
               "provenance": {"worker": {"backend": "x"}}, "files": [{"file": "f"}]}
        lay = self.d / f"layout-{name}"
        C.write_layout(pkg, official, lay)
        vdir = lay / "vectors"
        if ours_from:
            capture(self.install, vdir, ours_from)
        return vdir

    def install(self, vdir, i, **kw):
        ns = dict(manifest=str(self.rel[i] / "vectors-manifest.json"), files_dir=str(self.files), vectors_dir=str(vdir),
                  dry_run=False, force=True, otzaria_dir=None, db_version=None, allow_db_mismatch=False, plan=False,
                  no_delta=False, max_chain=3, max_ratio=100.0, force_reinstall=False)
        ns.update(kw)
        return C.cmd_install(argv_ns(**ns))

    def ours(self, vdir):
        live = opv.LiveSet(vdir)
        o = [s for s in live.doc["segments"] if opv.is_ours(s)]
        self.assertEqual(len(o), 1)
        return o[0], D.sha256_file(vdir / o[0]["file"]), live.doc["generation"], len(live.doc["segments"])

    def test_manifests_chain(self):
        self.assertEqual([len(self.m[i]["deltas"]) for i in (1, 2, 3)], [0, 1, 2])
        d = self.m[3]["deltas"]
        self.assertEqual((d[0]["from"]["sha256"], d[1]["to"]["sha256"]),
                         (self.m[1]["segment"]["sha256"], self.m[3]["segment"]["sha256"]))
        self.assertEqual(d[0]["to"]["sha256"], d[1]["from"]["sha256"])
        self.assertEqual([x["tag"] for x in d], ["vectors-v52", "vectors-v53"])
        self.assertEqual(self.m[3]["releaseTag"], "vectors-v53")

    def test_chain_cap(self):
        out = self.d / "rel4"
        out.mkdir()
        make_segment(self.d / "s4.oxv", v3() + book_rows(6, [8000, 8001, 8002]))
        capture(self.pack, 4, self.d / "s4.oxv", out, str(self.rel[3] / "vectors-manifest.json"), str(self.seg[2]),
                "vectors-v54")
        m4 = json.loads((out / "vectors-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([x["tag"] for x in m4["deltas"]], ["vectors-v53", "vectors-v54"])   # הישנה ביותר נושרת

    def test_prev_segment_mismatch_gives_no_delta(self):
        out = self.d / "rel5"
        out.mkdir()
        capture(self.pack, 5, self.seg[2], out, str(self.rel[1] / "vectors-manifest.json"), str(self.seg[1]), "vectors-v55")
        self.assertEqual(json.loads((out / "vectors-manifest.json").read_text(encoding="utf-8"))["deltas"], [])

    def test_delta_install_equals_base_install(self):
        a = self.layout("a", ours_from=1)
        _o, sha_before, gen_before, _n = self.ours(a)
        self.assertEqual(sha_before, self.m[1]["segment"]["sha256"])
        self.assertEqual(json.loads(capture(self.install, a, 3, plan=True)[1])["mode"], "delta")
        _r, out = capture(self.install, a, 3)     # שרשרת של 2 דלתאות
        self.assertIn("הותקן (דלתאות:", out)
        o, sha, gen, nseg = self.ours(a)
        b = self.layout("b")
        _r, out_b = capture(self.install, b, 3)   # אין מקטע אישי: בסיס מלא
        self.assertIn("הותקן (בסיס מלא:", out_b)
        _ob, shab, _g, _n2 = self.ours(b)
        self.assertEqual(sha, self.m[3]["segment"]["sha256"])
        self.assertEqual(sha, shab)
        self.assertEqual(o["slots"], self.m[3]["segment"]["slots"])
        self.assertEqual((gen, nseg), (gen_before + 1, 2))        # דור חדש אחד; בסיס רשמי + מקטע אישי אחד
        self.assertEqual(opv.read_pointer(a, "PREVIOUS")["generation"], gen_before)
        gen_now = opv.LiveSet(a).doc["generation"]                # עדכון חוזר: כבר מותקן
        self.assertEqual(capture(self.install, a, 3)[0], 0)
        self.assertEqual(opv.LiveSet(a).doc["generation"], gen_now)

    def test_single_step_and_plan(self):
        v = self.layout("c", ours_from=2)
        _r, out = capture(self.install, v, 3, plan=True)
        plan = json.loads(out)
        self.assertEqual(plan["mode"], "delta")
        self.assertEqual([n["tag"] for n in plan["need"]], ["vectors-v53"])
        capture(self.install, v, 3)
        self.assertEqual(self.ours(v)[1], self.m[3]["segment"]["sha256"])

    def test_fallback_to_base_when_delta_missing(self):
        v = self.layout("d", ours_from=1)
        for f in list(self.files.glob("*.oxd")):
            f.unlink()
        _r, out = capture(self.install, v, 3)
        self.assertIn("בסיס מלא", out)
        self.assertEqual(self.ours(v)[1], self.m[3]["segment"]["sha256"])

    def test_fallback_when_installed_not_in_chain(self):
        v = self.layout("e")
        capture(self.install, v, 3)
        sha3 = self.ours(v)[1]
        # מותקן v3 ומבקשים v2 (ישן יותר): אין שרשרת אחורה -> בסיס מלא של v2
        _r, out = capture(self.install, v, 2)
        self.assertIn("בסיס מלא", out)
        self.assertEqual(self.ours(v)[1], self.m[2]["segment"]["sha256"])
        self.assertNotEqual(sha3, self.m[2]["segment"]["sha256"])

    def test_corrupt_delta_falls_back_to_base_and_missing_everything_fails(self):
        v = self.layout("f", ours_from=1)
        for f in self.files.glob("*.oxd"):
            b = bytearray(f.read_bytes())
            b[len(b) // 2] ^= 0xFF
            f.write_bytes(bytes(b))              # גודל זהה, sha256 שונה
        _r, out = capture(self.install, v, 3)
        self.assertIn("מתקינים את הבסיס המלא", out)
        self.assertEqual(self.ours(v)[1], self.m[3]["segment"]["sha256"])
        v2_ = self.layout("g", ours_from=1)
        for f in self.files.glob("*"):
            f.unlink()
        with self.assertRaises(C.CiError):
            self.install(v2_, 3)

    def test_default_ratio_decides_by_real_sizes(self):
        # בנתונים הסינתטיים הקטנים השרשרת v1->v3 כבדה יחסית: ההחלטה (דלתא/בסיס) חייבת להתאים לגדלים האמיתיים
        v = self.layout("i", ours_from=1)
        m = self.m[3]
        deltas = sum(e["size"] for d in m["deltas"] for e in d["files"])
        base = sum(e["size"] for e in m["files"])
        _r, out = capture(self.install, v, 3, plan=True, max_ratio=0.5)
        self.assertEqual(json.loads(out)["mode"], "delta" if deltas < 0.5 * base else "base")
        _r, out = capture(self.install, v, 3, plan=True, max_ratio=deltas / base + 0.01)
        self.assertEqual(json.loads(out)["mode"], "delta")
        _r, out = capture(self.install, v, 3, plan=True, max_ratio=deltas / base - 0.01)
        self.assertEqual(json.loads(out)["mode"], "base")

    def test_no_delta_flag(self):
        v = self.layout("h", ours_from=1)
        _r, out = capture(self.install, v, 3, plan=True, no_delta=True)
        self.assertEqual(json.loads(out)["mode"], "base")


if __name__ == "__main__":
    unittest.main()
