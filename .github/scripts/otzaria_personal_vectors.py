#!/usr/bin/env python3
# ---------------------------------------------------------------------------------------------
# מקור: otzaria_personal_vectors.py גרסה 1.0, מאת "קרית חנה" (פורום otzaria.org, נושא 3073).
# הועתק למאגר באישור הכותב (נמסר למשתמש). sha256 של הקובץ המקורי, ללא שורות הכותרת האלה:
#   71f3799d0a47106678f065c63da4e2254f317c8497981ad705bf839cbc534a7c
# הקובץ הזה הוא נקודת ההחלפה היחידה: vectors_ci.py טוען אותו לפי נתיב (importlib) ואינו מעתיק
# ממנו קוד, ולכן אפשר להחליף אותו בגרסה מתוקנת בקובץ אחד, בלי לגעת בשאר. אין לערוך בו כאן
# שינויים עצמאיים: שינוי נעשה ביחס לגרסה של הכותב, עם diff מתועד.
# ---------------------------------------------------------------------------------------------
"""
otzaria_personal_vectors.py - semantic search inside Otzaria for your personal books and for the
books of attached book databases (מסדי ספרים מצורפים).

Otzaria's semantic search ("חיפוש לפי משמעות") only searches books its downloaded vector set
covers. This script embeds the other books with Otzaria's own model, the way Otzaria's vectors
were built, and publishes them into Otzaria's vector set as one extra segment. After that,
Otzaria's semantic search finds them like any library book.

    python otzaria_personal_vectors.py --check    # what would be embedded and published; changes nothing
    python otzaria_personal_vectors.py            # embed what is missing, publish (close Otzaria first)
    python otzaria_personal_vectors.py --remove   # take the personal segment out of the set again

Requirements: Python 3.9+, and
    pip install numpy onnxruntime tokenizers zstandard
(onnxruntime-gpu or onnxruntime-directml instead of onnxruntime to embed on a GPU; see --gpu).
Otzaria must have downloaded its semantic vectors and model (the model folder, such as
meivin-qa491-onnx, in Otzaria's books folder), and indexed the books for search: Otzaria shows
a hit only for a line its search index holds.

What it reads (nothing is configured here):
  personal books   the .txt books Otzaria lists in databases/user_books.db (other formats are
                   skipped);
  attached DBs     the book databases listed in Otzaria's settings (hidden ones are skipped).

How it works
  Every line is cut into passages exactly as Otzaria does (short lines take their neighbours as
  context; long lines get up to 5 more 512-character windows), and every passage is named by
  Otzaria's passage key (SHA-256 of its text). A passage whose key Otzaria's set already holds
  is not embedded or written again: Otzaria finds that text in your books by itself. The rest
  is embedded with the model's document graph on the CPU (or GPU), and written as an .oxv
  segment (Otzaria's segment format 3, codec rot192-i8i4) into <otzaria>/vectors/segments; a
  new generation of the set lists Otzaria's own segments, unchanged, plus this one, and
  CURRENT is pointed at it (the generation that was live before stays as PREVIOUS, which
  Otzaria falls back to if the new one cannot be opened).

  Embedding is the slow part (on a CPU, very roughly 20-100 passages a second). Every vector
  is kept in a cache folder (--cache-dir), so the run can be interrupted with Ctrl+C and
  continued, and a later run embeds only new or changed text.

When to run it again
  after adding or changing books; after Otzaria installed a new *base* vector set (a model
  change), which replaces the whole set (the cache is then not used: a new model needs new
  vectors). An ordinary Otzaria vector update keeps the personal segment. When Otzaria
  compacts its set, the personal vectors move into its compacted segment and stay found.

This is not a feature of Otzaria: it writes into Otzaria's vector folder in the format Otzaria
reads (otzaria-semantic-search, store format 3), and a future change to that format may need a
new version of this script. Close Otzaria while it publishes.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import sys
import threading
import time
import urllib.parse
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

VERSION = "1.0"

# ------------------------------------------------------------------ the passage recipe
# Otzaria's production chunking (otzaria_search_engine semantic_keys.rs, production_chunking;
# otzaria-semantic-search chunker.rs): checked against the set's chunking_identity at start.
MIN_MEANINGFUL, CONTEXT, MAX_CHARS, MIN_EMBEDDABLE = 20, 2, 512, 5
CHUNKING_VERSION, TEXT_VERSION, NORMALIZATION_VERSION = 2, 2, 1
WINDOW_CHARS, WINDOW_OVERLAP, MAX_WINDOWS = 512, 128, 6

SLUG_RE = re.compile(r"^[\w.\-]{1,64}$")

# ------------------------------------------------------------------ the .oxv format
MAGIC = b"OXVSEG\r\n"
HEADER_LEN, BLOCK = 4096, 1 << 20
FLAG_BASE, FLAG_DELTA, FLAG_COMPACTED, FLAG_FOREIGN, FLAG_TOMBS = 1, 2, 4, 8, 16
SEC_CRITICAL, SEC_BLOCKSUMMED = 1, 2
HAS_EXTRAS = 1 << 31
(BOOKS, BOOK_NAMES, CODEC_PARAMS, BOOK_HASHES, SLOT_KEYS, SLOT_HINTS, VECTORS, SCALES, SLOT_WINDOWS,
 EXTRAS, EXTRAS_BY_SLOT, FOREIGN, TOMBSTONES, OCCURRENCES, BLOCK_CRCS) = (
    0x0001, 0x0002, 0x0003, 0x0004, 0x0010, 0x0011, 0x0012, 0x0013, 0x0014,
    0x0020, 0x0021, 0x0022, 0x0023, 0x0024, 0x007F)
CODEC_ROT = 0x0103
BOOK_DT = np.dtype([("name_off", "<u4"), ("name_len", "<u2"), ("flags", "<u2"), ("slot_start", "<u4"),
                    ("slot_count", "<u4"), ("extra_start", "<u4"), ("extra_count", "<u4"),
                    ("foreign_start", "<u4"), ("foreign_count", "<u4"), ("pad", "<u8")])
OCC_DT = np.dtype([("key", ">u8"), ("book", "<u4"), ("hint", "<u4"), ("anchor", ">u8"),
                   ("window", "u1"), ("role", "u1"), ("pad", "<u2")])
WORKER = f"otzaria-personal-vectors {VERSION}"
# segments this script (or its predecessor in otzaria-sources) published
OURS = ("otzaria-personal-vectors", "otzaria-sources personal export")
DEL_MAGIC, LINKS_MAGIC = b"OXVDEL1\n", b"OXVLNK1\n"


# A program that drives this module (the window of OtzariaPersonalVectors.exe) sets these hooks:
# log  (message)              every line of the log; default: printed
# event(kind, **data)         the run's milestones, for a summary in the user's language
# progress(done, rate)        passages embedded so far, and per second
# confirm_closed()            Otzaria is running and must be closed to publish: True once the user
#                             says it is closed, False to stop; default: stop with an error
HOOKS = {"log": None, "event": None, "progress": None, "confirm_closed": None}
CANCEL = threading.Event()   # set to stop a run; what was embedded is kept


class Cancelled(Exception):
    pass


def log(*a):
    msg = " ".join(str(x) for x in a)
    if HOOKS["log"]:
        HOOKS["log"](msg)
    elif sys.stdout:
        print(msg, flush=True)


def emit(kind, **data):
    if HOOKS["event"]:
        HOOKS["event"](kind, **data)


def check_cancel():
    if CANCEL.is_set():
        raise KeyboardInterrupt


def crc(b):
    return zlib.crc32(b) & 0xFFFFFFFF


def recipe_identity(windows=True):
    """Otzaria's chunking_identity of this recipe (ChunkerConfig::identity): the set records the
    windowed one, the search index the one without windows."""
    fields = [MIN_MEANINGFUL, CONTEXT, MAX_CHARS, MIN_EMBEDDABLE, CHUNKING_VERSION if windows else 1,
              TEXT_VERSION, NORMALIZATION_VERSION] + ([WINDOW_CHARS, WINDOW_OVERLAP, MAX_WINDOWS] if windows else [])
    d = hashlib.sha256(b"".join(struct.pack("<Q", f) for f in fields)).digest()
    return struct.unpack("<Q", d[:8])[0]


# ------------------------------------------------------------------ Otzaria's line text
# An exact port of the line recipe Otzaria's index stores and keys every semantic vector from
# (otzaria_search_engine, rust/src/hebrew_query.rs normalize_text_for_indexing; the index
# metadata's line_text_version 4), and of how the app hands a book's text to the indexer
# (lib/indexing/repository/indexing_repository.dart). The same block is in personal_index.py
# and otzaria_personal_vectors.py; test_line_text.py holds both to the engine's own output.
#   - tags: removed; the HTML block tags and <br> become a space; <sup>/<sub> separate words
#     (תורה<sup>30</sup> -> "תורה 30"), except one raised letter inside a word (מְ<sup>נַ</sup>שֶּׁה)
#   - entities: &nbsp; &thinsp; &ensp; &emsp; become a space, any other &...; is removed
#   - Hebrew presentation forms (U+FB1D-FB4F) decomposed, attached nikud/teamim removed
#     (maqaf, paseq, sof pasuq and nun hafukha kept), whitespace collapsed and trimmed
#   - a line starting with "<h" opens a new section (what a short line's context stays inside)
#   - before the book is split into lines, embedded data: URIs of 64+ characters are removed

LINE_TEXT_VERSION = 4

_BREAKING_TAGS = frozenset((
    "address", "article", "aside", "blockquote", "br", "caption", "center", "dd", "div", "dl", "dt",
    "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
    "nav", "ol", "p", "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul"))
_SCRIPT_TAGS = frozenset(("sub", "sup"))
_SPACE_ENTITIES = ("&nbsp;", "&thinsp;", "&ensp;", "&emsp;")
_CGJ = "͏"
_PRESENTATION = {
    0xFB1D: "יִ", 0xFB1E: "", 0xFB1F: "ײַ", 0xFB20: "ע", 0xFB21: "א",
    0xFB22: "ד", 0xFB23: "ה", 0xFB24: "כ", 0xFB25: "ל", 0xFB26: "ם",
    0xFB27: "ר", 0xFB28: "ת", 0xFB2A: "שׁ", 0xFB2B: "שׂ",
    0xFB2C: "שּׁ", 0xFB2D: "שּׂ", 0xFB2E: "אַ",
    0xFB2F: "אָ", 0xFB30: "אּ", 0xFB31: "בּ", 0xFB32: "גּ",
    0xFB33: "דּ", 0xFB34: "הּ", 0xFB35: "וּ", 0xFB36: "זּ",
    0xFB38: "טּ", 0xFB39: "יּ", 0xFB3A: "ךּ", 0xFB3B: "כּ",
    0xFB3C: "לּ", 0xFB3E: "מּ", 0xFB40: "נּ", 0xFB41: "סּ",
    0xFB43: "ףּ", 0xFB44: "פּ", 0xFB46: "צּ", 0xFB47: "קּ",
    0xFB48: "רּ", 0xFB49: "שּ", 0xFB4A: "תּ", 0xFB4B: "וֹ",
    0xFB4C: "בֿ", 0xFB4D: "כֿ", 0xFB4E: "פֿ", 0xFB4F: "אל"}
# Rust's char::is_whitespace (Unicode White_Space), which Python's str.isspace() is not
_WS = "\t\n\x0b\x0c\r \x85\xa0           " \
      "     　"
_WS_RUN_RE = re.compile("[" + _WS + "]+")


def _attached_mark(c):
    return "֑" <= c <= "ׇ" and c not in "־׀׃׆"


def _word_mark(c):
    return _attached_mark(c) or "̀" <= c <= "ͯ" or c == "ﬞ"


def _invisible(c):
    return ("​" <= c <= "‏" or "‪" <= c <= "‮" or "⁦" <= c <= "⁩"
            or c == "﻿")


def _script_invisible(c):
    return _invisible(c) or c == _CGJ or c == "ﬞ"


def _hebrew_letter(c):
    return "א" <= c <= "ת"


def _script_letter(c):
    """A Hebrew letter, or a presentation form that folds to one letter and its marks."""
    f = _PRESENTATION.get(ord(c))
    if f is None:
        return _hebrew_letter(c)
    return bool(f) and _hebrew_letter(f[0]) and all(_attached_mark(m) for m in f[1:])


def _tag_name(body):
    """(closing, name) of a tag body (the text between < and >), or None."""
    closing = body.startswith("/")
    name = body[1:] if closing else body
    n = 0
    while n < len(name) and name[n].isascii() and name[n].isalnum():
        n += 1
    return (closing, name[:n]) if 1 <= n <= 10 else None


def _inline_tag(body):
    t = _tag_name(body)
    return not (t and (t[1].lower() in _BREAKING_TAGS or t[1].lower() in _SCRIPT_TAGS))


def _lstrip_if(s, pred):
    i = 0
    while i < len(s) and pred(s[i]):
        i += 1
    return s[i:]


def _rstrip_if(s, pred):
    j = len(s)
    while j > 0 and pred(s[j - 1]):
        j -= 1
    return s[:j]


def _trim_formatting_start(s):
    while True:
        s = _lstrip_if(s, _script_invisible)
        if not s.startswith("<"):
            return s
        gt = s.find(">", 1)
        if gt < 0 or not _inline_tag(s[1:gt]):
            return s
        s = s[gt + 1:]


def _trim_formatting_end(s):
    while True:
        s = _rstrip_if(s, _script_invisible)
        if not s.endswith(">"):
            return s
        body = s[:-1]
        lt = body.rfind("<")
        if lt < 0 or ">" in body[lt + 1:] or not _inline_tag(body[lt + 1:]):
            return s
        s = body[:lt]


def _trim_context_end(s):
    while True:
        t = _rstrip_if(_trim_formatting_end(s), _word_mark)
        if len(t) == len(s):
            return s
        s = t


def _trim_context_start(s):
    while True:
        t = _lstrip_if(_trim_formatting_start(s), _word_mark)
        if len(t) == len(s):
            return s
        s = t


def _marks_footnote(attributes):
    low = "".join(c.lower() if c.isascii() else c for c in attributes)

    def word(c):
        return c is not None and c.isascii() and (c.isalnum() or c == "_")
    i = low.find("footnote-marker")
    while i >= 0:
        end = i + len("footnote-marker")
        if not word(low[i - 1] if i else None) and not word(low[end] if end < len(low) else None):
            return True
        i = low.find("footnote-marker", i + 1)
    return False


def _word_internal_script(src, lt, gt):
    """Whether the open tag src[lt..gt] wraps one letter (with marks) between letters."""
    t = _tag_name(src[lt + 1:gt])
    if not t or t[0]:
        return False
    if _marks_footnote(src[lt + 1:gt]):
        return False
    before = _trim_context_end(src[:lt])
    if not before or not _script_letter(before[-1]):
        return False
    content = _trim_formatting_start(src[gt + 1:])
    if not content or not _script_letter(content[0]):
        return False
    rest = _trim_context_start(content[1:])
    if not rest.startswith("<"):
        return False
    close_gt = rest.find(">", 1)
    if close_gt < 0:
        return False
    c = _tag_name(rest[1:close_gt])
    if not (c and c[0] and c[1].lower() == t[1].lower()):
        return False
    after = _trim_context_start(rest[close_gt + 1:])
    return bool(after) and _script_letter(after[0])


def _script_open_before(src, lt):
    before = _trim_context_end(src[:lt])
    if not before or not _script_letter(before[-1]):
        return None
    gt = len(_trim_formatting_end(before[:-1])) - 1
    if gt < 0 or src[gt] != ">":
        return None
    lt2 = src.rfind("<", 0, gt)
    if lt2 < 0 or ">" in src[lt2 + 1:gt]:
        return None
    return lt2, gt


def _tag_breaks_word(src, lt, gt):
    t = _tag_name(src[lt + 1:gt])
    if not t:
        return False
    name = t[1].lower()
    if name in _BREAKING_TAGS:
        return True
    if name not in _SCRIPT_TAGS:
        return False
    opening = _script_open_before(src, lt) if t[0] else (lt, gt)
    return not (opening is not None and _word_internal_script(src, *opening))


def strip_html_for_indexing(text, entities=True):
    """Tags and entities removed as Otzaria's indexer removes them (nikud kept). entities=False:
    only the tags (for display, where the entities are decoded instead)."""
    if "<" not in text and ("&" not in text or not entities):
        return text
    out, i, copied, n = [], 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "<":
            close = text.find(">", i + 1)
            if close >= 0:
                rep, nxt = (" " if _tag_breaks_word(text, i, close) else ""), close + 1
            else:
                rep, nxt = "<", i + 1
        elif ch == "&" and entities:
            ent = next((e for e in _SPACE_ENTITIES if text.startswith(e, i)), None)
            if ent:
                rep, nxt = " ", i + len(ent)
            else:
                j, semi = i + 1, -1
                while j < n:
                    if text[j] == ";":
                        semi = j
                        break
                    if text[j] == "&":
                        e = next((e for e in _SPACE_ENTITIES if text.startswith(e, j)), None)
                        j += len(e) if e else 1
                    else:
                        j += 1
                if semi > i + 1:
                    rep, nxt = "", semi + 1
                else:
                    rep, nxt = "&", i + 1
        else:
            i += 1
            continue
        if copied < i:
            out.append(text[copied:i])
        out.append(rep)
        i = copied = nxt
    out.append(text[copied:])
    return "".join(out)


# presentation forms folded, then attached marks dropped, in one translate
_FOLD_STRIP = {cp: "".join(c for c in f if not _attached_mark(c)) for cp, f in _PRESENTATION.items()}
_FOLD_STRIP.update({cp: None for cp in range(0x0591, 0x05C8) if _attached_mark(chr(cp))})


def rust_trim(s):
    return s.strip(_WS)


def line_text(raw):
    """A line as Otzaria's index stores it (normalize_text_for_indexing)."""
    t = strip_html_for_indexing(raw or "").translate(_FOLD_STRIP)
    return _WS_RUN_RE.sub(" ", t).strip(" ")


