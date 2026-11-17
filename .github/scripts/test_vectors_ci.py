# -*- coding: utf-8 -*-
"""בדיקות ל-vectors_ci.py: החלקים שאינם דורשים את המודל (בחירת הוצאה, רשימת ספרים ורסיסים, הרכבת set.json,
פענוח/פיצול zstd, שמירת cache). דורש numpy ו-zstandard (כמו ה-workflow). בדיקת מקצה לקצה עם המודל נעשית
ידנית (ראו הערות ב-build-vectors.yml)."""
import json
import os
import struct
import sys
import tarfile
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vectors_ci as C

try:
    import zstandard  # noqa: F401
    import numpy as np
    HAVE_Z = True
except ImportError:  # pragma: no cover
    HAVE_Z = False


def rel(tag, base_id="a31f7627", n=38, published="2026-11-15T03:22:45Z", **kw):
    mf = f"otzaria-vectors-{base_id}-v{n}-base.manifest.json"
    z = f"otzaria-vectors-{base_id}-v{n}-base.oxv.zst"
    d = {"tag_name": tag, "published_at": published, "draft": False, "prerelease": True,
         "assets": [{"name": mf, "size": 3000}, {"name": z, "size": 1838290156}, {"name": "gates.json", "size": 1}]}
    d.update(kw)
    return d


class PickOfficial(unittest.TestCase):
    def test_picks_highest_windows_base(self):
        rs = [rel("vectors-qa491-windows-v36-20261101064231", n=36, published="2026-11-02T14:47:07Z"),
              rel("vectors-qa491-windows-v38-20261112163847"),
              rel("vectors-qa491-v38-20261112163847", base_id="0e87cbd9", published="2026-11-15T01:51:17Z"),
              rel("vectors-qa491-windows-delta-v38-20261112163847"),
              rel("vectors-windows-v38-20261112163847", base_id="4a7322af"),
              rel("vectors-ledger-v38-20261112163847"),
              rel("vectors-qa491-windows-v39-20261120000000", draft=True)]
        tag, n, mf, zst, size = C.pick_official(rs)
        self.assertEqual((tag, n), ("vectors-qa491-windows-v38-20261112163847", 38))
        self.assertTrue(mf.endswith("-base.manifest.json") and zst.endswith("-base.oxv.zst"))
        self.assertEqual(size, 1838290156)

    def test_none(self):
        with self.assertRaises(C.CiError):
            C.pick_official([rel("vectors-qa491-v38-20261112163847")])

    def test_requires_both_assets(self):
        r = rel("vectors-qa491-windows-v38-20261112163847")
        r["assets"] = r["assets"][:1]
        with self.assertRaises(C.CiError):
            C.pick_official([r])


def make_db(path, lib="otzarya-unofficial-books", ver=51, books=((7, "ב"), (3, "א"), (12, "ג"))):
    import sqlite3
    con = sqlite3.connect(path)
    con.executescript("""create table schema_meta(key text primary key, value text);
        create table category(id integer primary key, parentId integer, title text);
        create table book(id integer primary key, title text, categoryId integer);
        create table line(id integer primary key, bookId integer, lineIndex integer, content text);""")
    con.execute("insert into schema_meta values('library_id', ?)", (lib,))
    con.execute("insert into schema_meta values('db_version', ?)", (str(ver),))
    con.execute("insert into category values(1, null, 'ק')")
    for i, t in books:
        con.execute("insert into book values(?,?,1)", (i, t))
        con.execute("insert into line(bookId, lineIndex, content) values(?,0,?)", (i, "שורה"))
    con.commit()
    con.close()


