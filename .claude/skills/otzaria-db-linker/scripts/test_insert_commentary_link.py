"""insert_commentary_link.py stores links exactly as SeforimLibrary's Generator.kt would.

Run: python3 -m unittest -v test_insert_commentary_link   (from this directory)
"""
import contextlib
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("icl", HERE / "insert_commentary_link.py")
icl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(icl)

ON = " על "  # the " al " separator titleDeclaresDependencyOn looks for
ENUM = [
    "COMMENTARY", "SUPER_COMMENTARY", "TARGUM", "REFERENCE", "SOURCE", "MIDRASH", "QUOTATION",
    "MESORAT_HASHAS", "EIN_MISHPAT", "DIBUR_HAMATCHIL", "PARSHANUT", "MISHNAH_IN_TALMUD",
    "RELATED", "OTHER", "LINKER", "SIFREI_MITZVOT", "ESSAY", "ALLUSION", "LITURGY",
    "ELUCIDATION", "EXPLICATION", "LAW", "SUMMARY", "FOOTNOTES",
]
SCHEMA = """
CREATE TABLE source (id INTEGER PRIMARY KEY NOT NULL, name TEXT NOT NULL UNIQUE);
CREATE TABLE book (
    id INTEGER PRIMARY KEY NOT NULL, categoryId INTEGER NOT NULL DEFAULT 1,
    sourceId INTEGER NOT NULL, title TEXT NOT NULL,
    orderIndex INTEGER NOT NULL DEFAULT 999, totalLines INTEGER NOT NULL DEFAULT 0,
    isBaseBook INTEGER NOT NULL DEFAULT 0,
    hasTargumConnection INTEGER NOT NULL DEFAULT 0, hasReferenceConnection INTEGER NOT NULL DEFAULT 0,
    hasSourceConnection INTEGER NOT NULL DEFAULT 0, hasCommentaryConnection INTEGER NOT NULL DEFAULT 0,
    hasOtherConnection INTEGER NOT NULL DEFAULT 0);
CREATE TABLE line (id INTEGER PRIMARY KEY NOT NULL, bookId INTEGER NOT NULL,
    lineIndex INTEGER NOT NULL, content TEXT NOT NULL);
CREATE TABLE connection_type (id INTEGER PRIMARY KEY NOT NULL, name TEXT NOT NULL UNIQUE);
CREATE TABLE link (
    id INTEGER PRIMARY KEY NOT NULL, sourceBookId INTEGER NOT NULL, targetBookId INTEGER NOT NULL,
    sourceLineId INTEGER NOT NULL, targetLineId INTEGER NOT NULL, targetLineIndex INTEGER NOT NULL,
    targetBookOrderIndex INTEGER NOT NULL, connectionTypeId INTEGER NOT NULL,
    baseProvenance INTEGER NOT NULL DEFAULT 0);
CREATE TABLE link_anchor (linkId INTEGER NOT NULL, side INTEGER NOT NULL DEFAULT 0,
    charStart INTEGER NOT NULL, charEnd INTEGER, label TEXT, PRIMARY KEY (linkId, side, charStart));
CREATE TABLE link_range (linkId INTEGER NOT NULL, side INTEGER NOT NULL, endLineId INTEGER NOT NULL,
    endLineIndex INTEGER NOT NULL, PRIMARY KEY (linkId, side));
CREATE TABLE link_coverage (lineId INTEGER NOT NULL, linkId INTEGER NOT NULL, side INTEGER NOT NULL,
    PRIMARY KEY (lineId, linkId, side));
CREATE TABLE book_has_links (bookId INTEGER PRIMARY KEY, hasSourceLinks INTEGER NOT NULL DEFAULT 0,
    hasTargetLinks INTEGER NOT NULL DEFAULT 0);
"""
BASE = "Base"
CITING = "Notes" + ON + BASE     # a title that declares its base
PLAIN = "Notes"                  # a title that does not
REF = "Other"                    # a non-base book
FLAG_COLS = ("hasTargumConnection", "hasReferenceConnection", "hasSourceConnection",
             "hasCommentaryConnection", "hasOtherConnection")


