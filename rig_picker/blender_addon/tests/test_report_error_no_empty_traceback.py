"""Expected storage conditions (merge conflicts, ...) must not print a bare
"NoneType: None" traceback; real exceptions still print theirs."""
import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import AddonTestCase  # noqa: E402


class ReportErrorTests(AddonTestCase):
    def setUp(self):
        super().setUp()
        self.jm = self.load_json_manager()
        self.errors = []
        self.jm.set_error_handler(self.errors.append)
        self.addCleanup(self.jm.set_error_handler, None)

    def capture(self, fn):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            fn()
        return out.getvalue(), err.getvalue()

    def test_merge_conflict_report_has_no_empty_traceback(self):
        out, err = self.capture(
            lambda: self.jm._report_merge_conflicts("data.json", ["armature_a", "armature_b"]))
        self.assertNotIn("NoneType", out + err)
        self.assertIn("armature_a", out)                  # message still reaches the console
        self.assertEqual(len(self.errors), 1)             # ...and the UI handler

    def test_report_error_without_exception_prints_message_not_traceback(self):
        out, err = self.capture(lambda: self.jm._report_error("something odd"))
        self.assertNotIn("NoneType", out + err)
        self.assertIn("something odd", out)
        self.assertEqual(self.errors, ["something odd"])

    def test_report_error_with_exception_still_prints_traceback(self):
        def run():
            try:
                raise OSError("disk full")
            except OSError:
                self.jm._report_error("couldn't save")
        out, err = self.capture(run)
        self.assertIn("Traceback (most recent call last)", err)
        self.assertIn("disk full", err)
        self.assertEqual(self.errors, ["couldn't save"])


if __name__ == "__main__":
    unittest.main()
