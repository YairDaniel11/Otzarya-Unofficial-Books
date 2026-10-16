#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regression tests for check_daf.py (synthetic fixtures, stdlib only).

  python -X utf8 test_check_daf.py            # tests the check_daf.py next to this file
  CHECK_DAF=/path/to/other/check_daf.py python -X utf8 test_check_daf.py

The fixtures are the bug that shipped until Sept 2026: a `source`-typed file
(the modern citing-named norm) was skipped entirely — 0 entries checked, exit 0.
Hebrew is written as \\u escapes on purpose.
"""
import contextlib, importlib.util, io, json, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.environ.get("CHECK_DAF") or os.path.join(HERE, "check_daf.py")

DAF = "\u05d3\u05e3"          # daf
BET = "\u05d1"                # page 2
GIMEL = "\u05d2"              # page 3
TRACTATE = "\u05d1\u05e8\u05db\u05d5\u05ea"
WORD = "\u05d3\u05d1\u05e8"   # filler text
NOTES = "\u05d4\u05e2\u05e8\u05d5\u05ea"   # "notes"
ON = "\u05e2\u05dc"                          # "on"

# citing book: daf 2a -> lines 3,4 ; daf 2b -> line 6 ; daf 3a -> line 8
CITING = "\n".join([
    f"<h1>{WORD}</h1>",
    f"<h2>{DAF} {BET}.</h2>",
    f"{WORD} 1",
    f"{WORD} 2",
    f"<h2>{DAF} {BET}:</h2>",
    f"{WORD} 3",
    f"<h2>{DAF} {GIMEL}.</h2>",
    f"{WORD} 4",
])
NO_DAF_CITING = "\n".join([f"<h1>{WORD}</h1>", f"<h2>{WORD}</h2>", f"{WORD} 1", f"{WORD} 2"])


def heref(page, amud):
    return f"{TRACTATE} {page}{amud}, 1"


def entry(li1, page, amud, ctype):
    return {"line_index_1": li1, "line_index_2": 1, "heRef_2": heref(page, amud),
            "path_2": f"{TRACTATE}.txt", "Conection Type": ctype}


def load_module():
    spec = importlib.util.spec_from_file_location("check_daf_under_test", TARGET)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, os.path.dirname(os.path.abspath(TARGET)))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


cd = load_module()


class Fixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.links = os.path.join(self.tmp.name, "links")
        self.books = os.path.join(self.tmp.name, "books")
        os.makedirs(self.links); os.makedirs(self.books)

    def add(self, name, text, recs):
        with open(os.path.join(self.books, name + ".txt"), "w", encoding="utf-8") as f:
            f.write(text)
        lp = os.path.join(self.links, name + "_links.json")
        with open(lp, "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False)
        return lp, os.path.join(self.books, name + ".txt")

    def run_dir(self, *extra):
        return run_main(["--dir", self.links, "--books-root", self.books, *extra])


def run_main(argv):
    old = sys.argv
    sys.argv = ["check_daf.py", *argv]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = cd.main()
    finally:
        sys.argv = old
    return rc, buf.getvalue()


class SourceTypeIsChecked(unittest.TestCase):
    def test_source_amud_mismatch_is_caught(self):
        fx = Fixture()
        lp, cp = fx.add("A", CITING, [
            entry(3, BET, ".", "source"),
            entry(4, BET, ":", "source"),      # printed under 2a, linked to 2b
            entry(6, BET, ":", "source"),
            entry(8, GIMEL, ".", "source"),
        ])
        out, bad = cd.check(lp, cp)
        self.assertEqual(out["match"], 3)
        self.assertEqual(out["MISMATCH"], 1)
        self.assertEqual([b[0] for b in bad], [4])
        rc, _ = run_main(["--links", lp, "--citing", cp])
        self.assertEqual(rc, 1)

    def test_source_clean_passes(self):
        fx = Fixture()
        lp, cp = fx.add("A", CITING, [entry(3, BET, ".", "source"),
                                      entry(8, GIMEL, ".", "source")])
        rc, _ = run_main(["--links", lp, "--citing", cp])
        self.assertEqual(rc, 0)


class LegacyTypesStillWork(unittest.TestCase):
    def test_commentary_and_super(self):
        fx = Fixture()
        lp, cp = fx.add("A", CITING, [
            entry(3, BET, ".", "commentary"),
            entry(6, GIMEL, ":", "super_commentary"),   # mismatch
        ])
        out, bad = cd.check(lp, cp)
        self.assertEqual((out["match"], out["MISMATCH"]), (1, 1))
        self.assertEqual(run_main(["--links", lp, "--citing", cp])[0], 1)

    def test_footnotes_bare_title_is_target_unknown(self):
        # Real base-named footnotes file: path_2 = the notes book, heRef_2 = its bare
        # title. The title's last word must not be read as a daf.
        notes = f"{NOTES} {ON} {TRACTATE}"
        fx = Fixture()
        lp, cp = fx.add("A", CITING, [
            {"line_index_1": 3, "line_index_2": 2, "heRef_2": notes,
             "path_2": notes + ".txt", "Conection Type": "footnotes"},
            {"line_index_1": 6, "line_index_2": 2, "heRef_2": f"{NOTES} {BET}{BET}",
             "path_2": "x.txt", "Conection Type": "footnotes"},   # not a numeral
        ])
        out, bad = cd.check(lp, cp)
        self.assertEqual((out["match"], out["MISMATCH"]), (0, 0), bad)
        self.assertEqual(out["target_daf_unknown"], 2)
        self.assertEqual(run_main(["--links", lp, "--citing", cp])[0], 2)

    def test_footnotes_with_daf_is_checked(self):
        fx = Fixture()
        lp, cp = fx.add("A", CITING, [entry(8, BET, ".", "footnotes")])
        out, _ = cd.check(lp, cp)
        self.assertEqual(out["MISMATCH"], 1)

    def test_lateral_types_are_not_daf_checked(self):
        fx = Fixture()
        lp, cp = fx.add("A", CITING, [
            entry(3, BET, ".", "source"),
            entry(4, GIMEL, ":", "quotation"),       # other daf is legitimate here
            entry(6, GIMEL, ":", "mesorat_hashas"),
        ])
        out, _ = cd.check(lp, cp)
        self.assertEqual((out["match"], out["MISMATCH"]), (1, 0))
        self.assertEqual(out["skipped_type"], 2)


class ZeroCheckedIsNotAPass(unittest.TestCase):
    def test_only_lateral_entries(self):
        fx = Fixture()
        lp, cp = fx.add("A", CITING, [entry(3, BET, ".", "quotation")])
        rc, text = run_main(["--links", lp, "--citing", cp])
        self.assertEqual(rc, 2, text)

    def test_no_daf_headings(self):
        fx = Fixture()
        fx.add("A", NO_DAF_CITING, [entry(3, BET, ".", "source")])
        rc, text = fx.run_dir()
        self.assertEqual(rc, 2, text)

    def test_empty_file_in_batch_fails_unless_allowed(self):
        fx = Fixture()
        fx.add("A", CITING, [entry(3, BET, ".", "source")])
        fx.add("B", NO_DAF_CITING, [entry(3, BET, ".", "source")])
        self.assertEqual(fx.run_dir()[0], 2)
        self.assertEqual(fx.run_dir("--allow-unchecked")[0], 0)

    def test_missing_citing_txt_fails(self):
        fx = Fixture()
        fx.add("A", CITING, [entry(3, BET, ".", "source")])
        with open(os.path.join(fx.links, "Orphan_links.json"), "w") as f:
            json.dump([entry(3, BET, ".", "source")], f)
        self.assertEqual(fx.run_dir()[0], 2)

    def test_malformed_json_is_unverified_and_batch_continues(self):
        fx = Fixture()
        fx.add("A", CITING, [entry(4, BET, ":", "source")])        # real mismatch
        lp, _ = fx.add("B", CITING, [])
        with open(lp, "w") as f:
            f.write("{not json")
        fx.add("C", CITING, {"not": "an array"})
        rc, text = fx.run_dir()
        self.assertEqual(rc, 1, text)          # mismatch in A still found
        self.assertIn("B: unreadable", text)
        self.assertIn("C: unreadable", text)
        fx2 = Fixture()
        fx2.add("A", CITING, [entry(3, BET, ".", "source")])
        fx2.add("C", CITING, {"not": "an array"})
        self.assertEqual(fx2.run_dir()[0], 2)

    def test_single_file_missing_citing(self):
        fx = Fixture()
        lp, _ = fx.add("A", CITING, [entry(3, BET, ".", "source")])
        rc, _ = run_main(["--links", lp, "--citing", lp + ".missing.txt"])
        self.assertEqual(rc, 2)

    def test_usage_error_has_its_own_code(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                run_main(["--links", "x"])
        self.assertEqual(cm.exception.code, 3)

    def test_integral_float_line_index_accepted(self):
        fx = Fixture()
        lp, cp = fx.add("A", CITING, [entry(4.0, BET, ":", "source"),
                                      entry(4.5, BET, ".", "source"),
                                      entry(True, BET, ".", "source")])
        out, _ = cd.check(lp, cp)
        self.assertEqual(out["MISMATCH"], 1)
        self.assertEqual(out["bad_line_index"], 2)

    def test_bad_line_index_does_not_wrap(self):
        fx = Fixture()
        lp, cp = fx.add("A", CITING, [entry(-1, GIMEL, ".", "source"),
                                      entry(3, BET, ".", "source")])
        out, _ = cd.check(lp, cp)
        self.assertEqual((out["match"], out["MISMATCH"]), (1, 0))


class ConnectionTypeNormalization(unittest.TestCase):
    """Port of Link.kt ConnectionType.fromKnownStringOrNull."""

    def test_aliases(self):
        sys.path.insert(0, HERE)
        from connection_type import canonical_connection_type as c
        cases = {
            "source": "source", " Source\t": "source", "\u00a0source": "source",
            "footnote": "footnotes", "FOOTNOTES": "footnotes",
            "super commentary": "super_commentary", "supercommentary": "super_commentary",
            "mesorat hashas": "mesorat_hashas", "Mishnah in Talmud": "mishnah_in_talmud",
            "ein mishpat / ner mitsvah": "ein_mishpat", "ein mishpat / ner mitzvah": "ein_mishpat",
            "related passage": "related", "quotation_auto_tanakh": "quotation",
            "ellucidation": "elucidation", "sifrei mitzvot": "sifrei_mitzvot",
            "": "other", "none": "other", " None ": "other",
        }
        for raw, want in cases.items():
            self.assertEqual(c(raw), want, repr(raw))
        for raw in ("sifrei mitsvot", "notes", "note", "ein  mishpat", "foo", 3):
            self.assertIsNone(c(raw), repr(raw))

    def test_check_daf_uses_normalized_types(self):
        fx = Fixture()
        lp, cp = fx.add("A", CITING, [
            entry(4, BET, ":", " Source "),                 # checked -> mismatch
            entry(3, BET, ".", "footnote"),                 # checked -> match
            entry(6, BET, ":", "super commentary"),         # checked -> match
            entry(8, BET, ".", "ein mishpat / ner mitsvah"),  # halakha target: skipped
            entry(8, BET, ".", "mesorat hashas"),           # lateral: skipped
        ])
        out, _ = cd.check(lp, cp)
        self.assertEqual((out["match"], out["MISMATCH"], out["skipped_type"]), (2, 1, 2))

    def test_validate_links_classifies_spaced_types(self):
        import subprocess
        tmp = tempfile.TemporaryDirectory()
        d = tmp.name
        base = TRACTATE
        cit = f"{NOTES} {ON} {base}"
        with open(os.path.join(d, base + ".txt"), "w", encoding="utf-8") as f:
            f.write(f"<h1>{base}</h1>\n<h2>{DAF} {BET}.</h2>\n{WORD}\n{WORD}\n{WORD}\n{WORD}\n")
        with open(os.path.join(d, cit + ".txt"), "w", encoding="utf-8") as f:
            f.write(f"<h1>{cit}</h1>\n<h2>{DAF} {BET}.</h2>\n" + "\n".join(f"{WORD} {i}" for i in range(5)) + "\n")
        types = ["mesorat hashas", "ein mishpat / ner mitsvah", "related passage",
                 "none", "", "sifrei mitsvot"]
        recs = [{"line_index_1": 3 + i, "line_index_2": 3, "heRef_2": f"{base} {BET}., 1",
                 "path_2": base + ".txt", "Conection Type": t} for i, t in enumerate(types)]
        lp = os.path.join(d, cit + "_links.json")
        with open(lp, "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False)
        r = subprocess.run([sys.executable, os.path.join(HERE, "validate_links.py"),
                            "--links", lp, "--citing", os.path.join(d, cit + ".txt"),
                            "--target", os.path.join(d, base + ".txt")],
                           capture_output=True, text=True, encoding="utf-8")
        rep = json.loads(r.stdout)
        unexpected = [i for i in rep["issues"] if i["summary"].startswith("unexpected Conection Type")]
        self.assertEqual(len(unexpected), 1, unexpected)          # only "sifrei mitsvot"
        self.assertIn("sifrei mitsvot", unexpected[0]["summary"])
        groups = rep["stats"]["oriented_groups"]
        self.assertEqual([k.split(" -> ")[0] for k in groups], ["ein_mishpat"])
        tmp.cleanup()


# Characters Kotlin's String.trim() removes, measured on the JVM against
# SeforimLibrary's ConnectionType.fromKnownStringOrNull (not U+0085 / U+200B / U+FEFF).
KOTLIN_TRIM_BMP = {0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0xa0, 0x1680,
                   0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008,
                   0x2009, 0x200a, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000}


def run_validate_links(recs, extra=()):
    """Run validate_links.py on a tiny synthetic book pair; return the JSON report."""
    import subprocess
    with tempfile.TemporaryDirectory() as d:
        base, cit = TRACTATE, f"{NOTES} {ON} {TRACTATE}"
        with open(os.path.join(d, base + ".txt"), "w", encoding="utf-8") as f:
            f.write(f"<h1>{base}</h1>\n<h2>{DAF} {BET}.</h2>\n{WORD}\n{WORD}\n")
        with open(os.path.join(d, cit + ".txt"), "w", encoding="utf-8") as f:
            f.write(f"<h1>{cit}</h1>\n<h2>{DAF} {BET}.</h2>\n{WORD} 1\n{WORD} 2\n")
        lp = os.path.join(d, cit + "_links.json")
        with open(lp, "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False)
        r = subprocess.run([sys.executable, os.path.join(HERE, "validate_links.py"),
                            "--links", lp, "--citing", os.path.join(d, cit + ".txt"),
                            "--target", os.path.join(d, base + ".txt"), *extra],
                           capture_output=True, text=True, encoding="utf-8")
        return json.loads(r.stdout)


def link(li, ctype=..., **kw):
    e = {"line_index_1": li, "line_index_2": 3, "heRef_2": f"{TRACTATE} {BET}., 1",
         "path_2": TRACTATE + ".txt"}
    if ctype is not ...:
        e["Conection Type"] = ctype
    e.update(kw)
    return e


class GeneratorParity(unittest.TestCase):
    def test_kt_trim_over_the_bmp(self):
        sys.path.insert(0, HERE)
        from connection_type import canonical_connection_type as c
        for cp in range(0x10000):
            if 0xD800 <= cp <= 0xDFFF:
                continue
            ch = chr(cp)
            for s in (ch + "source", "source" + ch):
                self.assertEqual(c(s) == "source", cp in KOTLIN_TRIM_BMP, hex(cp))

    def test_null_and_missing_are_other(self):
        sys.path.insert(0, HERE)
        from connection_type import canonical_connection_type as c
        self.assertEqual(c(None), "other")

    def test_check_daf_ignores_misspelled_key_and_null(self):
        fx = Fixture()
        recs = [entry(4, BET, ":", "source")]
        recs[0]["Connection Type"] = recs[0].pop("Conection Type")   # generator ignores it
        recs.append({**entry(4, BET, ":", "source"), "Conection Type": None})
        recs.append({k: v for k, v in entry(4, BET, ":", "source").items() if k != "Conection Type"})
        lp, cp = fx.add("A", CITING, recs)
        out, _ = cd.check(lp, cp)
        self.assertEqual((out["match"], out["MISMATCH"], out["skipped_type"]), (0, 0, 3))
        self.assertEqual(out["skipped_type:<'Connection Type' key: ignored by generator -> OTHER>"], 1)
        self.assertEqual(out["skipped_type:<missing/null -> OTHER>"], 2)

    def test_validate_links_null_type_is_minor_with_accurate_reason(self):
        rep = run_validate_links([link(3, "source"), link(4, None)])
        hits = [i for i in rep["issues"] if "Conection Type\" is null" in i["summary"]]
        self.assertEqual(len(hits), 1, rep["issues"])
        self.assertEqual(hits[0]["severity"], "minor")
        self.assertIn("OTHER", hits[0]["summary"])

    def test_expected_linker_counts_canonical_values(self):
        recs = [link(3, "source"), link(4, " Linker "), link(4, "linker")]
        ok = run_validate_links(recs, ["--expected-linker", "2"])
        self.assertFalse([i for i in ok["issues"] if i["check"] == "linker_preserve"])
        bad = run_validate_links(recs, ["--expected-linker", "3"])
        self.assertTrue([i for i in bad["issues"] if i["check"] == "linker_preserve"])


class DafLabel(unittest.TestCase):
    def test_numerals(self):
        ok = [BET, "\u05e7\u05e2\u05d5", "\u05d8\u05d5", "\u05d8\u05d6", "\u05e7\u05f4\u05d1"]
        bad = [TRACTATE, "\u05e9\u05d1\u05ea", "\u05d1\u05d1", "\u05d0\u05d1", ""]
        for t in ok:
            self.assertTrue(cd.is_daf_label(t), ascii(t))
        for t in bad:
            self.assertFalse(cd.is_daf_label(t), ascii(t))


class OrientationPort(unittest.TestCase):
    """validate_links' port of Generator.titleDeclaresDependencyOn / orientation."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, HERE)
        import validate_links
        cls.v = validate_links

    def test_title_declares(self):
        f = self.v.title_declares_dependency_on
        base = TRACTATE
        self.assertTrue(f(f"{NOTES} {ON} {base}", base))
        self.assertTrue(f(f"{NOTES} {ON} \u05de\u05e1\u05db\u05ea {base}", base))
        self.assertFalse(f(f"{NOTES} {base}", base))
        # Java regex \\s is ASCII-only, so NBSP does not separate words there
        self.assertFalse(f(f"{NOTES}\u00a0{ON}\u00a0{base}", base))
        self.assertTrue(f(f"{NOTES} {ON} \u05e8\u05e9\u201d\u05d9", "\u05e8\u05e9\u05f4\u05d9"))

    def test_classify(self):
        class O:
            def __init__(self, m): self.m = m
            def is_base(self, t): return self.m.get(t)
        cit, base, other = f"{NOTES} {ON} {TRACTATE}", TRACTATE, WORD
        c = self.v.classify_orientation
        self.assertEqual(c("footnotes", cit, base, O({cit: False, base: True}))[0], "flipped")
        self.assertEqual(c("footnotes", other, base, O({other: False, base: True}))[0], "stored_other")
        self.assertEqual(c("footnotes", base, cit, O({cit: False, base: True}))[0], "as_is")
        self.assertEqual(c("footnotes", cit, base, O({}))[0], "unverifiable")

    def test_title_named_but_stored_as_is_is_major(self):
        class O:
            def is_base(self, t): return False      # target is not a base book
        cit, base = f"{NOTES} {ON} {TRACTATE}", TRACTATE
        out, sev, _ = self.v.classify_orientation("footnotes", cit, base, O())
        self.assertEqual((out, sev), ("as_is", "major"))

    def test_explication_is_never_flipped(self):
        class O:
            def is_base(self, t): return {TRACTATE: True}.get(t, False)
        c = self.v.classify_orientation
        cit = f"{NOTES} {ON} {TRACTATE}"
        self.assertEqual(c("explication", cit, TRACTATE, O())[:2], ("as_is", "major"))
        self.assertEqual(c("explication", WORD, TRACTATE, O())[:2], ("as_is", "minor"))

    def test_generator_target_title(self):
        g = self.v.generator_target_title
        self.assertEqual(g(f"{TRACTATE}.txt"), TRACTATE)
        self.assertEqual(g(f"a\\b\\{TRACTATE}.txt"), TRACTATE)      # backslash path
        self.assertEqual(g(f"a/b/{TRACTATE}.txt"), TRACTATE)
        self.assertEqual(g(f"a\\{TRACTATE}"), TRACTATE)                # no extension
        self.assertEqual(g("x.y.txt"), "x.y")

    def test_backslash_path2_is_classified_by_file_name(self):
        class O:
            def is_base(self, t): return {TRACTATE: True}.get(t, False)
        cit = f"{NOTES} {ON} {TRACTATE}"
        t = self.v.generator_target_title(f"dir\\{TRACTATE}.txt")
        self.assertEqual(self.v.classify_orientation("footnotes", cit, t, O())[0], "flipped")


if __name__ == "__main__":
    print(f"testing {TARGET}")
    unittest.main(verbosity=1)
