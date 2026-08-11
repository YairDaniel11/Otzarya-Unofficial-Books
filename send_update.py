import os
import subprocess
import sys
import time
import requests
from collections import defaultdict

# --- הגדרות ---
TOPIC_ID = "437"
FORUM_URL = "https://otzaria.org/forum"

# בתוך ספויילר אי אפשר להשתמש ברשימות Markdown ("- ") — הן שוברות אותו,
# ולכן כל שורת ספר נפתחת באימוג'י. שם התיקייה מוצג כמסלול מלא בסוגריים.
# אותו אייקון לשניהם (ריבוע), בגוון שונה: התיקייה כהה, הקבצים בהירים
FOLDER_MARK = "▪️"
BOOK_MARK = "▫️"

MAX_ATTEMPTS = 5          # מספר נסיונות מלאים (CSRF + לוגין + פרסום)
RETRY_DELAYS = [15, 30, 60, 120]   # שניות המתנה בין נסיון לנסיון

INCOMPATIBLE_FOLDER = "ספרים שאינם מותאמים לאוצריא"

def book_name(filepath):
    return os.path.splitext(os.path.basename(filepath))[0]

ROOT_LABEL = "תיקייה ראשית"


def folder_of(filepath):
    """המסלול המלא של התיקייה מתוך 'ספרים/' — כדי שלא ימוזגו תיקיות
    שונות בעלות אותו שם (למשל 'בבא מציעא' של שני מחברים)."""
    parts = filepath[len("ספרים/"):].split('/')
    return '/'.join(parts[:-1]) if len(parts) >= 2 else ROOT_LABEL

def is_incompatible(filepath):
    parts = filepath[len("ספרים/"):].split('/')
    return parts[0] == INCOMPATIBLE_FOLDER

def _find_last_user_diff_cmd():
    """מוצא את ה-commit האחרון שנגע בקבצי ספרים/ ומחזיר פקודת diff עבורו."""
    try:
        log_out = subprocess.check_output(
            ["git", "log", "--format=%H", "-50", "--", "ספרים/"],
            text=True, encoding="utf-8"
        )
    except subprocess.CalledProcessError:
        return ["git", "diff", "--name-status", "-M90", "HEAD~1", "HEAD"]

    for sha in log_out.strip().split('\n'):
        sha = sha.strip()
        if sha:
            return ["git", "diff", "--name-status", "-M90", f"{sha}^", sha]

    return ["git", "diff", "--name-status", "-M90", "HEAD~1", "HEAD"]


