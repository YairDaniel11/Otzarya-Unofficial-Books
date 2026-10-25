"""בדיקות לדירוג לפי סדר.txt ב-build_personal_db (python -X utf8 test_order.py)."""
import os, random, tempfile, unittest
from build_personal_db import order_key, collect_books

S = os.sep
PATHS = [S.join(p) for p in [
    ['א ב', 'x.txt'], ['א', 'x.txt'], ['א', 'y z.txt'], ['א', 'y.txt'], ['א', 'ב', 'q.txt'],
    ['ג', 'a.txt'], ['ג', 'a b.txt'], ['ג', 'a', 'k.txt'], ['01 תורה', 'n.txt'], ['ב.txt'],
]]


class T(unittest.TestCase):
    def test_no_order_equals_plain_sort(self):
        for _ in range(20):
            ps = PATHS[:]
            random.shuffle(ps)
            self.assertEqual(sorted(ps, key=lambda p: order_key(p, {})), sorted(ps))

    def test_order_applied(self):
        orders = {(): ['ג', 'א'], ('א',): ['y', 'ב']}
        got = sorted(PATHS, key=lambda p: order_key(p, orders))
        self.assertEqual([p for p in got if p.startswith('ג')], sorted(p for p in PATHS if p.startswith('ג')))
        self.assertLess(got.index(S.join(['ג', 'a.txt'])), got.index(S.join(['א', 'x.txt'])))
        self.assertLess(got.index(S.join(['א', 'y.txt'])), got.index(S.join(['א', 'ב', 'q.txt'])))
        self.assertLess(got.index(S.join(['א', 'ב', 'q.txt'])), got.index(S.join(['א', 'x.txt'])))
        # פריטים שלא ברשימה (א ב, 01 תורה) אחרי הרשימה
        self.assertGreater(got.index(S.join(['א ב', 'x.txt'])), got.index(S.join(['א', 'x.txt'])))

    def test_collect_excludes_order_file(self):
        with tempfile.TemporaryDirectory() as r:
            os.makedirs(os.path.join(r, 'ד'))
            for n in ['ד/א.txt', 'ד/ב.txt', 'ד/סדר.txt']:
                open(os.path.join(r, n), 'w', encoding='utf-8').write('x\n' if 'סדר' not in n else 'ב\nא\n')
            got = [os.path.basename(p) for p in collect_books(r, os.path.join(r, 'ד'))]
            self.assertEqual(got, ['ב.txt', 'א.txt'])


if __name__ == '__main__':
    unittest.main()
