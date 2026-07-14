"""
control_list.py

Displays circular picker controls on a free canvas.
"""

from PySide6.QtWidgets import (
    QWidget,
    QScrollArea,
)

from .circle_control import CircleControl
from PySide6.QtGui import QPixmap, QPainter,QImage
from PySide6.QtCore import Qt


class ControlList(QScrollArea):

    def __init__(self):

        super().__init__()

        self.controls = {}

        self.container = PickerCanvas()


        self.setWidgetResizable(True)

        self.setWidget(self.container)
        self.background = None

    # -----------------------------------------------------

    def add_control(self, bone_name, x=None, y=None):

        if bone_name in self.controls:
            return

        control = CircleControl(bone_name, radius=10)

        # Make the canvas the parent
        control.setParent(self.container)

        if x is None or y is None:

            index = len(self.controls)

            x = 30 + (index % 8) * 35
            y = 30 + (index // 8) * 35

        control.move(int(x), int(y))

        self.controls[bone_name] = control

        return control

    # -----------------------------------------------------

    def clear_controls(self):

        for control in self.controls.values():
            control.deleteLater()

        self.controls.clear()

    def set_background(self, image_path):

        image = QImage(image_path)

        if image.isNull():
            return

        width = image.width()
        height = image.height()

        crop_width = int(height * 0.70)

        if crop_width > width:
            crop_width = width

        left = (width - crop_width) // 2

        cropped = image.copy(
            left,
            0,
            crop_width,
            height,
        )

        pixmap = QPixmap.fromImage(cropped)

        self.container.background = pixmap

        self.container.image_x = 0
        self.container.image_y = 0

        self.container.image_offset_x = 0.0
        self.container.image_offset_y = 0.0

        self.container.update()

class PickerCanvas(QWidget):

    def __init__(self):
        super().__init__()

        self.background = None

        # Background image position
        self.image_x = 0
        self.image_y = 0

        # Relative position (0 = left/top, 1 = right/bottom)
        self.image_offset_x = 0.0
        self.image_offset_y = 0.0

        # Dragging state
        self.dragging = False
        self.drag_start = None

    def paintEvent(self, event):

        super().paintEvent(event)

        if self.background is None:
            return

        painter = QPainter(self)

        pixmap = self.background.scaled(
            self.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )

        painter.drawPixmap(
            self.image_x,
            self.image_y,
            pixmap
        )

    def mousePressEvent(self, event):

        if event.button() == Qt.LeftButton:

            child = self.childAt(event.position().toPoint())

            if child is None:
                self.dragging = True
                self.drag_start = event.position().toPoint()

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):

        if self.dragging:

            current = event.position().toPoint()

            delta = current - self.drag_start

            self.image_x += delta.x()
            self.image_y += delta.y()

            self.drag_start = current

            if self.background:

                pixmap = self.background.scaled(
                    self.size(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )

                image_w = pixmap.width()
                image_h = pixmap.height()

                canvas_w = self.width()
                canvas_h = self.height()

                # --------------------
                # Clamp X
                # --------------------

                if image_w >= canvas_w:

                    min_x = canvas_w - image_w
                    max_x = 0

                else:

                    min_x = 0
                    max_x = canvas_w - image_w

                # --------------------
                # Clamp Y
                # --------------------

                if image_h >= canvas_h:

                    min_y = canvas_h - image_h
                    max_y = 0

                else:

                    min_y = 0
                    max_y = canvas_h - image_h

                self.image_x = max(min_x, min(self.image_x, max_x))
                self.image_y = max(min_y, min(self.image_y, max_y))

                # Save relative position
                available_x = max(1, max_x - min_x)
                available_y = max(1, max_y - min_y)

                self.image_offset_x = (self.image_x - min_x) / available_x
                self.image_offset_y = (self.image_y - min_y) / available_y

            self.update()

        super().mouseMoveEvent(event)

    def resizeEvent(self, event):

        super().resizeEvent(event)

        if not self.background:
            return

        pixmap = self.background.scaled(
            self.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )

        image_w = pixmap.width()
        image_h = pixmap.height()

        canvas_w = self.width()
        canvas_h = self.height()

        if image_w >= canvas_w:

            min_x = canvas_w - image_w
            max_x = 0

        else:

            min_x = 0
            max_x = canvas_w - image_w

        if image_h >= canvas_h:

            min_y = canvas_h - image_h
            max_y = 0

        else:

            min_y = 0
            max_y = canvas_h - image_h

        available_x = max(1, max_x - min_x)
        available_y = max(1, max_y - min_y)

        self.image_x = int(
            min_x + self.image_offset_x * available_x
        )

        self.image_y = int(
            min_y + self.image_offset_y * available_y
        )

        self.update()