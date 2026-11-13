# -*- coding: utf-8 -*-
"""בדיקות ל-db_release_helpers.py (ספריית תקן בלבד; בדיקת zstd מדולגת אם אין zstd בנתיב)."""
import hashlib
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db_release_helpers as H


def rel(tag, assets, published="2026-01-01T00:00:00Z", **kw):
    d = {"tag_name": tag, "published_at": published, "draft": False, "prerelease": False,
         "assets": [{"name": n, "browser_download_url": f"https://x/{tag}/{n}", "size": 5} for n in assets]}
    d.update(kw)
    return d


class PickOfficial(unittest.TestCase):
    def test_picks_latest_with_asset(self):
        rs = [rel("v28-1", ["seforim.db.zst"], "2026-09-11T00:00:00Z"),
              rel("v31-2", [H.OFFICIAL_ASSET, "x"], "2026-10-04T00:00:00Z"),
              rel("v30-9", [H.OFFICIAL_ASSET], "2026-10-01T00:00:00Z"),
              rel("v29-old", ["seforim-schema6.db.zst"], "2026-12-30T00:00:00Z"),
              rel("vectors-5", [H.OFFICIAL_ASSET], "2026-12-01T00:00:00Z"),
              rel("v32-pre", [H.OFFICIAL_ASSET], "2026-11-01T00:00:00Z", prerelease=True),
              rel("v33-dr", [H.OFFICIAL_ASSET], "2026-11-02T00:00:00Z", draft=True)]
        tag, url, size, kind = H.pick_official(rs)
        self.assertEqual((tag, url, size, kind), ("v31-2", f"https://x/v31-2/{H.OFFICIAL_ASSET}", 5, "single"))

    def test_none(self):
        with self.assertRaises(H.HelperError):
            H.pick_official([rel("v28-1", ["seforim.db.zst"]), rel("v29-1", ["seforim-schema6.db.zst"])])

    def test_split_release(self):
        m = H.OFFICIAL_ASSET + H.MANIFEST_SUFFIX
        rs = [rel("v37-1", [H.OFFICIAL_ASSET], "2026-11-05T00:00:00Z"),
              rel("v38-1", [m, H.OFFICIAL_ASSET + ".part-000", H.OFFICIAL_ASSET + ".part-001", "seforim.db.buildstate.zst"],
                  "2026-11-12T00:00:00Z")]
        tag, url, size, kind = H.pick_official(rs)
        self.assertEqual((tag, url, size, kind), ("v38-1", f"https://x/v38-1/{m}", 10, "split"))

    def test_newest_wins_either_kind(self):
        m = H.OFFICIAL_ASSET + H.MANIFEST_SUFFIX
        split = rel("v38-1", [m, H.OFFICIAL_ASSET + ".part-000"], "2026-11-12T00:00:00Z")
        single = rel("v39-1", [H.OFFICIAL_ASSET], "2026-11-20T00:00:00Z")
        self.assertEqual(H.pick_official([split, single])[0], "v39-1")
        self.assertEqual(H.pick_official([single, split])[3], "single")
        single_old = rel("v37-1", [H.OFFICIAL_ASSET], "2026-11-05T00:00:00Z")
        self.assertEqual(H.pick_official([single_old, split])[3], "split")

    def test_manifest_without_parts_is_skipped(self):
        m = H.OFFICIAL_ASSET + H.MANIFEST_SUFFIX
        rs = [rel("v38-1", [m], "2026-11-12T00:00:00Z"), rel("v37-1", [H.OFFICIAL_ASSET], "2026-11-05T00:00:00Z")]
        self.assertEqual(H.pick_official(rs)[0], "v37-1")

    def test_prefers_single_in_same_release(self):
        m = H.OFFICIAL_ASSET + H.MANIFEST_SUFFIX
        r = rel("v38-1", [m, H.OFFICIAL_ASSET + ".part-000", H.OFFICIAL_ASSET])
        self.assertEqual(H.pick_official([r])[3], "single")


