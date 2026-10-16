#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Check 8 — daf agreement, over 100% of entries.

The daf heading a line is printed under must equal the daf its link resolves
to. This is the highest-precision check available without reading text: it
needs no judgement and no DB, and it catches whole-page misses that
word-overlap heuristics happily rate as good matches.

A mismatch is `major`. Per the skill's overriding principle, a mismatch you
cannot repair against the correct daf is REMOVED, not shipped.

Which entries are checked
-------------------------
Every dependent-text ("base <-> dependant") entry, whatever side the file is
named after. The check itself is direction-agnostic: it compares the daf
heading above `line_index_1` in the book the links file is NAMED AFTER
(`--citing`, the `line_index_1` side) with the daf in `heRef_2`.

  source            citing-named file (the modern norm): line_index_1 = the
                    commentary line, path_2 = base text / Rashi / Tosafot.
                    The generator flips it to base->commentary.
  commentary,       legacy citing-named files (stored backwards, a
  super_commentary  validate_links `link_direction` blocker), or the
                    base-named convention (line_index_1 = base line).
  footnotes         a notes book; either orientation.
  targum, midrash, parshanut, dibur_hamatchil, elucidation, explication
                    other oriented dependant types, same treatment.

NOT checked (reported as `skipped_type`): lateral references (quotation,
reference, mesorat_hashas, ein_mishpat, related, ...), whose target daf is
legitimately different, and `linker` rows. Unknown types are also skipped
and listed, never silently.

Target side with no daf: a notes book (the `path_2` of a base-named
`footnotes` file) is addressed by its bare title, e.g. heRef_2 = "<notes
title>" — there is no daf to compare. Such entries (heRef_2 whose title part
equals the path_2 basename, or whose last word is not a daf number in Hebrew
numerals) count as `target_daf_unknown`, never as a mismatch.

History: until Sept 2026 only commentary/super_commentary were checked, so
every `source` file checked 0 entries and "passed" with exit 0.

Usage:
  python -X utf8 check_daf.py --links "<...>_links.json" --citing "<citing>.txt"
  python -X utf8 check_daf.py --dir "<links dir>" --books-root "<repo root>"

Exit codes:
  0  every checked entry matches, and every file had >= 1 checked entry
  1  at least one daf MISMATCH (takes precedence over 2; the not-verified
     files are still listed)
  2  something was NOT verified: a file checked 0 entries, a citing .txt was
     not found, or a links file is unreadable / not a JSON array. This is
     NOT a pass. --allow-unchecked downgrades per-file cases to a warning
     (e.g. a batch that mixes Mishnah-mapped or non-Talmud books), but a run
     that verified nothing at all still exits 2.
  3  usage error (bad command line)
