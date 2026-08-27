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


# Fallback scale used only when no background image is loaded yet (so
# controls still get a sane, stable size before any capture). Once a
# background is loaded, image_scale() ignores this and computes the
# real on-canvas scale of that image instead, so controls track it as
# the window is resized.
DEFAULT_IMAGE_SCALE_ESTIMATE = 1.0

# Reference size used ONLY for spacing out auto-placed controls in
# add_control()'s fallback grid (when a control has no saved position
# yet) - deliberately NOT the same as each control's own actual
# control_size. A grid only avoids overlap if every cell reserves the
# same amount of space regardless of what ends up placed in it; using
# each control's own (possibly customized) size for its own cell's step
# meant a wider control could extend past its own cell boundary into
# wherever a narrower neighboring control landed. This is set a bit
# above the default control size (18) to comfortably cover typical
# resized-larger controls too, without needing to know every item's
# actual size in advance. Kept close to the default (rather than far
# above it) so auto-placed controls end up snugly spaced instead of
# scattered with visibly empty gaps between them.
GRID_CELL_REFERENCE_SIZE = 20

# Fixed pixel gap between grid cells, and the margin kept clear around
# the edge of the background image. Both are in "image space" (see
# _next_grid_position()'s docstring for what that actually means).
GRID_GAP = 6
GRID_MARGIN = 16

# Horizontal spacing between auto-placed controls is deliberately looser
# than it needs to be for default-sized controls if the full step (cell
# width + gap) is used - halving just the horizontal step tightens that
# up while leaving row spacing (which wasn't reported as a problem)
# alone.
HORIZONTAL_SPACING_SCALE = 0.5

