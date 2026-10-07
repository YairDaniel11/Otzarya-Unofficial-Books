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
              rel("vectors-5", [H.OFFICIAL_ASSET], "2026-12-01T00:00:00Z"),
              rel("v32-pre", [H.OFFICIAL_ASSET], "2026-11-01T00:00:00Z", prerelease=True),
              rel("v33-dr", [H.OFFICIAL_ASSET], "2026-11-02T00:00:00Z", draft=True)]
        tag, url, size = H.pick_official(rs)
        self.assertEqual((tag, url, size), ("v31-2", f"https://x/v31-2/{H.OFFICIAL_ASSET}", 5))

    def test_none(self):
        with self.assertRaises(H.HelperError):
            H.pick_official([rel("v28-1", ["seforim.db.zst"])])


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
