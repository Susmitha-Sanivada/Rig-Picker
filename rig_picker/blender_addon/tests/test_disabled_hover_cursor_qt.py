"""Hand cursor over DISABLED Quick Action buttons, against real Qt
(offscreen platform): Qt ignores a disabled widget's own cursor and shows
its parent's, so PickerCanvas puts the hand on itself while the pointer is
over one. The canvas methods are taken from control_list.py's source and
mounted on a small stand-in canvas (the real class needs Blender).
Skipped when PySide6 isn't installed."""
import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import PKG_DIR  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QEvent, QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget
except ImportError:                                    # pragma: no cover
    QWidget = None

METHODS = ("_refresh_canvas_cursor", "_update_symmetry_cursor",
           "_update_disabled_hover_cursor", "_clear_disabled_hover_cursor")


def canvas_tree():
    with open(os.path.join(PKG_DIR, "ui", "control_list.py"), encoding="utf8") as f:
        return ast.parse(f.read())


def picker_canvas(tree):
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PickerCanvas")


@unittest.skipIf(QWidget is None, "needs PySide6")
class DisabledHoverCursorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls_node = picker_canvas(canvas_tree())
        ns = {"Qt": Qt, "QEvent": QEvent}
        fns = [n for n in cls_node.body if isinstance(n, ast.FunctionDef) and n.name in METHODS]
        assert {f.name for f in fns} == set(METHODS)
        mod = ast.Module(body=fns, type_ignores=[])
        ast.fix_missing_locations(mod)
        exec(compile(mod, "control_list.py", "exec"), ns)

        class Canvas(QWidget):
            _OVERLAY_HOVER_EVENTS = (QEvent.Enter, QEvent.HoverEnter, QEvent.HoverMove,
                                     QEvent.Leave, QEvent.HoverLeave)
            symmetry_enabled = False
            dragging_symmetry = False
            _pending_drag_kind = None

            def __init__(self):
                super().__init__()
                self.resize(300, 200)
                self.setMouseTracking(True)
                self._symmetry_cursor_on = False
                self._disabled_hover_widget = None
                self.children_ = {}

            def add(self, enabled, x=100, y=100, kind=None):
                b = (kind or QPushButton)("x", self)
                b.move(x, y)
                b.resize(80, 30)
                b.setCursor(Qt.PointingHandCursor)
                b.setEnabled(enabled)
                b.installEventFilter(self)
                b.show()
                return b

            def eventFilter(self, obj, event):          # mirrors PickerCanvas.eventFilter
                if event.type() in self._OVERLAY_HOVER_EVENTS:
                    self._update_disabled_hover_cursor(obj, event.type())
                    return False
                return False

            def mouseMoveEvent(self, event):            # mirrors PickerCanvas.mouseMoveEvent
                self._clear_disabled_hover_cursor()
                self._update_symmetry_cursor(False)

        for name in METHODS:
            setattr(Canvas, name, ns[name])
        cls.Canvas = Canvas

    def setUp(self):
        self.c = self.Canvas()
        self.c.show()
        self.app.processEvents()

    def tearDown(self):
        self.c.close()

    def move(self, x, y):
        QTest.mouseMove(self.c, QPoint(x, y))
        self.app.processEvents()

    def cursor(self):
        return self.c.windowHandle().cursor().shape()

    def test_qt_really_ignores_a_disabled_childs_own_cursor(self):
        # The reason for all this: without the canvas fix a disabled button's
        # own hand cursor is not shown.
        b = self.c.add(enabled=False)
        self.move(10, 10)
        self.c.removeEventFilter(self.c)
        b.removeEventFilter(self.c)
        self.move(120, 110)
        self.move(125, 112)
        self.assertEqual(self.cursor(), Qt.ArrowCursor)

    def test_disabled_button_shows_the_hand_and_it_clears_on_leaving(self):
        self.c.add(enabled=False)
        self.move(10, 10)
        self.assertEqual(self.cursor(), Qt.ArrowCursor)
        self.move(120, 110)
        self.move(125, 112)
        self.assertEqual(self.cursor(), Qt.PointingHandCursor)
        self.move(10, 10)
        self.assertEqual(self.cursor(), Qt.ArrowCursor)

    def test_disabled_toggle_label_shows_the_hand_too(self):
        self.c.add(enabled=False, kind=QLabel)
        self.move(10, 10)
        self.move(110, 105)
        self.move(112, 108)
        self.assertEqual(self.cursor(), Qt.PointingHandCursor)
        self.move(10, 10)
        self.assertEqual(self.cursor(), Qt.ArrowCursor)

    def test_disabled_toggle_switch_shows_the_hand_too(self):
        # ToggleSwitch is a plain QWidget painted by hand.
        self.c.add(enabled=False, kind=lambda t, p: QWidget(p))
        self.move(10, 10)
        self.move(110, 105)
        self.move(112, 108)
        self.assertEqual(self.cursor(), Qt.PointingHandCursor)

    def test_enabled_button_still_shows_the_hand(self):
        self.c.add(enabled=True)
        self.move(10, 10)
        self.move(120, 110)
        self.move(125, 112)
        self.assertEqual(self.cursor(), Qt.PointingHandCursor)
        self.move(10, 10)
        self.assertEqual(self.cursor(), Qt.ArrowCursor)

    def test_moving_between_two_disabled_buttons_keeps_the_hand(self):
        self.c.add(enabled=False, x=20, y=100)
        self.c.add(enabled=False, x=140, y=100)
        self.move(10, 10)
        self.move(40, 110)
        self.move(45, 112)
        self.assertEqual(self.cursor(), Qt.PointingHandCursor)
        self.move(160, 110)
        self.move(165, 112)
        self.assertEqual(self.cursor(), Qt.PointingHandCursor)
        self.move(10, 10)
        self.assertEqual(self.cursor(), Qt.ArrowCursor)

    def test_hand_stays_if_symmetry_handle_cursor_is_also_on(self):
        b = self.c.add(enabled=False)
        self.c._symmetry_cursor_on = True
        self.c._update_disabled_hover_cursor(b, QEvent.Enter)
        self.c._update_disabled_hover_cursor(b, QEvent.Leave)
        self.assertTrue(self.c._symmetry_cursor_on)
        self.assertEqual(self.c.cursor().shape(), Qt.PointingHandCursor)

    def test_a_button_enabled_while_hovered_stops_counting(self):
        b = self.c.add(enabled=False)
        self.c._update_disabled_hover_cursor(b, QEvent.Enter)
        self.assertIs(self.c._disabled_hover_widget, b)
        b.setEnabled(True)
        self.c._update_disabled_hover_cursor(b, QEvent.HoverMove)
        self.assertIsNone(self.c._disabled_hover_widget)
        self.assertNotEqual(self.c.cursor().shape(), Qt.PointingHandCursor)

    def test_a_missed_leave_is_healed_by_the_next_canvas_move(self):
        b = self.c.add(enabled=False)
        self.c._update_disabled_hover_cursor(b, QEvent.Enter)
        self.c.mouseMoveEvent(None)
        self.assertIsNone(self.c._disabled_hover_widget)
        self.assertNotEqual(self.c.cursor().shape(), Qt.PointingHandCursor)

    def test_a_leave_for_another_widget_is_ignored(self):
        a = self.c.add(enabled=False, x=20)
        b = self.c.add(enabled=False, x=160)
        self.c._update_disabled_hover_cursor(a, QEvent.Enter)
        self.c._update_disabled_hover_cursor(b, QEvent.Leave)
        self.assertIs(self.c._disabled_hover_widget, a)