"""
import argparse, json, os, re, sys, io, collections

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daf_util import heading_daf_map, daf_from_heref, same_daf
from connection_type import canonical_connection_type

EXIT_OK, EXIT_MISMATCH, EXIT_UNVERIFIED, EXIT_USAGE = 0, 1, 2, 3

# Dependent-text types whose two sides share a daf (canonical names, see
# connection_type.py: "footnote", "Super Commentary", " source " all count).
LINKED = (
    "source", "commentary", "super_commentary", "footnotes",
    "targum", "midrash", "parshanut", "dibur_hamatchil", "elucidation", "explication",
)
# Alias with a clearer name; LINKED is kept because older callers import it.
DEPENDENT_TYPES = LINKED

COUNT_KEYS = ("match", "MISMATCH", "citing_daf_unknown", "target_daf_unknown",
              "bad_line_index", "skipped_type")

# Hebrew-numeral value of each (non-final) letter.
_GEMATRIA = {c: v for c, v in zip(
    "\u05d0\u05d1\u05d2\u05d3\u05d4\u05d5\u05d6\u05d7\u05d8"
    "\u05d9\u05db\u05dc\u05de\u05e0\u05e1\u05e2\u05e4\u05e6"
    "\u05e7\u05e8\u05e9\u05ea",
    (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 20, 30, 40, 50, 60, 70, 80, 90,
     100, 200, 300, 400))}
_STRIP = re.compile(r"[\"'\u05f3\u05f4“”‘’`\s]")


def is_daf_label(page):
    """True if `page` reads as a daf number: 1-4 Hebrew letters in descending
    numeral order (hundreds may repeat; tet-vav / tet-zayin allowed). Rejects
    words such as a book title's last word that parse_daf would otherwise
    return as a 'page'."""
    t = _STRIP.sub("", page or "")
    if not 1 <= len(t) <= 4 or any(c not in _GEMATRIA for c in t):
        return False
    v = [_GEMATRIA[c] for c in t]
    for i in range(len(v) - 1):
        a, b = v[i], v[i + 1]
        if a == 9 and b in (6, 7) and i == len(v) - 2:
            continue
        if a < b or (a == b and a < 100):
            return False
    return True


def target_daf(rec):
    """(page, amud) the entry's heRef_2 points at, or None if it has no daf."""
    he = rec.get("heRef_2")
    if not isinstance(he, str) or not he.strip():
        return None
    p2 = rec.get("path_2")
    if isinstance(p2, str) and p2:
        stem = os.path.splitext(os.path.basename(p2.replace("\\", "/")))[0]
        if he.split(",")[0].strip() == stem.strip():
            return None          # bare book title: a notes book, no daf locator
    td = daf_from_heref(he)
    if td is None or not is_daf_label(td[0]):
        return None
    return td


