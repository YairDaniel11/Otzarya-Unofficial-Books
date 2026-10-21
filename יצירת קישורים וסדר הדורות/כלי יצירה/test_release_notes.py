"""בדיקות ל-release_notes.py על מאגר git זמני. הרצה: python -m unittest test_release_notes"""
import json, shutil, subprocess, tempfile, unittest
from pathlib import Path

import release_notes as rn


def git(repo, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo), *a],
                          check=True, capture_output=True).stdout.decode("utf-8").strip()


class NotesTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        git(self.repo, "init", "-q")

    def put(self, rel, text="x"):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text * 50 + "\n", encoding="utf-8")

    def commit(self, msg):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", msg)
        return git(self.repo, "rev-parse", "HEAD")

    def notes(self, since, head, v="7"):
        return rn.make_notes(str(self.repo), since, head, v)

    def test_all_groups(self):
        self.put("ספרים/א/ספר ישן.txt", "a")
        self.put("ספרים/א/לעדכון.txt", "b")
        self.put("ספרים/א/להעביר.txt", "c")
        self.put("ספרים/א/לשינוי שם.txt", "d")
        self.put("ספרים/א/למחוק.txt", "e")
        base = self.commit("base")
        self.put("ספרים/ב/חדש, חלק א.txt", "n")
        self.put("ספרים/ב/חדש, חלק ב.txt", "m")
        self.put("ספרים/קבצי קישורים וסדר הדורות/דורות.csv", "z")
        self.put("ספרים/א/links/x_links.json", "z")
        self.put("ספרים/א/לעדכון.txt", "bb")
        (self.repo / "ספרים/ג").mkdir()
        shutil.move(self.repo / "ספרים/א/להעביר.txt", self.repo / "ספרים/ג/להעביר.txt")
        shutil.move(self.repo / "ספרים/א/לשינוי שם.txt", self.repo / "ספרים/א/שם חדש.txt")
        self.commit("שינויים")
        (self.repo / "ספרים/א/למחוק.txt").unlink()
        head = self.commit("מחיקה\n\nסיבה: כפילות - יש כבר בתיקייה אחרת")
        t = self.notes(base, head)
        lines = t.split("\n")
        self.assertEqual(lines[0], "מה חדש - גרסה 7")
        self.assertIn("נוספו (1): חדש", lines)
        self.assertIn("עודכנו (1): לעדכון", lines)
        self.assertIn("הועברו (1): ג", t)
        self.assertIn("שונה שם (1): לשינוי שם ← שם חדש", t)
        self.assertIn("נמחקו (1): למחוק (כפילות)", lines)
        self.assertEqual(lines[-1], "קומיט " + head[:7])
        self.assertNotIn("דורות", t)

    def test_empty_groups_hidden(self):
        self.put("ספרים/א/ס.txt")
        base = self.commit("base")
        self.put("ספרים/א/חדש.txt")
        head = self.commit("add")
        t = self.notes(base, head)
        self.assertNotIn("עודכנו", t)
        self.assertNotIn("נמחקו", t)

    def test_many_grouped(self):
        self.put("ספרים/ס.txt")
        base = self.commit("base")
        for i in range(8):
            self.put(f"ספרים/רמח״ל/ספרים/ר{i}.txt")
        for i in range(3):
            self.put(f"ספרים/ברסלב/ב{i}.txt")
        head = self.commit("add")
        t = self.notes(base, head)
        self.assertIn("נוספו (11): רמח״ל 8, ברסלב 3", t)

    def test_length_cut(self):
        self.put("ספרים/ס.txt")
        base = self.commit("base")
        for i in range(120):
            self.put(f"ספרים/תיקייה{i:03d}/ספר מספר {i}.txt")
        head = self.commit("add")
        t = self.notes(base, head)
        self.assertLessEqual(len(t), rn.SOFT + 50)
        self.assertTrue(t.endswith("קומיט " + head[:7]))
        self.assertIn("ועוד", t)

    def test_unknown_since(self):
        self.put("ספרים/ס.txt")
        head = self.commit("base")
        self.assertEqual(self.notes("", head), f"מה חדש - גרסה 7\nעדכון אוטומטי של הספרייה\nקומיט {head[:7]}")
        self.assertIn("עדכון אוטומטי", self.notes("deadbeef", head))

    def test_sha_parsing(self):
        self.assertEqual(rn.sha_from_notes("עדכון אוטומטי, גרסה 3, קומיט d7d7081, 01.01.2026"), "d7d7081")
        self.assertEqual(rn.sha_from_notes("מה חדש - גרסה 4\nקומיט abcdef1"), "abcdef1")
        self.assertEqual(rn.sha_from_notes(""), "")
        self.assertEqual(rn.sha_from_notes(None), "")


if __name__ == "__main__":
    unittest.main()
