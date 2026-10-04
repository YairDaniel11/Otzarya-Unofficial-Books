# -*- coding: utf-8 -*-
"""בדיקה של build_removed_files.py על מאגר git זמני."""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_removed_files as B


def git(repo, *args, t=None):
    env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
    if t:
        env['GIT_COMMITTER_DATE'] = env['GIT_AUTHOR_DATE'] = f'{t} +0000'
    subprocess.run(['git', '-c', 'core.quotePath=false', '-c', 'core.autocrlf=false', *args], cwd=repo, env=env, check=True,
                   capture_output=True)


def write(repo, path, text):
    p = os.path.join(repo, path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(text)


class RemovedFiles(unittest.TestCase):
    def test_history(self):
        with tempfile.TemporaryDirectory() as repo:
            git(repo, 'init', '-q')
            big = lambda n: ('שורה %d של תוכן ארוך מספיק כדי ש-git יזהה העברה\n' % n) * 20
            write(repo, 'ספרים/א/ספר ישן.txt', big(1))
            write(repo, 'ספרים/א/יימחק.txt', big(2))
            write(repo, 'ספרים/א/שרשרת1.txt', big(3))
            write(repo, 'ספרים/א/יחזור.txt', big(4))
            write(repo, 'ספרים/ב/נשאר.txt', big(5))
            git(repo, 'add', '-A'); git(repo, 'commit', '-qm', 'c1', t=1700001000)
            git(repo, 'mv', 'ספרים/א/ספר ישן.txt', 'ספרים/א/ספר חדש.txt')       # שינוי שם
            git(repo, 'rm', '-q', 'ספרים/א/יימחק.txt')                             # מחיקה
            git(repo, 'mv', 'ספרים/א/שרשרת1.txt', 'ספרים/א/שרשרת2.txt')
            git(repo, 'rm', '-q', 'ספרים/א/יחזור.txt')
            git(repo, 'commit', '-qm', 'c2', t=1700002000)
            git(repo, 'mv', 'ספרים/א/שרשרת2.txt', 'ספרים/ב/שרשרת3.txt')           # המשך שרשרת, לתיקייה אחרת
            write(repo, 'ספרים/א/יחזור.txt', big(4))                                # נוסף שוב
            write(repo, 'ספרים/ג/אחר.txt', big(6))
            git(repo, 'add', '-A'); git(repo, 'commit', '-qm', 'c3', t=1700003000)
            write(repo, 'מחוץ/ספרים.txt', 'x')                                      # מחוץ לתיקיית ספרים: לא רלוונטי
            git(repo, 'add', '-A'); git(repo, 'commit', '-qm', 'c4', t=1700004000)
            got = {r['path']: r for r in B.compute(repo)}
            self.assertEqual(got['א/ספר ישן.txt']['to'], 'א/ספר חדש.txt')
            self.assertNotIn('to', got['א/יימחק.txt'])
            self.assertEqual(got['א/שרשרת1.txt']['to'], 'ב/שרשרת3.txt')          # שרשרת A->B->C הצטמצמה ל-A->C
            self.assertEqual(got['א/שרשרת1.txt']['t'], 1700003000)
            self.assertEqual(got['א/שרשרת2.txt']['to'], 'ב/שרשרת3.txt')          # גם שם הביניים נתיב ישן (מי שהוריד באמצע)
            self.assertNotIn('א/יחזור.txt', got)                                     # נמחק ונוסף שוב
            self.assertNotIn('ב/נשאר.txt', got)
            self.assertEqual(len(got), 4)

    def test_chain_ending_in_deletion(self):
        with tempfile.TemporaryDirectory() as repo:
            git(repo, 'init', '-q')
            write(repo, 'ספרים/א/x.txt', ('תוכן ארוך %d\n' % 1) * 30)
            git(repo, 'add', '-A'); git(repo, 'commit', '-qm', 'c1', t=1700001000)
            git(repo, 'mv', 'ספרים/א/x.txt', 'ספרים/א/y.txt'); git(repo, 'commit', '-qm', 'c2', t=1700002000)
            git(repo, 'rm', '-q', 'ספרים/א/y.txt'); git(repo, 'commit', '-qm', 'c3', t=1700003000)
            got = {r['path']: r for r in B.compute(repo)}
            self.assertEqual(set(got), {'א/x.txt', 'א/y.txt'})
            self.assertNotIn('to', got['א/x.txt'])                                   # היעד נמחק: אין "to"


if __name__ == '__main__':
    unittest.main(verbosity=2)
