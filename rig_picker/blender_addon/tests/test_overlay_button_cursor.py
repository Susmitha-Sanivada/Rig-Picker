"""Quick Action buttons and toggles, and the symmetry handle, show the hand
cursor like the picker controls do (disabled ones too). control_list.py needs PySide6,
so the relevant code is checked from its source / run against fakes."""
import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import PKG_DIR  # noqa: E402


def source_tree():
    with open(os.path.join(PKG_DIR, "ui", "control_list.py"), encoding="utf8") as f:
        return ast.parse(f.read())


def find_method(tree, cls, name):
    c = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    return next(n for n in c.body if isinstance(n, ast.FunctionDef) and n.name == name)


class OverlayButtonCursorTests(unittest.TestCase):
    def test_new_quick_action_buttons_get_the_hand_cursor(self):
        fn = find_method(source_tree(), "PickerCanvas", "_sync_quick_action_group")
        src = ast.unparse(fn)
        i = src.index("QPushButton(label, self)")
        self.assertIn("btn.setCursor(Qt.PointingHandCursor)", src[i:i + 400])

    def test_enabled_updates_go_through_the_cursor_helper(self):
        tree = source_tree()
        for name in ("update_quick_single_actions_enabled",
                     "update_quick_multi_actions_enabled"):
            src = ast.unparse(find_method(tree, "PickerCanvas", name))
            self.assertIn("self._set_overlay_button_enabled(btn, enabled)", src)
            self.assertNotIn("btn.setEnabled(enabled)", src)

    def test_helper_keeps_the_hand_cursor_whether_enabled_or_disabled(self):
        fn = find_method(source_tree(), "PickerCanvas", "_set_overlay_button_enabled")
        fn.decorator_list = []
        mod = ast.Module(body=[fn], type_ignores=[])
        ast.fix_missing_locations(mod)

        class Widget:                       # QPushButton / ToggleSwitch
            def __init__(self):
                self.enabled, self.cursor = True, None

            def setEnabled(self, v):
                self.enabled = v

            def setCursor(self, c):
                self.cursor = c

        qt = type("Qt", (), {"PointingHandCursor": "hand", "ArrowCursor": "arrow"})
        ns = {"Qt": qt}
        exec(compile(mod, "x", "exec"), ns)
        helper = ns["_set_overlay_button_enabled"]

        for kind in range(2):
            w = Widget()
            helper(w, False)
            self.assertEqual((w.enabled, w.cursor), (False, "hand"))
            helper(w, True)
            self.assertEqual((w.enabled, w.cursor), (True, "hand"))

    def test_toggle_labels_get_the_hand_cursor(self):
        fn = find_method(source_tree(), "PickerCanvas", "_sync_quick_action_group")
        src = ast.unparse(fn)
        i = src.index("name_label = QLabel")
        self.assertIn("name_label.setCursor(Qt.PointingHandCursor)", src[i:i + 600])

    def test_toggle_switch_sets_the_hand_cursor_itself(self):
        with open(os.path.join(PKG_DIR, "ui", "toggle_switch.py"), encoding="utf8") as f:
            self.assertIn("self.setCursor(Qt.PointingHandCursor)", f.read())


class SymmetryCursorTests(unittest.TestCase):
    def method(self):
        fn = find_method(source_tree(), "PickerCanvas", "_update_symmetry_cursor")
        mod = ast.Module(body=[fn], type_ignores=[])
        ast.fix_missing_locations(mod)
        qt = type("Qt", (), {"PointingHandCursor": "hand"})
        ns = {"Qt": qt}
        exec(compile(mod, "x", "exec"), ns)
        return ns["_update_symmetry_cursor"]

    def canvas(self, **kw):
        class Canvas:
            symmetry_enabled = True
            dragging_symmetry = False
            _pending_drag_kind = None
            _symmetry_cursor_on = False

            def __init__(self):
                self.calls = []

            def setCursor(self, c):
                self.calls.append(("set", c))

            def unsetCursor(self):
                self.calls.append(("unset",))

            _disabled_hover_widget = None

        refresh = find_method(source_tree(), "PickerCanvas", "_refresh_canvas_cursor")
        mod = ast.Module(body=[refresh], type_ignores=[])
        ast.fix_missing_locations(mod)
        ns = {"Qt": type("Qt", (), {"PointingHandCursor": "hand"})}
        exec(compile(mod, "x", "exec"), ns)
        Canvas._refresh_canvas_cursor = ns["_refresh_canvas_cursor"]
        c = Canvas()
        for k, v in kw.items():
            setattr(c, k, v)
        return c

    def test_hand_over_the_handle_and_back_to_normal_off_it(self):
        f, c = self.method(), self.canvas()
        f(c, True)
        self.assertEqual(c.calls, [("set", "hand")])
        f(c, True)                              # no redundant calls
        self.assertEqual(len(c.calls), 1)
        f(c, False)
        self.assertEqual(c.calls[-1], ("unset",))

    def test_no_hand_when_symmetry_is_off(self):
        f, c = self.method(), self.canvas(symmetry_enabled=False)
        f(c, True)
        self.assertEqual(c.calls, [])

    def test_hand_is_kept_through_a_drag_even_if_the_pointer_leaves_the_handle(self):
        f, c = self.method(), self.canvas(dragging_symmetry=True)
        f(c, False)
        self.assertEqual(c.calls, [("set", "hand")])

        f2, c2 = self.method(), self.canvas(_pending_drag_kind="symmetry")
        f2(c2, False)
        self.assertEqual(c2.calls, [("set", "hand")])

    def test_other_pending_drags_do_not_show_the_hand(self):
        f, c = self.method(), self.canvas(_pending_drag_kind="image")
        f(c, False)
        self.assertEqual(c.calls, [])

    def test_canvas_tracks_the_mouse_and_calls_it_from_move_and_release(self):
        tree = source_tree()
        init = ast.unparse(find_method(tree, "PickerCanvas", "__init__"))
        self.assertIn("self.setMouseTracking(True)", init)
        for name in ("mouseMoveEvent", "mouseReleaseEvent"):
            self.assertIn("self._update_symmetry_cursor(",
                          ast.unparse(find_method(tree, "PickerCanvas", name)))


if __name__ == "__main__":
    unittest.main()