class AssembleOfficial(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = os.urandom(3000)
        self.chunks = [self.data[:1200], self.data[1200:2500], self.data[2500:]]
        self.manifest = {
            "schemaVersion": 1, "archive": H.OFFICIAL_ASSET, "size": len(self.data),
            "sha256": hashlib.sha256(self.data).hexdigest(),
            "parts": [{"name": f"{H.OFFICIAL_ASSET}.part-{i:03d}", "size": len(c),
                       "sha256": hashlib.sha256(c).hexdigest()} for i, c in enumerate(self.chunks)]}

    def write(self, manifest=None, chunks=None):
        for p, c in zip(self.manifest["parts"], chunks or self.chunks):
            (self.tmp / p["name"]).write_bytes(c)
        mp = self.tmp / (H.OFFICIAL_ASSET + H.MANIFEST_SUFFIX)
        mp.write_text(json.dumps(manifest or self.manifest), encoding="utf-8")
        return mp

    def test_assemble_ok_deletes_parts(self):
        mp = self.write()
        out = self.tmp / H.OFFICIAL_ASSET
        H.assemble_official(mp, out)
        self.assertEqual(out.read_bytes(), self.data)
        self.assertEqual([p.name for p in self.tmp.iterdir() if ".part-" in p.name], [])

    def test_keep_parts(self):
        mp = self.write()
        H.assemble_official(mp, self.tmp / H.OFFICIAL_ASSET, delete_parts=False)
        self.assertEqual(len([p for p in self.tmp.iterdir() if p.name.endswith(tuple("012")) and ".part-" in p.name]), 3)

    def test_bad_part_sha(self):
        bad = list(self.chunks)
        bad[1] = bad[1][:-1] + bytes([bad[1][-1] ^ 1])   # אותו גודל, תוכן שונה
        mp = self.write(chunks=bad)
        out = self.tmp / H.OFFICIAL_ASSET
        with self.assertRaises(H.HelperError):
            H.assemble_official(mp, out)
        self.assertFalse(out.exists())
        self.assertFalse((self.tmp / (H.OFFICIAL_ASSET + ".partial")).exists())

    def test_bad_part_size_and_missing(self):
        mp = self.write()
        (self.tmp / self.manifest["parts"][2]["name"]).write_bytes(b"x")
        with self.assertRaises(H.HelperError):
            H.assemble_official(mp, self.tmp / H.OFFICIAL_ASSET)
        (self.tmp / self.manifest["parts"][2]["name"]).unlink()
        with self.assertRaises(H.HelperError):
            H.assemble_official(mp, self.tmp / H.OFFICIAL_ASSET)

    def test_bad_total_sha(self):
        m = json.loads(json.dumps(self.manifest))
        m["sha256"] = "0" * 64
        mp = self.write(manifest=m)
        out = self.tmp / H.OFFICIAL_ASSET
        with self.assertRaises(H.HelperError):
            H.assemble_official(mp, out)
        self.assertFalse(out.exists())

    def test_sizes_do_not_add_up_and_unsafe_name(self):
        m = json.loads(json.dumps(self.manifest))
        m["size"] += 1
        with self.assertRaises(H.HelperError):
            H.assemble_official(self.write(manifest=m), self.tmp / H.OFFICIAL_ASSET)
        m = json.loads(json.dumps(self.manifest))
        m["parts"][0]["name"] = "../evil"
        with self.assertRaises(H.HelperError):
            H.assemble_official(self.write(manifest=m), self.tmp / H.OFFICIAL_ASSET)


class Versions(unittest.TestCase):
    def test_next(self):
        self.assertEqual(H.next_version(5, None), 6)
        self.assertEqual(H.next_version(5, "3"), 6)
        self.assertEqual(H.next_version(5, "10"), 10)
        self.assertEqual(H.next_version(None, "7"), 7)
        with self.assertRaises(H.HelperError):
            H.next_version(None, None)
        with self.assertRaises(H.HelperError):
            H.next_version(None, "")

    def test_tags(self):
        tags = ["latest", "db", "db-v2", "db-v10", "db-v3", "db-v1x", "db-v4"]
        self.assertEqual(H.db_tags(tags), [(2, "db-v2"), (3, "db-v3"), (4, "db-v4"), (10, "db-v10")])
        self.assertEqual(H.select_prev(tags, 10), ["db-v4", "db-v3"])
        self.assertEqual(H.select_prev(tags, 3), ["db-v2"])
        self.assertEqual(H.select_prev(tags, 2), [])
        self.assertEqual(H.tags_to_prune(tags, 3), ["db-v2"])
        self.assertEqual(H.tags_to_prune(tags, 10), [])


class Orphans(unittest.TestCase):
    def test_orphans(self):
        tags = ["latest", "db", "db-v4", "db-v5", "db-v6", "db-v10"]
        self.assertEqual(H.orphan_tags(tags, 5), ["db-v6", "db-v10"])
        self.assertEqual(H.orphan_tags(tags, 10), [])


class SpacedPathRegression(unittest.TestCase):
    """רגרסיה: TOOLS_DIR מכיל רווחים; מערך פקודה במרכאות לא מתפצל, מחרוזת לא-מצוטטת כן."""

    def test_array_vs_string(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("אין bash")
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "יצירת קישורים וסדר" / "כלי יצירה"
            d.mkdir(parents=True)
            (d / "tool.py").write_text("import sys; print('ok')\n", encoding="utf-8")
            good = 'TOOLS_DIR="$1"; R=(python -X utf8 "$TOOLS_DIR/tool.py"); "${R[@]}"'
            r = subprocess.run([bash, "-c", good, "x", str(d)], capture_output=True)
            self.assertEqual(r.stdout.strip(), b"ok")
            bad = 'TOOLS_DIR="$1"; R="python -X utf8 $TOOLS_DIR/tool.py"; $R'
            r = subprocess.run([bash, "-c", bad, "x", str(d)], capture_output=True)
            self.assertNotEqual(r.stdout.strip(), b"ok")

    def test_push_without_release_skips_not_fails(self):
        wf = Path(__file__).resolve().parents[1] / "workflows" / "build-db.yml"
        text = wf.read_text(encoding="utf-8")
        i = text.index('"$EVENT_NAME" = "push"')
        j = text.index("next-version", i)
        block = text[i:j]
        self.assertIn("skip=true", block)
        self.assertIn("exit 0", block)

    def test_workflow_has_no_unquoted_R(self):
        wf = Path(__file__).resolve().parents[1] / "workflows" / "build-db.yml"
        text = wf.read_text(encoding="utf-8")
        self.assertNotIn("$R ", text)
        self.assertIsNone(re.search(r'(^|\s)R="', text, re.M))


class Lfs(unittest.TestCase):
    def test_pointer_detection(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "ספרים" / "תלמוד בבלי"
            d.mkdir(parents=True)
            (d / "טוב.txt").write_text("שלום\n", encoding="utf-8")
            (d / "מצביע.txt").write_text(
                "version https://git-lfs.github.com/spec/v1\noid sha256:ab\nsize 3\n", encoding="utf-8")
            (d / "ספר.pdf").write_text("version https://git-lfs.github.com/spec/v1\n", encoding="utf-8")
            bad = H.find_lfs_pointers(t)
            self.assertEqual([os.path.basename(b) for b in bad], ["מצביע.txt"])


class Fetch(unittest.TestCase):
    def test_roundtrip_multi_part(self):
        zstd = shutil.which("zstd")
        if not zstd:
            self.skipTest("אין zstd")
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            raw = os.urandom(3000) + b"abc" * 5000
            (t / "x.db").write_bytes(raw)
            subprocess.run([zstd, "-q", "--long=27", str(t / "x.db"), "-o", str(t / "x.zst")], check=True)
            z = (t / "x.zst").read_bytes()
            cut = len(z) // 2
            blobs = {"https://u/1": z[:cut], "https://u/2": z[cut:]}
            parts = [{"url": u, "size": len(b), "sha256": hashlib.sha256(b).hexdigest()} for u, b in blobs.items()]
            m = {"full": {"compression": "zstd", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                          "parts": parts}}
            (t / "m.json").write_text(json.dumps(m), encoding="utf-8")

            def fake(url, dst):
                Path(dst).write_bytes(blobs[url])
            H.fetch_full(t / "m.json", t / "out.db", t / "w", fetch=fake)
            self.assertEqual((t / "out.db").read_bytes(), raw)
            self.assertEqual(list((t / "w").iterdir()), [])

            blobs["https://u/2"] = b"bad" + blobs["https://u/2"][3:]
            with self.assertRaises(H.HelperError):
                H.fetch_full(t / "m.json", t / "out2.db", t / "w", fetch=fake)


class PlainAsset(unittest.TestCase):
    def _setup(self, t, raw=b"sqlite-bytes" * 100):
        t = Path(t)
        (t / "new.db").write_bytes(raw)
        m = {"full": {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}}
        (t / "m.json").write_text(json.dumps(m), encoding="utf-8")
        return t, raw

    def test_name(self):
        self.assertEqual(H.plain_asset_name(12), "otzarya-unofficial-books-12.db")

    def test_ok(self):
        with tempfile.TemporaryDirectory() as t:
            t, raw = self._setup(t)
            r = H.prepare_plain_asset(t / "m.json", t / "new.db", t / "asset", 7)
            self.assertEqual(r["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(Path(r["path"]).name, "otzarya-unofficial-books-7.db")
            self.assertEqual(Path(r["path"]).read_bytes(), raw)
            self.assertTrue((t / "new.db").exists())

    def test_too_big_skips(self):
        with tempfile.TemporaryDirectory() as t:
            t, raw = self._setup(t)
            r = H.prepare_plain_asset(t / "m.json", t / "new.db", t / "asset", 7, max_bytes=len(raw) - 1)
            self.assertIsNone(r["path"])
            self.assertFalse((t / "asset").exists())
            r = H.prepare_plain_asset(t / "m.json", t / "new.db", t / "asset", 7, max_bytes=len(raw))
            self.assertIsNotNone(r["path"])

    def test_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as t:
            t, raw = self._setup(t)
            (t / "new.db").write_bytes(raw[:-1] + b"X")
            with self.assertRaises(H.HelperError):
                H.prepare_plain_asset(t / "m.json", t / "new.db", t / "asset", 7)

    def test_limit_below_2gib(self):
        self.assertLess(H.PLAIN_ASSET_MAX, 2 * (1 << 30))
        self.assertGreater(H.PLAIN_ASSET_MAX, int(1.85 * (1 << 30)))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as t:
            t, raw = self._setup(t)
            rc = H.main(["plain-asset", str(t / "m.json"), str(t / "new.db"), "--dest-dir", str(t / "a"), "--version", "3"])
            self.assertEqual(rc, 0)
            self.assertTrue((t / "a" / "otzarya-unofficial-books-3.db").exists())

    def test_workflow_wiring(self):
        text = (Path(__file__).resolve().parents[1] / "workflows" / "build-db.yml").read_text(encoding="utf-8")
        self.assertIn("plain-asset", text)
        self.assertIn("::warning", text)
        # הנכס מועלה ב-db-v לפני עדכון המניפסט ב-release db
        self.assertLess(text.index("PLAIN_ASSET"), text.index("Update rolling 'db' release"))
        self.assertLess(text.index('gh release upload "$TAG" "$f"'), text.index("Update rolling 'db' release"))


class Summary(unittest.TestCase):
    def test_summary(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "a.zst").write_bytes(b"x" * 2048)
            s = H.format_summary(7, t, "build 125\nזמן עם רווח 5\n", {"commit": "abc"})
            self.assertIn("גרסה 7", s)
            self.assertIn("`a.zst` | 2.00 KB", s)
            self.assertIn("| build | 2:05 |", s)
            self.assertIn("| זמן עם רווח | 0:05 |", s)
            self.assertIn("2:10", s)


if __name__ == "__main__":
    unittest.main()