def is_section_start(raw):
    """A line that opens a new section in Otzaria's index (and so bounds a short line's context)."""
    return (raw or "").startswith("<h")


def _data_uri_char(c):
    return c.isascii() and (c.isalnum() or c in "+/;,=.-")


def strip_data_uris(text):
    """The app's stripDataUrisForIndex: every data: followed by 64+ payload characters goes."""
    m = text.find("data:")
    if m < 0:
        return text
    out, copied, n = [], 0, len(text)
    while m >= 0:
        end = m + 5
        while end < n and _data_uri_char(text[end]):
            end += 1
        if end - m - 5 >= 64:
            out.append(text[copied:m])
            copied = end
        m = text.find("data:", end)
    if copied == 0:
        return text
    out.append(text[copied:])
    return "".join(out)


def book_text_lines(text, from_db=False):
    """A book's lines as the app hands them to the indexer: data: URIs removed, split on \\n.
    from_db: the text is a database's rows joined by \\n. The app passes those bytes on as they
    are, a leading BOM included, unless "data:" occurs anywhere in them: then it decodes them to
    clean them, and the decoder drops the BOM."""
    if from_db and text.startswith("﻿") and "data:" in text:
        text = text[1:]
    return strip_data_uris(text).split("\n")


def index_lines(raw_lines, from_db=False):
    """A book's lines as Otzaria's indexer receives them (book_text_lines of the joined text, line
    by line: a data: URI never spans a line)."""
    if not raw_lines:
        return raw_lines
    out = [strip_data_uris(x or "") for x in raw_lines]
    if from_db and out[0].startswith("\ufeff") and any("data:" in x for x in raw_lines):
        out[0] = out[0][1:]
    return out


# ------------------------------------------------------------------ passages

def decode_book(data):
    """Otzaria reads personal .txt as UTF-8 when valid, otherwise by detection."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    return data.decode("cp1255", errors="replace")


def window_spans(text):
    """[(a, b)] character spans of a line's windows (Otzaria's chunker.rs window_spans): the first
    512 characters, then 512-character windows overlapping the one before by 128, the start moved
    to the next word and the end back to a space; at most MAX_WINDOWS."""
    n = len(text)
    spans = [(0, min(n, MAX_CHARS))]
    while spans[-1][1] < n and len(spans) < MAX_WINDOWS:
        ps, pe = spans[-1]
        a = max(pe - WINDOW_OVERLAP, ps + 1)
        if text[a - 1] != " ":
            sp = text.find(" ", a, pe)
            if sp >= 0:
                a = sp + 1
        b = min(n, a + WINDOW_CHARS)
        if b < n:
            sp = text.rfind(" ", a + WINDOW_CHARS // 2, b + 1)
            if sp >= 0:
                b = sp
        spans.append((a, b))
    return spans


def windowed_passages(raw_lines, from_db=False):
    """(line_index, passage_text, window) for every passage the recipe embeds, a line's windows
    in order after its first passage. from_db: the lines are an attached database's rows."""
    lines = index_lines(raw_lines, from_db)
    texts = [line_text(x) for x in lines]
    section, sec = [], 0
    for x in lines:
        if is_section_start(x):
            sec += 1
        section.append(sec)
    out = []
    n = len(texts)
    for i, t in enumerate(texts):
        if len(t) < MIN_EMBEDDABLE:
            continue
        if len(t) < MIN_MEANINGFUL:
            lo = i
            for _ in range(CONTEXT):
                if lo == 0 or section[lo - 1] != section[i]:
                    break
                lo -= 1
            hi = i
            for _ in range(CONTEXT):
                if hi + 1 >= n or section[hi + 1] != section[i]:
                    break
                hi += 1
            body = " ".join(texts[lo:hi + 1])
        else:
            body = t
        body = rust_trim(rust_trim(body)[:MAX_CHARS])
        if body:
            out.append((i, "[PASSAGE] " + body, 0))
        if len(t) >= MIN_MEANINGFUL and len(t) > MAX_CHARS:
            seen = {body}
            for w, (a, b) in enumerate(window_spans(t)):
                if w == 0:
                    continue
                piece = rust_trim(t[a:b])
                if piece and piece not in seen:
                    seen.add(piece)
                    out.append((i, "[PASSAGE] " + piece, w))
    return out


