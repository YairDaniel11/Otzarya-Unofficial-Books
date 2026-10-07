"""בדיקות ל-release_db.py על מסד קטן. הרצה: python -m unittest test_release_db
אם מוגדר KEEP_DIR (נתיב), פלט הבדיקה המלאה נשמר שם לאימות מול הכלי של Dart."""
import json, os, random, shutil, sqlite3, tempfile, unittest
from pathlib import Path

import release_db as r

PREFIX = "https://example.invalid/releases/v2"


def make_db(path, version, edits=0, library_id=r.LIBRARY_ID):
    rnd = random.Random(1)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE line_content(id INTEGER PRIMARY KEY, content BLOB)")
    con.executemany("INSERT INTO line_content VALUES (?,?)",
                    [(i, os.urandom(1) * 0 + bytes(rnd.getrandbits(8) for _ in range(200))) for i in range(1500)])
    for i in range(edits):
        con.execute("UPDATE line_content SET content=? WHERE id=?", (b"edited-%d" % i, i * 7))
    con.executemany("INSERT INTO schema_meta VALUES (?,?)",
                    [("library_id", library_id), ("db_version", str(version))])
    con.commit()
    con.close()


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.key = self.tmp / "k.key"
        self.pub = r.keygen(self.key)
        self.v1, self.v2 = self.tmp / "v1.db", self.tmp / "v2.db"
        make_db(self.v1, 1)
        make_db(self.v2, 2, edits=5)
        for db in (self.v1, self.v2):
            r.inject(db, manifest_url="https://example.invalid/m/manifest.json", public_key=self.pub)
        self.out = self.tmp / "out"

    def release(self, **kw):
        mp = r.pack(self.v2, self.out, PREFIX, notes="הערות", warn=lambda s: None, **kw)
        r.sign(mp, self.key)
        return mp

    def test_keygen_refuses_overwrite(self):
        with self.assertRaises(r.ReleaseError):
            r.keygen(self.key)
        self.assertEqual(r.public_key_of(self.key), self.pub)

    def test_inject_changes_only_meta(self):
        con = sqlite3.connect(self.v1)
        before = con.execute("SELECT * FROM line_content").fetchall()
        con.close()
        r.inject(self.v1, db_version=7, manifest_url="https://example.invalid/x.json", public_key=self.pub)
        meta = r.read_meta(self.v1)
        self.assertEqual((meta["library_id"], meta["db_version"]), (r.LIBRARY_ID, "7"))
        self.assertEqual(meta["update_manifest_url"], "https://example.invalid/x.json")
        con = sqlite3.connect(self.v1)
        self.assertEqual(before, con.execute("SELECT * FROM line_content").fetchall())
        con.close()

    def test_inject_rejects_foreign_library_and_http(self):
        other = self.tmp / "o.db"
        make_db(other, 1, library_id="other")
        with self.assertRaises(r.ReleaseError):
            r.inject(other, db_version=2)
        with self.assertRaises(r.ReleaseError):
            r.inject(self.v1, manifest_url="http://example.invalid/m.json")

    def test_full_roundtrip_single_part(self):
        mp = self.release()
        self.assertTrue(Path(str(mp) + ".sig").exists())
        m = r.verify(mp, self.pub, self.out, db=self.v2)
        self.assertEqual((m["format"], m["db_version"], len(m["full"]["parts"])), (1, 2, 1))
        self.assertEqual(m["full"]["sha256"], r.sha256_file(self.v2))
        self.assertTrue(m["full"]["parts"][0]["url"].startswith(PREFIX + "/otzarya-unofficial-books-2.db.zst"))

    def test_split_parts(self):
        mp = self.release(part_size=50_000)
        m = r.verify(mp, self.pub, self.out)
        names = [p["url"].rsplit("/", 1)[1] for p in m["full"]["parts"]]
        self.assertGreater(len(names), 1)
        self.assertEqual(names[:2], [f"otzarya-unofficial-books-2.db.zst.{i:03d}" for i in (1, 2)])

    def test_delta(self):
        if not r.find_zstd():
            self.skipTest("אין zstd בנתיב")
        mp = self.release(prev_dbs=[self.v1])
        m = r.verify(mp, self.pub, self.out, prev_dbs=[self.v1])
        d = m["delta"][0]
        self.assertEqual((d["from_db_version"], d["compression"]), (1, "zstd-patch"))
        self.assertEqual(d["from_sha256"], r.sha256_file(self.v1))
        self.assertEqual(d["sha256"], m["full"]["sha256"])
        self.assertLess(sum(p["size"] for p in d["parts"]), sum(p["size"] for p in m["full"]["parts"]))
        with self.assertRaises(r.ReleaseError):  # בלי המסד הישן אי אפשר לאמת דלתא
            r.verify(mp, self.pub, self.out)
        if os.environ.get("KEEP_DIR"):
            keep = Path(os.environ["KEEP_DIR"])
            shutil.rmtree(keep, True)
            shutil.copytree(self.out, keep / "out")
            shutil.copy(self.v1, keep / "v1.db")
            shutil.copy(self.v2, keep / "v2.db")

    def test_delta_dropped_when_not_smaller(self):
        if not r.find_zstd():
            self.skipTest("אין zstd בנתיב")
        self.out.mkdir()
        warnings = []
        d = r._pack_delta(self.v1, self.v2, self.out, "b.db", PREFIX + "/", r.LIBRARY_ID, 2,
                          r.PART_SIZE, None, 1, warnings.append, set())  # המלא "קטן" מכל תיקון
        self.assertIsNone(d)
        self.assertTrue(any("נזרקה" in w for w in warnings))
        self.assertFalse(list(self.out.glob("*.patch.zst")))

    def test_delta_input_validation(self):
        newer = self.tmp / "n.db"
        make_db(newer, 3)
        with self.assertRaises(r.ReleaseError):
            r.pack(self.v2, self.out, PREFIX, prev_dbs=[newer])
        with self.assertRaises(r.ReleaseError):
            r.pack(self.v2, self.out, PREFIX, prev_dbs=[self.v1] * 5)

    def test_tampering_detected(self):
        mp = self.release()
        part = next(self.out.glob("*.zst"))
        part.write_bytes(part.read_bytes()[:-1] + b"\x00")
        with self.assertRaises(r.ReleaseError):
            r.verify(mp, self.pub, self.out)
        mp.write_bytes(mp.read_bytes() + b" ")  # שינוי בית במניפסט אחרי החתימה
        with self.assertRaises(r.ReleaseError):
            r.verify(mp, self.pub)

    def test_wrong_key_rejected(self):
        mp = self.release()
        other = r.keygen(self.tmp / "o.key")
        with self.assertRaises(r.ReleaseError):
            r.verify(mp, other)

    def test_pack_requires_https_prefix(self):
        with self.assertRaises(r.ReleaseError):
            r.pack(self.v2, self.out, "http://example.invalid/x")

    def test_cli_release_and_manifest_format(self):
        rc = r.main(["release", "--db", str(self.v2), "--out", str(self.out), "--url-prefix", PREFIX,
                     "--key", str(self.key), "--prev-dbs", str(self.v1), "--notes", "x"])
        self.assertEqual(rc, 0)
        text = (self.out / "manifest.json").read_text("utf-8")
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(list(json.loads(text))[:4], ["format", "library_id", "db_version", "release_notes"])
        self.assertEqual(r.main(["verify", str(self.out / "manifest.json"), "--db", str(self.v2),
                                 "--parts", str(self.out), "--prev-dbs", str(self.v1)]), 0)


if __name__ == "__main__":
    unittest.main()
