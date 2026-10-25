"""axis_depths() / overlap_grew() compare how deep two shapes overlap at
two positions. The numbers only mean something if they're measured along
the SAME directions: for a circle against a polygon, SAT's last axis points
at the polygon's nearest vertex, and that direction changes as the circle
moves, so the lists from two positions must not be compared by index.
Pure Python (no Qt)."""
import importlib.util
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import PKG_DIR  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "collision_shapes", os.path.join(PKG_DIR, "ui", "collision_shapes.py"))
shapes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shapes)

TRI = shapes.triangle_shape(100, 100, 60, 60)    # apex (130,100), base (100,160)-(160,160)


def circle(x, y, d=30):
    return shapes.shape_for("CIRCLE", x, y, d, d)


class AxisDepthsTests(unittest.TestCase):
    def test_circle_vertex_axis_changes_between_positions(self):
        before = shapes.separating_axes(circle(80, 140), TRI)
        after = shapes.separating_axes(circle(79, 142), TRI)
        self.assertEqual(before[:-1], after[:-1])        # triangle edge normals don't move
        self.assertNotEqual(before[-1], after[-1])       # nearest-vertex axis does

    def test_growth_is_measured_along_the_start_directions(self):
        start, cand = circle(80, 140), circle(79, 142)
        axes = shapes.separating_axes(start, TRI)
        start_depths = shapes.axis_depths(start, TRI, axes)
        # by position the last entries are different directions (29.14 vs 8.29)
        # and the growth goes unseen ...
        self.assertFalse(shapes.overlap_grew(shapes.axis_depths(cand, TRI), start_depths))
        # ... along the start direction it is 29.14 -> 29.85
        along = shapes.axis_depths(cand, TRI, axes)
        self.assertAlmostEqual(along[-1], 29.85, places=2)
        self.assertTrue(shapes.overlap_grew(along, start_depths))

    def test_moving_out_along_the_start_axes_is_not_growth(self):
        start = circle(80, 140)
        axes = shapes.separating_axes(start, TRI)
        start_depths = shapes.axis_depths(start, TRI, axes)
        # sliding down-left: shallower or equal on every axis
        for x, y in ((78, 142), (76, 144), (74, 146)):
            cand = circle(x, y)
            self.assertTrue(shapes.overlaps(cand, TRI), (x, y))
            self.assertFalse(
                shapes.overlap_grew(shapes.axis_depths(cand, TRI, axes), start_depths), (x, y))

    def test_default_axes_match_explicit_axes_for_the_same_position(self):
        c = circle(80, 140)
        self.assertEqual(shapes.axis_depths(c, TRI),
                         shapes.axis_depths(c, TRI, shapes.separating_axes(c, TRI)))

    def test_separated_along_a_stored_axis_is_not_growth(self):
        start = circle(80, 140)
        axes = shapes.separating_axes(start, TRI)
        start_depths = shapes.axis_depths(start, TRI, axes)
        far = circle(0, 0)
        self.assertIsNone(shapes.axis_depths(far, TRI, axes))
        self.assertFalse(shapes.overlap_grew(None, start_depths))

    def test_rectangles_keep_the_same_axes_wherever_they_are(self):
        wall = shapes.rect_shape(130, 110, 80, 80)
        a = shapes.separating_axes(shapes.rect_shape(100, 100, 40, 40), wall)
        b = shapes.separating_axes(shapes.rect_shape(100, 110, 40, 40), wall)
        self.assertEqual(a, b)

    def test_two_circles_are_not_pinned_so_swinging_round_is_not_growth(self):
        """Two circles have one axis, the line between the centres, and the
        overlap along it is r1 + r2 - distance. Pinned to the start
        direction, a circle swinging round the other would read as deeper
        (10 -> 11) even though the centres moved apart (30.0 -> 31.02)."""
        obstacle = circle(130, 100, 40)
        start, cand = circle(100, 100, 40), circle(101, 111, 40)
        self.assertIsNone(shapes.pinned_axes(start, obstacle))
        start_depths = shapes.axis_depths(start, obstacle, None)
        self.assertAlmostEqual(start_depths[0], 10.0)
        self.assertLess(shapes.depth_of(cand, obstacle), shapes.depth_of(start, obstacle))
        self.assertFalse(shapes.overlap_grew(
            shapes.axis_depths(cand, obstacle, None), start_depths))
        # what pinning would have said (the bug this guards against)
        pinned = shapes.separating_axes(start, obstacle)
        self.assertTrue(shapes.overlap_grew(
            shapes.axis_depths(cand, obstacle, pinned), start_depths))

    def test_two_circles_moving_deeper_are_still_growth(self):
        obstacle = circle(130, 100, 40)
        start_depths = shapes.axis_depths(circle(100, 100, 40), obstacle, None)
        for x, y in ((105, 100), (100 + 3, 100 + 2), (110, 90)):
            self.assertTrue(shapes.overlap_grew(
                shapes.axis_depths(circle(x, y, 40), obstacle, None), start_depths), (x, y))

    def test_pairs_with_a_polygon_are_pinned(self):
        for ob in (TRI, shapes.rect_shape(130, 110, 80, 80)):
            c = circle(100, 130)
            self.assertEqual(shapes.pinned_axes(c, ob), shapes.separating_axes(c, ob))
        self.assertIsNotNone(shapes.pinned_axes(
            shapes.rect_shape(0, 0, 10, 10), shapes.rect_shape(5, 5, 10, 10)))

    def test_circle_centred_on_a_vertex_has_no_vertex_axis_and_does_not_raise(self):
        c = shapes.circle_shape(130, 100, 15)            # exactly on the triangle's apex
        axes = shapes.pinned_axes(c, TRI)
        self.assertEqual(len(axes), 3)                   # edge normals only
        depths = shapes.axis_depths(c, TRI, axes)
        self.assertFalse(shapes.overlap_grew(depths, depths))


if __name__ == "__main__":
    unittest.main()
