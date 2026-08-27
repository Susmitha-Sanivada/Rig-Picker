"""
circle_control.py

Professional anti-aliased picker control with solid (flat) colors,
dynamic selection sizing, and crisp vector path borders.
"""

from PySide6.QtCore import Qt, QPoint, Signal, QRectF, QPointF
from PySide6.QtGui import (
    QColor,
    QPainter,
    QPen,
    QBrush,
    QPolygonF,
    QPainterPath,
)
from PySide6.QtWidgets import QWidget


class CircleControl(QWidget):

    clicked = Signal(str, bool, bool)  # bone_name, shift, was_drag

    # Slightly brightened studio palette
    COLORS = {
        "RED": QColor(185, 55, 55),      # Crimson
        "GREEN": QColor(58, 142, 88),    # Emerald Green
        "BLUE": QColor(48, 112, 180),    # Deep Blue
        "YELLOW": QColor(200, 152, 38),  # Muted Gold
    }

    def __init__(self, bone_name, size=18, shape="CIRCLE", color="GREEN"):
        super().__init__()

        self.bone_name = bone_name
        self.size = size
        self.shape = shape
        self.color_name = color
        self.display_scale = 1.0

        self.color = self.COLORS.get(color, self.COLORS["GREEN"])

        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)

        self.hover = False
        self.dragging = False
        self.drag_offset = None
        self._press_pos = None
        self.active = False

    def set_display_scale(self, scale):
        """Resize the hit area and display geometries dynamically.

        Width/height are derived from control_dimensions() - the same
        unscaled (image-space) footprint that the mirroring/re-centering
        math in controller.py and control_list.py uses - and scale is
        applied AFTER that footprint is computed, with a single round().
        Previously this scaled `size` first (round(size * scale)) and
        only then applied the RECTANGLE 1.6x width factor
        (round(height * 1.6)): two roundings in the opposite order from
        control_dimensions(). For CIRCLE/SQUARE/TRIANGLE that happened to
        match (width == height, one rounding either way), but for
        RECTANGLE it made the widget's actual on-screen width diverge by
        a few pixels from the unscaled width the position math assumes -
        so a rectangle control's *rendered* footprint quietly drifted off
        the center that image_position.x()/y() were computed for,
        visible as the control sitting slightly off (typically left of)
        its true, mathematically-centered spot whenever the canvas image
        wasn't shown at 1:1 scale.
        """
        from ..backend import control_dimensions

        self.display_scale = max(0.1, scale)
        unscaled_width, unscaled_height = control_dimensions(self.size, self.shape)
        height = max(10, round(unscaled_height * self.display_scale))
        width = (
            max(14, round(unscaled_width * self.display_scale))
            if self.shape == "RECTANGLE"
            else height
        )
        self.setFixedSize(width, height)

    def set_appearance(self, size=None, shape=None, color=None):
        if size is not None:
            self.size = size
        if shape is not None:
            self.shape = shape
        if color is not None:
            self.color_name = color
            self.color = self.COLORS.get(color, self.COLORS["GREEN"])
        self.set_display_scale(self.display_scale)
        self.update()

    # -----------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        # Base padding reserved for strokes and hover scaling
        base_padding = max(3.0, 4.0 * self.display_scale)

        # Dynamic Selection Sizing: pop out slightly larger when active.
        # This used to be `base_padding * 0.4` - shrinking the padding by
        # 60% of its own value, which at typical display scales added
        # roughly 4-5px to the visible diameter. Adjacent Size dropdown
        # steps (Large-X/Large/Medium/Small/Small-X) only differ by 2px
        # of actual widget diameter, so that pop was over double the
        # size difference itself - selecting a control and changing its
        # size looked identical either way, since the selection
        # highlight's own visual growth swamped the real one. A small,
        # near-constant pixel reduction (not scaled by a percentage of
        # base_padding) keeps the "this is selected" cue visible while
        # staying well under one real size step, so an actual resize
        # stays clearly distinguishable from the highlight itself.
        if self.active:
            padding = max(1.0, base_padding - 0.5)
        else:
            padding = base_padding

        rect = QRectF(self.rect()).adjusted(padding, padding, -padding, -padding)

        base_color = QColor(self.color)

        # -------------------------------
        # Geometry Path Construction
        # -------------------------------
        path = QPainterPath()

        if self.shape == "RECTANGLE":
            path.addRoundedRect(rect, 4.0, 4.0)

        elif self.shape == "SQUARE":
            path.addRoundedRect(rect, 3.0, 3.0)

        elif self.shape == "TRIANGLE":
            top_pt = QPointF(rect.center().x(), rect.top())
            left_pt = QPointF(rect.left(), rect.bottom())
            right_pt = QPointF(rect.right(), rect.bottom())

            path.moveTo(top_pt)
            path.lineTo(left_pt)
            path.lineTo(right_pt)
            path.closeSubpath()  # Closed path for crisp triangle borders

        else:  # CIRCLE
            path.addEllipse(rect)

        # Determine join style: Miter for sharp triangles, Round for soft shapes
        join_style = Qt.MiterJoin if self.shape == "TRIANGLE" else Qt.RoundJoin

        # -------------------------------
        # Selection vs Default Rendering (Solid Colors)
        # -------------------------------
        if self.active:
            # ACTIVE STATE: Solid brighter fill
            fill_color = base_color.lighter(135)

            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(fill_color))
            painter.drawPath(path)

            # Vibrant inner accent ring
            ring_pen = QPen(base_color.lighter(180), max(2.0, 2.5 * self.display_scale))
            ring_pen.setJoinStyle(join_style)
            painter.setPen(ring_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)

            # Solid dark outer boundary line
            outer_pen = QPen(QColor(10, 10, 10, 240), max(1.0, 1.2 * self.display_scale))
            outer_pen.setJoinStyle(join_style)
            painter.setPen(outer_pen)
            painter.drawPath(path)

        else:
            # UNSELECTED STATE: Solid even fill
            if self.dragging:
                fill_color = base_color.darker(130)
            elif self.hover:
                fill_color = base_color.lighter(118)
            else:
                fill_color = base_color

            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(fill_color))
            painter.drawPath(path)

            # Solid dark outline
            border_pen = QPen(QColor(15, 15, 15, 200), max(1.0, 1.2 * self.display_scale))
            border_pen.setJoinStyle(join_style)
            painter.setPen(border_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)

    # -----------------------------------------------------

    def enterEvent(self, event):
        self.hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hover = False
        self.update()
        super().leaveEvent(event)

    # Minimum pixel movement (in local widget coordinates) before a press
    # is treated as a drag rather than a plain click-to-select.
    DRAG_THRESHOLD = 4

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # Don't snapshot or start dragging yet - a press could just be
            # a click-to-select with no movement at all. Recording an undo
            # snapshot here unconditionally (as before) meant every plain
            # select-click overwrote the single-level undo slot with a
            # no-op "before this click" state, so undo would land on
            # whatever was selected right before that click instead of
            # reverting an actual edit. The snapshot is now taken lazily,
            # in mouseMoveEvent, the moment real dragging is detected.
            self.dragging = False
            self._press_pos = event.position().toPoint()
            self.drag_offset = event.position().toPoint()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not self.dragging and self.drag_offset is not None:
            moved = event.position().toPoint() - self._press_pos
            if moved.manhattanLength() < self.DRAG_THRESHOLD:
                return

            # Movement just crossed the threshold - this press is a real
            # drag. Snapshot picker data now, right as the drag begins, so
            # a later undo() reverts to "before this drag" rather than to
            # some in-between position from a later mouseMoveEvent.
            controller = getattr(self.window(), "controller", None)
            if controller is not None:
                controller._record_undo_snapshot()

            self.dragging = True

        if self.dragging:
            parent = self.parent()
            parent_pos = parent.mapFromGlobal(event.globalPosition().toPoint())

            x = parent_pos.x() - self.drag_offset.x()
            y = parent_pos.y() - self.drag_offset.y()

            # Clamp inside parent canvas
            x = max(0, min(x, parent.width() - self.width()))
            y = max(0, min(y, parent.height() - self.height()))

            canvas = self.parent()

            if canvas.symmetry_enabled and canvas.symmetry_x >= 0:
                symmetry_canvas_x = canvas.image_x + canvas.symmetry_x * canvas.image_scale()
                center = x + self.width() / 2

                if abs(center - symmetry_canvas_x) < 8:
                    x = symmetry_canvas_x - self.width() / 2

            # round(), not the implicit truncation QPoint() does on a
            # float: the symmetry-snap branch above can leave x as a
            # float (symmetry_canvas_x - self.width()/2), and QPoint()
            # truncates that toward zero instead of rounding. That
            # truncation happens *before* the canvas position is ever
            # converted to image space, so it doesn't just cost a
            # sub-pixel of display rounding - it bakes up to a full
            # pixel of left/up bias into the stored image_position and
            # item["x"]/["y"] themselves. Everything downstream (resize/
            # reshape's center-preserving math included) then faithfully
            # preserves that already-biased position, which is why a
            # resize after a symmetry snap looked like it drifted left.
            parent.move_control_from_canvas(self, QPoint(round(x), round(y)))
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        # A press on this control started here (drag_offset was set),
        # whether or not it ever crossed the drag threshold. Either way
        # a release means "select this control" - a plain click reports
        # was_drag=False so select_control() knows to take its own
        # snapshot; a real drag reports was_drag=True so select_control()
        # skips snapshotting (mouseMoveEvent already did, before the
        # drag moved anything).
        if self.drag_offset is not None:
            was_drag = self.dragging
            self.dragging = False
            self.drag_offset = None
            shift = bool(event.modifiers() & Qt.ShiftModifier)
            self.clicked.emit(self.bone_name, shift, was_drag)
            event.accept()
            return

        super().mouseReleaseEvent(event)