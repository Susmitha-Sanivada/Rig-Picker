"""Toggle read-back verification with NaN / infinity."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import AddonTestCase  # noqa: E402

INF, NAN = float("inf"), float("nan")


class NonFiniteVerifyTests(AddonTestCase):
    def setUp(self):
        super().setUp()
        self.caq, _ = self.load_quick()

    def test_values_match(self):
        cases = [
            # actual, expected, should match
            (NAN, NAN, True),             # the reported failure
            (-NAN, NAN, True),            # sign/payload of nan is irrelevant
            (INF, INF, True),             # the reported failure
            (-INF, -INF, True),
            (INF, -INF, False),           # opposite infinities
            (-INF, INF, False),
            (NAN, INF, False), (INF, NAN, False),
            (NAN, 1.0, False), (1.0, NAN, False), (NAN, 0.0, False),
            (INF, 1.0, False), (1.0, INF, False), (-INF, 0.0, False),
            (1.0, 1.0, True), (0.0, 0.0, True),
            (1.0 + 5e-7, 1.0, True),      # float noise still passes
            (1.0 + 5e-6, 1.0, False),     # a clamped/driven value still fails
            (0.5, 1.0, False), (0, False, True), (1, True, True),
            (1e308, 1e308, True),         # large finite: ordinary path
        ]
        for actual, expected, want in cases:
            with self.subTest(actual=actual, expected=expected):
                self.assertIs(self.caq._toggle_values_match(actual, expected), want)

    def test_old_expression_failed_on_non_finite(self):
        """Documents what was wrong: the plain tolerance check is False for
        a value that is exactly what was written."""
        tol = self.caq._TOGGLE_VERIFY_TOLERANCE
        for value in (NAN, INF, -INF):
            with self.subTest(value=value):
                self.assertFalse(abs(value - float(value)) <= tol)
                self.assertTrue(self.caq._toggle_values_match(value, value))

    def test_took_effect_end_to_end(self):
        caq = self.caq
        for value in (NAN, INF, -INF):
            with self.subTest(value=value), mock.patch.object(
                caq, "_eval_toggle_raw_value", return_value=value
            ):
                took, actual = caq._toggle_assignment_took_effect(None, "rig['P']", value)
                self.assertTrue(took)
        with mock.patch.object(caq, "_eval_toggle_raw_value", return_value=0.0):
            # a driver pinned it to 0
            self.assertFalse(caq._toggle_assignment_took_effect(None, "rig['P']", NAN)[0])
        with mock.patch.object(caq, "_eval_toggle_raw_value", return_value=None):
            # unreadable stays unreadable
            self.assertEqual(
                caq._toggle_assignment_took_effect(None, "rig['P']", NAN), (False, None)
            )


if __name__ == "__main__":
    unittest.main()
