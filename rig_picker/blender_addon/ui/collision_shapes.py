"""Shape-accurate collision geometry for the picker canvas.

No Qt/Blender imports on purpose: everything here is plain Python so it
can be unit-tested anywhere.

A shape is one of:
    ("circle", cx, cy, r)
    ("poly", [(x, y), ...])          convex polygon (rect / triangle)

Collision uses the separating-axis theorem (SAT). penetration() returns
the SMALLEST push that separates two shapes; overlaps() treats shapes as
colliding only when that push is at least MIN_PENETRATION pixels - the
same "intersect by >= 1px" rule the old rectangle test had (for two
axis-aligned rectangles the two tests are identical).
"""

import math

# Overlaps shallower than this are ignored. Positions end up rounded to
# whole pixels, which can leave up to ~0.7px of residual overlap after a
# push-out, so anything below 1px must not count as a collision.
MIN_PENETRATION = 1.0

_EPS = 1e-9


def rect_shape(x, y, w, h):
    return ("poly", [(x, y), (x + w, y), (x + w, y + h), (x, y + h)])


def circle_shape(cx, cy, r):
    return ("circle", cx, cy, max(0.0, r))


def triangle_shape(x, y, w, h):
    """Upward-pointing isosceles triangle filling the box - the same
    outline CircleControl draws for TRIANGLE."""
    return ("poly", [(x + w / 2.0, y), (x, y + h), (x + w, y + h)])


def shape_for(kind, x, y, w, h):
    """Shape of kind "CIRCLE" / "TRIANGLE" / anything else (rectangle)
    filling the box (x, y, w, h)."""
    if kind == "CIRCLE":
        return circle_shape(x + w / 2.0, y + h / 2.0, min(w, h) / 2.0)
    if kind == "TRIANGLE":
        return triangle_shape(x, y, w, h)
    return rect_shape(x, y, w, h)


def bounds(shape):
    """(left, top, right, bottom) bounding box."""
    if shape[0] == "circle":
        _, cx, cy, r = shape
        return cx - r, cy - r, cx + r, cy + r
    pts = shape[1]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def translate(shape, dx, dy):
    if shape[0] == "circle":
        _, cx, cy, r = shape
        return ("circle", cx + dx, cy + dy, r)
    return ("poly", [(x + dx, y + dy) for x, y in shape[1]])


def _center(shape):
    if shape[0] == "circle":
        return shape[1], shape[2]
    pts = shape[1]
    return (
        sum(p[0] for p in pts) / len(pts),
        sum(p[1] for p in pts) / len(pts),
    )


def _project(shape, ax, ay):
    if shape[0] == "circle":
        _, cx, cy, r = shape
        c = cx * ax + cy * ay
        return c - r, c + r
    dots = [x * ax + y * ay for x, y in shape[1]]
    return min(dots), max(dots)


def _unit(x, y):
    length = math.hypot(x, y)
    if length < _EPS:
        return None
    return x / length, y / length


def _axes(a, b):
    axes = []
    for s in (a, b):
        if s[0] == "poly":
            pts = s[1]
            for i in range(len(pts)):
                x1, y1 = pts[i]
                x2, y2 = pts[(i + 1) % len(pts)]
                n = _unit(-(y2 - y1), x2 - x1)
                if n is not None:
                    axes.append(n)

    circles = [s for s in (a, b) if s[0] == "circle"]
    polys = [s for s in (a, b) if s[0] == "poly"]
    if len(circles) == 2:
        n = _unit(circles[1][1] - circles[0][1], circles[1][2] - circles[0][2])
        axes.append(n if n is not None else (1.0, 0.0))
    elif len(circles) == 1:
        _, cx, cy, _r = circles[0]
        best, best_d = None, None
        for px, py in polys[0][1]:
            d = (px - cx) ** 2 + (py - cy) ** 2
            if best_d is None or d < best_d:
                best, best_d = (px, py), d
        n = _unit(best[0] - cx, best[1] - cy)
        if n is not None:
            axes.append(n)
    return axes


