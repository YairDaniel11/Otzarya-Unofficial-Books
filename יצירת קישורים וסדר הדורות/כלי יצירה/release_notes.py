"""בניית release_notes בעברית (טקסט פשוט) משינויי הספרים בין שני קומיטים.
שימוש: python release_notes.py --repo ROOT --since SHA --head SHA --version N [--prev-manifest manifest.json]
בכל מקרה של אי-ודאות מודפס טקסט גנרי בטוח. אין תלות חיצונית."""
import argparse, json, re, subprocess, sys
from collections import Counter, OrderedDict
from pathlib import PurePosixPath

BOOKS = "ספרים"
IGNORE_DIR = "ספרים/קבצי קישורים וסדר הדורות/"
SOFT = 600
HARD = 2000
MAX_NAMES = 5
SHA_RE = re.compile(r"קומיט\s+([0-9a-f]{7,40})")
PART_RE = re.compile(r"[,\s]+חלק\s+\S+$")


def git(repo, *args):
    p = subprocess.run(["git", "-c", "core.quotePath=false", "-C", repo, *args],
                       capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace"))
    return p.stdout.decode("utf-8", "replace")


def sha_from_notes(text):
    m = SHA_RE.findall(text or "")
    return m[-1] if m else ""


def relevant(path):
    return (path.startswith(BOOKS + "/") and path.endswith(".txt")
            and not path.startswith(IGNORE_DIR) and "/links/" not in path)


def parse_diff(out):
    toks = out.split("\0")
    i, res = 0, []
    while i < len(toks) and toks[i]:
        st = toks[i]
        if st[0] in "RC":
            res.append((st[0], toks[i + 1], toks[i + 2]))
            i += 3
        else:
            res.append((st[0], toks[i + 1], None))
            i += 2
    return res


def name_of(p):
    return PurePosixPath(p).name[:-4]


GENERIC_DIRS = {"ספרים", "מאמרים", "נספחים", "אחרונים", "ראשונים", "מחברי זמננו"}


def folder_of(p):
    """שם תיקייה משמעותי: עולים מעל תיקיות גנריות (ספרים/מאמרים/אחרונים...)."""
    parts = PurePosixPath(p).parent.parts
    for name in reversed(parts):
        if name not in GENERIC_DIRS:
            return name
    return parts[-1] if parts else ""


def base_title(name):
    """איחוד חלקים: 'טהרת הבית, חלק א' -> 'טהרת הבית'."""
    return PART_RE.sub("", name) or name


def uniq(seq):
    return list(OrderedDict.fromkeys(seq))


def detail_names(names):
    """שמות לתצוגה: קיבוץ לפי קידומת ' - ' כשיש בה 2 ספרים ומעלה."""
    groups = OrderedDict()
    for n in names:
        groups.setdefault(n.split(" - ")[0] if " - " in n else None, []).append(n)
    parts = []
    for pre, items in groups.items():
        if pre is None:
            parts.extend(items)
            continue
        segs = [n.split(" - ") for n in items]
        det = [s[2] if len(s) >= 3 else s[1] for s in segs]
        cnt = Counter(det)
        det = [f"{d} {s[1]}" if cnt[d] > 1 and len(s) >= 3 else d for d, s in zip(det, segs)]
        parts.append(f"{pre} ({', '.join(det)})")
    return "; ".join(parts)


def grouped(entries, level):
    """קיבוץ לפי תיקיית יעד, עם 'ועוד N'."""
    c = Counter(e["dest"] for e in entries)
    ordered = sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))
    keep = max(1, 6 - level)
    rest = sum(n for _, n in ordered[keep:])
    s = ", ".join(f"{k} {n}" for k, n in ordered[:keep])
    return s + (f" ועוד {rest}" if rest else "")


def short_reason(body):
    for ln in body.splitlines():
        m = re.match(r"\s*סיבה\s*:\s*(.+)", ln)
        if m:
            r = re.split(r"\s+[-–—]\s+|[.;:]", m.group(1).strip())[0].strip()
            if len(r) > 30:
                r = r[:30].rsplit(" ", 1)[0]
            return r
    return ""