def get_changed_books():
    before_sha = (os.environ.get("BEFORE_SHA") or "").strip()
    after_sha = (os.environ.get("AFTER_SHA") or "").strip()

    if before_sha and after_sha and before_sha != "0000000000000000000000000000000000000000":
        # SHA שגוי לא יפול בשקט לטווח אחר — עדיף להיכשל מיד
        for sha in (before_sha, after_sha):
            if subprocess.call(["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
                print(f"שגיאה: הקומיט {sha} לא קיים במאגר.")
                sys.exit(1)
        git_cmd = ["git", "diff", "--name-status", "-M90", before_sha, after_sha]
    else:
        # workflow_dispatch או ריצה ראשונה: מצא את ה-commit האחרון שאינו אוטומטי
        git_cmd = _find_last_user_diff_cmd()

    try:
        output = subprocess.check_output(git_cmd, text=True, encoding="utf-8")
    except subprocess.CalledProcessError:
        try:
            output = subprocess.check_output(
                ["git", "diff", "--name-status", "-M90", "HEAD~1", "HEAD"],
                text=True, encoding="utf-8"
            )
        except subprocess.CalledProcessError:
            return None

    added = defaultdict(list)       # folder -> [(name, incompatible)]
    modified = defaultdict(list)
    deleted = defaultdict(list)
    renamed = defaultdict(list)     # folder -> [(old_name, new_name, incompatible)]
    moved = []                       # [(name, old_folder, new_folder, incompatible)]

    for line in output.strip().split('\n'):
        if not line:
            continue
        parts = line.split('\t')
        status = parts[0]

        # העברה/שינוי שם בין תיקיות
        if status.startswith('R') and len(parts) == 3:
            old_path, new_path = parts[1], parts[2]
            if not old_path.startswith("ספרים/") and not new_path.startswith("ספרים/"):
                continue
            old_folder = folder_of(old_path) if old_path.startswith("ספרים/") else "?"
            new_folder = folder_of(new_path) if new_path.startswith("ספרים/") else "?"
            name = book_name(new_path if new_path.startswith("ספרים/") else old_path)
            inc = is_incompatible(new_path) if new_path.startswith("ספרים/") else is_incompatible(old_path)
            if old_folder != new_folder:
                moved.append((name, old_folder, new_folder, inc))
            else:
                # שינוי שם בתוך אותה תיקייה — סעיף נפרד, התוכן לא השתנה
                renamed[new_folder].append((book_name(old_path), name, inc))
            continue

        if len(parts) < 2:
            continue
        filepath = parts[1]
        if not filepath.startswith("ספרים/"):
            continue

        folder = folder_of(filepath)
        name = book_name(filepath)
        inc = is_incompatible(filepath)

        if status.startswith('A'):
            added[folder].append((name, inc))
        elif status.startswith('M'):
            modified[folder].append((name, inc))
        elif status.startswith('D'):
            deleted[folder].append((name, inc))

    def render_folders(by_folder):
        """שם התיקייה שבה הקבצים ככותרת מודגשת, הנתיב אליה בסוגריים,
        ומתחתיה הקבצים שבתוכה."""
        lines = ""
        for folder in sorted(by_folder):
            if folder != ROOT_LABEL:
                parts = folder.split('/')
                name, path = parts[-1], '/'.join(parts[:-1])
                lines += f"{FOLDER_MARK} **{name}**" + (f" ({path})" if path else "") + "\n"
            for leaf in sorted(by_folder[folder]):
                lines += f"{BOOK_MARK} {leaf}\n"
        return lines

    def format_groups(by_folder_compatible, by_folder_incompatible):
        lines = ""
        for label, group in [("(ספרים מותאמים לאוצריא)", by_folder_compatible),
                             ("(ספרים שאינם מותאמים לאוצריא)", by_folder_incompatible)]:
            if not group:
                continue
            lines += f"**{label}**\n"
            lines += render_folders(group)
            lines += "\n"
        return lines

    def format_book_list(books_dict):
        compatible   = {f: [b for b, i in books if not i] for f, books in books_dict.items()}
        incompatible = {f: [b for b, i in books if i]     for f, books in books_dict.items()}
        return format_groups({f: bs for f, bs in compatible.items() if bs},
                             {f: bs for f, bs in incompatible.items() if bs})

    def format_renamed(renamed_dict):
        compatible   = {f: [f"{old} ← {new}" for old, new, i in items if not i] for f, items in renamed_dict.items()}
        incompatible = {f: [f"{old} ← {new}" for old, new, i in items if i]     for f, items in renamed_dict.items()}
        return format_groups({f: bs for f, bs in compatible.items() if bs},
                             {f: bs for f, bs in incompatible.items() if bs})

    def format_moved(moved_list):
        compatible   = [(n, o, nf) for n, o, nf, i in moved_list if not i]
        incompatible = [(n, o, nf) for n, o, nf, i in moved_list if i]
        lines = ""
        for label, group in [("(ספרים מותאמים לאוצריא)", compatible), ("(ספרים שאינם מותאמים לאוצריא)", incompatible)]:
            if not group:
                continue
            lines += f"**{label}**\n"
            for name, old_f, new_f in sorted(group):
                lines += f"{BOOK_MARK} {name}: {old_f} ← {new_f}\n"
            lines += "\n"
        return lines

    def count_of(books_dict):
        return sum(len(v) for v in books_dict.values())

    def amount(n, singular, plural):
        return f"{singular} אחד" if n == 1 else f"{n} {plural}"

    def section(title, body):
        """כותרת עם מספרים + הפירוט המלא בתוך ספויילר
        (שורה ריקה לפניו, || לפני התוכן ו-|| אחריו)."""
        body = body.strip()
        if not body:
            return ""
        return f"### **{title}**\nהפירוט המלא בספויילר\n\n||\n{body}\n||\n\n"

    msg = ""
    if added:
        msg += section(f"נוספו למאגר - {amount(count_of(added), 'ספר', 'ספרים')}",
                       format_book_list(added))
    if moved:
        msg += section(f"הועברו בין תיקיות - {amount(len(moved), 'קובץ', 'קבצים')}",
                       format_moved(moved))
    if renamed:
        msg += section(f"שונה שם - {amount(count_of(renamed), 'קובץ', 'קבצים')}",
                       format_renamed(renamed))
    if modified:
        msg += section(f"עודכנו במאגר - {amount(count_of(modified), 'ספר', 'ספרים')}",
                       format_book_list(modified))
    if deleted:
        msg += section(f"הוסרו מהמאגר - {amount(count_of(deleted), 'ספר', 'ספרים')}",
                       format_book_list(deleted))

    return msg.strip() if msg else "בוצעו עדכונים טכניים במאגר (לא נמצאו שינויים ישירים בספרים)."


class ForumError(Exception):
    """שגיאה בתקשורת עם הפורום. retryable=True → כדאי לנסות שוב."""
    def __init__(self, message, retryable):
        super().__init__(message)
        self.retryable = retryable


def _try_post_to_nodebb(message, username, password):
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json'
    })

    def request(method, url, **kwargs):
        try:
            return session.request(method, url, timeout=60, **kwargs)
        except requests.RequestException as e:
            # תקלות רשת/TLS/timeout הן כמעט תמיד רגעיות
            raise ForumError(f"תקלת תקשורת: {e}", retryable=True)

    print(f"1. מתחבר ל-{FORUM_URL} כדי למשוך CSRF Token...")
    config_res = request('GET', f"{FORUM_URL}/api/config")
    if config_res.status_code != 200:
        # 5xx (כולל 502/503/525 של Cloudflare) ו-429 — תקלות שרת רגעיות
        retryable = config_res.status_code >= 500 or config_res.status_code == 429
        raise ForumError(f"שגיאה בגישה לשרת ({config_res.status_code})", retryable=retryable)

    try:
        csrf_token = config_res.json().get('csrf_token')
    except ValueError:
        raise ForumError("תשובת /api/config אינה JSON תקין", retryable=True)
    if not csrf_token:
        raise ForumError("לא נמצא אסימון אבטחה!", retryable=True)

    print("2. מבצע לוגין עם השם והסיסמה של הבוט...")
    session.headers.update({'x-csrf-token': csrf_token})
    login_res = request('POST', f"{FORUM_URL}/login", data={
        'username': username,
        'password': password,
        '_csrf': csrf_token
    })

    if login_res.status_code != 200:
        # 401/403 = פרטי התחברות שגויים → אין טעם לנסות שוב
        retryable = login_res.status_code >= 500 or login_res.status_code == 429
        raise ForumError(f"שגיאת התחברות (סטטוס {login_res.status_code}).", retryable=retryable)

    print(f"3. שולח את העדכון לנושא {TOPIC_ID}...")
    post_res = request('POST', f"{FORUM_URL}/api/v3/topics/{TOPIC_ID}", json={"content": message})

    if post_res.status_code != 200:
        retryable = post_res.status_code >= 500 or post_res.status_code == 429
        raise ForumError(
            f"שגיאה בעת פרסום ההודעה (סטטוס {post_res.status_code}): {post_res.text[:500]}",
            retryable=retryable
        )

    print("ההודעה פורסמה בהצלחה בפורום אוצריא!")


