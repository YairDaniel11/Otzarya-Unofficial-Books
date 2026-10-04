"""
הגדרות משותפות לכלי הקישורים. אין צורך לערוך את הקוד: מגדירים נתיבים בשורת הפקודה או במשתני סביבה.

  --base <תיקייה>     תיקיית "ספרים" של המאגר (זו שמכילה את "קבצי קישורים וסדר הדורות").  משתנה: LINKS_BOOKS_DIR
  --seforim <קובץ>    קובץ seforim.db של אוצריא (לקריאה בלבד, לבדיקת קיום היעדים).    משתנה: SEFORIM_DB
  --out <תיקייה>      תיקייה לדוחות שהכלים כותבים (ברירת מחדל: output ליד הכלים).       משתנה: LINKS_OUT_DIR

אם לא הוגדר --seforim, הכלי מחפש את seforim.db במקומות הרגילים של אוצריא:
  %APPDATA%\\io.github.kdroidfilter.seforimapp\\databases\\seforim.db
  %PROGRAMDATA%\\otzaria\\books\\seforim.db
  ~/.local/share/io.github.kdroidfilter.seforimapp/databases/seforim.db   (לינוקס)
  ~/Library/Application Support/io.github.kdroidfilter.seforimapp/databases/seforim.db   (מק)
"""
import os
import sys


def _take(flag, env):
    """קורא --flag value או --flag=value מ-sys.argv (ומסיר אותו, כדי שלא יפריע לדגלים של הסקריפטים)."""
    val = os.environ.get(env)
    i = 0
    while i < len(sys.argv):
        a = sys.argv[i]
        if a == flag and i + 1 < len(sys.argv):
            val = sys.argv[i + 1]
            del sys.argv[i:i + 2]
            continue
        if a.startswith(flag + '='):
            val = a.split('=', 1)[1]
            del sys.argv[i]
            continue
        i += 1
    return val


def _find_seforim():
    appdata = os.environ.get('APPDATA', '')
    home = os.path.expanduser('~')
    cands = [
        os.path.join(appdata, 'io.github.kdroidfilter.seforimapp', 'databases', 'seforim.db'),
        os.path.join(os.environ.get('PROGRAMDATA', ''), 'otzaria', 'books', 'seforim.db'),
        os.path.join(home, '.local', 'share', 'io.github.kdroidfilter.seforimapp', 'databases', 'seforim.db'),
        os.path.join(home, 'Library', 'Application Support', 'io.github.kdroidfilter.seforimapp', 'databases', 'seforim.db'),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def _die(msg):
    sys.exit('שגיאת הגדרות: ' + msg)


_base = _take('--base', 'LINKS_BOOKS_DIR')
_sef = _take('--seforim', 'SEFORIM_DB') or _find_seforim()
_out = _take('--out', 'LINKS_OUT_DIR')

if not _base:
    _die('חסרה תיקיית הספרים. הוסף --base "<נתיב לתיקיית ספרים>" (או משתנה הסביבה LINKS_BOOKS_DIR).')
if not os.path.isdir(_base):
    _die('תיקיית הספרים לא קיימת: ' + _base)
if not _sef or not os.path.isfile(_sef):
    _die('לא נמצא seforim.db. הוסף --seforim "<נתיב לקובץ>" (או משתנה הסביבה SEFORIM_DB).')

BASE = os.path.abspath(_base)
LINKS_DIR = os.path.join(BASE, 'קבצי קישורים וסדר הדורות')
SEFORIM_DB = os.path.abspath(_sef)
SEF_URI = 'file:' + SEFORIM_DB.replace('\\', '/') + '?mode=ro'
OUT_DIR = os.path.abspath(_out or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output'))
os.makedirs(OUT_DIR, exist_ok=True)