def passage_key(text):
    """Otzaria's slot key of a passage: the first 8 bytes of SHA-256 of its text (with the
    "[PASSAGE] " prefix), as a big-endian integer. Otzaria's own vector set and the personal shards
    share it, so a passage whose text Otzaria already embedded is copied, not embedded."""
    return int.from_bytes(hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest()[:8], "big")


def passage_keys(texts):
    return np.fromiter((passage_key(t) for t in texts), np.uint64, len(texts))


def lookup_sorted(sorted_keys, keys):
    """Index of each of `keys` in `sorted_keys` (ascending), or -1."""
    keys = np.asarray(keys, np.uint64)
    if len(sorted_keys) == 0 or len(keys) == 0:
        return np.full(len(keys), -1, np.int64)
    idx = np.minimum(np.searchsorted(sorted_keys, keys), len(sorted_keys) - 1)
    return np.where(np.asarray(sorted_keys[idx]) == keys, idx, -1)


# ------------------------------------------------------------------ Otzaria's settings and books

def default_otzaria_dir():
    return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")) / "otzaria"


def default_cache_dir():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "otzaria-personal-vectors"


def _read_text(path):
    try:
        t = Path(path).read_text(encoding="utf-8-sig").strip()
        return t or None
    except OSError:
        return None


def program_data_root():
    return Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "otzaria"


class Locations:
    r"""Where Otzaria keeps what this script reads and writes, found as Otzaria finds it
    (lib/core/app_paths.dart, semantic_paths.dart):

    data      its settings (app_preferences.hive): %APPDATA%\otzaria, or otzaria_data beside
              otzaria.exe for a portable copy;
    library   the library folder (books): the setting key-library-path, else library_path.txt in
              the data folder, else <data>\books, or C:\ProgramData\otzaria\books for an
              installation for all users;
    vectors   <the library's parent>\vectors;
    db_dir    the folder of seforim.db (library_database_path.txt), with the model folder in it;
    databases user_books.db: the setting key-databases-path, else <data>\databases when it
              exists, else <the library's parent>\databases.

    `data` / `root`: folders the user named; `root` is a folder holding vectors (and usually books),
    such as C:\ProgramData\otzaria, or a portable copy's folder."""

    def __init__(self, data=None, root=None, vectors=None):
        root = Path(root) if root else None
        if root is not None and data is None:
            for cand in (root, root / "otzaria_data"):
                if (cand / "app_preferences.hive").exists():
                    data = cand
                    break
        self.data = Path(data) if data else default_otzaria_dir()
        try:
            self.prefs = read_hive_strings(self.data / "app_preferences.hive")
        except OSError:
            self.prefs = {}
        lib = self.prefs.get("key-library-path") or _read_text(self.data / "library_path.txt")
        candidates = []
        if root is not None:
            candidates += [root / "books", root]
        if lib:
            candidates.append(Path(lib))
        candidates += [self.data / "books", program_data_root() / "books"]
        # the library whose parent holds Otzaria's vector set
        self.library = next((c for c in candidates if (c.parent / "vectors" / "CURRENT").exists()
                             or (c / "vectors" / "CURRENT").exists() and c == root), None)
        if self.library is None:
            self.library = next((c for c in candidates if c.exists()), candidates[0])
        if root is not None and (root / "vectors" / "CURRENT").exists():
            self.vectors = root / "vectors"
        else:
            self.vectors = self.library.parent / "vectors"
        if vectors:
            self.vectors = Path(vectors)
        dbd = _read_text(self.data / "library_database_path.txt")
        self.db_dirs = [Path(dbd)] if dbd else []
        self.db_dirs += [self.library, self.library.parent / "books"]
        saved = self.prefs.get("key-databases-path")
        if saved:
            self.databases = Path(saved)
        elif (self.data / "databases").exists():
            self.databases = self.data / "databases"
        else:
            self.databases = self.library.parent / "databases"

    def describe(self):
        return (f"settings {self.data}; library {self.library}; vectors {self.vectors}; "
                f"personal books list {self.databases / 'user_books.db'}")


def ro_uri(path):
    """A read-only SQLite URI for a path that may hold Hebrew, spaces, '#' or '?'."""
    return "file:" + urllib.parse.quote(Path(path).as_posix(), safe="/:") + "?mode=ro"


def category_path(cats, cid):
    parts, seen = [], set()
    while cid in cats and cid not in seen:
        seen.add(cid)
        parent, title = cats[cid]
        parts.append(title)
        cid = parent
    return list(reversed(parts))


def read_hive_strings(path):
    """String values of a Hive box (Otzaria's app_preferences.hive): frames of
    [u32 length][key][value][crc32], appended in order; the last frame for a key wins and a
    frame without a value deletes it. Non-string values are left out."""
    data = Path(path).read_bytes()
    out, pos = {}, 0
    while pos + 9 <= len(data):
        ln = struct.unpack_from("<I", data, pos)[0]
        if ln < 9 or pos + ln > len(data):
            break  # a frame still being written
        f = data[pos + 4:pos + ln - 4]
        pos += ln
        if f[0] == 1:
            kl = f[1]
            key, v = f[2:2 + kl].decode("utf-8", "replace"), f[2 + kl:]
        elif f[0] == 0:
            key, v = struct.unpack_from("<I", f, 1)[0], f[5:]
        else:
            break
        if not v:
            out.pop(key, None)
        elif v[0] == 4 and len(v) >= 5:
            sl = struct.unpack_from("<I", v, 1)[0]
            out[key] = v[5:5 + sl].decode("utf-8", "replace")
        else:
            out.pop(key, None)
    return out


def otzaria_settings(otzaria_dir):
    """`otzaria_dir`: the data folder (where app_preferences.hive is)."""
    """What Otzaria itself is configured with: personal folders and attached databases."""
    otzaria_dir = Path(otzaria_dir)
    s = {"custom_folders": [], "attached": [], "attached_folders": [], "error": None}
    try:
        kv = read_hive_strings(otzaria_dir / "app_preferences.hive")
    except OSError as e:
        s["error"] = f"cannot read Otzaria's settings: {e}"
        return s

    def js(key):
        try:
            v = json.loads(kv.get(key) or "[]")
            return v if isinstance(v, list) else []
        except ValueError:
            return []
    s["custom_folders"] = [x.get("path") for x in js("key-custom-folders") if isinstance(x, dict) and x.get("path")]
    s["attached"] = [x for x in js("key-attached-libraries") if isinstance(x, dict) and x.get("path")]
    s["attached_folders"] = [x for x in js("key-attached-library-folders") if isinstance(x, str)]
    return s


class BookDB:
    """An Otzaria-format book database (seforim.db layout; schema 6 keeps the line text
    zstd-compressed in line_content), read-only. Callers open a connection per task and close it,
    so Otzaria can swap the file during an update."""

    def __init__(self, path):
        self.path = Path(path)
        self._dctx = None
        self._sig = None

    def connect(self):
        return sqlite3.connect(ro_uri(self.path), uri=True, check_same_thread=False)

    def signature(self):
        st = self.path.stat()
        return st.st_size, st.st_mtime_ns

    def _prepare(self, con):
        sig = self.signature()
        if self._sig == sig:
            return
        tabs = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        line_cols = {r[1] for r in con.execute("pragma table_info(line)")}
        self.compressed = "line_content" in tabs and "content" not in line_cols
        self._dctx = {}
        self._plain = None
        if self.compressed:
            import zstandard as z
            self._plain = z.ZstdDecompressor()
            if "zstd_dict" in tabs:
                for _id, d in con.execute("select id, dict from zstd_dict"):
                    cd = z.ZstdCompressionDict(d)
                    self._dctx[cd.dict_id()] = z.ZstdDecompressor(dict_data=cd)
            self._z = z
        self.has_category = "category" in tabs
        self.has_meta = "schema_meta" in tabs
        self._sig = sig

    def decode(self, content):
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if content[:4] == b"\x28\xb5\x2f\xfd":
            p = self._z.get_frame_parameters(content)
            d = self._dctx.get(p.dict_id) if p.dict_id else self._plain
            if d is None:
                return ""
            return d.decompress(content, max_output_size=1 << 24).decode("utf-8", "replace")
        return content.decode("utf-8", "replace")

    def meta(self, con):
        self._prepare(con)
        if not self.has_meta:
            return {}
        return {k: v for k, v in con.execute("select key, value from schema_meta")}

    def catalog(self, con):
        """{book_id: (title, category path)}"""
        self._prepare(con)
        cats = {}
        if self.has_category:
            cats = {cid: (parent, title) for cid, parent, title in con.execute("select id, parentId, title from category")}
        return {bid: (title, category_path(cats, cid)) for bid, title, cid in
                con.execute("select id, title, categoryId from book")}

    def _rows(self, con, book_id=None):
        # the rows of a book in Otzaria's order (line_source.rs book_sql)
        if self.compressed:
            q = ("select l.bookId, l.lineIndex, c.content from line l left join line_content c on c.id = l.id")
        else:
            q = "select l.bookId, l.lineIndex, l.content from line l"
        if book_id is None:
            return con.execute(q + " order by l.bookId, l.lineIndex, l.id")
        return con.execute(q + " where l.bookId = ? order by l.lineIndex, l.id", (book_id,))

    @staticmethod
    def _assemble(pairs):
        """The book's lines as Otzaria indexes them: its rows joined by \\n, then split on \\n
        (so a row holding a line break is two lines, and a gap in lineIndex is no line)."""
        rows = [text for _li, text in pairs]
        if any("\n" in t for t in rows):
            return "\n".join(rows).split("\n")
        return rows

    def book_lines(self, con, book_id):
        self._prepare(con)
        return self._assemble((li, self.decode(c)) for _b, li, c in self._rows(con, book_id))

    def iter_books(self, con):
        """(book_id, lines) for every book, in one pass."""
        self._prepare(con)
        cur, acc = None, []
        for bid, li, c in self._rows(con):
            if bid != cur:
                if cur is not None:
                    yield cur, self._assemble(acc)
                cur, acc = bid, []
            acc.append((li, self.decode(c)))
        if cur is not None:
            yield cur, self._assemble(acc)