def penetration(a, b):
    """Smallest push that separates `a` from `b`.

    Returns (depth, nx, ny) where (nx, ny) is the unit direction to move
    `a` (away from `b`) by `depth` pixels to clear it, or None if the
    shapes don't overlap at all. Callers decide what counts as a collision
    with MIN_PENETRATION (see overlaps()).
    """
    best_depth, best_n = None, None
    for ax, ay in _axes(a, b):
        a_lo, a_hi = _project(a, ax, ay)
        b_lo, b_hi = _project(b, ax, ay)
        depth = min(a_hi, b_hi) - max(a_lo, b_lo)
        if depth <= 0:
            return None
        if best_depth is None or depth < best_depth:
            best_depth, best_n = depth, (ax, ay)

    if best_n is None:
        return None

    acx, acy = _center(a)
    bcx, bcy = _center(b)
    nx, ny = best_n
    if (acx - bcx) * nx + (acy - bcy) * ny < 0:
        nx, ny = -nx, -ny
    return best_depth, nx, ny


def separating_axes(a, b):
    """The unit axes SAT tests for `a` vs `b`, as (ax, ay) tuples.

    Polygon edge normals depend only on the polygon, but for a circle
    against a polygon the last axis points at the polygon's NEAREST
    vertex, so it changes direction as the circle moves. Don't compare
    results from two positions by list index; capture the axes once and
    pass them to axis_depths(..., axes=...) for both.
    """
    return list(_axes(a, b))


def pinned_axes(a, b):
    """Axes to hold fixed while comparing two positions of the same pair
    (pass the result as `axes=` to axis_depths() for both positions), or
    None to let axis_depths() use each position's own axes.

    Two circles have a single axis, the line between their centers, and
    the overlap along it is r1 + r2 - distance wherever they are: the same
    quantity at every position, so it must follow the axis as it turns.
    Pinning it to its starting direction would report the overlap as
    growing whenever one circle swings round the other, even while they
    move apart. Every pair that involves a polygon is pinned.
    """
    if a[0] == "circle" and b[0] == "circle":
        return None
    return separating_axes(a, b)


def axis_depths(a, b, axes=None):
    """Overlap of `a` and `b` along each axis in `axes` (default: every
    axis SAT tests for this pair right now), in the same order as
    `axes`, or None if some axis separates them.

    penetration()/depth_of() keep only the smallest of these, which is the
    right number for pushing shapes apart but hides growth along the other
    axes: a box overlapping a wall by 10px across and 30px along can slide
    to 40px along with its minimum depth still 10.

    To compare two positions of the same pair, take `axes` from
    pinned_axes() at the first position and pass it for both, so every
    number is measured along the same direction (None, for two circles,
    keeps each position's own center line - see pinned_axes()).
    """
    if axes is None:
        axes = _axes(a, b)
    depths = []
    for ax, ay in axes:
        a_lo, a_hi = _project(a, ax, ay)
        b_lo, b_hi = _project(b, ax, ay)
        depth = min(a_hi, b_hi) - max(a_lo, b_lo)
        if depth <= 0:
            return None
        depths.append(depth)
    return depths


def overlap_grew(depths, start_depths, eps=1e-6):
    """True if any entry of `depths` is deeper than the matching entry of
    `start_depths`. Both must come from axis_depths() with the SAME `axes`
    list. `depths` None means an axis separates the shapes, so the
    overlap is gone rather than deeper."""
    if depths is None:
        return False
    return any(d > s + eps for d, s in zip(depths, start_depths))


def depth_of(a, b):
    """Penetration depth, or 0.0 if they don't overlap."""
    p = penetration(a, b)
    return p[0] if p is not None else 0.0


def overlaps(a, b):
    """True if `a` and `b` overlap by at least MIN_PENETRATION pixels."""
    return depth_of(a, b) >= MIN_PENETRATION
