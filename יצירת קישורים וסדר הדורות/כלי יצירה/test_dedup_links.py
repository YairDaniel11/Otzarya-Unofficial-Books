"""בדיקת איחוד קישורים כפולים (אותה כותרת מקור ואותו יעד) ב-build_personal_db."""
import sqlite3, unittest
import build_personal_db as b


def make_db():
    db = sqlite3.connect(':memory:')
    db.executescript('''
    CREATE TABLE tocText (id INTEGER PRIMARY KEY, text TEXT);
    CREATE TABLE tocEntry (id INTEGER PRIMARY KEY, bookId INT, parentId INT, textId INT, level INT, lineId INT);
    CREATE TABLE line (id INTEGER PRIMARY KEY, bookId INT, lineIndex INT, content TEXT, tocEntryId INT);
    INSERT INTO tocText VALUES (1,'פרק'),(2,'דף ב.');
    INSERT INTO tocEntry VALUES (10,1,NULL,1,0,1),(11,1,10,2,1,2),(12,1,10,2,1,6);
    ''')
    # שורות 0-1 ו-2-4 תחת דף ב. (ערך 11), שורה 5 תחת ערך 12 בעל אותו טקסט, שורה 6 בלי כותרת
    for i, e in enumerate([10, 11, 11, 11, 11, 12, None]):
        db.execute('INSERT INTO line VALUES (?,?,?,?,?)', (i + 1, 1, i, 'x', e))
    return db


class T(unittest.TestCase):
    def test_same_heading_same_target_once(self):
        d = b.LabelDedup(make_db())
        res = [d.is_dup(1, i, 'עירובין', 'ב.', 5) for i in (1, 2, 3, 4)]
        self.assertEqual(res, [False, True, True, True])

    def test_same_label_text_other_entry_is_dup(self):
        d = b.LabelDedup(make_db())
        self.assertFalse(d.is_dup(1, 1, 'עירובין', 'ב.', 5))
        self.assertTrue(d.is_dup(1, 5, 'עירובין', 'ב.', 5))

    def test_different_target_kept(self):
        d = b.LabelDedup(make_db())
        self.assertFalse(d.is_dup(1, 1, 'עירובין', 'ב.', 5))
        self.assertFalse(d.is_dup(1, 2, 'עירובין', 'ב:', 6))

    def test_no_heading_not_merged(self):
        d = b.LabelDedup(make_db())
        self.assertFalse(d.is_dup(1, 6, 'עירובין', 'ב.', 5))
        self.assertFalse(d.is_dup(1, 6, 'עירובין', 'ב.', 5))


if __name__ == '__main__':
    unittest.main()
