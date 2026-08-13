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

    clicked = Signal(str, bool)

    # Slightly brightened studio palette
    COLORS = {
        "RED": QColor(185, 55, 55),      # Crimson
        "GREEN": QColor(58, 142, 88),    # Emerald Green
        "BLUE": QColor(48, 112, 180),    # Deep Blue
        "YELLOW": QColor(200, 152, 38),  # Muted Gold
    }

    def __init__(self, bone_name, size=36, shape="CIRCLE", color="GREEN"):
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
        self.active = False

    def set_display_scale(self, scale):
        """Resize the hit area and display geometries dynamically."""
        self.display_scale = max(0.1, scale)
        height = max(10, round(self.size * self.display_scale))
        width = height
        if self.shape == "RECTANGLE":
            width = max(14, round(height * 1.6))
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

        # Dynamic Selection Sizing: pop out slightly larger when active
        if self.active:
            padding = base_padding * 0.4
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

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.dragging = True
            self.drag_offset = event.position().toPoint()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
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

            parent.move_control_from_canvas(self, QPoint(x, y))
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.dragging:
            self.dragging = False
            shift = bool(event.modifiers() & Qt.ShiftModifier)
            self.clicked.emit(self.bone_name, shift)
            event.accept()
            return

        super().mouseReleaseEvent(event)