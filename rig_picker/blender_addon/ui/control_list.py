"""
control_list.py

Displays circular picker controls on a free canvas.
"""

from PySide6.QtWidgets import (
    QWidget,
    QScrollArea,
    QPushButton,
    QLabel,
)
from PySide6.QtGui import (
    QPen,
    QColor,
)

from .circle_control import CircleControl
from .toggle_switch import ToggleSwitch
from PySide6.QtGui import QPixmap, QPainter, QImage, QPolygon, QFontMetrics, QFont
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
            # hide() immediately makes it invisible; deleteLater() defers
            # the actual C++ destruction to the next event loop tick.
            #
            # Deliberately NOT calling setParent(None) here: reparenting a
            # widget to no parent turns it into its own top-level window,
            # which is unnecessary and risks it flashing as a stray
            # floating window before deleteLater() finishes it off. It's
            # unnecessary because layout_controls(), hit-testing, and the
            # symmetry drag all read from this `controls` dict (not Qt's
            # live widget tree), so once a control is removed from here it
            # can never be touched again regardless of when Qt actually
            # deletes the underlying widget.
            control.hide()
            control.deleteLater()

        self.controls.clear()

    def set_background(self, image_path, offset_x=0.0, offset_y=0.0):

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

        # Restore the saved relative drag position (0..1) instead of
        # always resetting to the top-left corner, so switching back to
        # an armature keeps its previously-dragged image position.
        self.container.image_offset_x = offset_x
        self.container.image_offset_y = offset_y

        self.container.apply_image_offset()
        self.container.layout_controls()
        self.container.update_overlay_buttons()
        self.container.update()

    def clear_background(self):
        """Removes the background image (e.g. when switching to an armature
        that has no captured view saved yet)."""

        self.container.background = None

        self.container.image_x = 0
        self.container.image_y = 0

        self.container.image_offset_x = 0.0
        self.container.image_offset_y = 0.0

        self.container.layout_controls()
        self.container.update_overlay_buttons()
        self.container.update()