def user_books(databases_dir):
    """Personal .txt books: [(name, title, path)], and how many of another format were skipped."""
    db = Path(databases_dir) / "user_books.db"
    if not db.exists():
        return [], 0
    try:
        con = sqlite3.connect(ro_uri(db), uri=True)
        rows = con.execute("select id, title, filePath, fileType from book where filePath is not null").fetchall()
    except sqlite3.Error:  # Otzaria holds it open
        con = sqlite3.connect(ro_uri(db) + "&immutable=1", uri=True)
        rows = con.execute("select id, title, filePath, fileType from book where filePath is not null").fetchall()
    finally:
        con.close()
    out, skipped = [], 0
    for bid, title, fp, ftype in rows:
        if (ftype or "").lower() not in ("txt", "") or not fp.lower().endswith(".txt"):
            skipped += 1
            continue
        if os.path.isfile(fp):
            out.append({"name": f"uid:{int(bid)}", "title": title, "path": fp})
    return out, skipped


def attached_books(settings):
    """Books of the attached databases Otzaria lists: [(name, title, db, id)], and a report."""
    out, report, seen = [], [], set()
    for e in settings.get("attached", []):
        path, slug = e.get("path"), str(e.get("slug") or "")
        if not path or not SLUG_RE.match(slug) or slug in seen:
            continue
        seen.add(slug)
        name = e.get("displayName") or slug
        if e.get("hidden") or (e.get("status") or "ok") != "ok":
            report.append(f"{name}: skipped ({'hidden' if e.get('hidden') else e.get('status')})")
            continue
        try:
            db = BookDB(path)
            con = db.connect()
            try:
                cat = db.catalog(con)
            finally:
                con.close()
        except Exception as ex:
            report.append(f"{name}: cannot be read ({ex})")
            continue
        for bid, (title, _c) in cat.items():
            out.append({"name": f"db:{slug}:{int(bid)}", "title": title, "db": path, "book_id": int(bid)})
        report.append(f"{name}: {len(cat):,} books")
    return out, report


def read_lines(book, dbs):
    if "path" in book:
        with open(book["path"], "rb") as f:
            return decode_book(f.read()).split("\n")
    db = dbs.get(book["db"])
    if db is None:
        db = dbs[book["db"]] = BookDB(book["db"])
    con = db.connect()
    try:
        return db.book_lines(con, book["book_id"])
    finally:
        con.close()


# ------------------------------------------------------------------ worker processes: read and key

_W = {}


def _winit(known_path):
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C is handled by the main process
    _W["known"] = np.load(known_path, mmap_mode="r")
    _W["dbs"] = {}


def _prepare(book):
    """A book's passages: keys, lines, windows, the key of each line's first passage (its
    anchor), and the text of the passages no known vector has."""
    try:
        lines = read_lines(book, _W["dbs"])
    except Exception as e:
        return {"book": book, "error": str(e)}
    ps = windowed_passages(lines, from_db="db" in book)
    keys = passage_keys([t for _i, t, _w in ps])
    line = np.array([i for i, _t, _w in ps], np.int64)
    win = np.array([w for _i, _t, w in ps], np.uint8)
    anchor = np.zeros(len(ps), np.uint64)
    first = {}
    for j, (i, _t, w) in enumerate(ps):
        if w == 0:
            first[i] = keys[j]
    for j, (i, _t, w) in enumerate(ps):
        anchor[j] = first.get(i, 0)
    texts, known = {}, {}
    if len(ps):
        found = lookup_sorted(_W["known"], keys) >= 0
        for j in np.flatnonzero(~found).tolist():
            texts.setdefault(int(keys[j]), ps[j][1])
        for j in np.flatnonzero(found & (win == 0))[:2].tolist():  # for the model check
            known[int(keys[j])] = ps[j][1]
    return {"book": book, "key": keys, "line": line, "win": win, "anchor": anchor, "texts": texts,
            "known": known}


# ------------------------------------------------------------------ Otzaria's vector set

def read_header(path):
    with open(path, "rb") as f:
        h = f.read(HEADER_LEN)
    if len(h) < HEADER_LEN or h[:8] != MAGIC:
        raise ValueError(f"{path}: not an .oxv segment")
    if struct.unpack_from("<I", h, 4088)[0] != crc(h[:4088]):
        raise ValueError(f"{path}: header CRC")
    version, hlen, flags, dim, codec, block, nsec = struct.unpack_from("<IIIHHII", h, 8)
    slots, extras, foreign, tombs = struct.unpack_from("<4Q", h, 112)
    books, frm, to = struct.unpack_from("<3I", h, 144)
    tag = h[160:224].split(b"\0")[0].decode("utf-8")
    sections = []
    for i in range(nsec):
        kind, fl, es, off, ln, cnt, c = struct.unpack_from("<HHIQQQI", h, 224 + 40 * i)
        sections.append({"kind": kind, "flags": fl, "elem": es, "off": off, "len": ln, "count": cnt, "crc": c})
    return {"version": version, "flags": flags, "dim": dim, "codec": codec, "block": block,
            "id": h[32:48], "identity_digest": h[48:80], "params_sha256": h[80:112],
            "slots": slots, "extras": extras, "foreign": foreign, "tombstones": tombs, "books": books,
            "from": frm, "to": to, "tag": tag, "sections": sections, "raw": h}


def section(path, header, kind):
    for s in header["sections"]:
        if s["kind"] == kind:
            with open(path, "rb") as f:
                f.seek(s["off"])
                return f.read(s["len"])
    return None


