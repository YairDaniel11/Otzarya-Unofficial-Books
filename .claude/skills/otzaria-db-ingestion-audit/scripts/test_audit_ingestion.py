"""audit_ingestion.py checks each entry against its own path_2 book, stored the way
SeforimLibrary's Generator.kt stores it. The DB is written by the otzaria-db-linker
engine (itself pinned to Generator.kt by its own tests).

Run: python3 -m unittest -v test_audit_ingestion   (from this directory)
"""
import contextlib
import importlib.util
import io
import sqlite3
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
LINKER_TESTS = HERE.parents[1] / "otzaria-db-linker" / "scripts" / "test_insert_commentary_link.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


audit = _load("audit_ingestion", HERE / "audit_ingestion.py")
fx_mod = _load("icl_tests", LINKER_TESTS)
BASE, CITING, PLAIN, REF, e = fx_mod.BASE, fx_mod.CITING, fx_mod.PLAIN, fx_mod.REF, fx_mod.e


def run_audit(fx, *extra, citing=CITING):
    argv = ["audit_ingestion.py", "--db", str(fx.db), "--citing", citing, "--target", BASE,
            "--links", fx.cfg["links_json_path"], *extra]
    out = io.StringIO()
    old = sys.argv
    sys.argv = argv
    try:
        with contextlib.redirect_stdout(out):
            rc = audit.main()
    finally:
        sys.argv = old
    return rc, out.getvalue()


class AuditTests(unittest.TestCase):
    def test_mixed_targets_types_and_directions_pass(self):
        # source -> Base, source -> REF (a second path_2 book), footnotes -> Base,
        # mesorat hashas -> Base (lateral: stored as written, citing -> Base).
        fx = fx_mod.Fixture(CITING, [
            e(1, 1, "source"), e(2, 3, "source", REF + ".txt"),
            e(3, 2, "footnotes"), e(4, 4, "mesorat hashas"),
        ])
        try:
            fx.run()
            rc, out = run_audit(fx)
            self.assertEqual(rc, 0, out)
            self.assertIn("4/4", out)
        finally:
            fx.close()

    def test_missing_row_fails(self):
        fx = fx_mod.Fixture(CITING, [e(1, 1, "source"), e(2, 3, "source", REF + ".txt")])
        try:
            fx.run()
            conn = sqlite3.connect(fx.db)
            conn.execute("DELETE FROM link WHERE sourceBookId=4")  # the REF row
            conn.commit()
            conn.close()
            rc, out = run_audit(fx)
            self.assertEqual(rc, 1)
            self.assertIn("1/2", out)
        finally:
            fx.close()

    def test_wrong_flag_fails(self):
        fx = fx_mod.Fixture(CITING, [e(1, 1, "source")])
        try:
            fx.run()
            conn = sqlite3.connect(fx.db)
            conn.execute("UPDATE book SET hasSourceConnection=0 WHERE id=2")
            conn.commit()
            conn.close()
            rc, out = run_audit(fx)
            self.assertEqual(rc, 1)
            self.assertIn("hasSourceConnection!=1", out)
        finally:
            fx.close()

    def test_forced_type_id_checks_only_that_type(self):
        fx = fx_mod.Fixture(CITING, [e(1, 1, "source"), e(3, 2, "footnotes")])
        try:
            fx.run()
            rc, out = run_audit(fx, "--type-id", "1")
            self.assertEqual(rc, 1)
            self.assertIn("1/2", out)
        finally:
            fx.close()

    def test_other_demotion_is_what_is_audited(self):
        # PLAIN does not name Base: the generator (and the linker) store OTHER, unflipped.
        fx = fx_mod.Fixture(PLAIN, [e(1, 1, "footnotes")])
        try:
            fx.run()
            rc, out = run_audit(fx, citing=PLAIN)
            self.assertEqual(rc, 0, out)
        finally:
            fx.close()


if __name__ == "__main__":
    unittest.main()
