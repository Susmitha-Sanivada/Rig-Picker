"""The picker's Select All hotkey is the plain A key only: Ctrl+A, Shift+A,
Alt+A (and Meta+A) must not select everything. The same goes for the G grab
hotkey: Ctrl/Shift/Alt/Meta+G must not start a grab. Real Qt (offscreen); the
window's keyPressEvent() is taken from main_window.py's source and mounted on
a bare QWidget with a fake controller. Skipped without PySide6."""
import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import PKG_DIR  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication, QWidget
except ImportError:                                    # pragma: no cover
    QWidget = None


def _window_class():
    with open(os.path.join(PKG_DIR, "ui", "main_window.py"), encoding="utf8") as f:
        tree = ast.parse(f.read())
    fn = next(n for c in tree.body if isinstance(c, ast.ClassDef)
              for n in c.body if isinstance(n, ast.FunctionDef) and n.name == "keyPressEvent")
    fn.decorator_list = []
    # Inside a class body so the zero-argument super() in the handler works.
    cls = ast.ClassDef(name="Win", bases=[ast.Name("QWidget", ast.Load())],
                       keywords=[], body=[fn], decorator_list=[])
    mod = ast.Module(body=[cls], type_ignores=[])
    ast.fix_missing_locations(mod)
    ns = {"Qt": Qt, "QWidget": QWidget}
    exec(compile(mod, "main_window.py", "exec"), ns)
    return ns["Win"]


@unittest.skipIf(QWidget is None, "needs PySide6")
class AHotkeyModifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.Win = _window_class()

    def press(self, key, mods=None):
        calls = []

        class Controller:
            def select_all(s): calls.append("select_all")
            def invert_selection(s): calls.append("invert")

        win = self.Win()
        self.addCleanup(win.deleteLater)
        win.controller = Controller()
        ev = QKeyEvent(QEvent.KeyPress, key, mods if mods is not None else Qt.NoModifier)
        win.keyPressEvent(ev)
        return calls, ev.isAccepted()

    def test_plain_a_selects_all(self):
        calls, accepted = self.press(Qt.Key_A)
        self.assertEqual(calls, ["select_all"])
        self.assertTrue(accepted)

    def test_modified_a_does_not_select_all(self):
        for name, mod in (("Ctrl", Qt.ControlModifier), ("Shift", Qt.ShiftModifier),
                          ("Alt", Qt.AltModifier), ("Meta", Qt.MetaModifier),
                          ("Ctrl+Shift", Qt.ControlModifier | Qt.ShiftModifier)):
            calls, accepted = self.press(Qt.Key_A, mod)
            self.assertEqual(calls, [], name)
            self.assertFalse(accepted, name)        # left for whoever else handles it

    def test_other_shortcuts_are_unaffected(self):
        calls, accepted = self.press(Qt.Key_I, Qt.ControlModifier)
        self.assertEqual(calls, ["invert"])
        self.assertTrue(accepted)


@unittest.skipIf(QWidget is None, "needs PySide6")
class GHotkeyModifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.Win = _window_class()

    def press(self, mods=None):
        import types
        calls = []
        win = self.Win()
        self.addCleanup(win.deleteLater)
        win.controller = None          # the G branch must not need it
        win.control_list = types.SimpleNamespace(
            container=types.SimpleNamespace(begin_grab=lambda: calls.append("grab") or True))
        ev = QKeyEvent(QEvent.KeyPress, Qt.Key_G, mods if mods is not None else Qt.NoModifier)
        win.keyPressEvent(ev)
        return calls, ev.isAccepted()

    def test_plain_g_starts_a_grab(self):
        calls, accepted = self.press()
        self.assertEqual(calls, ["grab"])
        self.assertTrue(accepted)

    def test_modified_g_does_not_start_a_grab(self):
        for name, mod in (("Ctrl", Qt.ControlModifier), ("Shift", Qt.ShiftModifier),
                          ("Alt", Qt.AltModifier), ("Meta", Qt.MetaModifier),
                          ("Ctrl+Shift", Qt.ControlModifier | Qt.ShiftModifier)):
            calls, accepted = self.press(mod)
            self.assertEqual(calls, [], name)
            self.assertFalse(accepted, name)        # left for whoever else handles it


if __name__ == "__main__":
    unittest.main()