def post_to_nodebb(message):
    """מפרסם את ההודעה בפורום. זורק ForumError אם כל הנסיונות נכשלו."""
    username = os.environ.get("USER_NAME")
    password = os.environ.get("PASSWORD")

    if not username or not password:
        raise ForumError("חסרים שם משתמש או סיסמה בסודות של גיטאב.", retryable=False)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            print(f"\n--- נסיון {attempt} מתוך {MAX_ATTEMPTS} ---")
        try:
            _try_post_to_nodebb(message, username, password)
            return
        except ForumError as e:
            print(f"נסיון {attempt} נכשל: {e}")
            if not e.retryable:
                raise
            if attempt == MAX_ATTEMPTS:
                raise
            delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
            print(f"ממתין {delay} שניות ומנסה שוב...")
            time.sleep(delay)


if __name__ == "__main__":
    repo = os.environ.get("GITHUB_REPOSITORY", "")

    changes_text = get_changed_books()

    if changes_text is None:
        print("שגיאה: לא הצלחתי לקבל רשימת שינויים - לא מפרסם פוסט.")
        sys.exit(1)

    if "לא נמצאו שינויים ישירים בספרים" not in changes_text:
        final_post = (
            changes_text
            + '\n\n---\nניתן להוריד באמצעות התוסף "[הורדת מאגר גיטאב](https://otzaria.org/plugins/6a0081ae54ae49eaed8d6a73)"\n'
            + f'או מ-[עמוד ה-Releases](https://github.com/{repo}/releases/latest).\n\n'
            + '**פוסט זה נכתב ע"י בוט**'
        )
        try:
            post_to_nodebb(final_post)
        except ForumError as e:
            print(f"\n::error::הפוסט לא פורסם בפורום: {e}")
            sys.exit(1)
    else:
        print("הריצה הסתיימה: לא זוהו שינויים בקבצי הספרים, לכן לא פורסם פוסט בפורום.")
