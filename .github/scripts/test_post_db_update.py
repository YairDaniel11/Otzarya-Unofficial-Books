import json
import os
import tempfile
import unittest

import post_db_update as p


class PrevShaTest(unittest.TestCase):
    def _manifest(self, notes):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({"release_notes": notes}, f, ensure_ascii=False)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_last_sha_wins(self):
        path = self._manifest("עדכון, קומיט abcdef1 ... ואחר כך קומיט 1234567890abcdef")
        self.assertEqual(p.prev_sha_from_manifest(path), "1234567890abcdef")

    def test_missing_or_bad(self):
        self.assertEqual(p.prev_sha_from_manifest(""), "")
        self.assertEqual(p.prev_sha_from_manifest("/no/such/file.json"), "")
        self.assertEqual(p.prev_sha_from_manifest(self._manifest("בלי קומיט")), "")


class MessageTest(unittest.TestCase):
    def test_footer_with_plain_asset(self):
        f = p.build_footer("o/r", "7", "otzarya-unofficial-books-7.db")
        self.assertIn("https://github.com/o/r/releases/download/db-v7/otzarya-unofficial-books-7.db", f)
        self.assertIn("https://github.com/o/r/releases/tag/db-v7", f)
        self.assertIn("דרך אוצריא", f)
        self.assertIn('נכתב ע"י בוט', f)

    def test_footer_without_plain_asset(self):
        f = p.build_footer("o/r", "7", "")
        self.assertNotIn("releases/download", f)
        self.assertIn("releases/tag/db-v7", f)

    def test_message_no_direct_changes(self):
        m = p.build_message("בוצעו עדכונים טכניים (" + p.NO_CHANGES + ")", "7")
        self.assertIn("גרסה 7", m)
        self.assertIn("קישורים", m)
        self.assertNotIn(p.NO_CHANGES, m)

    def test_message_with_changes(self):
        self.assertIn("נוספו", p.build_message("נוספו ספרים", "7"))


if __name__ == "__main__":
    unittest.main()