def deletion_reason(repo, since, head, path):
    try:
        out = git(repo, "log", "--diff-filter=D", "-n1", "--format=%B",
                  f"{since}..{head}", "--", path)
    except RuntimeError:
        return ""
    return short_reason(out)


def build_lines(groups, level):
    out = []
    for label, entries in groups:
        if not entries:
            continue
        names = sorted(uniq(e["title"] for e in entries))
        total = len(entries) if label in ("עודכנו", "הועברו", "שונה שם") else len(names)
        if label != "הועברו" and len(names) <= MAX_NAMES and level == 0:
            if label == "נמחקו":
                body = "; ".join(uniq(e["text"] for e in entries))
            else:
                body = detail_names(names)
        else:
            body = grouped(entries, level)
        out.append(f"{label} ({total}): {body}")
    return out


def generic(version, sha):
    t = f"מה חדש - גרסה {version}\nעדכון אוטומטי של הספרייה"
    return t + (f"\nקומיט {sha[:7]}" if sha else "")


def cut(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;( ")


def make_notes(repo, since, head, version):
    if not since:
        return generic(version, head)
    try:
        git(repo, "rev-parse", "--verify", "-q", f"{since}^{{commit}}")
        out = git(repo, "diff", "--name-status", "-z", "-M", "--find-renames=90%",
                  f"{since}..{head}", "--", BOOKS)
    except RuntimeError:
        return generic(version, head)
    added, mod, moved, renamed, deleted = [], [], [], [], []

    def dele(p):
        r = deletion_reason(repo, since, head, p)
        n = name_of(p)
        deleted.append({"title": n, "dest": folder_of(p), "text": f"{n} ({r})" if r else n})

    for st, a, b in parse_diff(out):
        if st == "A" and relevant(a):
            added.append({"title": base_title(name_of(a)), "dest": folder_of(a)})
        elif st == "M" and relevant(a):
            mod.append({"title": name_of(a), "dest": folder_of(a)})
        elif st == "D" and relevant(a):
            dele(a)
        elif st == "R" and (relevant(a) or relevant(b)):
            if not relevant(b):
                dele(a)
            elif not relevant(a):
                added.append({"title": base_title(name_of(b)), "dest": folder_of(b)})
            elif name_of(a) == name_of(b):
                moved.append({"title": name_of(b), "dest": folder_of(b)})
            else:
                renamed.append({"title": f"{name_of(a)} ← {name_of(b)}", "dest": folder_of(b)})
    groups = [("נוספו", added), ("עודכנו", mod), ("הועברו", moved),
              ("שונה שם", renamed), ("נמחקו", deleted)]
    header = f"מה חדש - גרסה {version}"
    footer = f"קומיט {head[:7]}"
    if not any(e for _, e in groups):
        return "\n".join([header, "עדכון אוטומטי של הספרייה", footer])
    for level in range(0, 6):
        text = "\n".join([header, *build_lines(groups, level), footer])
        if len(text) <= SOFT:
            return text
    body = [cut(x, 400) for x in build_lines(groups, 5)]
    text = "\n".join([header, *body, footer])
    while len(text) > HARD and body:
        body.pop()
        text = "\n".join([header, *body, footer])
    return text


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--since", default="")
    ap.add_argument("--head", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--prev-manifest", help="manifest.json קודם: ממנו נשלף ה-sha אם --since ריק")
    a = ap.parse_args(argv)
    since = a.since
    if not since and a.prev_manifest:
        try:
            with open(a.prev_manifest, encoding="utf-8") as f:
                since = sha_from_notes(json.load(f).get("release_notes"))
        except Exception:
            since = ""
    try:
        text = make_notes(a.repo, since, a.head, a.version)
    except Exception:
        text = generic(a.version, a.head)
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    main()