def write_segment(path, *, kind_flag, identity_digest, params, from_version, to_version, tag, dim,
                  books, names, keys, hints, extras=b"", extras_by_slot=b"", foreign=b"", tombstones=b"",
                  windows=None, occurrences=b"", book_hashes=None, fill_vectors, segment_id=None,
                  version=3, codec=CODEC_ROT, vector_bytes=160):
    """One .oxv file, laid out as otzaria-semantic-search's oxv::writer lays it out (format 3).

    books: BOOK_DT array; names: bytes; keys: '>u8' array; hints: '<u4' array (bit 31 already set
    for slots with extras); extras/extras_by_slot/foreign/tombstones/occurrences: section bytes;
    windows: u1 array or None; book_hashes: '<u8' array or None;
    fill_vectors(vec, scales): fills an (n, vector_bytes) uint8 and an (n,) '<f4' array.
    segment_id None: derived from everything the file holds but the vectors' own bytes are
    covered by their block CRCs. -> {path, size, sha256, id, slots}"""
    n = len(keys)
    key_len = 8
    assert version == 3 and keys.dtype == np.dtype(">u8") and hints.dtype == np.dtype("<u4")
    small = [(BOOKS, 40, books.tobytes(), len(books)), (BOOK_NAMES, 1, names, len(names)),
             (CODEC_PARAMS, 1, params, len(params)), (EXTRAS, 8, extras, len(extras) // 8),
             (EXTRAS_BY_SLOT, 4, extras_by_slot, len(extras_by_slot) // 4),
             (FOREIGN, key_len + 8, foreign, len(foreign) // (key_len + 8)),
             (TOMBSTONES, key_len, tombstones, len(tombstones) // key_len)]
    hint_b, key_b = hints.tobytes(), keys.tobytes()
    win_b = windows.tobytes() if windows is not None and windows.any() else None
    hash_b = book_hashes.astype("<u8").tobytes() if book_hashes is not None else None
    blocks = lambda ln: -(-ln // BLOCK)
    big = [(SLOT_HINTS, 4, len(hint_b), n), (SLOT_KEYS, key_len, len(key_b), n),
           (VECTORS, vector_bytes, n * vector_bytes, n), (SCALES, 4, n * 4, n)]
    if win_b is not None:
        big.append((SLOT_WINDOWS, 1, len(win_b), n))
    if occurrences:
        big.append((OCCURRENCES, 2 * key_len + 12, len(occurrences), len(occurrences) // (2 * key_len + 12)))
    if hash_b is not None:
        big.append((BOOK_HASHES, 8, len(hash_b), len(books)))
    crc_count = sum(blocks(ln) for _k, _e, ln, _c in big)
    plan = [(k, e, len(b), c, False) for k, e, b, c in small] + [(BLOCK_CRCS, 4, crc_count * 4, crc_count, False)] \
        + [(k, e, ln, c, True) for k, e, ln, c in big]
    off, sections = HEADER_LEN, []
    for k, e, ln, c, summed in plan:
        align = 4096 if k == VECTORS else 64
        off = -(-off // align) * align
        sections.append({"kind": k, "flags": SEC_CRITICAL | (SEC_BLOCKSUMMED if summed else 0), "elem": e,
                         "off": off, "len": ln, "count": c, "crc": 0})
        off += ln
    total = off
    by_kind = {s["kind"]: s for s in sections}
    path = Path(path)
    with open(path, "wb") as f:
        f.truncate(total)
    block_crcs = []

    def put(kind, data):
        s = by_kind[kind]
        with open(path, "r+b") as f:
            f.seek(s["off"])
            f.write(data)

    for (k, _e, b, _c), s in zip(small, sections):
        put(k, b)
        s["crc"] = crc(b)
    put(SLOT_HINTS, hint_b)
    put(SLOT_KEYS, key_b)
    for b in (hint_b, key_b):
        block_crcs += [crc(b[i:i + BLOCK]) for i in range(0, len(b), BLOCK)]
    scales = np.zeros(n, "<f4")
    if n:
        vs = by_kind[VECTORS]
        vec = np.memmap(path, np.uint8, "r+", vs["off"], (n, vector_bytes))
        fill_vectors(vec, scales)
        vec.flush()
        del vec
        with open(path, "rb") as f:
            f.seek(vs["off"])
            left = vs["len"]
            while left:
                chunk = f.read(min(BLOCK, left))
                block_crcs.append(crc(chunk))
                left -= len(chunk)
    tail = [(SCALES, scales.tobytes())]
    if win_b is not None:
        tail.append((SLOT_WINDOWS, win_b))
    if occurrences:
        tail.append((OCCURRENCES, occurrences))
    if hash_b is not None:
        tail.append((BOOK_HASHES, hash_b))
    for k, b in tail:
        put(k, b)
        block_crcs += [crc(b[i:i + BLOCK]) for i in range(0, len(b), BLOCK)]
    crc_b = np.asarray(block_crcs, "<u4").tobytes()
    assert len(crc_b) == by_kind[BLOCK_CRCS]["len"], "the block checksums fill their section"
    put(BLOCK_CRCS, crc_b)
    by_kind[BLOCK_CRCS]["crc"] = crc(crc_b)

    flags = kind_flag | (FLAG_FOREIGN if foreign else 0) | (FLAG_TOMBS if tombstones else 0)
    h = bytearray(HEADER_LEN)
    h[0:8] = MAGIC
    struct.pack_into("<IIIHHII", h, 8, version, HEADER_LEN, flags, dim, codec, BLOCK, len(sections))
    h[48:80] = identity_digest
    h[80:112] = hashlib.sha256(params).digest()
    struct.pack_into("<4Q", h, 112, n, len(extras) // 8, len(foreign) // (key_len + 8), len(tombstones) // key_len)
    struct.pack_into("<3I", h, 144, len(books), from_version, to_version)
    tb = tag.encode("utf-8")
    assert len(tb) <= 64
    h[160:160 + len(tb)] = tb
    for i, s in enumerate(sections):
        struct.pack_into("<HHIQQQI", h, 224 + 40 * i, s["kind"], s["flags"], s["elem"], s["off"], s["len"],
                         s["count"], s["crc"])
    if segment_id is None:
        d = hashlib.sha256(b"otzaria-sources-personal-segment\n")
        d.update(bytes(h[:4088]))
        for b in (hint_b, key_b, crc_b):
            d.update(hashlib.sha256(b).digest())
        segment_id = d.digest()[:16]
    h[32:48] = segment_id
    struct.pack_into("<I", h, 4088, crc(bytes(h[:4088])))
    with open(path, "r+b") as f:
        f.write(h)
        f.flush()
        os.fsync(f.fileno())
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 << 20), b""):
            sha.update(chunk)
    return {"path": path, "size": total, "sha256": sha.hexdigest(), "id": segment_id.hex(), "slots": n}


def self_test(src):
    """Rewrite a segment of Otzaria's from its own sections and compare every byte."""
    src = Path(src)
    h = read_header(src)
    sec = lambda k: section(src, h, k)
    vec_s = next(s for s in h["sections"] if s["kind"] == VECTORS)
    vsrc = np.memmap(src, np.uint8, "r", vec_s["off"], (h["slots"], vec_s["elem"]))
    ssrc = np.frombuffer(sec(SCALES), "<f4")

    def fill(vec, scales):
        step = 1 << 16
        for i in range(0, len(vec), step):
            vec[i:i + step] = vsrc[i:i + step]
        scales[:] = ssrc

    win, hashes = sec(SLOT_WINDOWS), sec(BOOK_HASHES)
    import tempfile
    out = Path(tempfile.gettempdir()) / (src.stem + ".selftest.oxv")
    r = write_segment(out, kind_flag=h["flags"] & 7, identity_digest=h["identity_digest"],
                      params=sec(CODEC_PARAMS), from_version=h["from"], to_version=h["to"], tag=h["tag"],
                      dim=h["dim"], books=np.frombuffer(sec(BOOKS), BOOK_DT), names=sec(BOOK_NAMES),
                      keys=np.frombuffer(sec(SLOT_KEYS), ">u8"), hints=np.frombuffer(sec(SLOT_HINTS), "<u4"),
                      extras=sec(EXTRAS), extras_by_slot=sec(EXTRAS_BY_SLOT), foreign=sec(FOREIGN),
                      tombstones=sec(TOMBSTONES),
                      windows=np.frombuffer(win, "u1") if win is not None else None,
                      occurrences=sec(OCCURRENCES) or b"",
                      book_hashes=np.frombuffer(hashes, "<u8") if hashes is not None else None,
                      fill_vectors=fill, segment_id=h["id"], version=h["version"], codec=h["codec"],
                      vector_bytes=vec_s["elem"])
    sha = hashlib.sha256()
    with open(src, "rb") as f:
        for chunk in iter(lambda: f.read(4 << 20), b""):
            sha.update(chunk)
    same = sha.hexdigest() == r["sha256"] and src.stat().st_size == r["size"]
    log(f"{src.name}: {h['slots']:,} slots, {src.stat().st_size:,} bytes; rewritten: {r['size']:,} bytes, "
        + ("IDENTICAL (sha256 " + r["sha256"][:16] + "...)" if same else f"DIFFERENT ({sha.hexdigest()[:16]} vs {r['sha256'][:16]})"))
    out.unlink(missing_ok=True)
    return same


class Rot:
    """rot192-i8i4, from a set's CODEC_PARAMS (codec.rs): y = R x; the first `head` components
    whitened by a frozen scale and coded int8 with one scale per vector; the next `tail` coded in
    4 bits over a frozen range, two to a byte, the even one in the low nibble."""

    def __init__(self, params):
        ver, dim, out_dim, head, tail = struct.unpack_from("<5I", params, 0)
        if ver != 1 or head + tail != out_dim or tail % 2:
            raise ValueError(f"rot192-i8i4 parameters v{ver}")
        fl = np.frombuffer(params, "<f4", offset=20)
        if len(fl) != head + 2 * tail + out_dim * dim:
            raise ValueError("rot192-i8i4 parameters: length")
        self.dim, self.head, self.tail = dim, head, tail
        self.sigma = fl[:head].astype(np.float32)
        self.lo = fl[head:head + tail].astype(np.float32)
        self.hi = fl[head + tail:head + 2 * tail].astype(np.float32)
        self.R = fl[head + 2 * tail:].reshape(out_dim, dim).astype(np.float64)
        self.bytes = head + tail // 2

    @staticmethod
    def _round(x):  # f32::round: half away from zero
        return np.sign(x) * np.floor(np.abs(x) + np.float32(0.5))

    def encode(self, x):
        """(n, dim) float32 -> ((n, bytes) uint8, (n,) float32 scales)."""
        y = (x.astype(np.float64) @ self.R.T).astype(np.float32)
        z = y[:, :self.head] / self.sigma
        scale = (np.abs(z).max(axis=1) / np.float32(127.0)).astype(np.float32)
        safe = np.where(scale > 0, scale, np.float32(1.0))
        c = np.clip(self._round(z / safe[:, None]), -127, 127).astype(np.int8)
        c[scale <= 0] = 0
        t = y[:, self.head:]
        q = np.clip(self._round((t - self.lo) / (self.hi - self.lo) * np.float32(15.0)), 0, 15).astype(np.uint8)
        out = np.empty((len(x), self.bytes), np.uint8)
        out[:, :self.head] = c.view(np.uint8)
        out[:, self.head:] = q[:, 0::2] | (q[:, 1::2] << 4)
        return out, scale

    def decode(self, b, scale):
        y = np.empty((len(b), self.head + self.tail), np.float64)
        y[:, :self.head] = b[:, :self.head].view(np.int8).astype(np.float32) * scale[:, None] * self.sigma
        p = b[:, self.head:]
        q = np.empty((len(b), self.tail), np.float32)
        q[:, 0::2], q[:, 1::2] = p & 15, p >> 4
        y[:, self.head:] = self.lo + q * ((self.hi - self.lo) / np.float32(15.0))
        return (y @ self.R).astype(np.float32)


def read_pointer(vdir, name):
    p = Path(vdir) / name
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8-sig"))


class LiveSet:
    """The generation CURRENT names: its document, its segments' headers, and the keys its
    segments hold live (not marked in the generation's .del files)."""

    def __init__(self, vdir):
        self.dir = Path(vdir)
        self.pointer = read_pointer(self.dir, "CURRENT")
        if not self.pointer:
            raise RuntimeError(f"no vector set in {self.dir} (no CURRENT)")
        self.set_path = self.dir / self.pointer["set"]
        raw = self.set_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.pointer["set_sha256"]:
            raise RuntimeError("CURRENT does not name the bytes of its set.json")
        self.doc = json.loads(raw.decode("utf-8"))
        self.gen_dir = self.set_path.parent
        self.headers = [read_header(self.dir / s["file"]) for s in self.doc["segments"]]

    @staticmethod
    def is_ours(seg):
        return is_ours(seg)

    def dead(self, seg, slots):
        b = (self.gen_dir / seg["del"]["file"]).read_bytes()
        if b[:8] != DEL_MAGIC or struct.unpack_from("<Q", b, 8)[0] != slots or crc(b[:-4]) != struct.unpack_from("<I", b, len(b) - 4)[0]:
            raise RuntimeError(f"{seg['del']['file']}: not this segment's tombstone bitmap")
        words = np.frombuffer(b, "<u8", offset=24, count=-(-slots // 64))
        return np.unpackbits(words.view(np.uint8), bitorder="little")[:slots].astype(bool)

    def live_keys(self, skip_ours=True):
        """Sorted unique keys of the live slots of every segment (but the personal one)."""
        out = []
        for seg, h in zip(self.doc["segments"], self.headers):
            if skip_ours and self.is_ours(seg):
                continue
            if h["version"] != 3:
                raise RuntimeError(f"{seg['file']} is segment format {h['version']}; this exporter writes format 3 only")
            s = next(x for x in h["sections"] if x["kind"] == SLOT_KEYS)
            keys = np.fromfile(self.dir / seg["file"], ">u8", h["slots"], offset=s["off"]).astype(np.uint64)
            out.append(keys[~self.dead(seg, h["slots"])])
        return np.unique(np.concatenate(out)) if out else np.zeros(0, np.uint64)


def otzaria_running():
    if os.name != "nt":
        return False
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq otzaria.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=20).stdout
        return "otzaria.exe" in out.lower()
    except Exception:
        return False


class SetLock:
    """<vectors>/.lock, as Otzaria takes it for an install (an exclusive lock on the file)."""

    def __init__(self, vdir):
        self.f = open(Path(vdir) / ".lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.f.close()
            raise RuntimeError("the vector set is locked: Otzaria is installing or compacting vectors")

    def release(self):
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.f.close()


def write_durable(path, data):
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def next_generation(vdir):
    nums = [0]
    for d in Path(vdir).glob("gen-*"):
        try:
            nums.append(int(d.name[4:]))
        except ValueError:
            pass
    for name in ("CURRENT", "PREVIOUS"):
        try:
            nums.append(int((read_pointer(vdir, name) or {}).get("generation", 0)))
        except (ValueError, OSError):
            pass
    return max(nums) + 1


def publish_generation(live, written, provenance_package, device):
    """A new generation: the live one's segments (the old personal one replaced or dropped),
    their .del/.links copied as they are, and `written` (None: just drop the personal one)."""
    vdir, doc = live.dir, live.doc
    gen = next_generation(vdir)
    gdir = vdir / f"gen-{gen:06d}"
    gdir.mkdir()
    others = [s for s in doc["segments"] if not live.is_ours(s)]
    at = next((i for i, s in enumerate(doc["segments"]) if live.is_ours(s)), None)
    for seg in others:
        for key in ("del", "links"):
            if seg.get(key):
                shutil.copyfile(live.gen_dir / seg[key]["file"], gdir / seg[key]["file"])
    # A delta Otzaria applied after the personal segment names segments by position in its
    # .links, so the personal segment keeps its place; otherwise it is the last one.
    middle = at is not None and at < len(others)
    segments = list(others)
    if written is None:
        if middle:
            raise RuntimeError("Otzaria applied an update after the personal segment; it cannot be removed from "
                               "the middle of the set (it disappears with Otzaria's next compaction or base set)")
    else:
        sid, n = written["id"], written["slots"]
        delb = DEL_MAGIC + struct.pack("<QQ", n, 0) + b"\0" * (8 * (-(-n // 64)))
        delb += struct.pack("<I", crc(delb))
        linkb = LINKS_MAGIC + struct.pack("<Q", 0)
        linkb += struct.pack("<I", crc(linkb))
        write_durable(gdir / f"{sid}.del", delb)
        write_durable(gdir / f"{sid}.links", linkb)
        version = written["version"]
        ours = {"id": sid, "file": f"segments/{sid}.oxv", "kind": "delta", "from": version, "to": version,
                "sha256": written["sha256"], "size": written["size"], "slots": n,
                "provenance": {"passage_package": provenance_package,
                               "worker": {"backend": WORKER, "device": device}},
                "del": {"file": f"{sid}.del", "crc32": crc(delb[:-4]), "count": 0},
                "links": {"file": f"{sid}.links", "crc32": crc(linkb[:-4]), "count": 0}}
        segments.insert(at if middle else len(others), ours)
    new = dict(doc)
    new["generation"] = gen
    new["segments"] = segments
    headers = {s["id"]: h for s, h in zip(doc["segments"], live.headers)}
    new["stats"] = {
        "slots": sum(s["slots"] for s in segments),
        "slots_dead": sum(s["del"]["count"] for s in segments),
        "foreign": sum(headers[s["id"]]["foreign"] for s in segments if s["id"] in headers),
        "foreign_unresolved": sum((s.get("links") or {}).get("count", 0) for s in segments),
        "bytes": sum(s["size"] for s in segments)}
    new["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    raw = json.dumps(new, ensure_ascii=False, indent=2).encode("utf-8")
    write_durable(gdir / "set.json", raw)
    pointer = json.dumps({"generation": gen, "set": f"gen-{gen:06d}/set.json",
                          "set_sha256": hashlib.sha256(raw).hexdigest()}, separators=(",", ":")).encode()
    write_durable(vdir / "PREVIOUS", (vdir / "CURRENT").read_bytes())
    write_durable(vdir / "CURRENT", pointer)
    return gen


def plan_segment(order, rows):
    """Records as oxv::writer assigns them: books in the byte order of their names; a key's slot
    belongs to the first book that holds it, later books hold an extra record; within a book a
    key is recorded once, at its first line. Occurrences: every (key, book, anchor, window)."""
    K, B, H, W, A = rows["K"], rows["B"], rows["H"], rows["W"], rows["A"]
    n = len(K)
    # one record per (key, book): its first line, lowest window
    o = np.lexsort((W, H, B, K))
    firstkb = np.ones(n, bool)
    firstkb[1:] = (K[o][1:] != K[o][:-1]) | (B[o][1:] != B[o][:-1])
    rec = o[firstkb]                                   # sorted by (K, B)
    firstk = np.ones(len(rec), bool)
    firstk[1:] = K[rec][1:] != K[rec][:-1]
    prim, extra = rec[firstk], rec[~firstk]
    po = np.lexsort((K[prim], W[prim], H[prim], B[prim]))
    prim = prim[po]                                     # slot order
    slot_keys = K[prim]
    ks = np.argsort(slot_keys, kind="stable")
    sorted_keys = slot_keys[ks]
    slot_of = lambda keys: ks[np.searchsorted(sorted_keys, keys)]
    nb = len(order)
    ex_slot = slot_of(K[extra]).astype(np.int64)
    eo = np.lexsort((ex_slot, H[extra], B[extra]))      # per book: by (hint, slot)
    extra, ex_slot = extra[eo], ex_slot[eo]
    hints = H[prim].astype(np.uint32)
    hints[np.unique(ex_slot).astype(np.int64)] |= HAS_EXTRAS
    books = np.zeros(nb, BOOK_DT)
    name_bytes = [s.encode("utf-8") for s in order]
    books["name_len"] = [len(b) for b in name_bytes]
    books["name_off"] = np.concatenate([[0], np.cumsum(books["name_len"])[:-1]]) if nb else []
    sc = np.bincount(B[prim], minlength=nb)
    ec = np.bincount(B[extra], minlength=nb)
    books["slot_count"], books["extra_count"] = sc, ec
    books["slot_start"] = np.concatenate([[0], np.cumsum(sc)[:-1]]) if nb else []
    books["extra_start"] = np.concatenate([[0], np.cumsum(ec)[:-1]]) if nb else []
    used = (sc + ec) > 0
    # books without a record are left out (the writer adds none): renumber
    renum = np.cumsum(used) - 1
    books = books[used]
    kept_names = [b for b, u in zip(name_bytes, used) if u]
    books["name_off"] = np.concatenate([[0], np.cumsum(books["name_len"])[:-1]]) if len(books) else []
    books["foreign_start"] = 0
    extras = np.zeros(len(extra), np.dtype([("slot", "<u4"), ("hint", "<u4")]))
    extras["slot"], extras["hint"] = ex_slot, H[extra]
    by_slot = np.lexsort((np.arange(len(extra)), ex_slot)).astype("<u4")
    # occurrences: unique (key, book, anchor, window) with the first hint, sorted (key, book, hint, anchor, window)
    o = np.lexsort((H, W, A, B, K))
    u = np.ones(n, bool)
    u[1:] = (K[o][1:] != K[o][:-1]) | (B[o][1:] != B[o][:-1]) | (A[o][1:] != A[o][:-1]) | (W[o][1:] != W[o][:-1])
    occ = o[u]
    occ = occ[np.lexsort((W[occ], A[occ], H[occ], B[occ], K[occ]))]
    occs = np.zeros(len(occ), OCC_DT)
    occs["key"], occs["book"], occs["hint"] = K[occ], renum[B[occ]], H[occ]
    occs["anchor"], occs["window"] = A[occ], W[occ]
    # a slot's window: the smallest among its key's occurrences
    windows = np.full(len(prim), 255, np.uint8)
    np.minimum.at(windows, slot_of(K[occ]), W[occ])
    return {"books": books, "names": b"".join(kept_names), "keys": slot_keys.astype(">u8"), "hints": hints.astype("<u4"),
            "extras": extras.tobytes(), "extras_by_slot": by_slot.tobytes(), "windows": windows,
            "occurrences": occs.tobytes(), "prim": prim, "n_extras": len(extra), "n_occ": len(occ),
            "n_books": int(used.sum())}


def live_vectors(live, keys):
    """Decoded vectors of Otzaria's set for some keys (those it holds): {key: unit vector}."""
    want = np.unique(np.asarray(keys, np.uint64))
    out = {}
    for seg, h in zip(live.doc["segments"], live.headers):
        p = live.dir / seg["file"]
        s = next(x for x in h["sections"] if x["kind"] == SLOT_KEYS)
        k = np.fromfile(p, ">u8", h["slots"], offset=s["off"]).astype(np.uint64)
        slots = np.flatnonzero(np.isin(k, want))
        if not len(slots):
            continue
        rot = Rot(section(p, h, CODEC_PARAMS))
        vs = next(x for x in h["sections"] if x["kind"] == VECTORS)
        vec = np.memmap(p, np.uint8, "r", vs["off"], (h["slots"], vs["elem"]))
        sc = np.memmap(p, "<f4", "r", next(x for x in h["sections"] if x["kind"] == SCALES)["off"], (h["slots"],))
        d = rot.decode(np.asarray(vec[slots]), np.asarray(sc[slots]))
        d /= np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-12)
        for kk, v in zip(k[slots].tolist(), d):
            out.setdefault(kk, v)
    return out


# ------------------------------------------------------------------ the personal segment in the set

class OwnVectors:
    """The live vectors of the personal segment already in Otzaria's set: published by an earlier
    run of this script, or by otzaria-sources (build_personal_index.py --publish-oxv), whose
    vectors this script's cache never saw. A passage whose key it holds is copied from it, not
    embedded again."""

    def __init__(self, live):
        self.parts = []
        for seg, h in zip(live.doc["segments"], live.headers):
            if not live.is_ours(seg):
                continue
            p = live.dir / seg["file"]
            s = next(x for x in h["sections"] if x["kind"] == SLOT_KEYS)
            k = np.fromfile(p, ">u8", h["slots"], offset=s["off"]).astype(np.uint64)
            ok = k != 0
            if seg.get("del"):
                ok &= ~live.dead(seg, h["slots"])
            slots = np.flatnonzero(ok)
            o = np.argsort(k[slots], kind="stable")
            self.parts.append((p, h, k[slots][o], slots[o]))
        self.keys = (np.unique(np.concatenate([x[2] for x in self.parts])) if self.parts
                     else np.zeros(0, np.uint64))

    def __len__(self):
        return len(self.keys)

    def fill(self, keys, rot, vec, scales, params):
        """Fill the rows of `keys` the segment holds, coded with the set's parameters (`params`).
        -> the rows filled."""
        keys = np.asarray(keys, np.uint64)
        have = np.zeros(len(keys), bool)
        for p, h, sk, sl in self.parts:
            need = np.flatnonzero(~have)
            if not len(need):
                break
            ix = lookup_sorted(sk, keys[need])
            m = ix >= 0
            if not m.any():
                continue
            rows, slots = need[m], sl[ix[m]]
            o = np.argsort(slots, kind="stable")  # read the file in order
            rows, slots = rows[o], slots[o]
            vs = next(x for x in h["sections"] if x["kind"] == VECTORS)
            vmap = np.memmap(p, np.uint8, "r", vs["off"], (h["slots"], vs["elem"]))
            smap = np.memmap(p, "<f4", "r", next(x for x in h["sections"] if x["kind"] == SCALES)["off"], (h["slots"],))
            seg_params = section(p, h, CODEC_PARAMS)
            step = 1 << 18
            for i in range(0, len(slots), step):
                c, sc = np.asarray(vmap[slots[i:i + step]]), np.asarray(smap[slots[i:i + step]])
                if seg_params != params:  # coded with other parameters: decoded and coded again
                    c, sc = rot.encode(Rot(seg_params).decode(c, sc))
                vec[rows[i:i + step]] = c
                scales[rows[i:i + step]] = sc
            have[rows] = True
            del vmap, smap
        return have


# ------------------------------------------------------------------ the vector cache

class Cache:
    """Every vector embedded so far, rot192-i8i4-coded as Otzaria's set stores them, in parts of
    <cache>/<model>/part-*.npz (keys, codes, scales), with the codec parameters they were coded
    with. A part is written every few thousand vectors, so an interrupted run loses little."""

    def __init__(self, root, family, params):
        safe = re.sub(r"[^\w.\-]+", "_", family)
        self.dir = Path(root) / safe
        self.dir.mkdir(parents=True, exist_ok=True)
        self.params = params
        pfile = self.dir / "codec_params.bin"
        if pfile.exists() and pfile.read_bytes() != params:
            # the set's codec changed under the same model: the cached codes are re-coded on read
            self.old = Rot(pfile.read_bytes())
        else:
            self.old = None
            if not pfile.exists():
                write_durable(pfile, params)
        self.parts = sorted(self.dir.glob("part-*.npz"))
        keys = []
        for p in self.parts:
            try:
                with np.load(p) as z:
                    keys.append(z["keys"].astype(np.uint64))
            except Exception:
                log(f"  cache part {p.name} is unreadable and is ignored")
                keys.append(np.zeros(0, np.uint64))
        self.part_keys = keys
        self.keys = np.unique(np.concatenate(keys)) if keys else np.zeros(0, np.uint64)
        self.pending = []

    def __len__(self):
        return len(self.keys)

    def add(self, keys, codes, scales):
        self.pending.append((keys, codes, scales))
        if sum(len(k) for k, _c, _s in self.pending) >= 20000:
            self.flush()

    def flush(self):
        if not self.pending:
            return
        k = np.concatenate([x[0] for x in self.pending]).astype(np.uint64)
        c = np.concatenate([x[1] for x in self.pending])
        s = np.concatenate([x[2] for x in self.pending]).astype(np.float32)
        n = len(self.parts) + 1
        p = self.dir / f"part-{n:06d}.npz"
        while p.exists():
            n += 1
            p = self.dir / f"part-{n:06d}.npz"
        tmp = self.dir / f"part-{n:06d}.tmp.npz"
        np.savez(tmp, keys=k, codes=c, scales=s)
        os.replace(tmp, p)
        self.parts.append(p)
        self.part_keys.append(k)
        self.pending = []

    def fill(self, keys, rot, vec, scales, have=None):
        """Write the vectors of `keys` into vec / scales, row for row, coded with the set's
        parameters: every row `have` does not mark already filled must be in the cache. Each part
        of the cache is read once."""
        keys = np.asarray(keys, np.uint64)
        order = np.argsort(keys, kind="stable")
        sk = keys[order]
        have = np.zeros(len(keys), bool) if have is None else have
        for p, pk in zip(self.parts, self.part_keys):
            ix = lookup_sorted(sk, pk)
            hit = np.flatnonzero(ix >= 0)
            if not len(hit):
                continue
            with np.load(p) as z:
                c, s = z["codes"][hit], z["scales"][hit]
            if self.old is not None:
                c, s = rot.encode(self.old.decode(c, s))
            dst = order[ix[hit]]
            o = np.argsort(dst)
            vec[dst[o]] = c[o]
            scales[dst[o]] = s[o]
            have[dst] = True
        if not have.all():
            raise RuntimeError(f"{int((~have).sum())} vectors are missing from the cache")


# ------------------------------------------------------------------ the model

class Embedder:
    """The model's passage (document) graph in ONNX Runtime, as Otzaria builds its vectors:
    "[PASSAGE] " + text, at most max_tokens tokens, L2-normalized. Texts are batched by length
    (padding is masked; checked at start against one text at a time)."""

    def __init__(self, model_dir, max_tokens, gpu=False, threads=0):
        """gpu: True / "auto" try a GPU provider (CUDA, ROCm, DirectML) before the CPU."""
        import onnxruntime as ort
        from tokenizers import Tokenizer

        d = Path(model_dir)
        graph = d / "document.fp32.onnx"
        if not graph.exists():
            graphs = sorted(d.glob("*.onnx"))
            if len(graphs) != 1:
                raise RuntimeError(f"no document.fp32.onnx in {d}")
            graph = graphs[0]
        self.tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        self.tok.no_padding()
        self.tok.enable_truncation(max_length=max_tokens)
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if threads:
            so.intra_op_num_threads = threads
        providers = ["CPUExecutionProvider"]
        if gpu:
            avail = ort.get_available_providers()
            providers = [p for p in ("CUDAExecutionProvider", "ROCMExecutionProvider", "DmlExecutionProvider")
                         if p in avail] + providers
            if len(providers) == 1 and gpu is True:
                log("  --gpu: this onnxruntime has no GPU provider (install onnxruntime-gpu or "
                    "onnxruntime-directml); embedding on the CPU")
            if "DmlExecutionProvider" in providers:  # what DirectML requires
                so.enable_mem_pattern = False
                so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.sess = ort.InferenceSession(str(graph), so, providers=providers)
        self.device = self.sess.get_providers()[0].replace("ExecutionProvider", "")
        names = [o.name for o in self.sess.get_outputs()]
        self.out = next((n for n in ("sentence_embedding", "embedding") if n in names), names[0])
        self.inputs = {i.name for i in self.sess.get_inputs()}
        self.batch_tokens = 8192 if self.device != "CPU" else 4096
        self.graph = graph.name

    def _run(self, ids_list):
        L = max(len(x) for x in ids_list)
        ids = np.zeros((len(ids_list), L), np.int64)
        mask = np.zeros((len(ids_list), L), np.int64)
        for r, x in enumerate(ids_list):
            ids[r, :len(x)], mask[r, :len(x)] = x, 1
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        v = self.sess.run([self.out], feed)[0].astype(np.float32)
        return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)

    def embed(self, texts):
        enc = [np.asarray(e.ids, np.int64) for e in self.tok.encode_batch(list(texts), add_special_tokens=True)]
        out = None
        order = np.argsort([-len(e) for e in enc], kind="stable")
        i = 0
        while i < len(order):
            L = len(enc[order[i]])
            n = max(1, self.batch_tokens // max(L, 1))
            idx = order[i:i + n]
            v = self._run([enc[j] for j in idx])
            if out is None:
                out = np.zeros((len(enc), v.shape[1]), np.float32)
            out[idx] = v
            i += len(idx)
        return out if out is not None else np.zeros((0, 0), np.float32)

    def check_batching(self, texts):
        """Batched vectors must equal one-at-a-time ones (the padding is masked); else batch 1."""
        texts = list(texts)[:16]
        if len(texts) < 2:
            return
        a = self.embed(texts)
        b = np.stack([self._run([np.asarray(self.tok.encode(t).ids, np.int64)])[0] for t in texts])
        worst = float((a * b).sum(1).min())
        if worst < 0.9999:
            log(f"  batched embedding differs from single (cosine {worst:.5f}): embedding one text at a time")
            self.batch_tokens = 1


def find_model_dir(folders, family):
    folders = [folders] if isinstance(folders, (str, Path)) else list(folders)
    for mj in [m for f in folders for m in sorted(Path(f).glob("*/model.json"))]:
        try:
            if json.loads(mj.read_text(encoding="utf-8-sig")).get("family_id") == family:
                return mj.parent
        except (OSError, ValueError):
            continue
    raise RuntimeError(f"no model folder for {family} in {folders[0]}: open Otzaria's semantic search once "
                       f"so it downloads the model, or pass --model-dir")


# ------------------------------------------------------------------ the run

def is_ours(seg):
    return (seg.get("provenance") or {}).get("worker", {}).get("backend", "").startswith(OURS)


def run(args):
    loc = Locations(data=args.otzaria_dir or None, root=getattr(args, "library_dir", None) or None,
                    vectors=args.vectors_dir or None)
    log(f"Otzaria: {loc.describe()}")
    emit("locations", data=str(loc.data), library=str(loc.library), vectors=str(loc.vectors))
    otz = loc.data
    vdir = loc.vectors
    if not (vdir / "CURRENT").exists():
        raise RuntimeError(f"no vector set in {vdir} (no CURRENT): has Otzaria downloaded its semantic search? "
                           f"Otherwise name Otzaria's folder with --library-dir")
    live = LiveSet(vdir)
    doc = live.doc
    model = doc["identity"]["model"]
    family = model["family_id"]
    had = [s for s in doc["segments"] if is_ours(s)]
    log(f"Otzaria's vector set: generation {doc['generation']}, library v{doc['library_version']}, "
        f"{len(doc['segments'])} segment(s), {doc['stats']['slots']:,} vectors; model {family}"
        + (f"; personal segment of {had[0]['slots']:,} vectors" if had else ""))

    if args.remove:
        if not had:
            log("the set holds no personal segment")
            emit("no_segment")
            return
        if args.check:
            log(f"--remove would publish a generation without the personal segment ({had[0]['slots']:,} vectors)")
            return
        guard(vdir, args.force)
        lock = SetLock(vdir)
        try:
            gen = publish_generation(LiveSet(vdir), None, None, None)
        finally:
            lock.release()
        log(f"generation {gen} published without the personal segment")
        emit("removed", generation=gen)
        return

    # what this script can write: checked, not assumed
    if (model.get("embedding_text_version"), model.get("normalization_version")) != (TEXT_VERSION, NORMALIZATION_VERSION):
        raise RuntimeError(f"the set's model embeds text recipe {model.get('embedding_text_version')} / normalization "
                           f"{model.get('normalization_version')}; this script writes {TEXT_VERSION} / {NORMALIZATION_VERSION}")
    line_version = (doc["identity"].get("text") or {}).get("line_text_version")
    if line_version is not None and int(line_version) != LINE_TEXT_VERSION:
        raise RuntimeError(f"Otzaria's index stores its lines by line recipe {line_version}; this script cuts them by "
                           f"recipe {LINE_TEXT_VERSION}: a newer version of the script is needed")
    if int(model.get("chunking_identity", 0)) != recipe_identity():
        raise RuntimeError(f"the set's passages are cut by recipe {model.get('chunking_identity')}, not by the one this "
                           f"script knows ({recipe_identity()}): a newer version of the script is needed")
    if doc["identity"]["store"].get("vector_precision") != "rot192-i8i4" or any(
            h["codec"] != CODEC_ROT or h["version"] != 3 for h in live.headers):
        raise RuntimeError("Otzaria's set is not stored as segment format 3 / rot192-i8i4, the format this script writes")
    params = section(live.dir / doc["segments"][0]["file"], live.headers[0], CODEC_PARAMS)
    if hashlib.sha256(params).hexdigest() != doc["codec_params_sha256"]:
        raise RuntimeError("the base segment's codec parameters are not the set's")
    rot = Rot(params)
    model_dir = Path(args.model_dir) if args.model_dir else find_model_dir(loc.db_dirs, family)
    mj = json.loads((model_dir / "model.json").read_text(encoding="utf-8-sig"))
    if mj.get("family_id") != family:
        raise RuntimeError(f"{model_dir} holds {mj.get('family_id')}, the set {family}")
    if mj.get("tokenizer_checksum") and hashlib.sha256((model_dir / "tokenizer.json").read_bytes()).hexdigest() \
            != mj["tokenizer_checksum"]:
        raise RuntimeError(f"{model_dir / 'tokenizer.json'} is not the tokenizer the model declares")

    # the books
    settings = otzaria_settings(otz)
    if settings.get("error"):
        log(f"  {settings['error']}: attached databases are left out")
    ubooks, skipped = user_books(loc.databases)
    abooks, db_report = attached_books(settings)
    log(f"personal books: {len(ubooks):,} .txt" + (f" ({skipped} of another format skipped)" if skipped else ""))
    emit("books", personal=len(ubooks), skipped=skipped, attached=len(abooks), databases=db_report)
    for r in db_report:
        log(f"attached database {r}")
    books = sorted(ubooks + abooks, key=lambda b: b["name"].encode("utf-8"))
    if not books:
        log("no personal or attached books")
        return

    log("reading Otzaria's set ...")
    live_keys = live.live_keys(skip_ours=True)
    own = OwnVectors(live)
    cache = Cache(args.cache_dir, family, params)
    work = Path(args.cache_dir) / "work"
    work.mkdir(parents=True, exist_ok=True)
    known_path = work / f"known-{os.getpid()}.npy"
    np.save(known_path, np.union1d(np.union1d(live_keys, cache.keys), own.keys))

    order, names = [], {}
    cols = {k: [] for k in "KBHWA"}
    st = {"passages": 0, "in_set": 0, "cached": 0, "errors": 0}
    new_texts, queued = {}, set()
    sample = {}  # a few texts whose vector the set holds: the model check
    emb = None
    embedded, t_emb = 0, [0.0]

    def make_emb(gpu):
        e = Embedder(model_dir, int(mj.get("max_tokens", 256)), gpu=gpu, threads=args.threads)
        log(f"model: {model_dir.name}/{e.graph} on {e.device}")
        e.check_batching(list(new_texts.values())[:16] or list(sample.values())[:16])
        if sample:
            ref = live_vectors(live, list(sample))
            ks = [k for k in sample if k in ref][:64]
            if ks:
                v = e.embed([sample[k] for k in ks])
                cos = (v * np.stack([ref[k] for k in ks])).sum(1)
                log(f"check against Otzaria's own vectors ({len(ks)} passages): mean cosine {cos.mean():.4f}, "
                    f"min {cos.min():.4f}")
                emit("model_check", device=e.device, mean=float(cos.mean()), n=len(ks))
                if cos.mean() < 0.98:
                    raise RuntimeError("the vectors this model gives are not Otzaria's: wrong model folder?")
        return e

    def get_emb():
        nonlocal emb
        if emb is None:
            try:
                emb = make_emb(args.gpu)
            except Exception as ex:
                if not args.gpu:
                    raise
                log(f"  the GPU did not work ({ex}); embedding on the CPU")
                emb = make_emb(False)
            emit("model", device=emb.device)
        return emb

    def embed_pending(final=False):
        nonlocal new_texts, embedded
        if not new_texts or (not final and len(new_texts) < args.group):
            return
        e = get_emb()
        if not t_emb[0]:
            t_emb[0] = time.time()
        keys = np.fromiter(new_texts.keys(), np.uint64, len(new_texts))
        texts = list(new_texts.values())
        step = 512 if HOOKS["progress"] else 2048
        for i in range(0, len(texts), step):
            check_cancel()
            v = e.embed(texts[i:i + step])
            c, s = rot.encode(v)
            embedded += len(v)
            cache.add(keys[i:i + step], c, s)
            rate = embedded / max(time.time() - t_emb[0], 1e-6)
            if HOOKS["progress"]:
                HOOKS["progress"](embedded, rate)
            elif sys.stdout:
                print(f"\r  {embedded:,} passages embedded ({rate:,.0f}/s)", end="", flush=True)
        new_texts = {}

    try:
        def results(ex):
            # in slices, so books read ahead of the embedding do not pile up in memory
            for i in range(0, len(books), 256):
                yield from ex.map(_prepare, books[i:i + 256], chunksize=2)

        with ProcessPoolExecutor(max_workers=args.workers, initializer=_winit, initargs=(str(known_path),)) as ex:
            for nread, r in enumerate(results(ex), 1):
                check_cancel()
                if nread % 20 == 0 or nread == len(books):
                    emit("reading", done=nread, total=len(books))
                b = r["book"]
                if "error" in r:
                    st["errors"] += 1
                    log(f"  cannot read {b['title']}: {r['error']}")
                    continue
                keys = r["key"]
                st["passages"] += len(keys)
                if not len(keys):
                    continue
                ins = lookup_sorted(live_keys, keys) >= 0
                st["in_set"] += int(ins.sum())
                if len(sample) < 64:
                    for k, t in r["known"].items():
                        if lookup_sorted(live_keys, np.array([k], np.uint64))[0] >= 0:
                            sample[k] = t
                keep = ~ins & (r["anchor"] != 0)
                if not keep.any():
                    continue
                bi = names.setdefault(b["name"], len(order))
                if bi == len(order):
                    order.append(b["name"])
                cols["K"].append(keys[keep]); cols["B"].append(np.full(int(keep.sum()), bi, np.int64))
                cols["H"].append(r["line"][keep]); cols["W"].append(r["win"][keep]); cols["A"].append(r["anchor"][keep])
                for k, t in r["texts"].items():
                    if k not in queued:
                        queued.add(k)
                        if not args.check:
                            new_texts[k] = t
                if not args.check:
                    embed_pending()
            if not args.check:
                embed_pending(final=True)
                if embedded and sys.stdout and not HOOKS["progress"]:
                    print()
    except KeyboardInterrupt:
        cache.flush()
        log(f"\ninterrupted: {embedded:,} passages embedded and kept in the cache; run again to continue")
        known_path.unlink(missing_ok=True)
        emit("cancelled", embedded=embedded)
        raise Cancelled(embedded)
    finally:
        cache.flush()
        known_path.unlink(missing_ok=True)

    rows = {"K": np.concatenate(cols["K"]) if cols["K"] else np.zeros(0, np.uint64)}
    for k, dt in (("B", np.int64), ("H", np.int64), ("W", np.uint8), ("A", np.uint64)):
        rows[k] = np.concatenate(cols[k]).astype(dt) if cols[k] else np.zeros(0, dt)
    missing = len(queued)
    uk = np.unique(rows["K"])
    to_publish = len(uk)
    from_own = int((lookup_sorted(own.keys, uk) >= 0).sum()) if len(own) else 0
    report = {"books": len(books), "passages": st["passages"], "in_set": st["in_set"], "to_publish": to_publish,
              "to_embed": missing if args.check else 0, "embedded": embedded, "errors": st["errors"],
              "from_personal_segment": from_own,
              "published": False}
    emit("summary", **report)
    log(f"{len(books):,} books, {st['passages']:,} passages: {st['in_set']:,} already in Otzaria's set, "
        f"{to_publish:,} distinct passages to publish"
        + (f" ({', '.join(x for x in (f'{from_own:,} copied from the personal segment already in the set' if from_own else '', f'{missing:,} still to embed' if args.check else '') if x)})"
           if from_own or args.check else "")
        + (f"; {st['errors']} books could not be read" if st["errors"] else ""))
    if args.check:
        if missing:
            log(f"--check: nothing was embedded or written. Embedding {missing:,} passages takes, very roughly, "
                f"{missing / 50 / 3600:,.1f} hours on a CPU")
        return report
    if not len(rows["K"]):
        log("nothing to publish: Otzaria's set already holds every passage of these books"
            + (" (the personal segment stays as it is; --remove takes it out)" if had else ""))
        emit("nothing")
        return report

    plan = plan_segment(order, rows)
    n = len(plan["keys"])
    log(f"segment: {n:,} vectors, {plan['n_books']:,} books, {plan['n_occ']:,} occurrences "
        f"({n * (rot.bytes + 24) / 1e6:,.0f} MB)")
    guard(vdir, args.force)

    def fill(vec, scales):
        keys = plan["keys"].astype(np.uint64)
        have = own.fill(keys, rot, vec, scales, params)
        cache.fill(keys, rot, vec, scales, have=have)

    lock = SetLock(vdir)
    try:
        live2 = LiveSet(vdir)
        if live2.doc["generation"] != doc["generation"]:
            raise RuntimeError("the set changed during the run (did Otzaria update it?): run again")
        segs = live2.doc["segments"]
        pv = next((s["to"] for i, s in enumerate(segs) if is_ours(s) and i < len(segs) - 1), doc["library_version"])
        tmp = vdir / "segments" / "personal-vectors.oxv.tmp"
        tmp.unlink(missing_ok=True)
        w = write_segment(tmp, kind_flag=FLAG_DELTA, identity_digest=bytes.fromhex(doc["identity_digest"]),
                          params=params, from_version=pv, to_version=pv, tag=doc.get("library_release_tag", ""),
                          dim=rot.dim, books=plan["books"], names=plan["names"], keys=plan["keys"],
                          hints=plan["hints"], extras=plan["extras"], extras_by_slot=plan["extras_by_slot"],
                          windows=plan["windows"], occurrences=plan["occurrences"],
                          book_hashes=np.zeros(len(plan["books"]), "<u8"), fill_vectors=fill)
        if had and had[0]["id"] == w["id"]:
            tmp.unlink()
            log("the personal segment in the set is already this one: nothing published")
            emit("unchanged")
            return report
        final = vdir / "segments" / f"{w['id']}.oxv"
        os.replace(tmp, final)
        w["path"], w["version"] = final, pv
        package = next((p for p in model.get("query_packages", []) if p.get("quantization") == "fp32"), None) \
            or doc["segments"][0].get("provenance", {}).get("passage_package")
        gen = publish_generation(live2, w, package, f"{model_dir.name}/{mj.get('family_id')} (onnxruntime)")
    finally:
        lock.release()
    log(f"published: generation {gen}, segment {w['id']} ({w['size'] / 1e6:,.0f} MB). "
        f"Otzaria uses it the next time it starts.")
    emit("published", generation=gen, vectors=n)
    report["published"] = True
    return report


def guard(vdir, force):
    if force:
        return
    while otzaria_running():
        if not HOOKS["confirm_closed"]:
            raise RuntimeError("Otzaria is running: close it and run again (--force publishes anyway)")
        if not HOOKS["confirm_closed"]():
            raise Cancelled(0)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="report what would be embedded and published; change nothing")
    ap.add_argument("--remove", action="store_true", help="publish a generation without the personal segment")
    ap.add_argument("--gpu", action="store_true", help="embed on a GPU (onnxruntime-gpu / onnxruntime-directml)")
    ap.add_argument("--auto-gpu", dest="gpu", action="store_const", const="auto",
                    help="use a GPU when there is one that works, else the CPU")
    ap.add_argument("--library-dir", default="", help="Otzaria's folder that holds books and vectors, when it is not "
                    "found by itself (e.g. C:\\ProgramData\\otzaria, or a portable copy's folder)")
    ap.add_argument("--otzaria-dir", default="", help="Otzaria's settings folder (default: %%APPDATA%%\\otzaria)")
    ap.add_argument("--vectors-dir", default="", help="Otzaria's vector set (default: beside the library folder)")
    ap.add_argument("--model-dir", default="", help="the model folder (default: found beside seforim.db)")
    ap.add_argument("--cache-dir", default=str(default_cache_dir()), help="the embedded vectors (default: %%LOCALAPPDATA%%\\otzaria-personal-vectors)")
    ap.add_argument("--threads", type=int, default=0, help="CPU threads for the model (default: all)")
    ap.add_argument("--workers", type=int, default=max(1, min(4, (os.cpu_count() or 2) - 1)), help="processes reading the books")
    ap.add_argument("--group", type=int, default=20000, help="passages embedded per group")
    ap.add_argument("--force", action="store_true", help="publish although Otzaria seems to be running")
    ap.add_argument("--self-test", metavar="SEGMENT", help="rewrite an .oxv of Otzaria's from its sections and compare the bytes")
    args = ap.parse_args()
    if args.self_test:
        raise SystemExit(0 if self_test(args.self_test) else 1)
    try:
        run(args)
    except Cancelled:
        raise SystemExit(1)
    except RuntimeError as e:
        log(f"stopped: {e}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