# Fallback grid width (image-space pixels) used only when no background
# image is loaded yet to measure against - shouldn't normally happen,
# since Add Selected is disabled until a background exists, but keeps
# this from crashing/misbehaving if it's ever called anyway.
GRID_FALLBACK_WIDTH = 320



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

    def add_control(self, bone_name, x=None, y=None, size=18, shape="CIRCLE", color="GREEN"):

        if bone_name in self.controls:
            return

        control = CircleControl(bone_name, size=size, shape=shape, color=color)

        # Make the canvas the parent
        control.setParent(self.container)

        if x is None or y is None:
            x, y = self._next_grid_position(len(self.controls))

        # Positions saved in Blender are in the unscaled background image's
        # coordinate space, not the current canvas's pixel space.
        control.image_position = QPoint(int(x), int(y))

        self.controls[bone_name] = control
        self.container.layout_controls()
        control.show()

        return control

    def _next_grid_position(self, index):
        """Computes the auto-placed (x, y) - in image space - for the
        index'th control with no saved position yet.

        "Image space" here is NOT the background's raw native pixel
        size. image_scale() (see PickerCanvas) re-baselines to the
        CANVAS WIDGET's on-screen size the moment a background loads
        (reset_scale_reference()), so control.image_position - and
        therefore this grid - has to be measured in that same
        canvas-relative space, not the source PNG's actual resolution.
        Using the raw pixel width here (as an earlier version of this
        function did) meant the bound itself was frequently many times
        wider than the space controls are really laid out in - e.g. a
        captured image saved at 1200px wide but displayed in a 320px
        canvas - so the grid still placed later controls well outside
        the visible image despite "fitting" against that wrong number.
        canvas_to_image_position() is the addon's own established
        conversion for this (see Controller._ensure_symmetry_default(),
        which hits the exact same trap for the symmetry guide line) -
        reused here instead of re-deriving it.
        """
        from ..backend import control_dimensions

        cell_width, cell_height = control_dimensions(
            GRID_CELL_REFERENCE_SIZE, "RECTANGLE"
        )
        step_x = max(1, round((cell_width + GRID_GAP) * HORIZONTAL_SPACING_SCALE))
        step_y = cell_height + GRID_GAP

        canvas = self.container
        pixmap = canvas.scaled_background()

        if pixmap is not None and pixmap.width() > 0:
            image_width = canvas.canvas_to_image_position(
                QPoint(canvas.image_x + pixmap.width(), 0)
            ).x()
        else:
            image_width = 0

        if image_width <= 0:
            image_width = GRID_FALLBACK_WIDTH

        usable_width = max(image_width - 2 * GRID_MARGIN, step_x)
        columns = max(1, (usable_width // step_x) + 1)

        column = index % columns

        row = index // columns

        x = GRID_MARGIN + column * step_x
        y = GRID_MARGIN + row * step_y

        # Belt-and-braces clamp: guards against the last column's cell
        # nosing past the image's right edge on an odd width/step
        # combination, keeping every auto-placed control's full cell
        # (not just its top-left corner) within the image.
        max_x = max(GRID_MARGIN, image_width - cell_width - GRID_MARGIN)
        x = min(x, max_x)

        return x, y

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
        """Loads image_path onto the canvas. Returns True on success.

        On failure (missing file, moved .blend, corrupted image, stale
        path, etc.) this must never just bail out and leave whatever
        image happens to already be on the canvas - that's what let one
        rig's background silently bleed into another rig that has no
        (or a broken) saved image of its own. Fall back to a clean,
        background-less canvas instead, and report failure so callers
        (Controller.refresh()) can also stop treating this rig as
        "has a background" - keeping the Add/Clear/Delete/Symmetry/
        IK-FK/Motion Paths buttons correctly disabled instead of staying
        enabled against an image that isn't actually there.
        """

        image = QImage(image_path)

        if image.isNull():
            self.clear_background()
            return False

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

        # Loading (or swapping in) a background image should never by
        # itself resize/move the controls - only re-baseline the "1.0
        # scale" point to the canvas's current size, so a *later* window
        # resize is what changes their scale from here on.
        self.container.reset_scale_reference()

        # Restore the saved relative drag position (0..1) instead of
        # always resetting to the top-left corner, so switching back to
        # an armature keeps its previously-dragged image position.
        self.container.image_offset_x = offset_x
        self.container.image_offset_y = offset_y

        self.container.apply_image_offset()
        self.container.layout_controls()
        self.container.update_overlay_buttons()
        self.container.update()

        return True

    def clear_background(self):
        """Removes the background image (e.g. when switching to an armature
        that has no captured view saved yet, or one whose saved image
        failed to load)."""

        self.container.background = None
        self.container._scale_reference_size = None

        self.container.image_x = 0
        self.container.image_y = 0

        self.container.image_offset_x = 0.0
        self.container.image_offset_y = 0.0

        self.container.layout_controls()
        self.container.update_overlay_buttons()
        self.container.update()

        return False

class PickerCanvas(QWidget):

    def __init__(self):
        super().__init__()

        self.background = None

        # The canvas size captured as the "1.0 scale" baseline for
        # image_scale() - see reset_scale_reference(). None until a
        # background is first loaded.
        self._scale_reference_size = None

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

            # ensurePolished() forces Qt to resolve this widget's
            # stylesheet rules (including the #ikfkOverlayButton id
            # selector below, which zeroes out the generic QPushButton
            # rule's padding/min-height/max-height so setFixedSize() can
            # fully control this button's size later) right now, instead
            # of leaving that resolution to happen lazily on first show/
            # paint. Without this, adjustSize() below can compute
            # sizeHint() against the generic QPushButton rule's
            # `padding: 1px 6px` / `min-height: 18px` / `max-height: 20px`
            # (since #ikfkOverlayButton hasn't been "polished" in yet),
            # inflating _base_width/_base_height. That wrong base then
            # gets baked into every future button_scale calculation in
            # update_overlay_buttons() for the rest of the session, since
            # nothing ever re-captures it afterward - unlike the picker
            # controls' geometry, which is computed fresh each time from
            # control_dimensions() rather than a cached, polish-dependent
            # base size.
            btn.ensurePolished()
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
        # Right-align "IK" so, once fit_font's breathing-room shrink
        # leaves its glyph smaller than its own box, the leftover empty
        # space sits on the outer/left side of the label instead of
        # between the text and the switch (QLabel default-aligns left).
        self.ikfk_label_left.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.ikfk_label_right = QLabel("FK", self)
        self.ikfk_label_right.setObjectName("ikfkOverlayLabel")
        # Left-align (the QLabel default, set explicitly for clarity) so
        # "FK"'s glyph hugs the switch on its left, with any leftover
        # empty space on the outer/right side of the label instead.
        self.ikfk_label_right.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.ikfk_switch = ToggleSwitch(checked=False, parent=self)

        # Neutral until a bone with a live IK_FK state is selected -
        # matches update_ikfk_toggle(None) below.
        self.ikfk_switch.setEnabled(False)

        for widget in (self.ikfk_label_left, self.ikfk_switch, self.ikfk_label_right):
            # Same reasoning as the ensurePolished() call above for the
            # overlay buttons - resolve #ikfkOverlayLabel's stylesheet
            # rule before measuring, so a plain-click-through construction
            # order doesn't leave adjustSize() reading unstyled metrics.
            widget.ensurePolished()
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

        # Press position for a click that *might* turn into a symmetry-line
        # or background-image drag, but hasn't crossed the movement
        # threshold yet - see mousePressEvent/mouseMoveEvent. A plain click
        # with no movement never sets dragging_symmetry/dragging_image and
        # never records an undo snapshot.
        self._pending_drag_start = None
        self._pending_drag_kind = None  # "symmetry" | "image" | None
        self.DRAG_THRESHOLD = 4

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

    def reset_scale_reference(self):
        """Captures the canvas's current size as the new "1.0 scale"
        baseline for image_scale() - call this whenever the background
        image itself changes (a picture loaded or swapped in, e.g. via
        the JSON data's "background" property changing on armature
        switch/capture), NOT on a plain window resize.

        Right after this runs, image_scale() returns 1.0, so controls
        are laid out at exactly their saved size/position - loading a
        new image never itself resizes or moves them. Only a canvas
        resize *after* this point (maximizing/restoring/dragging the
        window) changes the scale from here on, since it changes the
        displayed image size relative to this baseline rather than
        relative to the image's native pixel size."""
        self._scale_reference_size = self.size()

    def native_image_scale(self):
        """Returns current display width / native background pixel width
        - the raw ratio the original addon used for image_scale() before
        the reference-baseline rework above. Kept as a separate method
        (not reused for image_scale() itself) because switching the
        picker CONTROLS to this raw ratio is exactly what caused their
        session-to-session size drift: this ratio is typically well
        under 1.0 (a screen-sized canvas showing a much-higher-resolution
        source image) and isn't stable across restarts/whether a
        background is loaded at all, whereas image_scale()'s baseline-
        relative definition is.

        The IK/FK and motion-path OVERLAY BUTTONS, unlike the picker
        controls, were never given an image-space "native size" via
        control_dimensions() - their _base_width/_base_height is just
        whatever Qt/the stylesheet computed for the unstyled button at
        construction time, sized to look right when multiplied by a
        typically-small raw ratio like this one. Multiplying that same
        base by the new image_scale() instead (which is 1.0 right after
        any image loads, regardless of that image's actual resolution)
        made these buttons balloon to ~2x their base size on load and
        stop tracking the image's actual on-canvas size - so overlay
        buttons use this method instead, matching the original addon's
        behavior, while picker controls keep using image_scale().
        """
        pixmap = self.scaled_background()
        if pixmap is None or self.background is None or self.background.width() == 0:
            return 1.0
        return pixmap.width() / self.background.width()

    def image_scale(self):
        """Returns how large the background image is currently being
        drawn on the canvas relative to how large it was drawn right
        after it was (last) loaded - see reset_scale_reference(). A
        window resize after that point grows/shrinks this ratio, which
        controls are multiplied by (see canvas_to_image_position /
        layout_controls) so they track the image's on-screen size and
        position as the window is resized. Loading a new background
        image itself never changes this ratio, since
        reset_scale_reference() re-baselines it to the canvas's size at
        that exact moment.

        Falls back to DEFAULT_IMAGE_SCALE_ESTIMATE (a no-op 1.0) when
        there's no background loaded yet, or no baseline has been
        captured yet."""
        if self.background is None or self.background.width() <= 0:
            return DEFAULT_IMAGE_SCALE_ESTIMATE

        if self._scale_reference_size is None or self._scale_reference_size.isEmpty():
            return DEFAULT_IMAGE_SCALE_ESTIMATE

        reference_display = self.background.scaled(
            self._scale_reference_size,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )

        if reference_display.width() <= 0:
            return DEFAULT_IMAGE_SCALE_ESTIMATE

        current_display = self.scaled_background()
        return current_display.width() / reference_display.width()

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

            # round(), not int(): truncation biases every control's
            # on-screen position left/up by up to half a pixel whenever
            # display_x/display_y land on a fractional remainder - the
            # same issue already fixed for the mirror control's stored
            # position above, but this is what actually paints controls
            # on screen, so it was still visible after a symmetry snap.
            control.move(
                round(display_x),
                round(display_y),
            )

    def move_control_from_canvas(self, control, position):
        """Move a control from a drag and persist its image-relative position."""
        control.image_position = self.canvas_to_image_position(position)

        controller = getattr(self.window(), "controller", None)

        if controller is None:
            self.layout_controls()
            return

        if controller.data.get("symmetry"):

            from ..backend import mirror_name, control_dimensions

            mirror_bone = mirror_name(control.bone_name)

            if mirror_bone:

                mirror_control = self.control_list.controls.get(mirror_bone)

                if mirror_control:

                    # Each control can have its own size/shape, so its
                    # on-screen width can differ from its mirror's - use
                    # control_dimensions() (the same source of truth used
                    # everywhere else in the codebase, e.g. controller.py's
                    # appearance-resize re-centering) to get *each*
                    # control's own width instead of reusing the dragged
                    # control's width for both. Reusing one width for both
                    # is exactly why the mirror control was landing off
                    # the symmetry line whenever the two controls' sizes
                    # or shapes differed.
                    control_width, _ = control_dimensions(control.size, control.shape)
                    mirror_width, _ = control_dimensions(
                        mirror_control.size, mirror_control.shape
                    )

                    center = control.image_position.x() + control_width / 2.0
                    mirror_center = (
                        2 * controller.data["symmetry_x"]
                        - center
                    )
                    mirror_x = mirror_center - mirror_width / 2.0

                    # round(), not int()/`//`: truncation biases the
                    # mirror's position left/up by up to half a pixel
                    # whenever the centering math lands on a .5 remainder,
                    # so it never quite sits on the true symmetry-line
                    # reflection of the dragged control's center.
                    mirror_control.image_position.setX(round(mirror_x))
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

        # native_image_scale(), not image_scale(): the overlay buttons'
        # _base_width/_base_height were sized (via adjustSize() at
        # construction) against the original addon's raw display/native
        # pixel ratio, not the newer load-baseline-relative image_scale()
        # used for picker controls - see native_image_scale()'s
        # docstring for why using image_scale() here made these buttons
        # balloon on load instead of tracking the image's actual
        # on-canvas size.
        scale = self.native_image_scale()

        # Overlay buttons scale with the image like the picker controls
        # do, but at 2x that rate so they stay comfortably tappable/
        # legible instead of shrinking down to the same tiny size as a
        # control - slightly larger than picker controls at any given
        # image scale.
        button_scale = max(0.1, scale) * 2.0

        margin = round(10 * button_scale)
        spacing = round(4 * button_scale)

        # The IK/FK switch and its two flanking labels read as one
        # compound control, not three separate ones - so they sit flush
        # against each other with no gap, unlike the spacing between
        # separate buttons.
        tight_spacing = 0
        tight_rows = (self.ikfk_label_left, self.ikfk_switch, self.ikfk_label_right)

        x = self.image_x + margin
        y = self.image_y + margin

        for row in rows:

            row_x = x
            row_spacing = tight_spacing if row == tight_rows else spacing

            # First pass: resize every widget in the row (and fit its
            # font, where applicable) so we know the row's tallest
            # widget before positioning anything.
            sizes = []
            for btn in row:

                width = max(8, round(btn._base_width * button_scale))
                height = max(8, round(btn._base_height * button_scale))

                btn.setFixedSize(width, height)

                # ToggleSwitch has no text to size a font for - it's
                # drawn entirely in paintEvent - so skip the QPushButton/
                # QLabel-only font-fitting step for it.
                if hasattr(btn, "text"):
                    self.fit_font(btn)

                # The label's box width comes from adjustSize() at
                # construction time and doesn't shrink back down after
                # fit_font() picks a smaller font for breathing room -
                # so for the IK/switch/FK compound specifically, ask Qt
                # to recompute the label's ideal box for its now-fitted
                # font (instead of hand-measuring it, which can clip
                # the text if the measurement comes out too tight).
                if row == tight_rows and isinstance(btn, QLabel):
                    btn.adjustSize()
                    width = btn.width()
                    height = btn.height()

                sizes.append((width, height))

            row_height = max(height for _, height in sizes)

            # Second pass: position each widget, vertically centering it
            # within the row so shorter widgets (e.g. the toggle switch)
            # line up with taller ones (e.g. its flanking labels) instead
            # of sharing a common top edge.
            for btn, (width, height) in zip(row, sizes):

                btn_y = y + (row_height - height) // 2

                btn.move(round(row_x), round(btn_y))

                btn.raise_()
                btn.show()

                row_x += width + row_spacing

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

            # round(), not raw floats: canvas_x/line_bottom are floats
            # (symmetry_x * a float scale), but QPoint's constructor
            # expects ints - passing floats here silently aborts the
            # rest of this paintEvent (Qt/PySide reports the TypeError
            # to the console, not to the UI), which meant the guide line
            # (and its triangle handle) never actually got drawn even
            # once symmetry_x held a correct, on-canvas value.
            painter.drawLine(
                QPoint(round(canvas_x), round(line_top)),
                QPoint(round(canvas_x), round(line_bottom))
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
                        False,
                        False,  # was_drag: this is a direct pick, never a drag
                    )
                    return

                # Truly empty space
                window = self.window()
                if hasattr(window, "controller"):
                    window.controller.deselect_all()

                controller = getattr(self.window(), "controller", None)

                if self.symmetry_enabled:
                    
                    if (
                        self.symmetry_enabled and
                        self.symmetry_handle_rect().contains(
                            event.position().toPoint()
                        )
                    ):
                        # Don't snapshot or start dragging yet - wait for
                        # actual movement (see mouseMoveEvent). A plain
                        # click on the handle with no drag isn't an edit.
                        self._pending_drag_start = click_pos
                        self._pending_drag_kind = "symmetry"
                        return

                # Don't snapshot or start dragging yet either - a press on
                # empty space with a background image loaded used to be
                # treated as the start of an image drag unconditionally,
                # so a plain click-to-deselect (no movement at all) still
                # pushed an undo snapshot, making undo() jump back to that
                # click instead of the last real edit. Now the snapshot is
                # deferred to mouseMoveEvent, the moment real movement
                # crosses the drag threshold.
                if self.background:
                    self._pending_drag_start = click_pos
                    self._pending_drag_kind = "image"

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        hover = self.symmetry_handle_rect().contains(
            event.position().toPoint()
        )
        if hover != self.symmetry_handle_hover:
            self.symmetry_handle_hover = hover
            self.update()

        # A press on the symmetry handle or empty space (with a
        # background image loaded) is pending until movement actually
        # crosses the drag threshold - only then does it become a real
        # drag, and only then is an undo snapshot taken. A plain click
        # with no movement leaves dragging_symmetry/dragging_image False
        # and never touches the undo stack.
        if self._pending_drag_start is not None:
            moved = event.position().toPoint() - self._pending_drag_start
            if moved.manhattanLength() < self.DRAG_THRESHOLD:
                return

            controller = getattr(self.window(), "controller", None)
            if controller is not None:
                controller._record_undo_snapshot()

            if self._pending_drag_kind == "symmetry":
                self.dragging_symmetry = True
            elif self._pending_drag_kind == "image":
                self.dragging_image = True
                self.drag_start = self._pending_drag_start

            self._pending_drag_start = None
            self._pending_drag_kind = None

        if self.dragging_symmetry:

            image_x = self.canvas_to_image_position(
                QPoint(event.position().x(), 0)
            ).x()

            # Keep symmetry line inside the image. self.background.width()
            # is the background's raw NATIVE pixel width, but image_x here
            # (from canvas_to_image_position()) is in the current
            # canvas-relative image space - typically much smaller, so
            # this clamp almost never actually triggers. Use the image's
            # own width converted into that same current image space
            # instead, matching _ensure_symmetry_default() in
            # controller.py.
            image_space_width = self.canvas_to_image_position(
                QPoint(self.image_x + self.scaled_background().width(), 0)
            ).x()
            image_x = max(
                0,
                min(image_x, image_space_width)
            )

            desired_delta = image_x - self.symmetry_x

            # Dragging the line shifts EVERY control by the same delta
            # (see the loop below) - so even though the line itself stays
            # inside the image, controls positioned near the OPPOSITE
            # edge from whichever direction the line is being dragged
            # can get pushed straight out of the image once the same
            # delta is applied to them too. Only the line's own position
            # was being bounds-checked; the controls being dragged along
            # with it weren't. If there are no controls yet, there's
            # nothing this could push out of bounds, so the line is free
            # to go anywhere within the image as before.
            controls = (
                self.control_list.controls.values()
                if self.control_list is not None
                else self.findChildren(CircleControl)
            )
            controls = list(controls)

            delta = desired_delta

            if controls:
                from ..backend import control_dimensions

                min_left = None
                max_right = None

                for control in controls:
                    width, _height = control_dimensions(
                        control.size, control.shape
                    )
                    left = control.image_position.x()
                    right = left + width

                    if min_left is None or left < min_left:
                        min_left = left
                    if max_right is None or right > max_right:
                        max_right = right

                # delta can't push the leftmost control's left edge below
                # 0, and can't push the rightmost control's right edge
                # past the image's own width.
                allowed_min_delta = -min_left
                allowed_max_delta = image_space_width - max_right

                if allowed_min_delta > allowed_max_delta:
                    # Controls already span (or exceed) the full image
                    # width - no room to shift them at all without one
                    # end going out of bounds, so hold the line still
                    # rather than picking an arbitrary direction.
                    delta = 0
                else:
                    delta = max(
                        allowed_min_delta,
                        min(delta, allowed_max_delta)
                    )

            image_x = self.symmetry_x + delta

            self.symmetry_x = image_x

            controller = getattr(self.window(), "controller", None)

            if controller is not None:
                controller.data["symmetry_x"] = image_x

                for item in controller.data["items"]:
                    item["x"] += delta

                controller.save()

            # Move every control by the same amount
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

        # Clear any pending drag that never crossed the threshold (i.e.
        # this was a plain click, not a drag) so it doesn't leak into
        # the next press.
        self._pending_drag_start = None
        self._pending_drag_kind = None

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