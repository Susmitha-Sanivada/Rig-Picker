"""Quick Action buttons/toggles can't be dragged onto picker controls or onto
each other, and controls can't be dragged onto buttons. Real Qt (offscreen);
the canvas methods come from control_list.py's source on a stand-in canvas."""
import ast
import math
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

METHODS = ("_other_picker_controls", "resolve_control_collision",
           "_overlay_obstacle_rects", "_rects_overlap",
           "_resolve_overlay_collision")


@unittest.skipIf(QWidget is None, "needs PySide6")
class OverlayCollisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        with open(os.path.join(PKG_DIR, "ui", "control_list.py"), encoding="utf8") as f:
            tree = ast.parse(f.read())
        canvas = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PickerCanvas")
        names = {"_other_picker_controls", "resolve_control_collision",
                 "_overlay_obstacle_rects", "_overlay_obstacle_shapes",
                 "_collision_rect", "_collision_shape",
                 "_make_group_grab", "_group_members_at", "_group_blocked", "_group_grab_move",
                 "_rects_overlap", "_resolve_overlay_collision", "mirror_collides", "_sweep_to"}
        fns = [n for n in canvas.body if isinstance(n, ast.FunctionDef) and n.name in names]
        assert {f.name for f in fns} == names
        for f in fns:
            f.decorator_list = [d for d in f.decorator_list
                                if not (isinstance(d, ast.Name) and d.id == "safe_override")]
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "collision_shapes", os.path.join(PKG_DIR, "ui", "collision_shapes.py"))
        shapes = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shapes)
        cls.shapes = shapes
        # COLLISION_INSET = 0: these tests assert on full widget rectangles
        # (no shrunk collision boxes); the inset itself is covered separately.
        from PySide6.QtCore import QPoint
        ns = {"QRect": QRect, "QPoint": QPoint, "CircleControl": QWidget, "shapes": shapes,
              "COLLISION_INSET": 0}
        mod = ast.Module(body=fns, type_ignores=[])
        ast.fix_missing_locations(mod)
        exec(compile(mod, "control_list.py", "exec"), ns)
        cls.fns = {f.name: ns[f.name] for f in fns}
        cls.fns["_rects_overlap"] = staticmethod(cls.fns["_rects_overlap"])
        cls.fns["_collision_rect"] = staticmethod(cls.fns["_collision_rect"])
        cls.fns["_sweep_to"] = staticmethod(cls.fns["_sweep_to"])

    def canvas(self):
        fns = self.fns

        class Ctl(QWidget):
            pass

        class Canvas(QWidget):
            control_list = None

            def __init__(s):
                super().__init__()
                s.resize(600, 400)
                s.controls = []
                s.OVERLAY_DRAG_GROUPS = {}

            def findChildren(s, *a, **k):
                return s.controls

            def move_control_from_canvas(s, control, pos):
                control.move(pos)

        for n, f in fns.items():
            setattr(Canvas, n, f)
        c = Canvas()
        c.mirror = None            # (partner widget, dx) when a test wants symmetry
        c._mirror_rect_for = lambda control, x, y: (
            None if c.mirror is None else
            (c.mirror[0], QRect(c.mirror[1], y, c.mirror[0].width(), c.mirror[0].height())))
        self.addCleanup(c.deleteLater)
        return c, Ctl

    def ctl(self, canvas, Ctl, x, y, size=20):
        w = Ctl(canvas)
        w.setGeometry(x, y, size, size)
        w.show()
        canvas.controls.append(w)
        return w

    def btn(self, canvas, key, x, y, w=80, h=30):
        b = QPushButton("b", canvas)
        b.setGeometry(x, y, w, h)
        b.show()
        canvas.OVERLAY_DRAG_GROUPS[key] = [b]
        return b

    def drag_group(self, canvas, key, dx, dy):
        widgets = canvas.OVERLAY_DRAG_GROUPS[key]
        clamp = lambda a, b: (a, b)
        dx, dy = canvas._sweep_to(
            (0, 0), (dx, dy),
            lambda x, y, hold: canvas._resolve_overlay_collision(key, widgets, x, y, clamp, hold))
        for w in widgets:
            w.move(w.x() + dx, w.y() + dy)

    def drag_control(self, canvas, ctl, tx, ty):
        x, y = canvas._sweep_to(
            (ctl.x(), ctl.y()), (tx, ty),
            lambda cx, cy, hold: canvas.resolve_control_collision(ctl, cx, cy, hold))
        ctl.move(x, y)

    def test_button_stops_at_a_control_from_every_angle(self):
        for deg in range(0, 360, 30):
            canvas, Ctl = self.canvas()
            ctl = self.ctl(canvas, Ctl, 300, 200)
            b = self.btn(canvas, "q", 300 + round(250 * math.cos(math.radians(deg))) - 40,
                         200 + round(150 * math.sin(math.radians(deg))) - 15)
            ang = math.radians(deg + 180)                   # drag toward the control
            for _ in range(80):
                self.drag_group(canvas, "q", round(5 * math.cos(ang)), round(5 * math.sin(ang)))
                self.assertFalse(b.geometry().intersects(ctl.geometry()), deg)

    def test_button_never_overlaps_control_when_dragged_straight_at_it(self):
        canvas, Ctl = self.canvas()
        ctl = self.ctl(canvas, Ctl, 300, 100)
        b = self.btn(canvas, "q", 100, 95)
        for _ in range(60):
            self.drag_group(canvas, "q", 8, 0)
            self.assertFalse(b.geometry().intersects(ctl.geometry()))
        self.assertGreater(b.x(), 100)                      # it did move, then stopped

    def test_button_vs_button(self):
        canvas, Ctl = self.canvas()
        a = self.btn(canvas, "a", 300, 100)
        b = self.btn(canvas, "b", 50, 100)
        for _ in range(80):
            self.drag_group(canvas, "b", 7, 1)
            self.assertFalse(a.geometry().intersects(b.geometry()))
        self.assertGreater(b.x(), 50)

    def test_fast_jump_cannot_pass_through_or_land_on_a_button(self):
        canvas, Ctl = self.canvas()
        b = self.btn(canvas, "q", 300, 100, 80, 30)
        ctl = self.ctl(canvas, Ctl, 100, 105)
        self.drag_control(canvas, ctl, 320, 105)           # target ON the button
        self.assertFalse(ctl.geometry().intersects(b.geometry()))
        self.assertLess(ctl.x(), b.x())                    # stayed on its own side
        self.drag_control(canvas, ctl, 500, 105)           # free spot beyond it: lands there
        self.assertEqual((ctl.x(), ctl.y()), (500, 105))
        self.assertFalse(ctl.geometry().intersects(b.geometry()))
        canvas2, Ctl2 = self.canvas()
        ctl2 = self.ctl(canvas2, Ctl2, 100, 105)
        b2 = self.btn(canvas2, "q", 300, 100, 80, 30)
        self.drag_group(canvas2, "q", -250, 0)             # same for a button vs a control
        self.assertFalse(b2.geometry().intersects(ctl2.geometry()))
        self.assertGreater(b2.x(), ctl2.x())

    def test_control_and_button_block_at_the_same_distance_from_every_angle(self):
        gaps = set()
        for deg in range(0, 360, 45):
            canvas, Ctl = self.canvas()
            ctl = self.ctl(canvas, Ctl, 300, 200)
            b = self.btn(canvas, "q", 0, 0)
            ang = math.radians(deg)
            b.move(300 + round(200 * math.cos(ang)) - 40, 200 + round(120 * math.sin(ang)) - 15)
            self.drag_group(canvas, "q", -round(200 * math.cos(ang)), -round(120 * math.sin(ang)))
            self.assertFalse(b.geometry().intersects(ctl.geometry()), deg)
            gaps.add(deg)
        self.assertEqual(len(gaps), 8)

    def test_hidden_buttons_do_not_block(self):
        canvas, Ctl = self.canvas()
        a = self.btn(canvas, "a", 300, 100)
        a.hide()
        b = self.btn(canvas, "b", 50, 100)
        self.drag_group(canvas, "b", 280, 0)
        self.assertEqual(b.x(), 330)

    def test_control_stops_at_a_button(self):
        canvas, Ctl = self.canvas()
        b = self.btn(canvas, "q", 300, 100)
        ctl = self.ctl(canvas, Ctl, 100, 105)
        for _ in range(60):
            self.drag_control(canvas, ctl, ctl.x() + 8, ctl.y())
            self.assertFalse(ctl.geometry().intersects(b.geometry()))
        self.assertGreater(ctl.x(), 100)

    def test_control_boxed_in_between_two_buttons_holds_still(self):
        canvas, Ctl = self.canvas()
        self.btn(canvas, "a", 100, 100, 80, 30)
        self.btn(canvas, "b", 100, 134, 80, 30)           # 4px gap, control is 20px
        ctl = self.ctl(canvas, Ctl, 300, 110)
        x, y = canvas.resolve_control_collision(ctl, 120, 112)
        self.assertEqual((x, y), (300, 110))

    def test_mirror_partner_landing_on_a_button_blocks_the_drag(self):
        canvas, Ctl = self.canvas()
        self.btn(canvas, "q", 400, 100)
        partner = self.ctl(canvas, Ctl, 500, 300)
        canvas.mirror = (partner, 410)                    # partner would land on the button
        self.assertTrue(canvas.mirror_collides(None, 150, 100))
        canvas.mirror = (partner, 520)                    # clear of it
        self.assertFalse(canvas.mirror_collides(None, 150, 100))

    def test_control_vs_control_unchanged(self):
        canvas, Ctl = self.canvas()
        other = self.ctl(canvas, Ctl, 200, 100)
        ctl = self.ctl(canvas, Ctl, 100, 100)
        x, y = canvas.resolve_control_collision(ctl, 195, 100)
        ctl.move(x, y)
        self.assertFalse(ctl.geometry().intersects(other.geometry()))

    # --- canvas bounds are resolved together with collisions ----------

    def in_bounds(self, canvas, ctl):
        g = ctl.geometry()
        return (g.left() >= 0 and g.top() >= 0
                and g.right() < canvas.width() and g.bottom() < canvas.height())

    def test_escape_push_never_leaves_the_canvas_left_edge(self):
        # Obstacle just right of a control hugging the left edge: the old
        # push-left escape landed at a negative x.
        canvas, Ctl = self.canvas()
        self.ctl(canvas, Ctl, 12, 100)
        ctl = self.ctl(canvas, Ctl, 300, 300)
        x, y = canvas.resolve_control_collision(ctl, 5, 105)
        ctl.move(x, y)
        self.assertTrue(self.in_bounds(canvas, ctl), (x, y))
        self.assertFalse(ctl.geometry().intersects(canvas.controls[0].geometry()))

    def test_every_edge_stays_in_bounds_after_collision(self):
        W, H = 600, 400
        for ox, oy, tx, ty in [
            (12, 100, 5, 105),                 # left edge
            (W - 32, 100, W - 25, 105),        # right edge
            (100, 12, 105, 5),                 # top edge
            (100, H - 32, 105, H - 25),        # bottom edge
            (12, 12, 5, 5),                    # corner
            (W - 32, H - 32, W - 25, H - 25),  # opposite corner
        ]:
            canvas, Ctl = self.canvas()
            obstacle = self.ctl(canvas, Ctl, ox, oy)
            ctl = self.ctl(canvas, Ctl, 300, 200)
            x, y = canvas.resolve_control_collision(ctl, tx, ty)
            ctl.move(x, y)
            self.assertTrue(self.in_bounds(canvas, ctl), (ox, oy, x, y))
            self.assertFalse(ctl.geometry().intersects(obstacle.geometry()), (ox, oy, x, y))

    def test_out_of_bounds_candidate_is_clamped_even_without_obstacles(self):
        canvas, Ctl = self.canvas()
        ctl = self.ctl(canvas, Ctl, 300, 200)
        self.assertEqual(canvas.resolve_control_collision(ctl, -9, 450), (0, 380))
        self.assertEqual(canvas.resolve_control_collision(ctl, 999, -3), (580, 0))

    def test_no_valid_in_bounds_position_keeps_last_valid_position(self):
        # Canvas is 600x400; two obstacles wall the control into the
        # top-left corner so every in-bounds spot near it is taken.
        canvas, Ctl = self.canvas()
        self.btn(canvas, "a", 0, 20, 600, 380)    # everything below y=20
        self.btn(canvas, "b", 20, 0, 580, 20)     # everything right of x=20, top strip
        ctl = self.ctl(canvas, Ctl, 0, 0)         # last valid spot: the 20x20 corner
        x, y = canvas.resolve_control_collision(ctl, 8, 8, (0, 0))
        self.assertEqual((x, y), (0, 0))
        # And with hold omitted, the control's own position is kept.
        self.assertEqual(canvas.resolve_control_collision(ctl, 8, 8), (0, 0))

    def test_random_drags_always_end_in_bounds_and_clear(self):
        import random
        rng = random.Random(1234)
        for _ in range(300):
            canvas, Ctl = self.canvas()
            others = [self.ctl(canvas, Ctl, rng.randint(0, 580), rng.randint(0, 380))
                      for _ in range(rng.randint(1, 5))]
            # Start somewhere valid and clear.
            ctl = None
            for _try in range(50):
                sx, sy = rng.randint(0, 580), rng.randint(0, 380)
                probe = QRect(sx, sy, 20, 20)
                if not any(probe.intersects(o.geometry()) for o in others):
                    ctl = self.ctl(canvas, Ctl, sx, sy)
                    break
            if ctl is None:
                continue
            for _step in range(10):
                before = ctl.geometry()
                self.drag_control(canvas, ctl, rng.randint(-60, 660), rng.randint(-60, 460))
                self.assertTrue(self.in_bounds(canvas, ctl), ctl.geometry())
                self.assertFalse(any(ctl.geometry().intersects(o.geometry()) for o in others))

    # --- escaping a pre-existing overlap ------------------------------

    def test_button_starting_on_a_control_can_be_dragged_away(self):
        canvas, Ctl = self.canvas()
        ctl = self.ctl(canvas, Ctl, 300, 100)
        b = self.btn(canvas, "q", 280, 95)                  # overlaps ctl at the start
        self.assertTrue(b.geometry().intersects(ctl.geometry()))
        self.drag_group(canvas, "q", -200, 0)
        self.assertFalse(b.geometry().intersects(ctl.geometry()))
        # Followed the drag exactly. Before the fix the still-overlapping
        # control was treated as a blocker, so the button was shoved sideways
        # (y jumped ~25px) instead of tracking the pointer.
        self.assertEqual((b.x(), b.y()), (80, 95))

    def test_overlapped_button_escapes_in_every_direction(self):
        for deg in range(0, 360, 45):
            canvas, Ctl = self.canvas()
            ctl = self.ctl(canvas, Ctl, 300, 200)
            b = self.btn(canvas, "q", 290, 195)
            self.assertTrue(b.geometry().intersects(ctl.geometry()))
            dx = round(150 * math.cos(math.radians(deg)))
            dy = round(100 * math.sin(math.radians(deg)))
            self.drag_group(canvas, "q", dx, dy)
            self.assertFalse(b.geometry().intersects(ctl.geometry()), deg)
            self.assertEqual((b.x(), b.y()), (290 + dx, 195 + dy), deg)

    def test_escaping_one_overlap_cannot_create_a_new_one(self):
        canvas, Ctl = self.canvas()
        self.ctl(canvas, Ctl, 300, 100)                     # the one it starts on
        other = self.ctl(canvas, Ctl, 100, 100)             # in the way of the escape
        b = self.btn(canvas, "q", 280, 95)
        self.drag_group(canvas, "q", -400, 0)
        self.assertFalse(b.geometry().intersects(other.geometry()))
        self.assertLess(b.x(), 280)                         # it did leave the first one

    def test_cannot_re_enter_an_obstacle_after_escaping_it(self):
        canvas, Ctl = self.canvas()
        ctl = self.ctl(canvas, Ctl, 300, 100)
        b = self.btn(canvas, "q", 280, 95)
        self.drag_group(canvas, "q", -200, 0)               # clear of ctl now
        self.assertFalse(b.geometry().intersects(ctl.geometry()))
        for _ in range(40):                                  # ...and now it is a normal obstacle again
            self.drag_group(canvas, "q", 8, 0)
            self.assertFalse(b.geometry().intersects(ctl.geometry()))

    def test_untouched_widget_in_a_group_is_still_blocked_by_that_obstacle(self):
        # A toggle-like group: w1 starts on the control, w2 doesn't.
        # Dragging left lets w1 leave, but w2 must not land on it.
        canvas, Ctl = self.canvas()
        ctl = self.ctl(canvas, Ctl, 100, 100)
        w1 = QPushButton("s", canvas); w1.setGeometry(90, 100, 30, 30); w1.show()
        w2 = QPushButton("l", canvas); w2.setGeometry(130, 100, 30, 30); w2.show()
        canvas.OVERLAY_DRAG_GROUPS["t"] = [w1, w2]
        self.assertTrue(w1.geometry().intersects(ctl.geometry()))
        self.assertFalse(w2.geometry().intersects(ctl.geometry()))
        self.drag_group(canvas, "t", -40, 0)
        self.assertFalse(w2.geometry().intersects(ctl.geometry()))

    # --- an existing overlap may shrink but never grow ----------------

    @staticmethod
    def inter(a, b):
        """(width, height) of the overlap of two geometries, or None."""
        if not a.intersects(b):
            return None
        r = a.intersected(b)
        return r.width(), r.height()

    def test_dragging_toward_the_obstacle_cannot_go_deeper(self):
        # Button's right 10px sit on the control's left edge; it is dragged
        # toward the control's centre one pixel at a time.
        canvas, Ctl = self.canvas()
        ctl = self.ctl(canvas, Ctl, 300, 100)
        b = self.btn(canvas, "q", 230, 100, 80, 30)
        self.assertEqual(self.inter(b.geometry(), ctl.geometry()), (10, 20))
        for _ in range(40):
            self.drag_group(canvas, "q", 1, 0)
            self.assertEqual(b.x(), 230)                    # pinned at its starting depth
            self.assertEqual(self.inter(b.geometry(), ctl.geometry()), (10, 20))
        self.drag_group(canvas, "q", 60, 0)                 # one big jump is stopped too
        self.assertEqual(b.x(), 230)

    def test_dragging_away_in_small_steps_follows_the_pointer_then_blocks_re_entry(self):
        canvas, Ctl = self.canvas()
        ctl = self.ctl(canvas, Ctl, 300, 100)
        b = self.btn(canvas, "q", 230, 100, 80, 30)
        for n in range(1, 21):
            self.drag_group(canvas, "q", -1, 0)
            self.assertEqual(b.x(), 230 - n)                # exactly tracks the drag
            inter = self.inter(b.geometry(), ctl.geometry())
            self.assertTrue(inter is None or inter[0] == 10 - n)   # overlap shrinks 1px/step
        self.assertIsNone(self.inter(b.geometry(), ctl.geometry()))   # clear at x == 210
        for _ in range(40):                                 # now an ordinary obstacle
            self.drag_group(canvas, "q", 1, 0)
            self.assertIsNone(self.inter(b.geometry(), ctl.geometry()))

    def test_after_backing_out_part_way_it_cannot_return_to_the_old_depth(self):
        canvas, Ctl = self.canvas()
        ctl = self.ctl(canvas, Ctl, 300, 100)
        b = self.btn(canvas, "q", 230, 100, 80, 30)
        self.drag_group(canvas, "q", -5, 0)                 # overlap 10 -> 5
        for _ in range(10):
            self.drag_group(canvas, "q", 1, 0)
        self.assertEqual(b.x(), 225)
        self.assertEqual(self.inter(b.geometry(), ctl.geometry())[0], 5)

    def test_sliding_along_the_obstacle_edge_is_allowed(self):
        canvas, Ctl = self.canvas()
        ctl = self.ctl(canvas, Ctl, 300, 100)
        b = self.btn(canvas, "q", 230, 100, 80, 30)
        for n in range(1, 10):                              # pushes toward AND down
            self.drag_group(canvas, "q", 3, 2)
            self.assertEqual(b.x(), 230)                    # x pinned...
            self.assertEqual(b.y(), 100 + 2 * n)            # ...y slides freely
            self.assertTrue(b.geometry().intersects(ctl.geometry()))
        # Once it has slid fully clear (y >= 120) the control is an ordinary
        # obstacle again: x is free until it would touch the control.
        self.drag_group(canvas, "q", 3, 2)
        self.assertEqual((b.x(), b.y()), (233, 120))
        self.assertFalse(b.geometry().intersects(ctl.geometry()))

    def test_corner_overlap_only_lets_either_axis_shrink(self):
        canvas, Ctl = self.canvas()
        ctl = self.ctl(canvas, Ctl, 300, 100)
        b = self.btn(canvas, "q", 290, 90, 20, 20)          # 10x10 on the control's corner
        self.assertEqual(self.inter(b.geometry(), ctl.geometry()), (10, 10))
        for _ in range(20):
            self.drag_group(canvas, "q", 2, 2)              # deeper on both axes
        self.assertEqual((b.x(), b.y()), (290, 90))
        for _ in range(8):
            self.drag_group(canvas, "q", 2, -2)             # y backs out; x is pinned meanwhile
            if b.geometry().intersects(ctl.geometry()):
                self.assertEqual(b.x(), 290)
        self.assertFalse(b.geometry().intersects(ctl.geometry()))   # got clear

    def test_group_member_overlap_cannot_deepen_either(self):
        canvas, Ctl = self.canvas()
        ctl = self.ctl(canvas, Ctl, 300, 100)
        w1 = QPushButton("s", canvas); w1.setGeometry(270, 100, 40, 30); w1.show()   # 10px onto ctl
        w2 = QPushButton("l", canvas); w2.setGeometry(210, 100, 50, 30); w2.show()
        canvas.OVERLAY_DRAG_GROUPS["t"] = [w1, w2]
        for _ in range(30):
            self.drag_group(canvas, "t", 1, 0)
        self.assertEqual((w1.x(), w2.x()), (270, 210))
        self.assertEqual(self.inter(w1.geometry(), ctl.geometry())[0], 10)

    def test_random_drags_never_increase_an_existing_overlap(self):
        import random
        rng = random.Random(99)
        runs = 0
        for _ in range(200):
            canvas, Ctl = self.canvas()
            ctl = self.ctl(canvas, Ctl, 300, 200)
            third = self.ctl(canvas, Ctl, rng.randint(150, 450), rng.randint(100, 300))
            b = self.btn(canvas, "q", rng.randint(255, 320), rng.randint(170, 220),
                         rng.choice((20, 40, 80)), rng.choice((20, 30)))
            if not b.geometry().intersects(ctl.geometry()) or b.geometry().intersects(third.geometry()):
                continue
            runs += 1
            for _step in range(60):
                before = self.inter(b.geometry(), ctl.geometry())
                self.drag_group(canvas, "q", rng.randint(-6, 6), rng.randint(-6, 6))
                after = self.inter(b.geometry(), ctl.geometry())
                if before is None:
                    self.assertIsNone(after)                # clear stays clear
                elif after is not None:
                    self.assertLessEqual(after[0], before[0])
                    self.assertLessEqual(after[1], before[1])
                self.assertFalse(b.geometry().intersects(third.geometry()))   # no new overlaps
        self.assertGreater(runs, 30)


    # ---- shape-accurate collision (circle / triangle / square) ----------

    def shaped(self, canvas, Ctl, kind, x, y, size=20):
        w = self.ctl(canvas, Ctl, x, y, size)
        w.shape = kind
        return w

    def test_circles_can_sit_corner_to_corner_inside_each_others_boxes(self):
        canvas, Ctl = self.canvas()
        a = self.shaped(canvas, Ctl, "CIRCLE", 100, 100)
        b = self.shaped(canvas, Ctl, "CIRCLE", 200, 200)
        # b is dragged so its BOX overlaps a's box at the corner, but the
        # circles themselves don't touch: that must be allowed.
        x, y = canvas.resolve_control_collision(b, 117, 117)
        self.assertEqual((x, y), (117, 117))
        # ...yet really overlapping circles are pushed apart.
        x, y = canvas.resolve_control_collision(b, 105, 105)
        self.assertNotEqual((x, y), (105, 105))
        sh = self.shapes
        self.assertFalse(sh.overlaps(sh.shape_for("CIRCLE", x, y, 20, 20),
                                     sh.shape_for("CIRCLE", 100, 100, 20, 20)))

    def test_square_next_to_triangle_can_use_its_empty_corner(self):
        canvas, Ctl = self.canvas()
        self.shaped(canvas, Ctl, "TRIANGLE", 100, 100, 40)
        sq = self.shaped(canvas, Ctl, "SQUARE", 300, 300, 10)
        # top-left box corner of the triangle is empty space
        x, y = canvas.resolve_control_collision(sq, 100, 100)
        self.assertEqual((x, y), (100, 100))

    def test_shaped_drag_never_ends_overlapping(self):
        import random
        rng = random.Random(7)
        sh = self.shapes
        kinds = ("CIRCLE", "TRIANGLE", "SQUARE", "RECTANGLE")
        for _ in range(150):
            canvas, Ctl = self.canvas()
            others = [self.shaped(canvas, Ctl, rng.choice(kinds),
                                  rng.randint(50, 500), rng.randint(50, 300),
                                  rng.choice((14, 20, 30))) for _ in range(3)]
            for o in others:       # keep the starting layout overlap-free
                o_s = sh.shape_for(o.shape, o.x(), o.y(), o.width(), o.height())
                if any(sh.overlaps(o_s, sh.shape_for(p.shape, p.x(), p.y(), p.width(), p.height()))
                       for p in others if p is not o):
                    break
            else:
                mover = self.shaped(canvas, Ctl, rng.choice(kinds), 5, 5, 20)
                tx, ty = rng.randint(0, 560), rng.randint(0, 360)
                x, y = canvas._sweep_to(
                    (mover.x(), mover.y()), (tx, ty),
                    lambda cx, cy, hold: canvas.resolve_control_collision(mover, cx, cy, hold))
                mover.move(x, y)
                m_s = sh.shape_for(mover.shape, x, y, 20, 20)
                for o in others:
                    o_s = sh.shape_for(o.shape, o.x(), o.y(), o.width(), o.height())
                    self.assertFalse(sh.overlaps(m_s, o_s))

    def test_button_can_tuck_into_circles_empty_corner(self):
        canvas, Ctl = self.canvas()
        self.shaped(canvas, Ctl, "CIRCLE", 300, 200, 40)
        b = self.btn(canvas, "q", 100, 100, 20, 20)
        # Box-vs-box this would collide; against the real circle it doesn't.
        # button's bottom-right corner lands at (304, 204): inside the
        # circle's bounding box, but 22.6px from its centre (radius 20).
        self.drag_group(canvas, "q", 284 - 100, 184 - 100)
        self.assertEqual((b.x(), b.y()), (284, 184))


    def test_button_can_be_dragged_past_a_full_row_of_controls(self):
        canvas, Ctl = self.canvas()
        for i in range(0, 600, 24):                        # gaps (6px) narrower than the button
            self.ctl(canvas, Ctl, i, 200)
        b = self.btn(canvas, "q", 300, 320, 80, 30)
        for pointer_y in range(320, 10, -10):              # pointer moves up past the row;
            self.drag_group(canvas, "q", 0, pointer_y - b.y())   # target = pointer, not b + 10
        self.assertLess(b.y(), 200)
        for c in canvas.findChildren(Ctl):
            self.assertFalse(b.geometry().intersects(c.geometry()))


    # ---- G grab of several selected controls moving as one --------------

    def grab_group(self, canvas, members, dx, dy, steps=1):
        for i, m in enumerate(members):
            m.shape = "CIRCLE"
            m.bone_name = f"bone{i}"
        grab = canvas._make_group_grab(members, members[0], QPoint(0, 0))
        for i in range(1, steps + 1):
            canvas._group_grab_move(grab, round(dx * i / steps), round(dy * i / steps))
        return grab

    def test_group_grab_moves_every_selected_control_by_the_same_delta(self):
        canvas, Ctl = self.canvas()
        ms = [self.ctl(canvas, Ctl, 100, 100), self.ctl(canvas, Ctl, 160, 130),
              self.ctl(canvas, Ctl, 220, 100)]
        before = [(m.x(), m.y()) for m in ms]
        self.grab_group(canvas, ms, 70, 50, steps=10)
        for m, (x, y) in zip(ms, before):
            self.assertEqual((m.x(), m.y()), (x + 70, y + 50))

    def test_group_grab_stops_at_unselected_control_and_stays_clear(self):
        canvas, Ctl = self.canvas()
        ms = [self.ctl(canvas, Ctl, 100, 100), self.ctl(canvas, Ctl, 100, 140)]
        wall = self.ctl(canvas, Ctl, 300, 110, 20)
        self.grab_group(canvas, ms, 205, 0, steps=40)      # pointer ends ON the wall
        for m in ms:
            self.assertFalse(m.geometry().intersects(wall.geometry()))
        self.assertGreater(ms[0].x(), 100)                 # it did move
        self.assertLessEqual(ms[0].x(), 280)               # ...and stopped at the wall

    def test_group_grab_keeps_every_member_inside_the_canvas(self):
        canvas, Ctl = self.canvas()
        ms = [self.ctl(canvas, Ctl, 100, 100), self.ctl(canvas, Ctl, 300, 300)]
        self.grab_group(canvas, ms, 900, 900, steps=30)
        for m in ms:
            self.assertTrue(canvas.rect().contains(m.geometry()))

    def test_group_grab_can_start_on_an_obstacle_and_leave(self):
        canvas, Ctl = self.canvas()
        ms = [self.ctl(canvas, Ctl, 100, 100), self.ctl(canvas, Ctl, 200, 100)]
        self.ctl(canvas, Ctl, 105, 105)                    # an obstacle under the first member
        self.grab_group(canvas, ms, 0, 150, steps=15)
        self.assertEqual((ms[0].y(), ms[1].y()), (250, 250))

    def test_group_grab_cannot_move_deeper_into_an_existing_overlap(self):
        canvas, Ctl = self.canvas()
        ms = [self.ctl(canvas, Ctl, 100, 100), self.ctl(canvas, Ctl, 100, 160)]
        wall = self.ctl(canvas, Ctl, 112, 100, 80)         # both members start overlapping it
        for i, m in enumerate(ms):
            m.shape = "CIRCLE"
            m.bone_name = f"bone{i}"
        grab = canvas._make_group_grab(ms, ms[0], QPoint(0, 0))

        def drag_to(dx, steps=20):
            start = grab["delta"][0]
            for i in range(1, steps + 1):
                canvas._group_grab_move(grab, start + round((dx - start) * i / steps), 0)

        drag_to(40)                                        # toward it: nothing moves
        self.assertEqual([(m.x(), m.y()) for m in ms], [(100, 100), (100, 160)])
        drag_to(-30)                                       # away from it: follows
        self.assertEqual([(m.x(), m.y()) for m in ms], [(70, 100), (70, 160)])
        drag_to(-5)                                        # back in: cleared, so it blocks again
        self.assertEqual([(m.x(), m.y()) for m in ms], [(92, 100), (92, 160)])   # at its edge
        for m in ms:
            self.assertFalse(m.geometry().intersects(wall.geometry()))

    def test_group_grab_cannot_deepen_one_dimension_of_an_overlap(self):
        canvas, Ctl = self.canvas()
        a = self.ctl(canvas, Ctl, 100, 100, 40)            # 10px across, 30px along the wall
        b = self.ctl(canvas, Ctl, 400, 20, 18)
        wall = self.ctl(canvas, Ctl, 130, 110, 80)
        for i, m in enumerate((a, b)):
            m.shape = "SQUARE"
            m.bone_name = f"bone{i}"
        grab = canvas._make_group_grab([a, b], a, QPoint(0, 0))
        for i in range(1, 11):                             # drag along the wall, deeper
            canvas._group_grab_move(grab, 0, i)
        self.assertEqual((a.x(), a.y()), (100, 100))
        for i in range(1, 11):                             # ...and the way that shrinks it
            canvas._group_grab_move(grab, 0, -i)
        self.assertEqual((a.x(), a.y()), (100, 90))
        self.assertTrue(a.geometry().intersects(wall.geometry()))   # still overlapping


if __name__ == "__main__":
    unittest.main()