def line_index(v):
    """int line index, accepting integral floats (the generator does .toInt())."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return None


def check(links_path, citing_path):
    """Return (Counter, mismatches). Counter also carries per-type skip counts
    under 'skipped_type:<type>'. Raises OSError / ValueError on unreadable or
    malformed input."""
    lines = open(citing_path, encoding="utf-8").read().split("\n")
    dmap = heading_daf_map(lines)
    recs = json.load(open(links_path, encoding="utf-8"))
    if not isinstance(recs, list):
        raise ValueError("links root is not a JSON array")
    out = collections.Counter()
    bad = []
    for r in recs:
        if not isinstance(r, dict):
            out["skipped_type"] += 1; out["skipped_type:<non-object>"] += 1
            continue
        # Only the generator's key counts. Missing / null -> "" -> OTHER; a misspelled
        # "Connection Type" key is ignored by the generator (ignoreUnknownKeys).
        if r.get("Conection Type") is None:
            why = ("<'Connection Type' key: ignored by generator -> OTHER>"
                   if "Connection Type" in r else "<missing/null -> OTHER>")
            out["skipped_type"] += 1; out[f"skipped_type:{why}"] += 1
            continue
        ctype = r["Conection Type"]
        # Classify like the generator (Link.kt): trim, lowercase, space->_ , aliases.
        if canonical_connection_type(ctype) not in LINKED:
            out["skipped_type"] += 1; out[f"skipped_type:{ctype}"] += 1
            continue
        li = line_index(r.get("line_index_1"))
        if li is None or not 1 <= li <= len(lines):
            out["bad_line_index"] += 1; continue
        cd = dmap[li]
        td = target_daf(r)
        if cd is None:
            out["citing_daf_unknown"] += 1; continue
        if td is None:
            out["target_daf_unknown"] += 1; continue
        if same_daf(cd, td):
            out["match"] += 1
        else:
            out["MISMATCH"] += 1
            bad.append((li, f"{cd[0]}{cd[1] or ''}", f"{td[0]}{td[1] or ''}",
                        r.get("heRef_2"),
                        re.sub(r"<[^>]+>", "", lines[li - 1])[:58]))
    return out, bad


def checked(out):
    return out["match"] + out["MISMATCH"]


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def main(argv=None):
    ap = _Parser()
    ap.add_argument("--links"); ap.add_argument("--citing")
    ap.add_argument("--dir"); ap.add_argument("--books-root")
    ap.add_argument("--allow-unchecked", action="store_true",
                    help="files with 0 checked entries / missing citing .txt / "
                         "unreadable links warn instead of exiting 2")
    a = ap.parse_args(argv)

    pairs = []
    unresolved = []
    if a.links and a.citing:
        if os.path.isfile(a.citing):
            pairs.append((a.links, a.citing))
        else:
            print(f"  !! citing .txt not found: {a.citing} — NOT CHECKED")
            unresolved.append(os.path.basename(a.links))
    elif a.dir and a.books_root:
        idx, dup = {}, collections.Counter()
        for root, _, files in os.walk(a.books_root):
            if ".git" in root: continue
            for f in files:
                if f.endswith(".txt"):
                    if f in idx: dup[f] += 1
                    idx.setdefault(f, os.path.join(root, f))
        for f in sorted(os.listdir(a.dir)):
            if not f.endswith("_links.json"): continue
            name = f[:-len("_links.json")] + ".txt"
            src = idx.get(name)
            if src:
                if dup[name]:
                    print(f"  !! {dup[name] + 1} copies of {name} under --books-root; "
                          f"using {src}")
                pairs.append((os.path.join(a.dir, f), src))
            else:
                print(f"  !! citing .txt not found for {f} — NOT CHECKED")
                unresolved.append(f)
    else:
        ap.error("pass --links and --citing, or --dir and --books-root")

    tot = collections.Counter(); empty = []; broken = []
    for lp, cp in pairs:
        name = os.path.basename(lp).replace("_links.json", "")
        try:
            out, bad = check(lp, cp)
        except (OSError, UnicodeDecodeError, ValueError) as e:
            print(f"\n!!! {name}: unreadable / invalid — NOT CHECKED ({type(e).__name__}: {e})")
            broken.append(name)
            continue
        tot.update(out)
        if checked(out) == 0:
            skipped = {k.split(":", 1)[1]: v for k, v in out.items()
                       if k.startswith("skipped_type:")}
            print(f"\n!!! {name}: 0 entries checked "
                  f"(skipped types {skipped or '{}'}, citing_daf_unknown "
                  f"{out['citing_daf_unknown']}, target_daf_unknown "
                  f"{out['target_daf_unknown']}, bad_line_index {out['bad_line_index']})")
            empty.append(name)
        if bad:
            print(f"\n--- {name}: {len(bad)} mismatches ---")
            for li, cd, td, h, tx in bad[:10]:
                print(f"  line {li}: citing daf {cd} -> link {td}   ({h})")
                print(f"      {tx}")

    n = checked(tot)
    total_files = len(pairs) + len(unresolved)
    print("\n" + "=" * 60)
    for k in COUNT_KEYS:
        print(f"  {k:<22} {tot[k]:>6}")
    for k in sorted(k for k in tot if k.startswith("skipped_type:")):
        print(f"    {k[len('skipped_type:'):]:<20} {tot[k]:>6}")
    print(f"  files checked          {total_files - len(empty) - len(broken) - len(unresolved):>6} / "
          f"{total_files}")
    if n:
        print(f"\n  daf accuracy: {100 * tot['match'] / n:.2f}%")
    unverified = len(empty) + len(broken) + len(unresolved)
    if unverified:
        msg = ("NOTHING VERIFIED" if n == 0 else f"{unverified} file(s) not verified")
        if a.allow_unchecked and n:
            print(f"\n  WARNING: {msg} (--allow-unchecked)")
            unverified = 0
        else:
            print(f"\n  FAIL: {msg} — 0 checked entries is not a pass")
    elif n == 0:
        print("\n  FAIL: NOTHING VERIFIED — 0 checked entries is not a pass")
        unverified = 1
    if tot["MISMATCH"]:
        return EXIT_MISMATCH
    return EXIT_UNVERIFIED if unverified else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