def build_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO connection_type VALUES (?, ?)", list(enumerate(ENUM, start=1)))
    conn.execute("INSERT INTO source VALUES (1, 'DictaToOtzaria')")
    books = [(1, BASE, 1, 1), (2, CITING, 0, 2), (3, PLAIN, 0, 3), (4, REF, 0, 4)]
    conn.executemany(
        "INSERT INTO book (id, sourceId, title, isBaseBook, orderIndex, totalLines) "
        "VALUES (?, 1, ?, ?, ?, 5)", books
    )
    line_id = 1
    for book_id, *_ in books:
        for idx in range(5):
            content = "<h2>head</h2>" if idx == 4 else f"<b>w{idx}</b> text {idx}"
            conn.execute("INSERT INTO line VALUES (?, ?, ?, ?)", (line_id, book_id, idx, content))
            line_id += 1
    conn.commit()
    conn.close()


def line_id(book_id: int, idx: int) -> int:
    return (book_id - 1) * 5 + idx + 1


def e(li1, li2, ctype, path_2=BASE + ".txt", **extra):
    return {"line_index_1": li1, "line_index_2": li2, "heRef_2": "x", "path_2": path_2,
            "Conection Type": ctype, **extra}


class Fixture:
    def __init__(self, citing_title: str, entries: list, **cfg):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "seforim.db"
        build_db(self.db)
        links = root / f"{citing_title}_links.json"   # the name the generator reads it by
        links.write_text(json.dumps(entries), encoding="utf-8")
        self.cfg_path = root / "job.json"
        self.cfg = {"project_root": str(root), "citing_title": citing_title, "target_title": BASE,
                    "links_json_path": str(links), "seforim_db_path": str(self.db),
                    "keep_backups": 1, "replace_existing": False, "dry_run": False, **cfg}

    def run(self, entries=None, **cfg) -> str:
        if entries is not None:
            Path(self.cfg["links_json_path"]).write_text(json.dumps(entries), encoding="utf-8")
        self.cfg.update(cfg)
        self.cfg_path.write_text(json.dumps(self.cfg), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = icl.run(self.cfg_path)
        assert rc == 0, out.getvalue()
        return out.getvalue()

    def q(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def links(self):
        return self.q("SELECT l.sourceBookId, l.targetBookId, l.sourceLineId, l.targetLineId, "
                      "l.targetLineIndex, l.targetBookOrderIndex, ct.name, l.baseProvenance "
                      "FROM link l JOIN connection_type ct ON ct.id = l.connectionTypeId ORDER BY l.id")

    def flags(self, book_id):
        return dict(zip(FLAG_COLS, self.q(f"SELECT {', '.join(FLAG_COLS)} FROM book WHERE id=?", (book_id,))[0]))

    def close(self):
        self.tmp.cleanup()


class PlanStorageTests(unittest.TestCase):
    """plan_storage == Generator.processLinksForBook's flip/storedType decision."""

    def test_source_is_always_flipped_to_commentary(self):
        self.assertEqual(icl.plan_storage("SOURCE", PLAIN, False, REF, False), (True, "COMMENTARY"))

    def test_oriented_type_flips_only_when_title_declares_the_base(self):
        self.assertEqual(icl.plan_storage("FOOTNOTES", CITING, False, BASE, True), (True, "FOOTNOTES"))
        self.assertEqual(icl.plan_storage("FOOTNOTES", PLAIN, False, BASE, True), (False, "OTHER"))
        # target not a base book / citing is a base book -> stored as written
        self.assertEqual(icl.plan_storage("FOOTNOTES", BASE, True, PLAIN, False), (False, "FOOTNOTES"))
        self.assertEqual(icl.plan_storage("COMMENTARY", CITING, True, BASE, True), (False, "COMMENTARY"))

    def test_lateral_and_as_is_types_are_never_flipped(self):
        for t in ("MESORAT_HASHAS", "REFERENCE", "QUOTATION", "EXPLICATION", "OTHER"):
            self.assertEqual(icl.plan_storage(t, CITING, False, BASE, True), (False, t))

    def test_title_dependency_port(self):
        self.assertTrue(icl.title_declares_dependency_on(CITING, BASE))
        masechet = "מסכת " + BASE  # prefix stripped on both sides
        self.assertTrue(icl.title_declares_dependency_on("X" + ON + masechet, BASE))
        self.assertFalse(icl.title_declares_dependency_on(PLAIN, BASE))

    def test_generator_target_title(self):
        self.assertEqual(icl.generator_target_title("a\\b\\Book.txt"), "Book")
        self.assertEqual(icl.generator_target_title("dir/Book.txt"), "Book")
        self.assertEqual(icl.generator_target_title("Book"), "Book")

    def test_type_spellings(self):
        self.assertEqual(icl.canonical_type("footnote"), "FOOTNOTES")
        self.assertEqual(icl.canonical_type("ein mishpat / ner mitsvah"), "EIN_MISHPAT")
        with self.assertRaises(ValueError):
            icl.canonical_type("sifrei mitsvot")

    def test_count_visible_chars(self):
        self.assertEqual(icl.count_visible_chars("<b>ab</b>c&amp;d", 16), 5)
        self.assertEqual(icl.count_visible_chars("<b>ab</b>c", 5), 2)


class StorageTests(unittest.TestCase):
    def test_every_orientation_rule(self):
        fx = Fixture(CITING, [
            e(1, 1, "source"),                               # flipped COMMENTARY
            e(2, 2, "footnotes"),                            # flipped FOOTNOTES (title declares Base)
            e(3, 3, "mesorat hashas"),                       # lateral: as written
            e(1, 2, "linker"),                               # never imported
            e(5, 1, "source"),                               # heading line -> skipped
        ])
        try:
            out = fx.run()
            self.assertEqual(fx.links(), [
                (1, 2, line_id(1, 0), line_id(2, 0), 0, 2, "COMMENTARY", 0),
                (1, 2, line_id(1, 1), line_id(2, 1), 1, 2, "FOOTNOTES", 0),
                (2, 1, line_id(2, 2), line_id(1, 2), 2, 1, "MESORAT_HASHAS", 0),
            ])
            self.assertIn('"skipped_linker": 1', out)
            self.assertIn('"skipped_heading_line": 1', out)
        finally:
            fx.close()

    def test_undeclared_title_is_stored_other_with_a_warning(self):
        fx = Fixture(PLAIN, [e(1, 1, "footnotes")])
        try:
            out = fx.run()
            self.assertEqual(fx.links(), [(3, 1, line_id(3, 0), line_id(1, 0), 0, 1, "OTHER", 0)])
            self.assertIn("WARNING", out)
            self.assertEqual(fx.flags(1)["hasOtherConnection"], 1)
        finally:
            fx.close()

    def test_flags_match_the_generator(self):
        fx = Fixture(CITING, [e(2, 2, "footnotes")])
        try:
            fx.run()
            # FOOTNOTES sets none of the per-type flags and no hasSourceConnection.
            self.assertEqual(set(fx.flags(1).values()) | set(fx.flags(2).values()), {0})
            self.assertEqual(fx.q("SELECT * FROM book_has_links ORDER BY bookId"), [(1, 1, 0), (2, 0, 1)])
            fx.run([e(1, 1, "source")], replace_existing=True)
            self.assertEqual(fx.flags(1)["hasCommentaryConnection"], 1)
            self.assertEqual(fx.flags(2)["hasCommentaryConnection"], 1)
            self.assertEqual(fx.flags(2)["hasSourceConnection"], 1)
            self.assertEqual(fx.flags(1)["hasSourceConnection"], 0)
            self.assertEqual(fx.flags(1)["hasOtherConnection"], 0)
        finally:
            fx.close()

    def test_replace_wipes_both_directions_and_clears_flags(self):
        fx = Fixture(PLAIN, [e(1, 1, "footnotes"), e(2, 2, "reference", REF + ".txt")])
        try:
            fx.run()
            self.assertEqual(fx.flags(1)["hasOtherConnection"], 1)
            self.assertEqual(fx.flags(4)["hasReferenceConnection"], 1)
            fx.run([e(1, 1, "source")], replace_existing=True)
            self.assertEqual([r[6] for r in fx.links()], ["COMMENTARY"])
            self.assertEqual(fx.flags(1)["hasOtherConnection"], 0)
            self.assertEqual(fx.flags(4)["hasReferenceConnection"], 0)
            self.assertEqual(fx.q("SELECT * FROM book_has_links WHERE bookId=4"), [(4, 0, 0)])
        finally:
            fx.close()

    def test_replace_keeps_other_books_flipped_links_into_this_one(self):
        fx = Fixture(CITING, [e(1, 1, "source")])
        try:
            fx.run()
            conn = sqlite3.connect(fx.db)
            # REF's own file said "source" on CITING -> stored CITING -> REF, COMMENTARY.
            conn.execute("INSERT INTO link (id, sourceBookId, targetBookId, sourceLineId, targetLineId, "
                         "targetLineIndex, targetBookOrderIndex, connectionTypeId) "
                         "VALUES (999, 2, 4, ?, ?, 0, 4, 1)", (line_id(2, 0), line_id(4, 0)))
            conn.commit()
            conn.close()
            fx.run([e(1, 1, "source")], replace_existing=True)
            self.assertIn((2, 4, "COMMENTARY"), [(r[0], r[1], r[6]) for r in fx.links()])
        finally:
            fx.close()

    def _insert(self, fx, rows):
        conn = sqlite3.connect(fx.db)
        ct = {n: i for i, n in conn.execute("SELECT id, name FROM connection_type")}
        conn.executemany(
            "INSERT INTO link (id, sourceBookId, targetBookId, sourceLineId, targetLineId, "
            "targetLineIndex, targetBookOrderIndex, connectionTypeId) VALUES (?, ?, ?, ?, ?, 0, 0, ?)",
            [(i, s, t, sl, tl, ct[name]) for i, s, t, sl, tl, name in rows],
        )
        conn.commit()
        conn.close()

    def test_spellings_that_store_alike_share_one_group(self):
        entries = [e(1, 1, "quotation", REF + ".txt"), e(2, 2, "quotation_auto_tanakh", REF + ".txt"),
                   e(3, 3, None, REF + ".txt"), e(4, 4, "", REF + ".txt"), e(1, 2, "other", REF + ".txt")]
        for replace in (False, True):
            fx = Fixture(CITING, entries)
            try:
                fx.run(replace_existing=replace)
                kinds = sorted(r[6] for r in fx.links())
                self.assertEqual(kinds, ["OTHER"] * 3 + ["QUOTATION"] * 2, replace)
                fx.run(replace_existing=replace)          # a re-run changes nothing
                self.assertEqual(sorted(r[6] for r in fx.links()), kinds)
            finally:
                fx.close()

    def test_non_replace_inserts_only_missing_rows(self):
        fx = Fixture(CITING, [e(1, 1, "source"), e(2, 2, "source")])
        try:
            self._insert(fx, [(900, 1, 2, line_id(1, 0), line_id(2, 0), "COMMENTARY")])
            out = fx.run()
            self.assertEqual(len(fx.links()), 2)
            self.assertIn('"skipped_existing": 1', out)
        finally:
            fx.close()

    def test_replace_never_touches_rows_this_file_does_not_produce(self):
        SUPER = 4  # REF plays a super-commentary on CITING
        fx = Fixture(CITING, [e(1, 1, "source"), e(3, 3, "quotation", REF + ".txt")])
        try:
            self._insert(fx, [
                (901, 2, SUPER, line_id(2, 0), line_id(4, 0), "COMMENTARY"),   # REF's flipped 'source'
                (902, SUPER, 2, line_id(4, 1), line_id(2, 1), "COMMENTARY"),   # REF's base-named file
                (903, SUPER, 2, line_id(4, 2), line_id(2, 2), "REFERENCE"),    # REF's reference
                (904, 2, SUPER, line_id(2, 3), line_id(4, 0), "LINKER"),       # LinkerToOtzaria
                (905, SUPER, 2, line_id(4, 0), line_id(2, 3), "LINKER"),
                (906, 1, 2, line_id(1, 3), line_id(2, 3), "COMMENTARY"),       # dependent row INTO citing
            ])
            out = fx.run(replace_existing=True)
            ids = {r[0] for r in fx.q("SELECT id FROM link")}
            self.assertLessEqual({901, 902, 903, 904, 905, 906}, ids)
            self.assertIn("STALE? 1", out)   # 906 reported, not deleted
            self.assertEqual(fx.q("SELECT hasSourceLinks FROM book_has_links WHERE bookId=2"), [(1,)])
        finally:
            fx.close()

    def test_replace_deletes_stale_rows_only_this_file_can_have_written(self):
        fx = Fixture(CITING, [e(1, 1, "source"), e(2, 2, "quotation", REF + ".txt")])
        try:
            fx.run()
            fx.run([e(1, 1, "source")], replace_existing=True)   # the quotation line was dropped
            self.assertEqual([r[6] for r in fx.links()], ["COMMENTARY"])
        finally:
            fx.close()

    def test_delete_reported_stale_is_opt_in_and_refused_for_sefaria(self):
        fx = Fixture(CITING, [e(1, 1, "source")])
        try:
            self._insert(fx, [(906, 1, 2, line_id(1, 3), line_id(2, 3), "COMMENTARY")])
            fx.run(replace_existing=True, delete_reported_stale=True)
            self.assertNotIn(906, {r[0] for r in fx.q("SELECT id FROM link")})
            conn = sqlite3.connect(fx.db)
            conn.execute("INSERT INTO source VALUES (2, 'Sefaria')")
            conn.execute("UPDATE book SET sourceId=2 WHERE id=2")
            conn.commit()
            conn.close()
            self._insert(fx, [(999, 1, 2, line_id(1, 3), line_id(2, 3), "COMMENTARY")])
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                fx.run()
            self.assertIn(999, {r[0] for r in fx.q("SELECT id FROM link")})
        finally:
            fx.close()

    def test_replace_refuses_a_file_not_named_after_the_citing_book(self):
        fx = Fixture(CITING, [e(1, 1, "source")])
        try:
            other = Path(fx.cfg["links_json_path"]).with_name("draft_links.json")
            Path(fx.cfg["links_json_path"]).rename(other)
            fx.cfg["links_json_path"] = str(other)
            fx.run()   # the default mode (never deletes) is still allowed
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                fx.run(replace_existing=True)
        finally:
            fx.close()

    def test_refresh_keeps_the_other_producers_satellites(self):
        # One stored row, two producers: Base's own base-named file wrote it as written
        # (with a word anchor and a range on side 0); this file writes it flipped.
        fx = Fixture(CITING, [e(1, 1, "source", line_index_1_end=2)])
        try:
            self._insert(fx, [(900, 1, 2, line_id(1, 0), line_id(2, 0), "COMMENTARY")])
            conn = sqlite3.connect(fx.db)
            conn.execute("INSERT INTO link_anchor VALUES (900, 0, 3, 5, NULL)")
            conn.execute("INSERT INTO link_range VALUES (900, 0, ?, 3)", (line_id(1, 3),))
            conn.commit()
            conn.close()
            fx.run(replace_existing=True)
            self.assertEqual([r[0] for r in fx.q("SELECT id FROM link")], [900])
            self.assertEqual(fx.q("SELECT linkId, side, charStart FROM link_anchor"), [(900, 0, 3)])
            self.assertEqual(fx.q("SELECT linkId, side, endLineIndex FROM link_range ORDER BY side"),
                             [(900, 0, 3), (900, 1, 1)])   # theirs kept, ours (citing end) added
        finally:
            fx.close()

    def test_refresh_rewrites_this_files_own_anchor(self):
        fx = Fixture(CITING, [e(1, 1, "mesorat hashas", start=3)])
        try:
            fx.run()
            fx.run([e(1, 1, "mesorat hashas", start=4)], replace_existing=True)
            self.assertEqual([r[0] for r in fx.q("SELECT charStart FROM link_anchor")], [1])
        finally:
            fx.close()

    def test_demoted_dependent_row_out_of_citing_is_reported(self):
        # An old DB row PLAIN -> Base COMMENTARY: the current generator stores this file's
        # entry as OTHER, so nothing it writes explains that row any more.
        fx = Fixture(PLAIN, [e(1, 1, "commentary")])
        try:
            self._insert(fx, [(900, 3, 1, line_id(3, 0), line_id(1, 0), "COMMENTARY")])
            out = fx.run(replace_existing=True)
            self.assertIn("STALE? 1", out)
            self.assertIn(900, [r[0] for r in fx.q("SELECT id FROM link")])
            fx.run(replace_existing=True, delete_reported_stale=True)
            self.assertNotIn(900, [r[0] for r in fx.q("SELECT id FROM link")])
            self.assertEqual(fx.flags(1)["hasCommentaryConnection"], 0)
            self.assertEqual(fx.flags(1)["hasOtherConnection"], 1)
        finally:
            fx.close()

    def test_satellites_follow_the_stored_sides(self):
        fx = Fixture(CITING, [
            e(1, 1, "mesorat hashas", start=3, end=5, line_index_1_end=3),   # as written
            e(2, 2, "source", start=3, line_index_1_end=4),                   # flipped
        ])
        try:
            fx.run()
            ids = [r[0] for r in fx.q("SELECT id FROM link ORDER BY id")]
            # anchor only on the unflipped link, side 0, in visible chars ("<b>" skipped)
            self.assertEqual(fx.q("SELECT linkId, side, charStart, charEnd FROM link_anchor"),
                             [(ids[0], 0, 0, 2)])
            # file end -> side 0 when as written, side 1 when flipped
            self.assertEqual(fx.q("SELECT linkId, side, endLineIndex FROM link_range ORDER BY linkId"),
                             [(ids[0], 0, 2), (ids[1], 1, 3)])
        finally:
            fx.close()

    def test_unknown_type_fails_before_writing(self):
        fx = Fixture(CITING, [e(1, 1, "source"), e(2, 2, "no_such_type")])
        try:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                fx.run()
            self.assertEqual(fx.links(), [])
        finally:
            fx.close()


if __name__ == "__main__":
    unittest.main()
