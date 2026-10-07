# -*- coding: utf-8 -*-
"""כלי עזר ל-.github/workflows/build-db.yml (בניית מסד אוצריא האישי ופרסומו).

ספריית תקן בלבד, כדי שיריץ בכל שלב ב-CI בלי התקנות. פקודות:
  pick-official   releases.json            -> שורת פלט: תג<TAB>כתובת<TAB>גודל
  next-version    [--prev N] [--start N]   -> מספר הגרסה הבאה
  select-prev     --tags ... --current N   -> תגי db-vK של הגרסאות הקודמות (לדלתאות)
  orphan-tags     --tags ... --pinned N    -> תגי db-vK יתומים (גרסה > pinned)
  prune-tags      --tags ... --keep N      -> תגי db-vK למחיקה
  check-lfs       ROOT                     -> נכשל אם נותרו קבצי-מצביע של git-lfs
  fetch-full      MANIFEST OUT             -> מוריד את ה-full של המניפסט ומפענח ל-OUT
  summary         --out DIR ...            -> טבלת markdown ל-GITHUB_STEP_SUMMARY
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

OFFICIAL_ASSET = "seforim-schema6.db.zst"
LFS_MAGIC = b"version https://git-lfs.github.com/spec/"
DB_TAG_RE = re.compile(r"^db-v(\d+)$")
OFFICIAL_TAG_RE = re.compile(r"^v\d+(-|$)")   # v31-20261004170255; לא vectors / pipeline-result


class HelperError(Exception):
    pass


# ---------- בחירת ה-DB הרשמי ----------
def pick_official(releases, asset=OFFICIAL_ASSET):
    """ההוצאה האחרונה (לפי published_at) שאינה טיוטה/prerelease, שתגה vN-... ושיש בה את ה-asset.

    ב-SeforimLibrary יש גם הוצאות ישנות שבהן ה-DB נקרא seforim.db.zst (סכמה ישנה): נדלגות.
    """
    best = None
    for r in releases:
        if r.get("draft") or r.get("prerelease") or not OFFICIAL_TAG_RE.match(r.get("tag_name", "")):
            continue
        for a in r.get("assets", []):
            if a.get("name") == asset:
                key = r.get("published_at") or r.get("created_at") or ""
                if best is None or key > best[0]:
                    best = (key, r["tag_name"], a["browser_download_url"], int(a.get("size", 0)))
    if not best:
        raise HelperError(f"לא נמצאה הוצאה רשמית עם {asset}")
    return best[1:]


# ---------- גרסאות ----------
def next_version(prev, start):
    """prev: db_version של ה-release הקודם שלנו, או None אם אין. start: קלט start_version או None."""
    if prev is None:
        if not start:
            raise HelperError("אין release קודם (תג 'db'). בנייה ראשונה דורשת הרצה ידנית עם start_version")
        return int(start)
    v = int(prev) + 1
    if start and int(start) > v:
        v = int(start)
    return v


def db_tags(tags):
    """[(גרסה, תג)] ממוינים עולה, רק תגים בצורה db-v<N>."""
    out = []
    for t in tags:
        m = DB_TAG_RE.match(t.strip())
        if m:
            out.append((int(m.group(1)), t.strip()))
    return sorted(set(out))


def select_prev(tags, current, count=2):
    """עד count הגרסאות הגבוהות ביותר שקטנות מ-current, מהחדשה לישנה."""
    older = [x for x in db_tags(tags) if x[0] < current]
    return [t for _, t in reversed(older[-count:])]


def orphan_tags(tags, pinned):
    """תגי db-vK שגרסתם גבוהה מ-pinned (db_version שבמניפסט 'db'): שרידי פרסום שנכשל באמצע."""
    return [t for v, t in db_tags(tags) if v > int(pinned)]


def tags_to_prune(tags, keep=3):
    allv = db_tags(tags)
    old = allv[:-keep] if keep > 0 else allv
    return [t for _, t in old]


# ---------- LFS ----------
def find_lfs_pointers(root, ext=".txt", limit=20):
    bad = []
    for d, _, fs in os.walk(root):
        for f in fs:
            if os.path.splitext(f)[1].lower() != ext:
                continue
            p = os.path.join(d, f)
            try:
                with open(p, "rb") as h:
                    if h.read(len(LFS_MAGIC)) == LFS_MAGIC:
                        bad.append(p)
            except OSError:
                continue
            if len(bad) >= limit:
                return bad
    return bad


# ---------- הורדת full ----------
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, dst, tries=4):
    for i in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "otzarya-db-build"})
            with urllib.request.urlopen(req, timeout=120) as r, open(dst, "wb") as o:
                shutil.copyfileobj(r, o, 1 << 20)
            return
        except Exception as e:  # noqa: BLE001 - רשת: מנסים שוב
            if i == tries:
                raise HelperError(f"הורדה נכשלה: {url}: {e}")
            time.sleep(5 * i)


def fetch_full(manifest_path, out, workdir, zstd="zstd", fetch=download):
    """מוריד את חלקי ה-full, מאמת sha256 לכל חלק ולתוצאה, ומפענח (zstd, --long=27) ל-out.

    כל חלק נמחק מיד אחרי השימוש כדי לחסוך דיסק.
    """
    m = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    full = m["full"]
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    out = Path(out)
    if full["compression"] == "zstd":
        exe = shutil.which(zstd)
        if not exe:
            raise HelperError("zstd לא נמצא בנתיב")
        proc = subprocess.Popen([exe, "-q", "-d", "-f", "--long=27", "-o", str(out)], stdin=subprocess.PIPE)
        sink = proc.stdin
    else:
        proc, sink = None, open(out, "wb")
    try:
        for i, p in enumerate(full["parts"], 1):
            f = work / f"part{i:04d}"
            fetch(p["url"], f)
            if f.stat().st_size != p["size"] or sha256_file(f) != p["sha256"]:
                raise HelperError(f"חלק {i} אינו תואם ל-size/sha256 שבמניפסט")
            with open(f, "rb") as s:
                shutil.copyfileobj(s, sink, 1 << 20)
            f.unlink()
        sink.close()
        if proc and proc.wait():
            raise HelperError("פענוח zstd נכשל")
    except BaseException:
        if proc and proc.poll() is None:
            proc.kill()
        raise
    if out.stat().st_size != full["size"] or sha256_file(out) != full["sha256"]:
        raise HelperError("הקובץ המפוענח אינו תואם ל-full.size/sha256")
    return out


# ---------- דוח ----------
def fmt_size(n):
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{n} B"


def parse_timings(text):
    rows = []
    for line in text.splitlines():
        name, _, secs = line.rpartition(" ")
        if name and secs.isdigit():
            rows.append((name, int(secs)))
    return rows


def format_summary(version, out_dir, timings_text="", extra=None):
    lines = [f"## מסד אוצריא אישי - גרסה {version}", ""]
    for k, v in (extra or {}).items():
        lines.append(f"- {k}: {v}")
    lines += ["", "| קובץ | גודל |", "|---|---|"]
    total = 0
    for p in sorted(Path(out_dir).iterdir()):
        if p.is_file():
            total += p.stat().st_size
            lines.append(f"| `{p.name}` | {fmt_size(p.stat().st_size)} |")
    lines.append(f"| **סה\"כ** | **{fmt_size(total)}** |")
    rows = parse_timings(timings_text)
    if rows:
        lines += ["", "| שלב | זמן |", "|---|---|"]
        lines += [f"| {n} | {s // 60}:{s % 60:02d} |" for n, s in rows]
        s = sum(x for _, x in rows)
        lines.append(f"| **סה\"כ** | **{s // 60}:{s % 60:02d}** |")
    return "\n".join(lines) + "\n"


# ---------- שורת פקודה ----------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("pick-official"); a.add_argument("releases"); a.add_argument("--asset", default=OFFICIAL_ASSET)
    a = sub.add_parser("next-version"); a.add_argument("--prev"); a.add_argument("--start")
    a = sub.add_parser("select-prev"); a.add_argument("--tags", nargs="*", default=[])
    a.add_argument("--current", type=int, required=True); a.add_argument("--count", type=int, default=2)
    a = sub.add_parser("orphan-tags"); a.add_argument("--tags", nargs="*", default=[]); a.add_argument("--pinned", type=int, required=True)
    a = sub.add_parser("prune-tags"); a.add_argument("--tags", nargs="*", default=[]); a.add_argument("--keep", type=int, default=3)
    a = sub.add_parser("check-lfs"); a.add_argument("root")
    a = sub.add_parser("fetch-full"); a.add_argument("manifest"); a.add_argument("out"); a.add_argument("--workdir", required=True)
    a = sub.add_parser("summary"); a.add_argument("--out", required=True); a.add_argument("--version", required=True)
    a.add_argument("--timings"); a.add_argument("--extra", nargs="*", default=[])
    args = ap.parse_args(argv)
    try:
        if args.cmd == "pick-official":
            data = json.loads(Path(args.releases).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data = [data]
            print("\t".join(map(str, pick_official(data, args.asset))))
        elif args.cmd == "next-version":
            print(next_version(int(args.prev) if args.prev else None, args.start))
        elif args.cmd == "select-prev":
            print("\n".join(select_prev(args.tags, args.current, args.count)))
        elif args.cmd == "orphan-tags":
            print("\n".join(orphan_tags(args.tags, args.pinned)))
        elif args.cmd == "prune-tags":
            print("\n".join(tags_to_prune(args.tags, args.keep)))
        elif args.cmd == "check-lfs":
            bad = find_lfs_pointers(args.root)
            for b in bad:
                print("מצביע LFS:", b, file=sys.stderr)
            return 1 if bad else 0
        elif args.cmd == "fetch-full":
            fetch_full(args.manifest, args.out, args.workdir)
            print("נוצר:", args.out)
        elif args.cmd == "summary":
            t = Path(args.timings).read_text(encoding="utf-8") if args.timings and Path(args.timings).exists() else ""
            extra = dict(x.split("=", 1) for x in args.extra if "=" in x)
            sys.stdout.write(format_summary(args.version, args.out, t, extra))
    except (HelperError, OSError, ValueError, KeyError) as e:
        print("שגיאה:", e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