class PickerCanvas(QWidget):

    def __init__(self):
        super().__init__()

        self.background = None

        # Set by ControlList right after construction; defaulted here so
        # it's always safe to reference even before that happens.
        self.control_list = None

       # IK/FK overlay buttons
        self.fk_to_ik_button = QPushButton("FK → IK", self)
        self.ik_to_fk_button = QPushButton("IK → FK", self)
        self.calculate_path_button = QPushButton("Calculate", self)
        self.clear_path_button = QPushButton("x", self)

        for btn in (
            self.fk_to_ik_button,
            self.ik_to_fk_button,
            self.calculate_path_button,
            self.clear_path_button,
        ):
            btn.setObjectName("ikfkOverlayButton")

            btn.adjustSize()

            btn._base_width = btn.width()
            btn._base_height = btn.height()

            f = btn.font()
            if f.pixelSize() > 0:
                btn._base_font_px = f.pixelSize()
            else:
                btn._base_font_px = round(
                    f.pointSizeF() * self.logicalDpiY() / 72
                )

            btn.raise_()
            btn.hide()

        # IK <-> FK switch - a sliding toggle with a fixed "IK" label on
        # the left and "FK" label on the right, in place of the old
        # text-swapping button. The knob's position reflects the
        # currently selected bone's actual IK_FK state (left = IK,
        # right = FK) and slides live as that state changes - whether
        # from clicking a control, using the switch itself, or (with the
        # Live toggle on) simply scrubbing to a different frame. Clicking
        # it anywhere still performs the same FK<->IK switch the old
        # button did.
        self.ikfk_label_left = QLabel("IK", self)
        self.ikfk_label_left.setObjectName("ikfkOverlayLabel")
        self.ikfk_label_right = QLabel("FK", self)
        self.ikfk_label_right.setObjectName("ikfkOverlayLabel")
        self.ikfk_switch = ToggleSwitch(checked=False, parent=self)

        # Neutral until a bone with a live IK_FK state is selected -
        # matches update_ikfk_toggle(None) below.
        self.ikfk_switch.setEnabled(False)

        for widget in (self.ikfk_label_left, self.ikfk_switch, self.ikfk_label_right):
            if isinstance(widget, QLabel):
                widget.adjustSize()
            widget._base_width = widget.width()
            widget._base_height = widget.height()
            widget.raise_()
            widget.hide()

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

        # Controls whether the IK/FK switch and its two snapping buttons
        # (FK -> IK, IK -> FK) are shown on the canvas at all. Driven by
        # the "IK-FK" checkbox in the main window; the Calculate/Clear
        # motion-path buttons are unaffected by this.
        self.ikfk_controls_enabled = False

        # Controls whether the Calculate/Clear motion-path buttons are
        # shown on the canvas. Driven by the "Motion Paths" checkbox in
        # the main window.
        self.motion_paths_controls_enabled = False
    
    def connect_controller(self, controller):
        self.ikfk_switch.toggled.connect(lambda checked: controller.toggle_ik_fk())
        self.fk_to_ik_button.clicked.connect(controller.fk_to_ik)
        self.ik_to_fk_button.clicked.connect(controller.ik_to_fk)
        self.calculate_path_button.clicked.connect(controller.calculate_motion_path)
        self.clear_path_button.clicked.connect(controller.clear_motion_path)

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

        controls = (
            self.control_list.controls.values()
            if self.control_list is not None
            else self.findChildren(CircleControl)
        )

        for control in controls:
            if not hasattr(control, "image_position"):
                control.image_position = self.canvas_to_image_position(control.pos())

            control.set_display_scale(scale)
            display_x = self.image_x + control.image_position.x() * scale
            display_y = self.image_y + control.image_position.y() * scale

            control.move(
                int(display_x),
                int(display_y),
            )

    def move_control_from_canvas(self, control, position):
        """Move a control from a drag and persist its image-relative position."""
        control.image_position = self.canvas_to_image_position(position)

        controller = getattr(self.window(), "controller", None)

        if controller is None:
            self.layout_controls()
            return

        if controller.data.get("symmetry"):

            from ..backend import mirror_name

            mirror_bone = mirror_name(control.bone_name)

            if mirror_bone:

                mirror_control = self.control_list.controls.get(mirror_bone)

                if mirror_control:

                    CONTROL_SIZE = control.size

                    center = control.image_position.x() + CONTROL_SIZE // 2
                    mirror_center = (
                        2 * controller.data["symmetry_x"]
                        - center
                    )
                    mirror_x = mirror_center - CONTROL_SIZE // 2

                    mirror_control.image_position.setX(int(mirror_x))
                    mirror_control.image_position.setY(control.image_position.y())

                    mirror_item = controller.find_item(mirror_bone)
                    if mirror_item:
                        mirror_item["x"] = mirror_control.image_position.x()
                        mirror_item["y"] = mirror_control.image_position.y()

        item = controller.find_item(control.bone_name)
        if item:
            item["x"] = control.image_position.x()
            item["y"] = control.image_position.y()

        controller.save()
        self.layout_controls()

    def fit_font(self, btn):
        text = btn.text()

        if not text:
            return

        # Margins scale with the button instead of being a flat 10px.
        # A flat margin can exceed the entire button at small scales,
        # meaning no size ever "fits" below - which is why the font used
        # to get left stuck at whatever size it last was (too big for the
        # new, smaller button) instead of shrinking with it.
        width_margin = max(2, round(btn.width() * 0.12))
        height_margin = max(2, round(btn.height() * 0.2))

        available_width = max(4, btn.width() - width_margin)
        available_height = max(4, btn.height() - height_margin)

        font = QFont(btn.font())

        # Absolute floor so a font is always applied, even for extremely
        # small buttons where nothing fits perfectly - matches btn.setFont()
        # always being called below instead of sometimes being skipped.
        best_size = 4

        size = max(4, int(btn.height() * 0.55))
        while size >= 4:
            font.setPixelSize(size)
            metrics = QFontMetrics(font)

            if (
                metrics.horizontalAdvance(text) <= available_width
                and metrics.height() <= available_height
            ):
                best_size = size
                break

            size -= 1

        # Shrink slightly below the max-that-fits so there's always some
        # visible breathing room between the text and the button border,
        # instead of the text touching the edges.
        best_size = max(4, round(best_size * 0.8))

        font.setPixelSize(best_size)
        btn.setFont(font)
    def update_ikfk_toggle(self, is_fk):
        """is_fk: True if the selected bone is currently in FK, False if
        currently in IK, or None if there's no single selected bone
        belonging to an IK/FK group. Unlike the old text-swapping button,
        the switch shows the CURRENT state (knob left = IK, right = FK)
        and slides to match it - clicking it (or the state changing under
        it, e.g. via the Live toggle) is what makes it move."""
        self.ikfk_switch.blockSignals(True)

        if is_fk is None:
            self.ikfk_switch.setEnabled(False)
            self.ikfk_label_left.setEnabled(False)
            self.ikfk_label_right.setEnabled(False)
        else:
            self.ikfk_switch.setEnabled(True)
            self.ikfk_label_left.setEnabled(True)
            self.ikfk_label_right.setEnabled(True)
            self.ikfk_switch.set_checked(is_fk)

        self.ikfk_switch.blockSignals(False)


    def update_overlay_buttons(self):

        # Each entry is a row; a row with more than one button lays them
        # out side by side instead of stacking them.
        rows = (
            (self.ikfk_label_left, self.ikfk_switch, self.ikfk_label_right),
            (self.fk_to_ik_button,),
            (self.ik_to_fk_button,),
            (self.calculate_path_button, self.clear_path_button),
        )

        # The switch and its two snapping buttons are gated behind the
        # "IK-FK" checkbox; the motion-path row is gated behind the
        # "Motion Paths" checkbox.
        ikfk_rows = rows[:3]
        motion_path_rows = rows[3:]

        all_buttons = [btn for row in rows for btn in row]

        if self.background is None:
            for btn in all_buttons:
                btn.hide()
            return

        if not self.ikfk_controls_enabled:
            for row in ikfk_rows:
                for btn in row:
                    btn.hide()
            rows = tuple(row for row in rows if row not in ikfk_rows)

        if not self.motion_paths_controls_enabled:
            for row in motion_path_rows:
                for btn in row:
                    btn.hide()
            rows = tuple(row for row in rows if row not in motion_path_rows)

        scale = self.image_scale()

        # Slightly larger than picker controls
        button_scale = max(0.1, scale) * 2.0

        margin = round(10 * button_scale)
        spacing = round(4 * button_scale)

        # The IK/FK switch and its two flanking labels read as one
        # compound control, not three separate ones - so they sit much
        # closer together than the spacing between separate buttons.
        tight_spacing = round(-3 * button_scale)
        tight_rows = (self.ikfk_label_left, self.ikfk_switch, self.ikfk_label_right)

        x = self.image_x + margin
        y = self.image_y + margin

        for row in rows:

            row_x = x
            row_height = 0
            row_spacing = tight_spacing if row == tight_rows else spacing

            for btn in row:

                width = max(8, round(btn._base_width * button_scale))
                height = max(8, round(btn._base_height * button_scale))

                btn.setFixedSize(width, height)

                # ToggleSwitch has no text to size a font for - it's
                # drawn entirely in paintEvent - so skip the QPushButton/
                # QLabel-only font-fitting step for it.
                if hasattr(btn, "text"):
                    self.fit_font(btn)

                btn.move(round(row_x), round(y))

                btn.raise_()
                btn.show()

                row_x += width + row_spacing
                row_height = max(row_height, height)

            y += row_height + spacing

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
        # Dark overlay
        painter.fillRect(
            self.rect(),
            QColor(0, 0, 0, 50)   # Alpha: 0-255
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

                controls = (
                    self.control_list.controls.values()
                    if self.control_list is not None
                    else self.findChildren(CircleControl)
                )

                for control in controls:

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

            controller = getattr(self.window(), "controller", None)

            if controller is not None:
                controller.data["symmetry_x"] = image_x

                for item in controller.data["items"]:
                    item["x"] += delta

                controller.save()

            # Move every control by the same amount
            controls = (
                self.control_list.controls.values()
                if self.control_list is not None
                else self.findChildren(CircleControl)
            )

            for control in controls:
                control.image_position.setX(
                    control.image_position.x() + delta
                )

            self.layout_controls()
            self.update_overlay_buttons()
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
            self.update_overlay_buttons()
            self.update()

        super().mouseMoveEvent(event)

    def apply_image_offset(self):
        """Positions the background image according to the stored relative
        offset (0..1 range, independent of canvas size), re-deriving
        image_x/image_y for the current canvas size.

        Used both on resize and right after a background is (re)loaded, so
        a previously-dragged position is restored instead of resetting to
        the top-left corner.
        """
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

        self.image_x = int(min_x + self.image_offset_x * available_x)
        self.image_y = int(min_y + self.image_offset_y * available_y)

    def resizeEvent(self, event):

        super().resizeEvent(event)

        if not self.background:
            return

        self.apply_image_offset()

        self.layout_controls()
        self.update_overlay_buttons()
        self.update()

    def mouseReleaseEvent(self, event):

        was_dragging_image = self.dragging_image

        self.dragging_image = False
        self.dragging_symmetry = False

        if was_dragging_image:
            controller = getattr(self.window(), "controller", None)
            if controller is not None:
                controller.data["image_offset_x"] = self.image_offset_x
                controller.data["image_offset_y"] = self.image_offset_y
                controller.save()

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