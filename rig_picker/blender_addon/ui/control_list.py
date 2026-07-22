"""
control_list.py

Displays circular picker controls on a free canvas.
"""

from PySide6.QtWidgets import (
    QWidget,
    QScrollArea,
)
from PySide6.QtGui import (
    QPen,
    QColor,
)
import bpy

from .circle_control import CircleControl
from PySide6.QtGui import QPixmap, QPainter, QImage, QPolygon
from PySide6.QtCore import Qt, QPoint, QRect


class ControlList(QScrollArea):

    def __init__(self):

        super().__init__()

        self.controls = {}

        self.container = PickerCanvas()
        self.container.control_list = self

        self.setWidgetResizable(True)

        self.setWidget(self.container)
        self.background = None

    # -----------------------------------------------------

    def add_control(self, bone_name, x=None, y=None, size=36, shape="CIRCLE", color="GREEN"):

        if bone_name in self.controls:
            return

        control = CircleControl(bone_name, size=size, shape=shape, color=color)

        # Make the canvas the parent
        control.setParent(self.container)

        if x is None or y is None:

            index = len(self.controls)

            x = 30 + (index % 8) * 35
            y = 30 + (index // 8) * 35

        # Positions saved in Blender are in the unscaled background image's
        # coordinate space, not the current canvas's pixel space.
        control.image_position = QPoint(int(x), int(y))

        self.controls[bone_name] = control
        self.container.layout_controls()
        control.show()

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

        self.container.layout_controls()
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
        self.drag_start = None

        self.symmetry_enabled = False

        self.symmetry_x = -1

        self.dragging_symmetry = False

        self.dragging_image = False
        self.dragging_symmetry = False

        self.symmetry_handle_size = 14
        self.symmetry_handle_hover = False

    def scaled_background(self):
        if self.background is None:
            return None

        return self.background.scaled(
            self.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )

    def image_scale(self):
        pixmap = self.scaled_background()
        if pixmap is None or self.background.width() == 0:
            return 1.0

        return pixmap.width() / self.background.width()

    def canvas_to_image_position(self, position):
        """Convert a canvas point to the unscaled background image space."""
        scale = self.image_scale()
        return QPoint(
            round((position.x() - self.image_x) / scale),
            round((position.y() - self.image_y) / scale),
        )

    def layout_controls(self):
        """Apply the current image transform to every picker control."""
        scale = self.image_scale()

        for control in self.findChildren(CircleControl):
            if not hasattr(control, "image_position"):
                control.image_position = self.canvas_to_image_position(control.pos())

            control.set_display_scale(scale)
            control.move(
                round(self.image_x + control.image_position.x() * scale),
                round(self.image_y + control.image_position.y() * scale),
            )

    def move_control_from_canvas(self, control, position):
        """Move a control from a drag and persist its image-relative position."""
        control.image_position = self.canvas_to_image_position(position)

        import bpy

        scene = bpy.context.scene

        if scene.rp_symmetry:

            from ..backend import mirror_name

            mirror_bone = mirror_name(control.bone_name)

            if mirror_bone:

                mirror_control = self.control_list.controls.get(mirror_bone)

                if mirror_control:

                    CONTROL_SIZE = control.size

                    center = control.image_position.x() + CONTROL_SIZE / 2
                    mirror_center = 2 * scene.rp_symmetry_x - center
                    mirror_x = mirror_center - CONTROL_SIZE / 2

                    mirror_control.image_position.setX(round(mirror_x))
                    mirror_control.image_position.setY(control.image_position.y())

                    for item in scene.rp_items:
                        if item.bone_name == mirror_bone:
                            item.x = mirror_control.image_position.x()
                            item.y = mirror_control.image_position.y()
                            break

        for item in scene.rp_items:
            if item.bone_name == control.bone_name:
                item.x = control.image_position.x()
                item.y = control.image_position.y()
                break

        self.layout_controls()

    def paintEvent(self, event):

        super().paintEvent(event)

        if self.background is None:
            return

        painter = QPainter(self)

        pixmap = self.scaled_background()

        painter.drawPixmap(
            self.image_x,
            self.image_y,
            pixmap
        )
        if self.symmetry_enabled and self.symmetry_x >= 0:

            painter.setPen(
                QPen(QColor(255,0,0),2)
            )
            canvas_x = self.image_x + self.symmetry_x * self.image_scale()

            # ---------------------------------------------------------
            # Draw symmetry guide
            # ---------------------------------------------------------

            canvas_x = self.image_x + self.symmetry_x * self.image_scale()

            # Dashed white line
            pen = QPen(Qt.white, 2)
            pen.setStyle(Qt.DashLine)
            pen.setCapStyle(Qt.RoundCap)

            painter.setPen(pen)

            triangle_height = 10
            line_top = self.image_y + triangle_height + 2
            line_bottom = (
                self.image_y
                + self.background.height() * self.image_scale()
            )

            painter.drawLine(
                QPoint(canvas_x, line_top),
                QPoint(canvas_x, line_bottom)
            )

            # Draw triangle handle
            triangle_width = 14

            triangle = QPolygon([
                QPoint(int(canvas_x), int(line_top - triangle_height)),
                QPoint(int(canvas_x - triangle_width / 2), int(line_top)),
                QPoint(int(canvas_x + triangle_width / 2), int(line_top)),
            ])

            painter.setPen(Qt.NoPen)
            painter.setBrush(Qt.white)
            painter.drawPolygon(triangle)

    def mousePressEvent(self, event):

        if event.button() == Qt.LeftButton:

            click_pos = event.position().toPoint()

            child = self.childAt(click_pos)

            # -----------------------------------------
            # Nothing directly clicked?
            # Look for a nearby control.
            # -----------------------------------------
            if child is None:

                PICK_RADIUS = 10

                nearest = None
                nearest_dist2 = PICK_RADIUS * PICK_RADIUS

                for control in self.findChildren(CircleControl):

                    center = control.geometry().center()

                    dx = center.x() - click_pos.x()
                    dy = center.y() - click_pos.y()

                    dist2 = dx * dx + dy * dy

                    if dist2 <= nearest_dist2:
                        nearest = control
                        nearest_dist2 = dist2

                # Click close enough to a control
                if nearest:
                    nearest.clicked.emit(
                        nearest.bone_name,
                        False
                    )
                    return

                # Truly empty space
                window = self.window()
                if hasattr(window, "controller"):
                    window.controller.deselect_all()
                    window.controller.hide_all()

                if self.symmetry_enabled:
                    
                    if (
                        self.symmetry_enabled and
                        self.symmetry_handle_rect().contains(
                            event.position().toPoint()
                        )
                    ):
                        self.dragging_symmetry = True
                        return

                self.dragging_image = True
                self.drag_start = click_pos

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        hover = self.symmetry_handle_rect().contains(
            event.position().toPoint()
        )
        if hover != self.symmetry_handle_hover:
            self.symmetry_handle_hover = hover
            self.update()

        if self.dragging_symmetry:

            image_x = self.canvas_to_image_position(
                QPoint(event.position().x(), 0)
            ).x()

            # Keep symmetry line inside the image
            image_x = max(
                0,
                min(image_x, self.background.width())
            )

            delta = image_x - self.symmetry_x

            self.symmetry_x = image_x
            bpy.context.scene.rp_symmetry_x = image_x

            # Move every control by the same amount
            for control in self.findChildren(CircleControl):
                control.image_position.setX(
                    control.image_position.x() + delta
                )

            # Update Blender's stored positions
            for item in bpy.context.scene.rp_items:
                item.x += delta

            self.layout_controls()
            self.update()
            return

        if self.dragging_image:

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

            self.layout_controls()
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

        self.layout_controls()
        self.update()

    def mouseReleaseEvent(self, event):

        self.dragging_image = False
        self.dragging_symmetry = False

        super().mouseReleaseEvent(event)
    

    def symmetry_handle_rect(self):

        if self.symmetry_x < 0:
            return QRect()

        canvas_x = self.image_x + self.symmetry_x * self.image_scale()

        size = self.symmetry_handle_size

        return QRect(
            int(canvas_x - 12),
            0,
            24,
            28
        )
