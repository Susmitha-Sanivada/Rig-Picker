"""A Toggle target that holds None must be told apart from one that can't be
read."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import AddonTestCase  # noqa: E402

ns = types.SimpleNamespace


class ToggleNoneValueTests(AddonTestCase):
    def setUp(self):
        super().setUp()
        self.caq, _ = self.load_quick()
        self.U = self.caq._TOGGLE_UNREADABLE
        self.ctx = ns()
        self.rig = ns(parent=None, mode=1.0, flag=True, name="Rig")
        self.rig.d = {"held_none": None, "n": 1, "s": "text"}

    def read(self, rig, command):
        return self.caq._eval_toggle_original(rig, command, self.ctx, unreadable=self.U)

    def click_decision(self, rig, command):
        """The click handler's decision, as the operator makes it."""
        caq = self.caq
        original = caq._read_quick_toggle_original(rig, command, unreadable=self.U)
        if original is not self.U and not caq.toggle_value_supported(original):
            return "refuse", caq._toggle_held_description(original)
        return ("first-run" if original is self.U else "run"), original

    # --- the read itself ---------------------------------------------------
    def test_held_none_is_a_value(self):
        self.assertIsNone(self.read(self.rig, "rig.parent"))
        self.assertIsNone(self.read(self.rig, "rig.d['held_none']"))
        self.assertEqual(self.read(self.rig, "rig.d['n']"), 1)

    def test_unreadable_returns_the_sentinel(self):
        U = self.U
        self.assertIs(self.read(self.rig, "rig.d['missing']"), U)   # KeyError
        self.assertIs(self.read(self.rig, "rig.nope"), U)           # AttributeError
        self.assertIs(self.read(self.rig, ""), U)
        self.assertIs(self.read(self.rig, "   "), U)
        self.assertIs(self.read(self.rig, "rig.mode.real()"), U)    # not a pure reference
        self.assertIs(self.read(None, "rig.mode"), U)               # no rig

    def test_default_behaviour_unchanged_for_other_callers(self):
        f = self.caq._eval_toggle_original
        self.assertIsNone(f(self.rig, "rig.parent", self.ctx))
        self.assertIsNone(f(self.rig, "rig.d['missing']", self.ctx))
        self.assertEqual(f(self.rig, "rig.mode", self.ctx), 1.0)

    def test_run_operator_reader(self):
        f = self.caq._read_quick_toggle_original
        self.assertIsNone(f(None, "rig.mode"))
        self.assertIs(f(None, "rig.mode", unreadable=self.U), self.U)
        self.assertIsNone(f(self.rig, "rig.parent", unreadable=self.U))

    # --- click decision ------------------------------------------------------
    def test_click_decision(self):
        d = self.click_decision
        self.assertEqual(d(self.rig, "rig.d['held_none']"), ("refuse", "None"))
        self.assertEqual(d(self.rig, "rig.parent"), ("refuse", "None"))
        self.assertEqual(d(self.rig, "rig.d['s']"), ("refuse", "a str"))
        self.assertEqual(d(self.rig, "rig.d['missing']"), ("first-run", self.U))  # still creatable
        self.assertEqual(d(None, "rig.mode"), ("first-run", self.U))
        self.assertEqual(d(self.rig, "rig.mode"), ("run", 1.0))
        self.assertEqual(d(self.rig, "rig.flag"), ("run", True))

    # --- undo/redo -----------------------------------------------------------
    def test_restore_blocker(self):
        b = self.caq._toggle_restore_blocker
        self.assertEqual(
            b(self.rig, "rig.d['held_none']", 1.0),
            "the property now holds None, not a number or boolean",
        )
        self.assertIsNone(b(self.rig, "rig.d['missing']", 1.0))   # undo may re-create it
        self.assertIsNone(b(self.rig, "rig.mode", 1.0))
        self.assertTrue(b(self.rig, "rig.d['s']", 1.0).startswith("the property now holds a str"))
        self.assertTrue(b(self.rig, "rig.mode", None).startswith("the saved value None"))

    def test_type_change_note(self):
        n = self.caq._toggle_type_change_note
        self.assertEqual(
            n(1.0, self.rig, "rig.parent", self.ctx),
            "the property is now NoneType (it was float)",
        )
        self.assertIsNone(n(1.0, self.rig, "rig.d['missing']", self.ctx))
        self.assertIsNone(n(1.0, self.rig, "rig.mode", self.ctx))

    def test_sentinel_hygiene(self):
        U = self.U
        self.assertIsNotNone(U)
        self.assertNotIsInstance(U, (bool, int, float, str))
        self.assertFalse(self.caq.toggle_value_supported(U))


if __name__ == "__main__":
    unittest.main()