class Books(unittest.TestCase):
    def test_shards_stable_and_cover(self):
        ids = list(range(1, 400))
        self.assertEqual([C.shard_of(i, 5) for i in ids], [C.shard_of(i, 5) for i in ids])
        self.assertEqual({C.shard_of(i, 5) for i in ids}, {0, 1, 2, 3, 4})
        self.assertEqual({C.shard_of(i, 1) for i in ids}, {0})

    def test_slug(self):
        self.assertEqual(C.sanitize_slug("otzarya-unofficial-books"), "otzarya-unofficial-books")
        self.assertEqual(C.sanitize_slug("A b/c"), "a-b-c")
        self.assertEqual(C.sanitize_slug(""), C.DEFAULT_SLUG)

    def test_book_names_are_db_keys(self):
        names = C.book_names("otzarya-unofficial-books", [3, 12])
        self.assertEqual(names, ["db:otzarya-unofficial-books:3", "db:otzarya-unofficial-books:12"])
        self.assertEqual(C.check_names(names, "otzarya-unofficial-books"), (True, 0))

    def test_check_names_rejects_other_namespaces(self):
        slug = "otzarya-unofficial-books"
        for bad in ("uid:5", "id:5", "ext:x", f"db:other:5", f"db:{slug}:", f"db:{slug}:5x", f"xdb:{slug}:5"):
            ok, n = C.check_names([f"db:{slug}:1", bad], slug)
            self.assertFalse(ok, bad)
            self.assertEqual(n, 1)

    @unittest.skipUnless(HAVE_Z, "דורש numpy")
    def test_db_info(self):
        try:
            import otzaria_personal_vectors  # noqa: F401
        except ImportError:
            self.skipTest("הסקריפט אינו זמין")
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "x.db"
            make_db(db)
            info = C.db_info(db)
            self.assertEqual((info["library_id"], info["db_version"], info["slug"]), ("otzarya-unofficial-books", 51, "otzarya-unofficial-books"))
            self.assertEqual([i for i, _t in info["books"]], [3, 7, 12])


# מניפסט החבילה של הבסיס v36 הרשמי (שדות כפי שנקראו מהקובץ של אוצריא) ו-set.json שאוצריא רשמה לו
IDENT = {"text": {"line_text_version": 4, "key_version": 1},
         "model": {"family_id": "otzaria/Meivin-Embed-QA491-native256", "embedding_dim": 256},
         "store": {"backend_id": "otzaria-oxv", "store_format_version": 3, "vector_precision": "rot192-i8i4"}}
PKG = {"kind": "base", "identity": IDENT, "identityDigest": "a31f76271e9979e11f72685b9ced3287a6ddb2013f3c666f090ea71802d692cc",
       "codecParamsSha256": "4408dd9c691b6ae957f2b0b25ad9fedb47d415838f6f0a5d5167020c4f7a785f", "fromLibraryVersion": 0,
       "toLibraryVersion": 36, "libraryReleaseTag": "v36-20261101064231", "segmentId": "bff46cf049f9f9d29e000f9990940d16",
       "counts": {"books": 8148, "slots": 10043833, "extras": 259733, "foreign": 0, "tombstones": 0},
       "segment": {"sha256": "5a525323a52d852c4b4642e3556143bc8c00d23212fa58fd4da1b969b21f0f64", "size": 2083481824},
       "packageDigest": "c56dd9235b7b9778e77ba491450cf854536fa302d7a0689a3cbdc21ee5551d2e",
       "provenance": {"worker": {"backend": "x"}}, "files": [{"file": "f"}]}


