"""פרסום שבועי בפורום (שרשור עדכוני ה-DB) אחרי שנבנה ופורסם מסד אוצריא האישי.

רץ כשלב אחרון ב-build-db.yml. משתמש באותו פורמט של הפרסומים של send_update.py (שינויי הספרים
בין הקומיט של ה-DB הקודם לקומיט של ה-DB החדש), בתוספת קישור להורדת קובץ ה-DB והערה למי שכבר
מותקן אצלו המסד. משתני סביבה:
  VERSION           מספר גרסת ה-DB החדשה (חובה)
  GITHUB_SHA        הקומיט שממנו נבנה ה-DB (חובה)
  GITHUB_REPOSITORY owner/repo
  PREV_MANIFEST     נתיב ל-manifest.json של ה-DB הקודם (משם נשלף הקומיט הקודם; אופציונלי)
  PLAIN_NAME        שם נכס ה-DB הלא דחוס (אם קיים), לקישור הורדה ישיר
  DB_TOPIC_ID       מספר השרשור בפורום (ברירת מחדל: 3050)
  DRY_RUN           'true' = להדפיס את הפוסט לקובץ/יומן בלי לפרסם
  USER_NAME, PASSWORD  פרטי הבוט בפורום (כמו ב-send_update.py)
"""
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

DEFAULT_TOPIC_ID = "3050"
SHA_RE = re.compile(r"קומיט\s+([0-9a-f]{7,40})")
NO_CHANGES = "לא נמצאו שינויים ישירים בספרים"


def prev_sha_from_manifest(path):
    """הקומיט שמתועד בהערות הגרסה של ה-manifest הקודם ('קומיט <sha>'), או ''."""
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            notes = json.load(f).get("release_notes") or ""
    except (OSError, ValueError):
        return ""
    found = SHA_RE.findall(notes)
    return found[-1] if found else ""


def build_footer(repo, version, plain_name):
    release_url = f"https://github.com/{repo}/releases/tag/db-v{version}"
    if plain_name:
        direct = f"https://github.com/{repo}/releases/download/db-v{version}/{plain_name}"
        download = (f"[הורדת קובץ המסד (גרסה {version})]({direct}), "
                    f"או מ-[עמוד ההוצאה]({release_url})")
    else:
        download = f"[הורדת המסד (גרסה {version}) מעמוד ההוצאה]({release_url})"
    return ("\n\n---\n"
            f"**הורדה:** {download}.\n\n"
            "מי שכבר מותקן אצלו המסד יכול לעדכן אותו ישירות דרך אוצריא, בלי להוריד את הקובץ.\n\n"
            '**פוסט זה נכתב ע"י בוט**')


def build_message(changes_text, version):
    head = f"**עדכון מסד הנתונים האישי - גרסה {version}**\n\n"
    if not changes_text or NO_CHANGES in changes_text:
        body = "בגרסה זו עודכנו הקישורים, סדר הספרים או נתוני המסד (בלי שינויים ישירים בקובצי הספרים)."
    else:
        body = changes_text
    return head + body


def _gh(*args):
    import subprocess
    return subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")


def resolve_latest_release(workdir):
    """מצב post_only: ה-DB האחרון שפורסם (db-v<N>). מחזיר (גרסה, sha, נתיב manifest קודם, שם נכס DB) או None."""
    r = _gh("release", "list", "--limit", "200", "--json", "tagName", "-q", ".[].tagName")
    versions = sorted(int(m.group(1)) for t in r.stdout.split()
                      for m in [re.fullmatch(r"db-v(\d+)", t)] if m)
    if not versions:
        print("::error::לא נמצא release db-v<N>")
        return None
    cur = versions[-1]
    cur_dir = os.path.join(workdir, "cur")
    os.makedirs(cur_dir, exist_ok=True)
    if _gh("release", "download", f"db-v{cur}", "-p", "manifest.json", "-D", cur_dir, "--clobber").returncode:
        print(f"::error::אין manifest.json ב-db-v{cur}")
        return None
    after = ""
    try:
        with open(os.path.join(cur_dir, "manifest.json"), encoding="utf-8") as f:
            found = SHA_RE.findall(json.load(f).get("release_notes") or "")
        after = found[-1] if found else ""
    except (OSError, ValueError):
        pass
    if not after:
        print(f"::error::אין קומיט בהערות הגרסה של db-v{cur}")
        return None
    prev_manifest = ""
    prev_dir = os.path.join(workdir, "prev")
    os.makedirs(prev_dir, exist_ok=True)
    if cur - 1 in versions and not _gh("release", "download", f"db-v{cur - 1}", "-p", "manifest.json",
                                       "-D", prev_dir, "--clobber").returncode:
        prev_manifest = os.path.join(prev_dir, "manifest.json")
    assets = _gh("release", "view", f"db-v{cur}", "--json", "assets", "-q", ".assets[].name").stdout.split()
    plain = next((a for a in assets if re.fullmatch(r"otzarya-unofficial-books-\d+\.db", a)), "")
    return str(cur), after, prev_manifest, plain


def main():
    if (os.environ.get("POST_ONLY") or "").strip().lower() == "true":
        import tempfile
        res = resolve_latest_release(tempfile.mkdtemp())
        if not res:
            return 1
        os.environ["VERSION"], os.environ["GITHUB_SHA"], os.environ["PREV_MANIFEST"], os.environ["PLAIN_NAME"] = res
        print(f"מצב post_only: גרסה {res[0]}, קומיט {res[1][:7]}")
    version = (os.environ.get("VERSION") or "").strip()
    after = (os.environ.get("GITHUB_SHA") or "").strip()
    if not version or not after:
        print("::error::חסרים VERSION או GITHUB_SHA")
        return 1
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    dry = (os.environ.get("DRY_RUN") or "").strip().lower() == "true"
    topic = (os.environ.get("DB_TOPIC_ID") or "").strip() or DEFAULT_TOPIC_ID

    import send_update as su
    su.TOPIC_ID = topic

    before = prev_sha_from_manifest(os.environ.get("PREV_MANIFEST"))
    changes = ""
    if before:
        os.environ["BEFORE_SHA"] = before
        os.environ["AFTER_SHA"] = after
        changes = su.get_changed_books()
        if changes is None:
            print("::warning::לא הצלחתי לחשב רשימת שינויים; הפוסט יפורסם בלי רשימה")
            changes = ""
    else:
        print("::notice::אין קומיט קודם ב-manifest; הפוסט יפורסם בלי רשימת שינויים")

    footer = build_footer(repo, version, (os.environ.get("PLAIN_NAME") or "").strip())
    text = build_message(changes, version)
    parts = su.split_post(text, su.MAX_POST_CHARS - len(footer) - 100)
    posts = []
    for i, part in enumerate(parts, 1):
        head = f"**חלק {i} מתוך {len(parts)}**\n\n" if len(parts) > 1 else ""
        posts.append(head + part + footer)

    out = os.environ.get("POST_PREVIEW_FILE")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(("\n\n=====\n\n").join(posts))
    print(f"נוצרו {len(posts)} פוסטים לשרשור {topic} ({[len(p) for p in posts]} תווים); dry_run={dry}")
    if dry:
        return 0
    try:
        for i, post in enumerate(posts):
            if i:
                time.sleep(su.POST_GAP_SECONDS)
            su.post_to_nodebb(post)
    except su.ForumError as e:
        print(f"::error::הפוסט לא פורסם בפורום: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
