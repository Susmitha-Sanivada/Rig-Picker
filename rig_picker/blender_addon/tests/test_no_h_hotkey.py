"""The picker window has no H / Alt+H hotkey (hide all / show all)."""
import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import PKG_DIR  # noqa: E402


class NoHHotkeyTests(unittest.TestCase):
    def test_key_press_handlers_do_not_use_the_h_key(self):
        with open(os.path.join(PKG_DIR, "ui", "main_window.py"), encoding="utf8") as f:
            tree = ast.parse(f.read())
        handlers = [n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef)
                    and n.name in ("keyPressEvent", "eventFilter")]
        self.assertTrue(handlers)
        used = {n.attr for h in handlers for n in ast.walk(h)
                if isinstance(n, ast.Attribute)}
        self.assertNotIn("Key_H", used)
        calls = {ast.unparse(n.func) for h in handlers for n in ast.walk(h)
                 if isinstance(n, ast.Call)}
        self.assertNotIn("self.controller.hide_all", calls)
        self.assertNotIn("self.controller.show_all", calls)

    def test_the_other_hotkeys_are_still_there(self):
        with open(os.path.join(PKG_DIR, "ui", "main_window.py"), encoding="utf8") as f:
            tree = ast.parse(f.read())
        keys = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertIn("Key_A", keys)
        self.assertIn("Key_Z", keys)

    def test_invert_selection_hotkey_is_wired(self):
        with open(os.path.join(PKG_DIR, "ui", "main_window.py"), encoding="utf8") as f:
            tree = ast.parse(f.read())
        kp = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "keyPressEvent")
        src = ast.unparse(kp)
        self.assertIn("Qt.Key_I", src)
        self.assertIn("Qt.ControlModifier", src)
        self.assertNotIn("Qt.Key_I and event.modifiers() & Qt.AltModifier", src)
        i = src.index("Qt.Key_I")
        self.assertNotIn("event.modifiers() & (Qt.ControlModifier | Qt.AltModifier)", src[i:i + 300])
        self.assertIn("self.controller.invert_selection()", src)


if __name__ == "__main__":
    unittest.main()
