"""
circle_control.py

Simple circular picker control.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QBrush
from PySide6.QtWidgets import QWidget


class CircleControl(QWidget):

    clicked = Signal(str)

    def __init__(self, bone_name, radius=10, color=None):

        super().__init__()

        self.bone_name = bone_name

        self.radius = radius

        self.color = color or QColor(60, 200, 80)

        self.setFixedSize(radius * 2 + 4, radius * 2 + 4)

        self.setCursor(Qt.PointingHandCursor)

        self.hover = False

        self.dragging = False

        self.drag_offset = None

    # -----------------------------------------------------

    def paintEvent(self, event):

        painter = QPainter(self)

        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect().adjusted(4, 4, -4, -4)

        color = QColor(self.color)

        if self.hover:
            color = color.lighter(130)

        painter.setBrush(QBrush(color))

        painter.setPen(QPen(Qt.white, 2))

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

            self.move(x, y)
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