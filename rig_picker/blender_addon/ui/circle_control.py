"""
circle_control.py

Simple circular picker control.
"""

from PySide6.QtCore import Qt, QPoint, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QPolygon
from PySide6.QtWidgets import QWidget


class CircleControl(QWidget):

    clicked = Signal(str)
    COLORS = {
        "RED": QColor(220, 70, 70),
        "GREEN": QColor(104, 161, 109),
        "BLUE": QColor(65, 140, 230),
        "YELLOW": QColor(230, 195, 55),
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

    def set_display_scale(self, scale):
        """Resize the hit area and drawing with the background image."""
        self.display_scale = max(0.1, scale)
        height = max(8, round(self.size * self.display_scale))
        width = height
        if self.shape == "RECTANGLE":
            width = max(12, round(height * 1.6))
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

        margin = max(1, round(4 * self.display_scale))
        rect = self.rect().adjusted(margin, margin, -margin, -margin)

        color = QColor(self.color)

        if self.hover:
            color = QColor(140, 200, 145)

        painter.setBrush(QBrush(color))

        painter.setPen(QPen(Qt.white, max(1, round(2 * self.display_scale))))

        if self.shape == "RECTANGLE":
            painter.drawRect(rect)
        elif self.shape == "TRIANGLE":
            painter.drawPolygon(QPolygon([
                rect.center() + QPoint(0, -rect.height() // 2),
                rect.bottomLeft(),
                rect.bottomRight(),
            ]))
        else:
            painter.drawEllipse(rect)

    # -----------------------------------------------------

    def enterEvent(self, event):

        self.hover = True

        self.update()

        super().enterEvent(event)

    # -----------------------------------------------------

    def leaveEvent(self, event):

        self.hover = False

        self.update()

        super().leaveEvent(event)

    # -----------------------------------------------------

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

            parent_pos = parent.mapFromGlobal(
                event.globalPosition().toPoint()
            )

            x = parent_pos.x() - self.drag_offset.x()
            y = parent_pos.y() - self.drag_offset.y()

            # ---------------------------------
            # Clamp inside parent canvas
            # ---------------------------------

            x = max(
                0,
                min(
                    x,
                    parent.width() - self.width()
                )
            )

            y = max(
                0,
                min(
                    y,
                    parent.height() - self.height()
                )
            )

            parent.move_control_from_canvas(self, QPoint(x, y))
            event.accept()
            return

        super().mouseMoveEvent(event)
    
    def mouseReleaseEvent(self, event):

        if self.dragging:

            self.dragging = False

            # If mouse barely moved, treat it as a click.
            self.clicked.emit(self.bone_name)

            event.accept()
            return

        super().mouseReleaseEvent(event)
