"""Small-canvas policy for dragging a Quick Action button/toggle group: an
axis along which the group is bigger than the canvas is locked (the group
keeps its position there), the other axis still works if it fits, and if
neither fits the group can't be dragged. Real Qt (offscreen); the canvas
methods come from control_list.py's source on a stand-in canvas. Skipped
without PySide6."""
import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import PKG_DIR  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtWidgets import QApplication, QPushButton, QWidget
except ImportError:                                    # pragma: no cover
    QWidget = None

NAMES = {"_drag_overlay_group", "_sweep_to", "_resolve_overlay_collision",
         "_rects_overlap", "_overlay_obstacle_rects", "_other_picker_controls"}


def _methods():
    with open(os.path.join(PKG_DIR, "ui", "control_list.py"), encoding="utf8") as f:
        tree = ast.parse(f.read())
    canvas = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PickerCanvas")
    fns = [n for n in canvas.body if isinstance(n, ast.FunctionDef) and n.name in NAMES]
    assert {f.name for f in fns} == NAMES
    for f in fns:
        f.decorator_list = [d for d in f.decorator_list
                            if isinstance(d, ast.Name) and d.id == "staticmethod"]
    ns = {"QRect": QRect, "QPoint": QPoint, "CircleControl": QWidget}
    mod = ast.Module(body=fns, type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "control_list.py", "exec"), ns)
    out = {f.name: ns[f.name] for f in fns}
    out["_rects_overlap"] = staticmethod(out["_rects_overlap"])
    out["_sweep_to"] = staticmethod(out["_sweep_to"])
    return out


@unittest.skipIf(QWidget is None, "needs PySide6")
class SmallCanvasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.fns = _methods()

    def canvas(self, w, h):
        fns = self.fns

        class Canvas(QWidget):
            control_list = None

            def __init__(s):
                super().__init__()
                s.resize(w, h)
                s.controls = []
                s.OVERLAY_DRAG_GROUPS = {}
                s._overlay_drag_offset = {}
                s.overlay_positions = {}
                s.controller = type("C", (), {"data": {}})()

            def findChildren(s, *a, **k): return s.controls
            def canvas_to_overlay_position(s, p): return QPoint(p)

        for n, f in fns.items():
            setattr(Canvas, n, f)
        c = Canvas()
        self.addCleanup(c.deleteLater)
        return c

    def group(self, canvas, key, geoms):
        ws = []
        for g in geoms:
            b = QPushButton("b", canvas)
            b.setGeometry(*g)
            b.show()
            ws.append(b)
        canvas.OVERLAY_DRAG_GROUPS[key] = ws
        canvas._overlay_drag_offset[key] = QPoint(0, 0)
        return ws

    def drag_to(self, canvas, key, x, y):
        canvas._drag_overlay_group(key, canvas.mapToGlobal(QPoint(x, y)))

    def test_fitting_group_is_still_clamped_inside_the_canvas(self):
        canvas = self.canvas(400, 300)
        a, b = self.group(canvas, "t", [(10, 10, 80, 30), (100, 10, 60, 30)])   # 150 wide
        self.drag_to(canvas, "t", 900, 900)
        self.assertEqual((a.x(), a.y()), (400 - 150, 300 - 30))
        self.assertEqual(b.x() + b.width(), 400)
        self.drag_to(canvas, "t", -900, -900)
        self.assertEqual((a.x(), a.y()), (0, 0))

    def test_group_wider_than_canvas_is_locked_horizontally_but_moves_vertically(self):
        canvas = self.canvas(100, 300)
        a, b = self.group(canvas, "t", [(10, 10, 80, 30), (100, 10, 60, 30)])   # 150 > 100
        self.drag_to(canvas, "t", 50, 200)
        self.assertEqual((a.x(), b.x()), (10, 100))        # no horizontal movement at all
        self.assertEqual((a.y(), b.y()), (200, 200))       # vertical drag still works
        self.assertEqual(canvas.overlay_positions["t"], QPoint(10, 200))

    def test_group_taller_than_canvas_is_locked_vertically_but_moves_horizontally(self):
        canvas = self.canvas(400, 50)
        a, = self.group(canvas, "t", [(10, 5, 80, 70)])                         # 70 > 50
        self.drag_to(canvas, "t", 300, 40)
        self.assertEqual(a.y(), 5)
        self.assertEqual(a.x(), 300)

    def test_group_that_fits_neither_way_cannot_be_dragged_or_persisted(self):
        canvas = self.canvas(100, 40)
        a, b = self.group(canvas, "t", [(0, 0, 80, 30), (90, 0, 60, 60)])
        before = (a.geometry(), b.geometry())
        self.drag_to(canvas, "t", 70, 30)
        self.assertEqual((a.geometry(), b.geometry()), before)
        self.assertNotIn("t", canvas.overlay_positions)     # nothing was written
        self.assertEqual(canvas.controller.data, {})

    def test_group_is_usable_again_once_the_canvas_is_big_enough(self):
        canvas = self.canvas(100, 40)
        a, = self.group(canvas, "t", [(0, 0, 150, 50)])      # too wide AND too tall
        self.drag_to(canvas, "t", 50, 10)
        self.assertEqual((a.x(), a.y()), (0, 0))
        canvas.resize(400, 300)
        self.drag_to(canvas, "t", 50, 100)
        self.assertEqual((a.x(), a.y()), (50, 100))


if __name__ == "__main__":
    unittest.main()
