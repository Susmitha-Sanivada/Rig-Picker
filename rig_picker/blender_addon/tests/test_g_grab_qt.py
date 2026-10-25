"""G key = grab whatever the hand cursor is on (picker control, Quick Action
button/toggle, symmetry handle): it starts the same drag a mouse
press-and-drag does, then follows the pointer with no button held until
click / Enter / G (confirm) or Esc / right-click (cancel).

Runs against real Qt (offscreen). The grab methods are taken from
control_list.py's source and mounted on a small stand-in canvas (the real
class needs Blender); the real CircleControl is used. Skipped without PySide6."""
import ast
import importlib.util
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import PKG_DIR  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, Qt
    from PySide6.QtGui import QCursor, QKeyEvent, QMouseEvent
    from PySide6.QtWidgets import QApplication, QPushButton, QWidget
except ImportError:                                    # pragma: no cover
    QWidget = None

METHODS = ("grab_target_under_cursor", "_drag_in_progress", "begin_grab",
           "_grab_move", "interrupt_for_undo", "end_grab",
           "_grab_group_controls", "_warn_group_grab_needs_symmetry_off", "_make_group_grab", "_group_members_at",
           "_group_blocked", "_group_grab_move")


def _load_circle_control():
    pkg = types.ModuleType("blender_addon")
    pkg.__path__ = [PKG_DIR]
    ui = types.ModuleType("blender_addon.ui")
    ui.__path__ = [os.path.join(PKG_DIR, "ui")]
    sys.modules.update({"blender_addon": pkg, "blender_addon.ui": ui})
    path = os.path.join(PKG_DIR, "ui", "circle_control.py")
    spec = importlib.util.spec_from_file_location("blender_addon.ui.circle_control", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.CircleControl


def _grab_code(CircleControl):
    with open(os.path.join(PKG_DIR, "ui", "control_list.py"), encoding="utf8") as f:
        tree = ast.parse(f.read())
    filt = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "_GrabFilter")
    canvas = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PickerCanvas")
    fns = [n for n in canvas.body if isinstance(n, ast.FunctionDef) and n.name in METHODS]
    assert {f.name for f in fns} == set(METHODS)
    cs_spec = importlib.util.spec_from_file_location(
        "collision_shapes", os.path.join(PKG_DIR, "ui", "collision_shapes.py"))
    shapes = importlib.util.module_from_spec(cs_spec)
    cs_spec.loader.exec_module(shapes)
    ns = {"Qt": Qt, "QEvent": QEvent, "QObject": QObject, "CircleControl": CircleControl,
          "QPoint": QPoint, "shapes": shapes,
          "safe_override": lambda default=None: (lambda fn: fn)}
    mod = ast.Module(body=[filt], type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "control_list.py", "exec"), ns)
    mod = ast.Module(body=fns, type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "control_list.py", "exec"), ns)
    return {f.name: ns[f.name] for f in fns}