class RealCanvasWiringTests(unittest.TestCase):
    """The real PickerCanvas is wired to the same hooks (checked from source)."""

    def method_src(self, name):
        node = next(n for n in picker_canvas(canvas_tree()).body
                    if isinstance(n, ast.FunctionDef) and n.name == name)
        return ast.unparse(node)

    def test_event_filter_dispatches_hover_events_before_the_drag_handling(self):
        src = self.method_src("eventFilter")
        i = src.index("self._update_disabled_hover_cursor(obj, event_type)")
        self.assertLess(i, src.index("QEvent.MouseButtonPress"))

    def test_canvas_move_clears_stale_disabled_hover_state(self):
        self.assertIn("self._clear_disabled_hover_cursor()", self.method_src("mouseMoveEvent"))

    def test_unregistering_a_group_clears_it(self):
        self.assertIn("self._clear_disabled_hover_cursor()",
                      self.method_src("_unregister_overlay_drag_group"))

    def test_hover_events_list_covers_enter_leave_and_hover(self):
        node = next(n for n in picker_canvas(canvas_tree()).body
                    if isinstance(n, ast.Assign)
                    and getattr(n.targets[0], "id", "") == "_OVERLAY_HOVER_EVENTS")
        names = {e.attr for e in node.value.elts}
        self.assertEqual(names, {"Enter", "HoverEnter", "HoverMove", "Leave", "HoverLeave"})


if __name__ == "__main__":
    unittest.main()