class SetDoc(unittest.TestCase):
    def test_matches_otzaria_gen6(self):
        doc = C.build_set_doc(PKG, generation=6, created_at="2026-11-02T23:03:03Z")
        seg = doc["segments"][0]
        # ערכים מ-set.json אמיתי של אוצריא (gen-000006) ומקובץ ה-.del שלה (כ-1255508 בייט, CRC 2484906093)
        self.assertEqual(seg["del"], {"file": "bff46cf049f9f9d29e000f9990940d16.del", "crc32": 2484906093, "count": 0})
        self.assertEqual(seg["file"], "segments/bff46cf049f9f9d29e000f9990940d16.oxv")
        self.assertEqual((seg["kind"], seg["from"], seg["to"], seg["slots"], seg["size"]), ("base", 0, 36, 10043833, 2083481824))
        self.assertEqual(doc["stats"], {"slots": 10043833, "slots_dead": 0, "foreign": 0, "foreign_unresolved": 0,
                                        "bytes": 2083481824})
        self.assertEqual(list(doc), ["format", "format_version", "generation", "identity", "identity_digest",
                                     "codec_params_sha256", "library_version", "library_release_tag", "segments",
                                     "stats", "created_at"])
        self.assertEqual(len(C._del_bytes(10043833)), 1255508)

    def test_rejects_delta(self):
        with self.assertRaises(C.CiError):
            C.build_set_doc(dict(PKG, kind="delta"))

    def test_write_layout(self):
        with tempfile.TemporaryDirectory() as d:
            seg = Path(d) / "s.oxv"
            seg.write_bytes(b"x")
            pkg = dict(PKG, counts={"slots": 70})
            v = C.write_layout(pkg, seg, Path(d) / "L")
            cur = json.loads((v / "CURRENT").read_text())
            raw = (v / cur["set"]).read_bytes()
            import hashlib
            self.assertEqual(hashlib.sha256(raw).hexdigest(), cur["set_sha256"])
            self.assertTrue((v / "segments" / f"{PKG['segmentId']}.oxv").exists())
            dl = (v / "gen-000001" / f"{PKG['segmentId']}.del").read_bytes()
            self.assertEqual(struct.unpack_from("<I", dl, len(dl) - 4)[0], zlib.crc32(dl[:-4]) & 0xFFFFFFFF)


@unittest.skipUnless(HAVE_Z, "דורש zstandard ו-numpy")
class Compression(unittest.TestCase):
    def test_split_and_decompress(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.oxv"
            data = os.urandom(300000) + b"A" * 1000
            src.write_bytes(data)
            parts = C.compress_split(src, Path(d) / "o", "a.oxv", part_size=100000)
            self.assertGreater(len(parts), 1)
            self.assertTrue(all(p.stat().st_size <= 100000 for p in parts))
            out = Path(d) / "b.oxv"
            import hashlib
            sha = C.decompress_zst(parts, out, hashlib.sha256(data).hexdigest(), len(data))
            self.assertEqual(out.read_bytes(), data)
            self.assertEqual(sha, hashlib.sha256(data).hexdigest())
            with self.assertRaises(C.CiError):
                C.decompress_zst(parts, out, "0" * 64)

    def test_single_file(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.oxv"
            src.write_bytes(b"hello" * 100)
            parts = C.compress_split(src, Path(d) / "o", "a.oxv")
            self.assertEqual([p.name for p in parts], ["a.oxv.zst"])


@unittest.skipUnless(HAVE_Z, "דורש numpy")
class CacheTransport(unittest.TestCase):
    def mk(self, d, names):
        d.mkdir(parents=True, exist_ok=True)
        (d / "codec_params.bin").write_bytes(b"P")
        for n in names:
            np.savez(d / n, keys=np.arange(5, dtype=np.uint64), codes=np.zeros((5, 2), np.uint8),
                     scales=np.zeros(5, np.float32))

    def test_roundtrip_only_new(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            self.mk(t / "c", ["part-000001.npz"])
            before = {p.name for p in (t / "c").glob("part-*.npz")}
            self.mk(t / "c", ["part-000002.npz", "part-000003.npz"])
            tars = t / "tars"
            tars.mkdir()
            self.assertEqual(C.pack_new_parts(t / "c", before, tars / "cache-s0-r1.tar", "s0r1"), 2)
            self.assertEqual(C.pack_new_parts(t / "c", {p.name for p in (t / "c").glob("part-*.npz")},
                                              tars / "n.tar", "x"), 0)
            n = C.restore_cache(tars, t / "d")
            self.assertEqual(n, 2)
            self.assertEqual(sorted(p.name for p in (t / "d").glob("part-*.npz")), ["part-s0r1-000001.npz", "part-s0r1-000002.npz"])
            self.assertEqual(C.cache_vectors(t / "d"), 10)

    def test_restore_rejects_foreign_member(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / "e.txt").write_text("x")
            with tarfile.open(t / "cache-bad.tar", "w") as tf:
                tf.add(t / "e.txt", arcname="../evil.txt")
            with self.assertRaises(C.CiError):
                C.restore_cache(t, t / "d")


if __name__ == "__main__":
    unittest.main()