@unittest.skipIf(QWidget is None, "needs PySide6")
class GKeyGrabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.CC = _load_circle_control()
        cls.methods = _grab_code(cls.CC)

    def make(self):
        CC, methods = self.CC, self.methods
        log = {"snap": 0, "save": 0, "undo": 0, "clicked": [], "overlay": [], "sym": []}

        class Controller:
            def _record_undo_snapshot(s, *a, **k): log["snap"] += 1
            def save(s): log["save"] += 1
            selected_bones = set()
            data = {}

            def undo(s, discard_redo=False):
                log["undo"] += 1
                log["discard_redo"] = discard_redo

        class Canvas(QWidget):
            DRAG_THRESHOLD = 4
            symmetry_enabled = True
            symmetry_x = -1
            image_x = 0
            dragging_symmetry = False
            dragging_image = False
            box_select_active = False
            _pending_drag_start = None

            def __init__(self):
                super().__init__()
                self.controller = Controller()
                self.resize(400, 300)
                self.move(50, 50)
                self._grab = None
                self._grab_filter = None
                self._overlay_drag_group_of = {}
                self.OVERLAY_DRAG_GROUPS = {}
                self._overlay_press_pos = {}
                self._overlay_drag_offset = {}
                self.symmetry_enabled = True
                self.control_list = types.SimpleNamespace(controls={})

            def window(self): return self
            def _mirror_rect_for(self, c, x, y): return None
            def _other_picker_controls(self, c):
                return [w for w in self.findChildren(CC) if w is not c]
            def _collision_shape(self, w):
                g = w.geometry()
                if getattr(w, "exact_shape", False):     # opt-in: its real outline
                    return shapes_mod.shape_for(w.shape, g.x(), g.y(), g.width(), g.height())
                return shapes_mod.rect_shape(g.x(), g.y(), g.width(), g.height())
            def _overlay_obstacle_shapes(self): return []
            def image_scale(self): return 1.0
            def resolve_control_collision(self, c, x, y, hold=None): return x, y
            def mirror_collides(self, c, x, y): return False
            def _sweep_to(self, start, target, resolve, step=4): return resolve(*target, start)
            def move_control_from_canvas(self, c, pos): c.move(pos)
            def symmetry_handle_rect(self): return QRect(190, 0, 24, 28)
            def _update_symmetry_cursor(self, over): pass
            def _drag_overlay_group(self, key, gp): log["overlay"].append((key, gp))
            def _drag_symmetry_to(self, x): log["sym"].append(x)

        shapes_mod = methods["_group_blocked"].__globals__["shapes"]
        for name, fn in methods.items():
            setattr(Canvas, name, fn)

        canvas = Canvas()
        canvas.show()
        self.addCleanup(self._dispose, canvas)

        ctl = CC("Bone.L")
        ctl.setParent(canvas)
        ctl.move(100, 100)
        ctl.clicked.connect(lambda *a: log["clicked"].append(a))
        ctl.show()

        off = CC("Off.L")
        off.setParent(canvas)
        off.move(300, 200)
        off.setEnabled(False)
        off.show()

        btn = QPushButton("act", canvas)
        btn.move(200, 150)
        btn.resize(80, 30)
        btn.show()
        btn_clicks = []
        btn.clicked.connect(lambda: btn_clicks.append(1))
        canvas._overlay_drag_group_of[btn] = "quick1"
        canvas.OVERLAY_DRAG_GROUPS["quick1"] = [btn]

        self.app.processEvents()
        return canvas, ctl, off, btn, btn_clicks, log

    def _dispose(self, canvas):
        canvas.end_grab(False)
        canvas.hide()
        canvas.deleteLater()
        self.app.processEvents()

    def point(self, canvas, widget, dx=5, dy=5):
        p = widget.mapToGlobal(QPoint(dx, dy))
        QCursor.setPos(p)
        self.app.processEvents()
        return p

    def move(self, canvas, gp):
        ev = QMouseEvent(QEvent.MouseMove, QPointF(canvas.mapFromGlobal(gp)), QPointF(gp),
                         Qt.NoButton, Qt.NoButton, Qt.NoModifier)
        QApplication.sendEvent(canvas, ev)

    def click(self, canvas, gp, button=None):
        # Resolved here, not in the signature: a `Qt.LeftButton` default
        # is evaluated while the module loads, which raises NameError
        # (breaking test discovery) when PySide6 is missing and Qt is
        # unbound - skipIf can't help, the class body still runs.
        if button is None:
            button = Qt.LeftButton
        for t in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
            ev = QMouseEvent(t, QPointF(canvas.mapFromGlobal(gp)), QPointF(gp),
                             button, button, Qt.NoModifier)
            QApplication.sendEvent(canvas, ev)

    def key(self, canvas, key, modifiers=None):
        if modifiers is None:
            modifiers = Qt.NoModifier
        QApplication.sendEvent(canvas, QKeyEvent(QEvent.KeyPress, key, modifiers))

    def test_g_over_control_follows_mouse_then_click_confirms(self):
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        start = self.point(canvas, ctl)
        self.assertTrue(canvas.begin_grab())
        self.assertEqual(log["snap"], 0)                 # nothing recorded until it moves
        self.move(canvas, start + QPoint(40, 25))
        self.assertEqual(ctl.pos(), QPoint(140, 125))
        self.assertTrue(ctl.dragging)
        self.assertEqual(log["snap"], 1)
        self.move(canvas, start + QPoint(60, 25))
        self.assertEqual(ctl.pos(), QPoint(160, 125))
        self.assertEqual(log["snap"], 1)                 # one snapshot for the whole move
        self.click(canvas, start + QPoint(60, 25))
        self.assertIsNone(canvas._grab)
        self.assertFalse(ctl.dragging)
        self.assertEqual(log["save"], 1)
        self.assertEqual(log["clicked"], [("Bone.L", False, True)])
        self.move(canvas, start + QPoint(200, 100))      # grab is over
        self.assertEqual(ctl.pos(), QPoint(160, 125))

    def select_group(self, canvas, ctl, *extra):
        """Select `ctl` plus extra controls, as shift-click would."""
        canvas.control_list.controls = {c.bone_name: c for c in (ctl,) + extra}
        canvas.controller.selected_bones = set(canvas.control_list.controls)

    def test_g_with_several_selected_moves_them_all_and_keeps_the_selection(self):
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        second = self.CC("Two.L")
        second.setParent(canvas)
        second.move(100, 200)
        second.show()
        self.select_group(canvas, ctl, second)
        start = self.point(canvas, ctl)                  # pointer on a selected control
        self.assertTrue(canvas.begin_grab())
        self.move(canvas, start + QPoint(40, 25))
        self.assertEqual(ctl.pos(), QPoint(140, 125))
        self.assertEqual(second.pos(), QPoint(140, 225))  # same delta
        self.assertEqual(log["snap"], 1)
        self.click(canvas, start + QPoint(40, 25))       # confirm
        self.assertEqual((log["save"], log["clicked"]), (1, []))   # selection untouched
        self.assertEqual(canvas.controller.selected_bones, {"Bone.L", "Two.L"})

    def test_g_with_several_selected_works_with_pointer_on_empty_canvas(self):
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        second = self.CC("Two.L")
        second.setParent(canvas)
        second.move(100, 200)
        second.show()
        self.select_group(canvas, ctl, second)
        empty = canvas.mapToGlobal(QPoint(350, 40))
        QCursor.setPos(empty)
        self.app.processEvents()
        self.assertTrue(canvas.begin_grab())
        self.move(canvas, empty + QPoint(-30, 20))
        self.assertEqual((ctl.pos(), second.pos()), (QPoint(70, 120), QPoint(70, 220)))

    def test_cancelling_a_group_grab_goes_through_undo(self):
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        second = self.CC("Two.L")
        second.setParent(canvas)
        second.move(100, 200)
        second.show()
        self.select_group(canvas, ctl, second)
        start = self.point(canvas, ctl)
        self.assertTrue(canvas.begin_grab())
        self.move(canvas, start + QPoint(30, 30))
        self.key(canvas, Qt.Key_Escape)
        self.assertEqual((log["undo"], log["save"], log["discard_redo"]), (1, 0, True))
        self.assertFalse(ctl.dragging or second.dragging)

    def overlapped_group(self, canvas, ctl):
        """Two selected controls (ctl at (100,100), second at (100,140))
        that both already overlap a third, unselected control at the start
        of the grab. Returns (second, wall)."""
        second = self.CC("Two.L")
        second.setParent(canvas)
        second.move(100, 140)
        second.show()
        wall = self.CC("Wall.L", size=60)
        wall.setParent(canvas)
        wall.move(108, 100)                              # covers x 108..168, y 100..160
        wall.show()
        self.select_group(canvas, ctl, second)
        return second, wall

    def test_group_grab_cannot_move_deeper_into_an_existing_overlap(self):
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        second, wall = self.overlapped_group(canvas, ctl)
        start = self.point(canvas, ctl)
        self.assertTrue(canvas.begin_grab())
        for dx in (1, 5, 20):                            # toward the wall: refused
            self.move(canvas, start + QPoint(dx, 0))
            self.assertEqual((ctl.pos(), second.pos()),
                             (QPoint(100, 100), QPoint(100, 140)), dx)

    def test_group_grab_can_move_out_of_an_existing_overlap(self):
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        second, wall = self.overlapped_group(canvas, ctl)
        start = self.point(canvas, ctl)
        self.assertTrue(canvas.begin_grab())
        self.move(canvas, start + QPoint(-4, 0))         # away, still overlapping
        self.assertEqual((ctl.pos(), second.pos()), (QPoint(96, 100), QPoint(96, 140)))
        self.move(canvas, start + QPoint(-2, 0))         # back toward it, but not past
        self.assertEqual((ctl.pos(), second.pos()),      # where the grab started
                         (QPoint(98, 100), QPoint(98, 140)))
        self.move(canvas, start + QPoint(10, 0))         # past the start depth: refused
        self.assertEqual((ctl.pos(), second.pos()), (QPoint(98, 100), QPoint(98, 140)))
        self.move(canvas, start + QPoint(-30, 0))        # all the way out
        self.assertEqual((ctl.pos(), second.pos()), (QPoint(70, 100), QPoint(70, 140)))

    def test_group_grab_blocks_an_obstacle_again_once_it_has_been_cleared(self):
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        second, wall = self.overlapped_group(canvas, ctl)
        start = self.point(canvas, ctl)
        self.assertTrue(canvas.begin_grab())
        self.move(canvas, start + QPoint(-30, 0))        # clear of the wall
        self.assertEqual((ctl.pos(), second.pos()), (QPoint(70, 100), QPoint(70, 140)))
        self.move(canvas, start + QPoint(-5, 0))         # back into it: no longer exempt
        self.assertEqual((ctl.pos(), second.pos()), (QPoint(70, 100), QPoint(70, 140)))
        self.move(canvas, start + QPoint(-10, 0))        # right up to its edge is fine
        self.assertEqual((ctl.pos(), second.pos()), (QPoint(90, 100), QPoint(90, 140)))

    def test_group_grab_cannot_deepen_one_dimension_of_an_overlap(self):
        """10px across + 30px along: sliding along the wall to 40px along
        keeps the SMALLEST depth at 10, so a min-depth check lets it grow."""
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        ctl.shape = "SQUARE"
        ctl.setFixedSize(40, 40)                         # x 100..140, y 100..140
        second = self.CC("Two.L", shape="SQUARE")
        second.setParent(canvas)
        second.move(300, 20)                             # clear of everything
        second.show()
        wall = self.CC("Wall.L", size=80, shape="SQUARE")
        wall.setParent(canvas)
        wall.move(130, 110)                              # x 130..210, y 110..190
        wall.show()
        self.select_group(canvas, ctl, second)

        shapes = self.methods["_group_blocked"].__globals__["shapes"]

        def box(y):
            return shapes.rect_shape(100, y, 40, 40)

        w = shapes.rect_shape(130, 110, 80, 80)
        self.assertEqual(sorted(set(shapes.axis_depths(box(100), w))), [10, 30])
        # the premise: one dimension grows (30 -> 40), the minimum doesn't
        self.assertEqual(shapes.depth_of(box(100), w), 10)
        self.assertEqual(shapes.depth_of(box(110), w), 10)
        self.assertGreater(max(shapes.axis_depths(box(110), w)),
                           max(shapes.axis_depths(box(100), w)))

        start = self.point(canvas, ctl)
        self.assertTrue(canvas.begin_grab())
        for dy in (1, 10):                               # along the wall, deeper: refused
            self.move(canvas, start + QPoint(0, dy))
            self.assertEqual(ctl.pos(), QPoint(100, 100), dy)
        self.move(canvas, start + QPoint(0, -10))        # along the wall, shallower: fine
        self.assertEqual(ctl.pos(), QPoint(100, 90))
        self.move(canvas, start + QPoint(-15, -10))      # out the other side: fine
        self.assertEqual(ctl.pos(), QPoint(85, 90))

    def test_group_grab_cannot_deepen_a_circle_triangle_overlap_as_the_vertex_axis_turns(self):
        """A circle overlapping a triangle is also tested along the axis to
        the triangle's nearest vertex, and that axis changes direction as
        the circle moves. Depths must be compared along the directions
        recorded at the start of the grab, not by position in the list."""
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        ctl.shape = "CIRCLE"
        ctl.setFixedSize(30, 30)
        ctl.move(80, 140)                                # overlaps the triangle's lower-left corner
        second = self.CC("Two.L", shape="SQUARE")
        second.setParent(canvas)
        second.move(300, 20)                             # clear of everything
        second.show()
        tri = self.CC("Tri.L", shape="TRIANGLE")
        tri.setParent(canvas)
        tri.setFixedSize(60, 60)
        tri.move(100, 100)                               # apex (130,100), base (100,160)-(160,160)
        tri.exact_shape = True
        tri.show()
        self.select_group(canvas, ctl, second)

        shapes = self.methods["_group_blocked"].__globals__["shapes"]
        tri_shape = shapes.triangle_shape(100, 100, 60, 60)

        def circle(x, y):
            return shapes.circle_shape(x + 15, y + 15, 15)

        start_axes = shapes.separating_axes(circle(80, 140), tri_shape)
        start_depths = shapes.axis_depths(circle(80, 140), tri_shape, start_axes)
        # the premise: the nearest-vertex axis (last) differs between the two
        # positions, and comparing by list position misses the growth along
        # the start direction (29.14 -> 29.85) that measuring along it sees
        self.assertNotEqual(start_axes[-1], shapes.separating_axes(circle(79, 142), tri_shape)[-1])
        self.assertFalse(shapes.overlap_grew(
            shapes.axis_depths(circle(79, 142), tri_shape), start_depths))
        self.assertTrue(shapes.overlap_grew(
            shapes.axis_depths(circle(79, 142), tri_shape, start_axes), start_depths))

        start = self.point(canvas, ctl)
        self.assertTrue(canvas.begin_grab())
        self.move(canvas, start + QPoint(-4, 4))         # past the drag threshold, shallower
        self.assertEqual(ctl.pos(), QPoint(76, 144))
        self.move(canvas, start + QPoint(-1, 2))         # deeper along the start axis: refused
        self.assertNotEqual(ctl.pos(), QPoint(79, 142))
        ax, ay = ctl.pos().x(), ctl.pos().y()
        self.assertFalse(shapes.overlap_grew(            # wherever it ended up, never deeper
            shapes.axis_depths(circle(ax, ay), tri_shape, start_axes), start_depths))
        self.move(canvas, start + QPoint(-10, 10))       # away from the triangle, clear of it: fine
        self.assertEqual(ctl.pos(), QPoint(70, 150))

    def test_group_grab_can_swing_a_circle_round_another_while_moving_apart(self):
        """Two overlapping circles: the overlap is r1 + r2 - distance, so
        moving the centres apart is always allowed, even when the line
        between them turns (a start-direction axis would call it deeper)."""
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        ctl.shape = "CIRCLE"
        ctl.setFixedSize(40, 40)
        ctl.move(100, 100)                               # centre (120,120)
        second = self.CC("Two.L", shape="SQUARE")
        second.setParent(canvas)
        second.move(300, 20)                             # clear of everything
        second.show()
        other = self.CC("Other.L", shape="CIRCLE")
        other.setParent(canvas)
        other.setFixedSize(40, 40)
        other.move(130, 100)                             # centre (150,120): 30 apart, 10 deep
        other.exact_shape = True
        other.show()
        self.select_group(canvas, ctl, second)

        start = self.point(canvas, ctl)
        self.assertTrue(canvas.begin_grab())
        self.move(canvas, start + QPoint(1, 11))         # centres 30.0 -> 31.02 apart: fine
        self.assertEqual(ctl.pos(), QPoint(101, 111))
        self.move(canvas, start + QPoint(10, 0))         # toward it: deeper, refused
        self.assertEqual(ctl.pos(), QPoint(101, 111))

    def test_g_on_a_control_outside_the_selection_grabs_only_that_one(self):
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        a = self.CC("A.L")
        a.setParent(canvas)
        a.move(100, 200)
        a.show()
        b = self.CC("B.L")
        b.setParent(canvas)
        b.move(200, 200)
        b.show()
        self.select_group(canvas, a, b)                  # ctl is NOT selected
        start = self.point(canvas, ctl)
        self.assertTrue(canvas.begin_grab())
        self.move(canvas, start + QPoint(20, 0))
        self.assertEqual(ctl.pos(), QPoint(120, 100))
        self.assertEqual((a.pos(), b.pos()), (QPoint(100, 200), QPoint(200, 200)))

    def test_g_with_several_selected_and_symmetry_on_warns_instead_of_grabbing(self):
        from PySide6.QtWidgets import QMessageBox
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        second = self.CC("Two.L")
        second.setParent(canvas)
        second.move(100, 200)
        second.show()
        self.select_group(canvas, ctl, second)
        canvas.controller.data = {"symmetry": True}
        start = self.point(canvas, ctl)
        self.assertTrue(canvas.begin_grab())              # key handled...
        self.assertIsNone(canvas._grab)                   # ...but nothing is grabbed
        box = canvas._group_symmetry_box
        self.assertIsInstance(box, QMessageBox)
        self.assertTrue(box.isVisible())
        self.assertIn("Symmetry", box.text())
        self.assertEqual((ctl.pos(), second.pos()), (QPoint(100, 100), QPoint(100, 200)))
        # a second G reuses the open warning instead of stacking another
        self.assertTrue(canvas.begin_grab())
        self.assertIs(canvas._group_symmetry_box, box)
        box.close()

    def test_symmetry_on_with_one_selected_control_still_grabs_normally(self):
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        self.select_group(canvas, ctl)                    # only one selected
        canvas.controller.data = {"symmetry": True}
        start = self.point(canvas, ctl)
        self.assertTrue(canvas.begin_grab())
        self.assertIsNotNone(canvas._grab)
        self.move(canvas, start + QPoint(10, 0))
        self.assertEqual(ctl.pos(), QPoint(110, 100))

    def test_escape_and_right_click_cancel_through_undo(self):
        for how in ("esc", "right"):
            canvas, ctl, off, btn, btn_clicks, log = self.make()
            start = self.point(canvas, ctl)
            self.assertTrue(canvas.begin_grab())
            self.move(canvas, start + QPoint(30, 30))
            if how == "esc":
                self.key(canvas, Qt.Key_Escape)
            else:
                self.click(canvas, start + QPoint(30, 30), Qt.RightButton)
            self.assertIsNone(canvas._grab)
            self.assertEqual((log["undo"], log["save"], log["clicked"]), (1, 0, []), how)
            # A cancelled grab must not leave a redo entry behind.
            self.assertTrue(log["discard_redo"], how)
            self._dispose(canvas)

    def test_ctrl_z_and_ctrl_shift_z_cancel_the_grab_without_an_undo_step(self):
        for mods in (Qt.ControlModifier, Qt.ControlModifier | Qt.ShiftModifier):
            canvas, ctl, off, btn, btn_clicks, log = self.make()
            start = self.point(canvas, ctl)
            self.assertTrue(canvas.begin_grab())
            self.move(canvas, start + QPoint(30, 30))
            self.assertEqual(ctl.pos(), QPoint(130, 130))
            self.key(canvas, Qt.Key_Z, mods)
            self.assertIsNone(canvas._grab)
            self.assertFalse(ctl.dragging)
            # Only the grab's own snapshot is taken back (like Esc), and it
            # must not become redoable; nothing is committed or selected.
            self.assertEqual((log["undo"], log["save"], log["clicked"]), (1, 0, []))
            self.assertTrue(log["discard_redo"])
            self._dispose(canvas)

    def test_a_plain_z_during_a_grab_is_still_swallowed_and_changes_nothing(self):
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        start = self.point(canvas, ctl)
        canvas.begin_grab()
        self.move(canvas, start + QPoint(30, 30))
        self.key(canvas, Qt.Key_Z)
        self.assertIsNotNone(canvas._grab)
        self.assertEqual((log["undo"], log["save"]), (0, 0))

    def test_interrupt_for_undo_cancels_a_grab_and_reports_it_consumed(self):
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        start = self.point(canvas, ctl)
        canvas.begin_grab()
        self.move(canvas, start + QPoint(30, 30))
        self.assertTrue(canvas.interrupt_for_undo())      # e.g. via Blender's keymap guard
        self.assertIsNone(canvas._grab)
        self.assertEqual((log["undo"], log["save"]), (1, 0))
        self.assertTrue(log["discard_redo"])

    def test_interrupt_for_undo_is_false_when_nothing_is_in_progress(self):
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        self.assertFalse(canvas.interrupt_for_undo())
        self.assertEqual(log["undo"], 0)

    def test_interrupt_for_undo_ignores_undo_during_a_held_mouse_drag(self):
        from unittest import mock
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        canvas.dragging_image = True                      # image drag, button held
        with mock.patch.object(QApplication, "mouseButtons",
                               staticmethod(lambda: Qt.LeftButton)):
            self.assertTrue(canvas.interrupt_for_undo())
        self.assertTrue(canvas.dragging_image)            # the drag carries on untouched
        self.assertEqual((log["undo"], log["save"]), (0, 0))

    def test_a_stale_drag_flag_with_no_button_held_never_blocks_undo(self):
        from unittest import mock
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        canvas.dragging_image = True                      # stuck flag, nothing pressed
        with mock.patch.object(QApplication, "mouseButtons",
                               staticmethod(lambda: Qt.NoButton)):
            self.assertFalse(canvas.interrupt_for_undo())

    def test_enter_and_g_confirm_and_unmoved_confirm_is_a_noop(self):
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        start = self.point(canvas, ctl)
        canvas.begin_grab()
        self.move(canvas, start + QPoint(20, 0))
        self.key(canvas, Qt.Key_G)
        self.assertEqual((log["save"], len(log["clicked"])), (1, 1))
        canvas2, ctl2, _, _, _, log2 = self.make()
        self.point(canvas2, ctl2)
        canvas2.begin_grab()
        self.key(canvas2, Qt.Key_Return)
        self.assertEqual((log2["snap"], log2["save"], log2["clicked"], log2["undo"]), (0, 0, [], 0))

    def test_nothing_hovered_or_disabled_control_does_not_grab(self):
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        QCursor.setPos(canvas.mapToGlobal(QPoint(20, 250)))
        self.app.processEvents()
        self.assertFalse(canvas.begin_grab())
        self.point(canvas, off)
        self.assertFalse(canvas.begin_grab())
        self.assertIsNone(canvas._grab)

    def test_overlay_button_grab_does_not_click_it(self):
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        start = self.point(canvas, btn, 10, 10)
        self.assertTrue(canvas.begin_grab())
        self.move(canvas, start + QPoint(50, 0))
        self.assertEqual(log["overlay"][-1][0], "quick1")
        self.click(canvas, start + QPoint(50, 0))
        self.click(btn, start + QPoint(50, 0)) if False else None
        self.assertEqual(btn_clicks, [])
        self.assertEqual(log["save"], 1)

    def test_symmetry_handle_grab(self):
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        canvas.symmetry_x = 200
        QCursor.setPos(canvas.mapToGlobal(QPoint(200, 10)))
        self.app.processEvents()
        self.assertTrue(canvas.begin_grab())
        gp = canvas.mapToGlobal(QPoint(230, 10))
        self.move(canvas, gp)
        self.assertEqual(log["sym"][-1], 230)
        self.assertTrue(canvas.dragging_symmetry)
        self.click(canvas, gp)
        self.assertFalse(canvas.dragging_symmetry)

    def test_symmetry_handle_ignored_when_symmetry_off(self):
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        canvas.symmetry_enabled = False
        QCursor.setPos(canvas.mapToGlobal(QPoint(200, 10)))
        self.app.processEvents()
        self.assertFalse(canvas.begin_grab())

    def test_no_grab_while_a_mouse_drag_is_in_progress(self):
        canvas, ctl, off, btn, btn_clicks, log = self.make()
        self.point(canvas, ctl)
        ctl.dragging = True
        self.assertFalse(canvas.begin_grab())


class WiredIntoWindowTests(unittest.TestCase):
    def test_g_key_calls_begin_grab_without_modifiers(self):
        with open(os.path.join(PKG_DIR, "ui", "main_window.py"), encoding="utf8") as f:
            tree = ast.parse(f.read())
        kp = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "keyPressEvent")
        src = ast.unparse(kp)
        self.assertIn("Qt.Key_G", src)
        self.assertIn("begin_grab()", src)
        self.assertIn("Qt.ControlModifier", src)


if __name__ == "__main__":
    unittest.main()
