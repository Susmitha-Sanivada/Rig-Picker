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
from . import collision_shapes as shapes
from PySide6.QtGui import QPixmap, QPainter, QImage, QImageReader, QPolygon, QFontMetrics, QFont
from PySide6.QtCore import Qt, QPoint, QRect, QRectF, QEvent, QObject
import os
import bpy
import uuid



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
GRID_GAP = 1
GRID_MARGIN = 16

# Collision boxes used while dragging are the real widget rectangles
# shrunk by this many pixels on EACH side (so two controls can overlap by
# this many pixels before they push each other apart). 0 restores the old
# full-size boxes; raise it to let controls sit even closer / overlap more.
COLLISION_INSET = 3

# Fallback grid width (image-space pixels) used only when no background
# image is loaded yet to measure against - shouldn't normally happen,
# since Add Selected is disabled until a background exists, but keeps
# this from crashing/misbehaving if it's ever called anyway.
GRID_FALLBACK_WIDTH = 320

# The #ikfkOverlayButton stylesheet rule (see blender_dark.qss) zeroes
# out padding so setFixedSize() can fully control these buttons' size -
# but that also means their "natural"/base width, measured straight off
# adjustSize()/sizeHint(), is just the bare text width with no room
# around it at all. That's invisible for long labels (their own glyph
# width dwarfs a fixed few extra pixels), but for a short label like
# "but" or "x" the button ends up sized almost exactly to the text, so
# fit_font()'s percentage-based margin has almost nothing to work with
# and the text visually touches the border. Baking a flat, fixed
# horizontal padding into the base width - on top of whatever the text
# itself needs - guarantees the same minimum breathing room on every
# button regardless of label length, instead of that gap shrinking to
# nothing for short names.
BUTTON_TEXT_HORIZONTAL_PADDING = 16



from .safe_qt import safe_override


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
            x, y = self._next_grid_position()

        # Positions saved in Blender are in the unscaled background image's
        # coordinate space, not the current canvas's pixel space.
        control.image_position = QPoint(int(x), int(y))

        self.controls[bone_name] = control
        self.container.layout_controls()
        control.show()

        return control

    def _next_grid_position(self):
        """Computes the auto-placed (x, y) - in image space - for a new
        control with no saved position yet: the first default grid slot
        that ISN'T already occupied by some other control's actual
        current position - not simply "the len(self.controls)'th slot
        in sequence".

        A raw running count breaks the moment dragging enters the
        picture: after adding a batch, dragging every one of them off
        to custom spots, and then adding a fresh batch, the grid's own
        default slots (0, 1, 2, ...) are all sitting empty on screen -
        but a count-based index just keeps climbing from wherever it
        left off (e.g. slot 5, 6, 7, ...), so the new batch appears to
        pick up "after" the previous one instead of refilling the
        now-visually-empty default area a rigger would expect. Checking
        each candidate slot against every control's real position
        instead means a slot only ever counts as occupied while
        something is actually still sitting on it.

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

        # The rightmost x a control's LEFT edge may sit at and still
        # have its own full cell_width fit inside the margin - not a
        # column count, just the one boundary each row is measured
        # against as it fills up.
        right_edge = max(GRID_MARGIN + cell_width, image_width - GRID_MARGIN)

        # A small tolerance rather than exact float equality - saved/
        # restored positions can drift by sub-pixel rounding, and this
        # only needs to catch "still basically sitting on this slot",
        # not survive a rigger deliberately nudging a control a couple
        # pixels off of it.
        OCCUPIED_TOLERANCE = 2

        occupied = [
            (control.image_position.x(), control.image_position.y())
            for control in self.controls.values()
        ]

        def slot_taken(x, y):
            return any(
                abs(x - ox) <= OCCUPIED_TOLERANCE and abs(y - oy) <= OCCUPIED_TOLERANCE
                for ox, oy in occupied
            )

        def candidates():
            """Yields default grid slots in reading order (left to
            right, top to bottom).

            The algorithm is deliberately simple: start a row at the
            left margin, keep placing the next slot exactly
            cell_width + GRID_GAP to the right of the previous one -
            a fixed step, not scaled - and only wrap to a new row once
            the NEXT slot placed that way would run its own cell_width
            past the image's right edge. That mirrors how the controls
            actually get placed one at a time (place, step right by
            the fixed spacing, check if the next one still fits,
            otherwise drop to the next line) rather than precomputing
            a column count up front.
            """
            y = GRID_MARGIN
            while True:
                x = GRID_MARGIN
                while True:
                    yield x, y
                    next_x = x + cell_width + GRID_GAP
                    if next_x + cell_width > right_edge:
                        break
                    x = next_x
                y += cell_height + GRID_GAP

        # Every candidate slot in reading order is checked in turn
        # against the controls that already occupy a position (this is
        # what makes adding a second batch skip over any slot still
        # held by an earlier control and drop into the first genuinely
        # free one instead of restarting from slot zero). Each x within
        # a row strictly increases and each row's y strictly increases,
        # so every candidate is distinct from every other one - meaning
        # checking len(self.controls) + 1 of them is guaranteed to
        # reach one that isn't occupied.
        slot_iterator = candidates()
        for _ in range(len(self.controls) + 1):
            x, y = next(slot_iterator)
            if not slot_taken(x, y):
                return x, y

        # Unreachable in practice, but keeps this returning a
        # plausible value rather than None if it's ever wrong.
        return GRID_MARGIN, GRID_MARGIN

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
        IK-FK buttons correctly disabled instead of staying enabled
        against an image that isn't actually there.
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

class _GrabFilter(QObject):
    """Application-wide event filter that lives only for the duration of a
    G-key grab (PickerCanvas.begin_grab()): mouse movement drives the
    grab, a click/Enter/G confirms, Esc/right-click cancels, and the
    clicks/keys it uses never reach the widgets underneath."""

    def __init__(self, canvas):
        super().__init__(canvas)
        self._canvas = canvas

    @safe_override(False)
    def eventFilter(self, obj, event):
        canvas = self._canvas
        if canvas._grab is None:
            return False
        t = event.type()

        if t == QEvent.MouseMove:
            canvas._grab_move(event.globalPosition().toPoint())
            return True

        if t in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
            canvas.end_grab(event.button() == Qt.LeftButton)
            return True

        if t == QEvent.MouseButtonRelease:
            return True

        if t == QEvent.KeyPress:
            if event.isAutoRepeat():
                return True
            key = event.key()
            if key == Qt.Key_Escape:
                canvas.end_grab(False)
            elif key == Qt.Key_Z and (event.modifiers() & Qt.ControlModifier):
                # Ctrl+Z / Ctrl+Shift+Z mid-grab cancels the grab, same as
                # Esc. It is not an undo/redo step: nothing is undone,
                # nothing becomes redoable.
                canvas.end_grab(False)
            elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_G):
                canvas.end_grab(True)
            return True

        if t in (QEvent.WindowDeactivate, QEvent.Hide) and obj is canvas.window():
            canvas.end_grab(False)
        return False


class PickerCanvas(QWidget):

    def __init__(self):
        super().__init__()

        self.setObjectName("pickerCanvas")

        self.background = None

        # The canvas size captured as the "1.0 scale" baseline for
        # image_scale() - see reset_scale_reference(). None until a
        # background is first loaded.
        self._scale_reference_size = None

        # Set by ControlList right after construction; defaulted here so
        # it's always safe to reference even before that happens.
        self.control_list = None

        # Reference base height for Quick Action Button widgets (see
        # sync_quick_actions() below) - a hidden, never-shown button
        # styled with the same #ikfkOverlayButton rule those buttons
        # use, measured once here so every Quick Action button can be
        # pinned to a single consistent height instead of each one
        # independently trusting Qt's style engine to size it (which
        # could drift a pixel or two between buttons under the same
        # rule).
        _overlay_button_ref = QPushButton("", self)
        _overlay_button_ref.setObjectName("ikfkOverlayButton")
        _overlay_button_ref.ensurePolished()
        _overlay_button_ref.adjustSize()
        self._overlay_button_base_height = _overlay_button_ref.height()
        _overlay_button_ref.hide()
        _overlay_button_ref.deleteLater()

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
        self._pending_drag_kind = None  # "symmetry" | "image" | "box_select" | None
        self.DRAG_THRESHOLD = 4

        # Shift+drag rubber-band box select - see mousePressEvent (starts
        # it, only on empty canvas with Shift held), mouseMoveEvent (grows
        # box_select_rect while dragging) and mouseReleaseEvent (selects
        # every control whose geometry intersects the final rect). Drawn
        # in paintEvent while box_select_active is True.
        self.box_select_active = False
        self.box_select_origin = None
        self.box_select_rect = QRect()

        self.symmetry_enabled = False

        self.symmetry_x = -1

        self.dragging_symmetry = False

        self.dragging_image = False
        self.dragging_symmetry = False

        self.symmetry_handle_size = 14
        self.symmetry_handle_hover = False
        # True while the hand cursor is set for the symmetry handle (see
        # _update_symmetry_cursor()).
        self._symmetry_cursor_on = False
        # The disabled overlay widget the pointer is currently over, if any
        # (see _update_disabled_hover_cursor()).
        self._disabled_hover_widget = None

        # Hover feedback (the symmetry handle's highlight and cursor) needs
        # mouseMoveEvent with no button held, which Qt only sends when the
        # widget tracks the mouse. Every other mouse handler already only
        # acts on a pending/active drag, so plain hover moves do nothing
        # else.
        self.setMouseTracking(True)

        # -------------------------------------------------------------
        # Draggable overlay widgets
        # -------------------------------------------------------------
        # Lets overlay widgets (currently just Quick Action buttons/
        # toggles - see sync_quick_actions() below) be repositioned by
        # dragging, the same way picker controls (CircleControl) can be
        # - see eventFilter() below, which does for these widgets what
        # CircleControl's own mousePressEvent/mouseMoveEvent/
        # mouseReleaseEvent do for a control. This only ever changes
        # WHERE these widgets are placed; update_overlay_buttons()'s
        # sizing pass (setFixedSize/fit_font/adjustSize) is untouched.
        #
        # Built via _register_overlay_drag_group() as a general
        # mechanism, so Quick Action buttons (added/removed later by
        # sync_quick_actions() as the Quick Actions list changes) can
        # register/unregister their own groups, instead of needing a
        # fixed key set known up front.
        self.OVERLAY_DRAG_GROUPS = {}
        self._overlay_drag_group_of = {}
        self.overlay_positions = {}

        # Populated by update_overlay_buttons() with whatever overlay
        # widgets it actually positioned/showed on its last call - see
        # the mouseMoveEvent image-drag handling, which uses this to
        # keep dragged buttons from being pushed off-canvas. Empty until
        # the first update_overlay_buttons() call.
        self._overlay_visible_widgets = []

        # Per-group drag bookkeeping, mirroring CircleControl's own
        # _press_pos/dragging/drag_offset fields.
        self._overlay_press_pos = {}
        self._overlay_dragging = {}
        self._overlay_drag_offset = {}

        # Prototype "Quick" actions (see custom_actions_quick.py) -
        # buttons keyed by uid, populated/rebuilt by sync_quick_actions()
        # below (the multi group is filtered to whichever rig is
        # currently loaded; the single group isn't filtered at all).
        self.quick_multi_buttons = {}
        self.quick_multi_labels = {}
        self.quick_single_buttons = {}
        self.quick_single_labels = {}

        # Minimum pixel movement (in canvas coordinates) before a press
        # on an overlay widget is treated as a drag rather than a plain
        # click - matches CircleControl.DRAG_THRESHOLD.
        self.OVERLAY_DRAG_THRESHOLD = 4

        # G-key grab (see begin_grab()): None, or a dict describing the
        # control / overlay group / symmetry line currently being moved
        # by the mouse without a button held.
        self._grab = None
        self._grab_filter = None

    def _register_overlay_drag_group(self, key, widgets):
        """Registers (or re-registers) one draggable overlay group under
        `key` - both the built-in overlay widgets set up above, and each
        Quick Action button sync_quick_actions() creates later. Every
        widget in the group starts hidden, matching the built-in
        widgets' own hide() call right after construction; the next
        update_overlay_buttons() call is what actually shows it.
        """
        self.OVERLAY_DRAG_GROUPS[key] = widgets
        for widget in widgets:
            self._overlay_drag_group_of[widget] = key
            widget.installEventFilter(self)
        if key not in self.overlay_positions:
            self.overlay_positions[key] = None

    def _unregister_overlay_drag_group(self, key):
        """Undoes _register_overlay_drag_group() for a group that no
        longer exists - used when sync_quick_actions() removes a
        Quick Action button. Only drops the group's own drag
        bookkeeping (OVERLAY_DRAG_GROUPS/_overlay_drag_group_of/press-
        drag-offset state) so update_overlay_buttons() and the drag
        event filter stop referencing widgets that are about to be
        deleted - it does NOT touch overlay_positions[key] itself; the
        action being gone for good (not just hidden) means its saved
        canvas position should go with it, which sync_quick_actions()
        handles itself right after calling this, since only it knows
        whether this is a real removal.
        """
        widgets = self.OVERLAY_DRAG_GROUPS.pop(key, ())
        for widget in widgets:
            self._overlay_drag_group_of.pop(widget, None)
            if widget is self._disabled_hover_widget:
                self._clear_disabled_hover_cursor()
        self._overlay_press_pos.pop(key, None)
        self._overlay_dragging.pop(key, None)
        self._overlay_drag_offset.pop(key, None)

    def _default_position_geometry(self):
        """Returns (scale, image_native_height) for working out a new
        button's default spot - or (scale, None) if the image's height
        can't be determined.

        Deliberately NOT just self.background / native_image_scale():
        this runs from sync_quick_actions(), which on a rig/panel switch
        executes BEFORE refresh() has loaded the new panel's image - so
        self.background is then either None (first open) or still the
        PREVIOUS panel's image, and a default computed from it would be
        saved (permanently, see below) against the wrong image. Read the
        panel's own image file's size straight from disk instead (header
        only, not a full decode) and rebuild the same cropped,
        aspect-fit scale set_background()/scaled_background() will end
        up using for it.
        """
        controller = getattr(self.window(), "controller", None)
        if (
            controller is not None
            and controller.armature_name
            and controller.panel_name
            and self.width() > 0
            and self.height() > 0
        ):
            try:
                from .. import json_manager
                path = json_manager.get_image_path(
                    controller.armature_name, controller.panel_name
                )
                if path and os.path.isfile(path):
                    size = QImageReader(path).size()
                    width, height = size.width(), size.height()
                    if width > 0 and height > 0:
                        # Same 0.70-of-height centre crop as
                        # set_background() - height itself is unchanged.
                        crop_width = max(1, min(width, int(height * 0.70)))
                        scale = min(
                            self.width() / crop_width, self.height() / height
                        )
                        return scale, height
            except Exception:
                pass

        if self.background is not None and self.background.height() > 0:
            return self.native_image_scale(), self.background.height()
        return self.native_image_scale(), None

    def _restore_removed_overlay_position(self, key, controller):
        """If button `key` was removed earlier this session (see
        json_manager._REMOVED_OVERLAY_POSITIONS), gives it back its saved
        position: the loaded panel's own goes into the canvas and
        controller.data, the other panels' straight into the stored data.
        Returns True if the loaded panel's position was restored, False
        if there was nothing to restore for it (the caller then assigns
        the usual default)."""
        from .. import json_manager

        positions = json_manager.take_removed_overlay_positions(key)
        if not positions:
            return False

        mine = positions.pop((controller.armature_name, controller.panel_name), None)
        json_manager.restore_overlay_positions(key, positions)
        if mine is None:
            return False

        self.overlay_positions[key] = QPoint(mine[0], mine[1])
        controller.data.setdefault("overlay_positions", {})[key] = [mine[0], mine[1]]
        controller.save()
        return True

    def _assign_default_custom_action_position(self, key):
        """Gives a newly created custom action button a permanent,
        absolute default position the moment it's created, and
        persists it right away exactly like a user drag would (see
        _drag_overlay_group() below) - rather than leaving it with no
        saved position (overlay_positions[key] left None), which is
        what update_overlay_buttons() reads as "use the default
        stacked layout", computed fresh from whichever rows currently
        happen to precede it.

        That's exactly what was still causing OTHER custom action
        buttons to shift position when one was removed, even after
        keying everything by uid instead of index: an un-dragged
        button's default spot isn't fixed, it's "wherever the rows
        above it currently end" - so removing an earlier row moved it
        up to fill the gap. Assigning (and saving) a real position here
        the moment a button is first created means every button
        behaves like a user-dragged one from then on, and
        update_overlay_buttons() never falls back to recomputing its
        position from the current row order again - so it's
        unaffected by any other row appearing, disappearing, or
        reordering later.

        No-op if this key already has a saved position (e.g. this
        canvas already ran this once for this uid earlier in the
        session).
        """
        if self.overlay_positions.get(key) is not None:
            return

        # self.overlay_positions is only a per-canvas in-memory cache -
        # refresh() is what repopulates it from the current rig's own
        # saved data (self.data / controller.data), but
        # sync_quick_actions() (which calls this) now runs BEFORE
        # refresh() on a rig switch (see Controller.load_armature()),
        # so a button that's simply becoming visible again on a rig
        # it was already positioned on - not a genuinely new button -
        # would otherwise find nothing here yet and get a brand new
        # default computed and saved on top of its real saved position.
        # Check the authoritative per-rig store directly first, so a
        # real saved position is restored instead of overwritten.
        controller = getattr(self.window(), "controller", None)
        if controller is not None:
            saved = (controller.data.get("overlay_positions") or {}).get(key)
            if saved:
                self.overlay_positions[key] = QPoint(saved[0], saved[1])
                return

            # A button that was removed earlier this session and has just
            # come back (undo/redo of the removal): put it where it was.
            if self._restore_removed_overlay_position(key, controller):
                return

        scale, image_native_height = self._default_position_geometry()

        # No image on this panel yet (button added BEFORE Capture View):
        # there's nothing to anchor "bottom-left" to, and saving the old
        # top-left fallback here would make it PERMANENT - so after a
        # later Capture View the button stayed stuck at the top-left
        # instead of the bottom-left default. Leave the position unset;
        # update_overlay_buttons() assigns the real bottom-left default
        # as soon as an image exists (see _assign_pending_default_positions()).
        if image_native_height is None:
            return

        button_scale = max(0.1, scale) * 2.0
        margin = round(10 * button_scale)
        spacing = round(4 * button_scale)
        base_h = max(8, round(self._overlay_button_base_height * button_scale))
        step = base_h + spacing

        # Default spot: the BOTTOM-LEFT corner of the image. The first
        # button goes in the very bottom-left corner, and each one added
        # after it stacks directly ABOVE the previous one - never below -
        # so the column grows upward. Just a reasonable, non-overlapping
        # starting spot; unlike the old cascading-stack default, this is
        # never recomputed from row order again once assigned.
        already_placed = sum(
            1
            for k, pos in self.overlay_positions.items()
            if k.startswith("custom_action_") and pos is not None
        )
        image_height = image_native_height * scale
        # Top edge of the bottom-most slot, then up one step per
        # button already placed. Clamped so a very tall stack stops
        # at the image's top margin instead of leaving the image.
        y = image_height - margin - base_h - already_placed * step
        y = max(margin, y)

        # Convert display pixels -> the overlay's native-image space using
        # the SAME `scale` y/margin were computed with above. Not
        # canvas_to_overlay_position(): that divides by
        # native_image_scale() of whatever is on the canvas right now,
        # which at this point can be 1.0 (no image yet) or the previous
        # panel's scale - see _default_position_geometry().
        safe_scale = max(0.0001, scale)
        overlay_pos = QPoint(round(margin / safe_scale), round(y / safe_scale))
        self.overlay_positions[key] = overlay_pos

        controller = getattr(self.window(), "controller", None)
        if controller is not None:
            controller.data.setdefault("overlay_positions", {})[key] = [
                overlay_pos.x(),
                overlay_pos.y(),
            ]
            controller.save()

    def _assign_pending_default_positions(self, multi_uids, single_uids):
        """Assigns the bottom-left default to any Quick button that has
        no saved position yet (created before the panel had an image).
        Runs in creation order so the stack grows upward as usual."""
        keys = [f"custom_action_quick_multi_{uid}" for uid in multi_uids]
        keys += [f"custom_action_quick_single_{uid}" for uid in single_uids]
        for key in keys:
            if self.overlay_positions.get(key) is None:
                self._assign_default_custom_action_position(key)

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

        The overlay BUTTONS (custom action buttons/toggles), unlike the
        picker controls, were never given an image-space "native size"
        via
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
        # Clamped like canvas_to_overlay_position() below: image_scale() is
        # a ratio of scaled widths and comes back 0.0 when the canvas has
        # been squeezed small enough that the scaled background rounds to
        # zero width, which raised ZeroDivisionError out of the mouse
        # handler mid-drag.
        scale = max(0.0001, self.image_scale())
        return QPoint(
            round((position.x() - self.image_x) / scale),
            round((position.y() - self.image_y) / scale),
        )

    def canvas_to_overlay_position(self, position):
        """Convert a canvas point to the overlay widgets' (custom
        action buttons/toggles) own "native image space" - the same
        conversion as
        canvas_to_image_position() above, but built on
        native_image_scale() instead of image_scale(). The overlay
        widgets' sizing/margins in update_overlay_buttons() are already
        computed from native_image_scale() (see that method's
        docstring for why), so a dragged position has to be stored in
        that same space to track the background image the same way
        their existing default stacked position already does."""
        scale = max(0.0001, self.native_image_scale())
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

    def _other_picker_controls(self, control):
        """All currently-placed CircleControls besides `control` itself -
        the set resolve_control_collision() below has to avoid landing
        on top of. Goes through self.control_list.controls (the same
        source of truth layout_controls() uses) rather than
        findChildren(), falling back to findChildren() only if this
        canvas somehow has no ControlList yet, matching the pattern
        layout_controls() already uses above.
        """
        controls = (
            self.control_list.controls.values()
            if self.control_list is not None
            else self.findChildren(CircleControl)
        )
        return [c for c in controls if c is not control]

    def resolve_control_collision(self, control, x, y, hold=None):
        """Given a drag's candidate canvas position (x, y) for `control`,
        push it off whichever other control it's currently overlapping,
        looking only at this one candidate position - not the mouse's
        path, its direction, or `control`'s previous position at all.

        Canvas bounds are part of the resolution, not a separate step
        before it: the candidate is clamped on the way in (a symmetry
        snap upstream can leave it outside), and every push out of an
        obstacle is clamped too. For each pass: build `control`'s hit
        rect at (x, y), find any other control it overlaps, and shove
        the candidate out along whichever axis (x or y) has the smaller
        overlap, by exactly that overlap amount, away from that
        control's center. If that shove would leave the canvas (a
        control against the left edge can't be pushed further left),
        clamping it back in would land it on the same obstacle again,
        so the other three sides of the obstacle are tried, nearest
        first, keeping the first one that is both in bounds and clear.
        Repeats a few times in case pushing clear of one control lands
        on top of another.

        The result is always inside the canvas and clear of every
        obstacle. If no such position exists, `hold` (the last accepted
        position; default: the control's current one) is returned
        instead - never an out-of-bounds or overlapping result.
        """
        width, height = control.width(), control.height()
        # A control wider/taller than the canvas can only sit at 0.
        max_x = max(0, self.width() - width)
        max_y = max(0, self.height() - height)

        def clamp(px, py):
            return min(max(px, 0), max_x), min(max(py, 0), max_y)

        x, y = clamp(x, y)

        # Collision is shape-accurate: the dragged control is its real
        # outline (circle / triangle / square / rectangle) and so is every
        # obstacle - see collision_shapes.py. Quick Action buttons and
        # toggles are rectangles.
        kind = getattr(control, "shape", None)

        def mine(px, py):
            return shapes.shape_for(kind, px, py, width, height)

        others = [
            self._collision_shape(c)
            for c in self._other_picker_controls(control)
        ]
        others += self._overlay_obstacle_shapes()
        if not others:
            return round(x), round(y)

        # Bounded: each pass clears one overlap, but a diagonal push out
        # of a circle/triangle can land on a different obstacle, so allow
        # a couple of extra passes before giving up and holding still.
        for _ in range(2 * len(others) + 2):
            here = mine(x, y)

            blocking = None
            for obstacle in others:
                if shapes.overlaps(here, obstacle):
                    blocking = obstacle
                    break

            if blocking is None:
                break

            depth, nx, ny = shapes.penetration(here, blocking)

            # Preferred escape: shove along the shortest separating
            # direction (axis-aligned for rectangles, along the contact
            # normal for circles/triangles so they slide around each other).
            preferred = (x + nx * depth, y + ny * depth)

            # Fallbacks: park against each side of the obstacle's bounding
            # box (always clear of the shape), nearest to the candidate
            # first.
            ol, ot, oright, obottom = shapes.bounds(blocking)
            sides = sorted(
                [(ol - width, y), (oright, y), (x, ot - height), (x, obottom)],
                key=lambda p: abs(p[0] - x) + abs(p[1] - y),
            )

            for cand in [preferred] + sides:
                cx, cy = clamp(*cand)
                if not shapes.overlaps(mine(cx, cy), blocking):
                    x, y = cx, cy
                    break
            else:
                # Every side of this obstacle is outside the canvas or
                # still on it - there's nowhere valid to go.
                return hold if hold is not None else (control.x(), control.y())

        x, y = round(x), round(y)

        # Same rule as _resolve_overlay_collision(): if pushing out of one
        # obstacle left it on another (or the pass budget ran out), hold
        # still instead of returning a position that overlaps something.
        final = mine(x, y)
        if any(shapes.overlaps(final, o) for o in others):
            return hold if hold is not None else (control.x(), control.y())

        return x, y

    @staticmethod
    def _sweep_to(start, target, resolve, step=4):
        """Moves from `start` toward `target` in steps of at most `step`
        px, running `resolve(x, y, hold)` -> (x, y) on every step and
        carrying its answer forward (`hold` being the last accepted spot).
        One shared strategy for controls and button groups: a fast drag
        can't land on something, because no single step is big enough to
        get into it - blocked, it slides along the edge. The one exception
        is the end of the drag: if the pointer's own spot is free it goes
        there, so a wall of controls can be crossed (see below).
        Returns the final (x, y)."""
        sx, sy = start
        tx, ty = target
        n = max(1, -(-max(abs(tx - sx), abs(ty - sy)) // step))
        vx, vy = (tx - sx) / n, (ty - sy) / n
        fx, fy = float(sx), float(sy)      # where an unobstructed drag would be
        px, py = sx, sy                    # last accepted spot
        for _ in range(n):
            nx, ny = fx + vx, fy + vy
            cx, cy = round(nx), round(ny)
            rx, ry = resolve(cx, cy, (px, py))
            if (rx, ry) == (cx, cy):
                fx, fy = nx, ny
            else:
                # Blocked/pushed: carry on from where it actually is, with
                # the same fixed step - never a bigger one.
                fx, fy = float(rx), float(ry)
            px, py = rx, ry

        # Blocked on the way (e.g. a row of controls with gaps narrower
        # than the dragged item acts as a wall that sliding can never get
        # past, which left everything on the far side of it unreachable):
        # if the exact spot under the pointer is itself completely free,
        # the item lands there. A target that overlaps something still
        # resolves exactly as before - it never lands on an obstacle.
        if (px, py) != (tx, ty):
            rx, ry = resolve(tx, ty, (px, py))
            if (rx, ry) == (tx, ty):
                return tx, ty
        return px, py

    def _mirror_rect_for(self, control, x, y):
        """(mirror_control, canvas QRect it will land on) if dragging
        `control` to canvas (x, y) also moves a mirror partner (symmetry on),
        else None. Mirrors the math in move_control_from_canvas()."""
        controller = getattr(self.window(), "controller", None)
        if controller is None or not controller.data.get("symmetry"):
            return None
        if self.control_list is None:
            return None

        from ..backend import mirror_name, control_dimensions

        mirror_bone = mirror_name(control.bone_name)
        mirror_control = self.control_list.controls.get(mirror_bone) if mirror_bone else None
        if mirror_control is None:
            return None

        control_width, _ = control_dimensions(control.size, control.shape)
        mirror_width, _ = control_dimensions(mirror_control.size, mirror_control.shape)
        image_pos = self.canvas_to_image_position(QPoint(x, y))
        center = image_pos.x() + control_width / 2.0
        mirror_x = 2 * controller.data["symmetry_x"] - center - mirror_width / 2.0
        scale = self.image_scale()
        rect = QRect(
            round(self.image_x + round(mirror_x) * scale),
            round(self.image_y + image_pos.y() * scale),
            mirror_control.width(),
            mirror_control.height(),
        )
        return mirror_control, rect

    def mirror_collides(self, control, x, y):
        """True if the mirror partner that would move along with `control`
        (see _mirror_rect_for) would land on a picker control or an overlay
        button/toggle. Like the dragged control itself, an obstacle the
        partner already sits on is ignored."""
        info = self._mirror_rect_for(control, x, y)
        if info is None:
            return False
        mirror_control, rect = info
        obstacles = [
            self._collision_shape(c)
            for c in self._other_picker_controls(control)
            if c is not mirror_control and not c.isHidden()
        ] + self._overlay_obstacle_shapes()
        current = self._collision_shape(mirror_control)
        mirror_kind = getattr(mirror_control, "shape", None)
        mirror_shape = shapes.shape_for(
            mirror_kind, rect.x(), rect.y(), rect.width(), rect.height()
        )
        return any(
            shapes.overlaps(mirror_shape, o)
            for o in obstacles
            if not shapes.overlaps(o, current)
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
                    mirror_control.manually_positioned = True

                    mirror_item = controller.find_item(mirror_bone)
                    if mirror_item:
                        mirror_item["x"] = mirror_control.image_position.x()
                        mirror_item["y"] = mirror_control.image_position.y()
                        mirror_item["manually_positioned"] = True

        control.manually_positioned = True

        item = controller.find_item(control.bone_name)
        if item:
            item["x"] = control.image_position.x()
            item["y"] = control.image_position.y()
            item["manually_positioned"] = True

        # Deliberately NOT controller.save() here - this runs on every
        # single mouseMoveEvent while a control is being dragged, and
        # save() is a full synchronous rig_picker_data.json rewrite
        # (serialize the whole file, copy a .bak, and - since
        # json_manager's cross-process lock was added - also acquire an
        # OS-level file lock), dozens of times a second for the length
        # of the drag. In-memory state (control.image_position, item,
        # the mirror control/item above) is kept fully up to date on
        # every move regardless; only the disk write is deferred to
        # once, when the drag actually ends - see
        # CircleControl.mouseReleaseEvent() in circle_control.py, which
        # mirrors how an image/symmetry-line drag already only saves on
        # release (see mouseReleaseEvent below).
        self.layout_controls()

    # -----------------------------------------------------
    # Draggable overlay widgets (custom action buttons/toggles)
    # -----------------------------------------------------

    @safe_override(False)
    def eventFilter(self, obj, event):
        """Lets each overlay widget (custom action buttons/toggles -
        registered via _register_overlay_drag_group() in __init__) be
        dragged to a custom position, the same way a CircleControl drags
        itself in its own mousePressEvent/mouseMoveEvent/
        mouseReleaseEvent.

        Every mouse event for a draggable overlay widget is fully
        handled (and consumed) right here, from press through move to
        release - rather than only stepping in once a drag is detected
        and otherwise leaning on each widget's own default press/
        release handling. Two of these widgets don't play well with
        that "leave it alone until it's a drag" approach: ToggleSwitch
        toggles itself the instant a press lands (see
        ToggleSwitch.mousePressEvent), which would fire at the very
        start of every drag; and QLabel's default mouse handling
        ignores/propagates presses in a way that could hand subsequent
        move/release events to the canvas's own (unrelated) drag
        handling instead of back here. Owning the whole press-move-
        release sequence unconditionally sidesteps both, and a plain
        click (one that never crosses the drag threshold) still ends
        up doing exactly what that widget's own click would have done
        - see the release branch below.
        """
        key = self._overlay_drag_group_of.get(obj)
        if key is None:
            return super().eventFilter(obj, event)

        event_type = event.type()

        # Qt ignores a disabled widget's own cursor and shows its parent's,
        # so hover over a greyed-out button/toggle is tracked here and the
        # hand is put on the canvas for as long as the pointer is on it.
        # (Enter/Leave/Hover events still reach a disabled widget's filter.)
        if event_type in self._OVERLAY_HOVER_EVENTS:
            self._update_disabled_hover_cursor(obj, event_type)
            return False

        if event_type == QEvent.MouseButtonPress:
            if event.button() != Qt.LeftButton:
                return False

            self._overlay_press_pos[key] = event.globalPosition().toPoint()
            self._overlay_dragging[key] = False

            anchor = self.OVERLAY_DRAG_GROUPS[key][0]
            self._overlay_drag_offset[key] = (
                self.mapFromGlobal(event.globalPosition().toPoint()) - anchor.pos()
            )

            if hasattr(obj, "setDown"):
                obj.setDown(True)

            return True

        if event_type == QEvent.MouseMove:
            if key not in self._overlay_press_pos:
                return False

            if not self._overlay_dragging[key]:
                moved = event.globalPosition().toPoint() - self._overlay_press_pos[key]
                if moved.manhattanLength() < self.OVERLAY_DRAG_THRESHOLD:
                    return True

                controller = getattr(self.window(), "controller", None)
                if controller is not None:
                    controller._record_undo_snapshot()

                self._overlay_dragging[key] = True

                # A button that was mid-press when the drag started
                # would otherwise stay visually "pressed" while it's
                # dragged around.
                if hasattr(obj, "setDown"):
                    obj.setDown(False)

            self._drag_overlay_group(key, event.globalPosition().toPoint())
            return True

        if event_type == QEvent.MouseButtonRelease:
            if key not in self._overlay_press_pos:
                return False

            was_dragging = self._overlay_dragging.get(key, False)

            self._overlay_press_pos.pop(key, None)
            self._overlay_dragging.pop(key, None)
            self._overlay_drag_offset.pop(key, None)

            if hasattr(obj, "setDown"):
                obj.setDown(False)

            if not was_dragging and obj.isEnabled():
                # A plain click (never crossed the drag threshold) -
                # perform whatever this widget's own press/release
                # would have done, since that default handling was
                # bypassed above so the widget could be dragged instead.
                if isinstance(obj, ToggleSwitch):
                    obj.set_checked(not obj.is_checked())
                    obj.toggled.emit(obj.is_checked())
                elif isinstance(obj, QPushButton):
                    obj.click()
            elif was_dragging:
                # The one disk write for this whole drag - see
                # _drag_overlay_group()'s own comment for why it's
                # deferred here instead of happening on every move.
                controller = getattr(self.window(), "controller", None)
                if controller is not None:
                    controller.save()

            return True

        return super().eventFilter(obj, event)

    def _overlay_obstacle_rects(self, exclude_key=None):
        """Geometry of every shown overlay widget (Quick Action buttons,
        toggles and their labels) outside the `exclude_key` group."""
        rects = []
        for key, widgets in self.OVERLAY_DRAG_GROUPS.items():
            if key == exclude_key:
                continue
            for w in widgets:
                if not w.isHidden():
                    # Full size on purpose: buttons/toggles/labels must
                    # never overlap each other (or sit on a control the
                    # user drags onto them). COLLISION_INSET only applies
                    # to picker controls.
                    rects.append(QRect(w.geometry()))
        return rects

    @staticmethod
    def _collision_rect(rect):
        """`rect` (a QRect) shrunk by COLLISION_INSET on every side - the
        box other controls/groups actually collide with. Never smaller
        than 1x1 so tiny widgets still block."""
        inset = COLLISION_INSET
        width = max(1, rect.width() - 2 * inset)
        height = max(1, rect.height() - 2 * inset)
        return QRect(
            rect.x() + (rect.width() - width) // 2,
            rect.y() + (rect.height() - height) // 2,
            width,
            height,
        )

    def _collision_shape(self, widget):
        """Collision outline of a picker control or overlay widget: its
        shrunk box (see _collision_rect) drawn as the control's own shape -
        circle, triangle, or a rectangle for square/rectangle controls
        and for buttons/toggles/labels (which have no `shape`)."""
        r = self._collision_rect(QRect(widget.geometry()))
        return shapes.shape_for(
            getattr(widget, "shape", None), r.x(), r.y(), r.width(), r.height()
        )

    def _overlay_obstacle_shapes(self, exclude_key=None):
        """_overlay_obstacle_rects() as (rectangle) shapes."""
        return [
            shapes.rect_shape(r.x(), r.y(), r.width(), r.height())
            for r in self._overlay_obstacle_rects(exclude_key)
        ]

    @staticmethod
    def _rects_overlap(group_rects, obstacles):
        return any(g.intersects(o) for g in group_rects for o in obstacles)

    def _resolve_overlay_collision(self, key, widgets, delta_x, delta_y, clamp, hold=(0, 0)):
        """Keeps a dragged overlay group (moved by delta_x/delta_y) off every
        picker control and every other overlay group, the way
        resolve_control_collision() does for a control: shove the whole
        group out of whatever it overlaps along the axis with the smaller
        overlap.

        An obstacle a widget ALREADY overlaps at its current spot (an
        existing layout, an older save) is handled differently so the
        widget can still be dragged away from it: the overlap may shrink
        or stay the same but never grow. Per axis, the widget may not move
        further into the obstacle than it already is (it is pinned at its
        current depth on that axis, and slides freely on the other one),
        and once it is completely clear the obstacle blocks like any
        other, so it can't be dragged back in. The baseline is the
        widget's position when this call starts - the live geometry, which
        moves with every mouse event - so repeated small drags can't
        creep deeper either. A widget that is merely larger than the
        obstacle (or entirely inside it) on an axis is already at the
        maximum overlap there, so that axis is unrestricted.

        `clamp` is the canvas-bounds clamp (dx, dy) -> (dx, dy); if the
        result still overlaps something after re-clamping, the group
        holds still."""
        current = [QRect(w.geometry()) for w in widgets]
        # Rectangle obstacles (buttons/toggles/labels and square/rectangle
        # controls) keep the per-axis logic below; circle and triangle
        # controls are shape-accurate (collision_shapes.py) and handled
        # by the *_shape passes further down.
        obstacles = []
        shape_obstacles = []
        for c in self._other_picker_controls(None):
            if c.isHidden():
                continue
            if getattr(c, "shape", None) in ("CIRCLE", "TRIANGLE"):
                shape_obstacles.append(self._collision_shape(c))
            else:
                obstacles.append(self._collision_rect(c.geometry()))
        obstacles += self._overlay_obstacle_rects(exclude_key=key)
        if not obstacles and not shape_obstacles:
            return delta_x, delta_y

        def extent(lo, hi, olo, ohi):
            return min(hi, ohi) - max(lo, olo)

        def sticks_out_side(lo, hi, olo, ohi):
            # +1: only the low edge is outside the obstacle (overlap grows
            # by moving toward +); -1: only the high edge is; 0: the
            # widget spans or sits inside the obstacle on this axis, so
            # the overlap is already as large as it can get.
            if lo < olo and hi < ohi:
                return 1
            if lo > olo and hi > ohi:
                return -1
            return 0

        # Per widget: obstacles it isn't touching block outright; ones it
        # already overlaps become (obstacle, overlap_w, overlap_h,
        # side_x, side_y) limits.
        blockers = []
        limited = []
        for g in current:
            blocking, limits = [], []
            for o in obstacles:
                if not g.intersects(o):
                    blocking.append(o)
                    continue
                gl, gr = g.x(), g.x() + g.width()
                gt, gb = g.y(), g.y() + g.height()
                ol, orr = o.x(), o.x() + o.width()
                ot, ob = o.y(), o.y() + o.height()
                limits.append((
                    o,
                    extent(gl, gr, ol, orr), extent(gt, gb, ot, ob),
                    sticks_out_side(gl, gr, ol, orr),
                    sticks_out_side(gt, gb, ot, ob),
                ))
            blockers.append(blocking)
            limited.append(limits)

        # Same idea for the shape-accurate obstacles: not touching ->
        # blocks outright; already overlapped -> (obstacle, starting depth)
        # limit that may shrink but never grow.
        grow_tolerance = 0.75
        shape_blockers = []
        shape_limited = []
        for g in current:
            gs = shapes.rect_shape(g.x(), g.y(), g.width(), g.height())
            blocking, limits = [], []
            for o in shape_obstacles:
                depth = shapes.depth_of(gs, o)
                if depth < shapes.MIN_PENETRATION:
                    blocking.append(o)
                else:
                    limits.append((o, depth))
            shape_blockers.append(blocking)
            shape_limited.append(limits)

        def moved(dx, dy):
            return [r.translated(dx, dy) for r in current]

        def as_shape(r):
            return shapes.rect_shape(r.x(), r.y(), r.width(), r.height())

        def shape_grew_by(r, limit):
            o, base_depth = limit
            extra = shapes.depth_of(as_shape(r), o) - base_depth
            return extra if extra > grow_tolerance else 0.0

        def deeper(g, limit):
            """(grew_x, grew_y) if `g` overlaps the limit's obstacle more
            than it did at the start of this call, else None."""
            o, base_w, base_h, _, _ = limit
            if not g.intersects(o):
                return None
            grew_x = extent(g.x(), g.x() + g.width(), o.x(), o.x() + o.width()) > base_w
            grew_y = extent(g.y(), g.y() + g.height(), o.y(), o.y() + o.height()) > base_h
            return (grew_x, grew_y) if (grew_x or grew_y) else None

        def overlaps(rects):
            return any(
                self._rects_overlap([g], obs) for g, obs in zip(rects, blockers)
            ) or any(
                deeper(g, lim) for g, lims in zip(rects, limited) for lim in lims
            ) or any(
                shapes.overlaps(as_shape(g), o)
                for g, obs in zip(rects, shape_blockers) for o in obs
            ) or any(
                shape_grew_by(g, lim)
                for g, lims in zip(rects, shape_limited) for lim in lims
            )

        pairs = sum(
            len(b) + len(lm) + len(sb) + len(sl)
            for b, lm, sb, sl in zip(blockers, limited, shape_blockers, shape_limited)
        )
        for _ in range(2 * (pairs + 1) + 2):
            rects = moved(delta_x, delta_y)
            hit = next(
                (
                    (g, o)
                    for g, obs in zip(rects, blockers)
                    for o in obs
                    if g.intersects(o)
                ),
                None,
            )
            if hit is not None:
                g, o = hit
                left, right = g.x(), g.x() + g.width()
                top, bottom = g.y(), g.y() + g.height()
                oleft, oright = o.x(), o.x() + o.width()
                otop, obottom = o.y(), o.y() + o.height()
                overlap_x = min(right, oright) - max(left, oleft)
                overlap_y = min(bottom, obottom) - max(top, otop)
                if overlap_x < overlap_y:
                    delta_x += -overlap_x if (left + right) < (oleft + oright) else overlap_x
                else:
                    delta_y += -overlap_y if (top + bottom) < (otop + obottom) else overlap_y
                continue

            # Circle / triangle obstacles: shove out along the contact
            # normal (shortest separating direction).
            shape_hit = next(
                (
                    (g, o)
                    for g, obs in zip(rects, shape_blockers)
                    for o in obs
                    if shapes.overlaps(as_shape(g), o)
                ),
                None,
            )
            if shape_hit is not None:
                g, o = shape_hit
                depth, nx, ny = shapes.penetration(as_shape(g), o)
                delta_x += nx * depth
                delta_y += ny * depth
                continue

            # No new overlap. Now stop any axis of an existing overlap from
            # getting deeper: pin the delta back to the starting depth.
            grew = next(
                (
                    (axes, lim)
                    for g, lims in zip(rects, limited)
                    for lim in lims
                    for axes in [deeper(g, lim)]
                    if axes is not None
                ),
                None,
            )
            if grew is None:
                # Existing overlaps with circle/triangle obstacles: push
                # back out by exactly how much deeper they got.
                shape_grew = next(
                    (
                        (g, lim)
                        for g, lims in zip(rects, shape_limited)
                        for lim in lims
                        if shape_grew_by(g, lim)
                    ),
                    None,
                )
                if shape_grew is None:
                    break
                g, lim = shape_grew
                depth, nx, ny = shapes.penetration(as_shape(g), lim[0])
                delta_x += nx * (depth - lim[1])
                delta_y += ny * (depth - lim[1])
                continue
            (grew_x, grew_y), lim = grew
            side_x, side_y = lim[3], lim[4]
            if grew_x:
                delta_x = min(delta_x, 0) if side_x > 0 else max(delta_x, 0)
            if grew_y:
                delta_y = min(delta_y, 0) if side_y > 0 else max(delta_y, 0)

        delta_x, delta_y = clamp(delta_x, delta_y)
        delta_x, delta_y = round(delta_x), round(delta_y)
        if overlaps(moved(delta_x, delta_y)):
            return hold
        return delta_x, delta_y

    def _drag_overlay_group(self, key, global_pos):
        """Moves every widget in OVERLAY_DRAG_GROUPS[key] together, by
        the same delta, and persists the group's new position - the
        overlay-widget equivalent of move_control_from_canvas() above.

        The group's bounding box is kept inside the canvas. A canvas too
        narrow/short to hold it is handled per axis: see "Small-canvas
        policy" below.
        """
        widgets = self.OVERLAY_DRAG_GROUPS[key]
        anchor = widgets[0]

        parent_pos = self.mapFromGlobal(global_pos)
        offset = self._overlay_drag_offset[key]
        target_anchor_pos = parent_pos - offset

        # Clamp the whole group's bounding box inside the canvas, not
        # just the anchor widget - otherwise a wide group (e.g. a
        # Toggle's switch flanked by its name label) could push its
        # trailing widget off the edge while the anchor itself still
        # reads as "inside".
        group_left = min(w.x() for w in widgets)
        group_top = min(w.y() for w in widgets)
        group_right = max(w.x() + w.width() for w in widgets)
        group_bottom = max(w.y() + w.height() for w in widgets)
        group_width = group_right - group_left
        group_height = group_bottom - group_top

        delta_x = target_anchor_pos.x() - anchor.x()
        delta_y = target_anchor_pos.y() - anchor.y()

        # Small-canvas policy: an axis along which the group is larger
        # than the canvas has no valid position (every placement leaves
        # part of it outside), so the group is locked on that axis - it
        # keeps its position there and can still be dragged along the
        # other axis if that one fits. If neither fits, it can't be
        # dragged at all. Nothing is resized or moved on its own, and
        # nothing is persisted for a drag that moved nothing; the
        # button/toggle is usable again once the canvas is big enough.
        fits_x = group_width <= self.width()
        fits_y = group_height <= self.height()
        if not (fits_x or fits_y):
            return

        def clamp(dx, dy):
            if fits_x:
                new_left = max(0, min(group_left + dx, self.width() - group_width))
            else:
                new_left = group_left
            if fits_y:
                new_top = max(0, min(group_top + dy, self.height() - group_height))
            else:
                new_top = group_top
            return new_left - group_left, new_top - group_top

        delta_x, delta_y = clamp(delta_x, delta_y)
        delta_x, delta_y = self._sweep_to(
            (0, 0), (delta_x, delta_y),
            lambda dx, dy, hold: self._resolve_overlay_collision(
                key, widgets, dx, dy, clamp, hold
            ),
        )

        for widget in widgets:
            widget.move(widget.x() + delta_x, widget.y() + delta_y)

        self.overlay_positions[key] = self.canvas_to_overlay_position(
            QPoint(anchor.x(), anchor.y())
        )

        controller = getattr(self.window(), "controller", None)
        if controller is None:
            return

        overlay_pos = self.overlay_positions[key]
        controller.data.setdefault("overlay_positions", {})[key] = [
            overlay_pos.x(),
            overlay_pos.y(),
        ]
        # Not controller.save() here - see the matching comment in
        # move_control_from_canvas() above. This runs on every
        # mouseMoveEvent for the length of the drag; the actual disk
        # write happens once, on release, in eventFilter()'s
        # MouseButtonRelease branch below.

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

        # update_overlay_buttons() calls fit_font() for every overlay
        # button on every sync (add/remove/rename/run/zoom) - not just
        # the one that actually changed. Without this cache, renaming
        # one button re-measures every OTHER button's font too, each
        # one doing a from-scratch pixel-size search below - all
        # synchronously inside the triggering operator's execute(),
        # which is why a rename could visibly stall the whole UI
        # (including Blender's own N-panel, which can't repaint until
        # that execute() call returns) on rigs with many buttons.
        # Text/width/height are the only inputs that affect the result,
        # so if none of them moved since the last fit, the previous
        # answer is still correct - skip the search entirely.
        cache_key = (text, btn.width(), btn.height())
        if getattr(btn, "_fit_font_cache_key", None) == cache_key:
            return

        font = QFont(btn.font())

        def fits(size):
            font.setPixelSize(max(4, size))
            metrics = QFontMetrics(font)
            return (
                metrics.horizontalAdvance(text) <= available_width
                and metrics.height() <= available_height
            )

        # Binary search instead of a 1-by-1 linear scan down from the
        # starting size - same result (the largest pixel size that
        # fits, or the 4px floor if nothing does), since "does size S
        # fit" only ever flips from True to False as S grows, but in
        # O(log height) QFontMetrics measurements instead of O(height).
        max_size = max(4, int(btn.height() * 0.55))

        # Absolute floor so a font is always applied, even for extremely
        # small buttons where nothing fits perfectly - matches btn.setFont()
        # always being called below instead of sometimes being skipped.
        best_size = 4

        lo, hi = 4, max_size
        while lo <= hi:
            mid = (lo + hi) // 2
            if fits(mid):
                best_size = mid
                lo = mid + 1
            else:
                hi = mid - 1

        # Shrink slightly below the max-that-fits so there's always some
        # visible breathing room between the text and the button border,
        # instead of the text touching the edges.
        best_size = max(4, round(best_size * 0.8))

        font.setPixelSize(best_size)
        btn.setFont(font)
        btn._fit_font_cache_key = cache_key
    @staticmethod
    def _set_overlay_button_enabled(btn, enabled):
        """setEnabled() for a Quick Action button or toggle switch, keeping
        the hand cursor on it. Note Qt ignores a DISABLED widget's own
        cursor (it shows the parent's), so the hand over a greyed-out one
        comes from PickerCanvas._update_disabled_hover_cursor(); this call
        covers the enabled case and keeps the cursor set for when it is
        enabled again."""
        btn.setEnabled(enabled)
        btn.setCursor(Qt.PointingHandCursor)

    @staticmethod
    def _apply_action_problem(btn, name_label, problem):
        """Tooltip + warning look for a button whose configuration can't
        work (see update_quick_*_actions_enabled()). `problem` is "" when
        there is none, which clears both. A Toggle is a ToggleSwitch + a
        label; the label carries the styling (the switch paints itself),
        both carry the tooltip."""
        styled = name_label if name_label is not None else btn
        for widget in (btn, name_label):
            if widget is not None and widget.toolTip() != problem:
                widget.setToolTip(problem)
        if bool(styled.property("actionProblem")) != bool(problem):
            styled.setProperty("actionProblem", bool(problem))
            styled.style().unpolish(styled)
            styled.style().polish(styled)

    def update_quick_single_actions_enabled(self, enabled_by_uid, problems_by_uid=None):
        """Applies an enabled/disabled state to a Quick single-command
        button in custom_actions_quick.py. Same setEnabled()-only
        approach for the state. The Controller passes True for every
        button except a malformed one (2+ commands), which comes with a
        problem text shown as a tooltip + warning look.
        """
        problems_by_uid = problems_by_uid or {}
        for uid, enabled in enabled_by_uid.items():
            btn = self.quick_single_buttons.get(uid)
            if btn is None:
                continue
            self._set_overlay_button_enabled(btn, enabled)

            name_label = self.quick_single_labels.get(uid)
            if name_label is not None:
                name_label.setEnabled(enabled)

            self._apply_action_problem(btn, name_label, problems_by_uid.get(uid, ""))

    def update_quick_multi_actions_enabled(self, enabled_by_uid, problems_by_uid=None):
        """Applies the disabled/neutral look to a Quick multi-command
        button in custom_actions_quick.py.
        Same setEnabled()-only approach for the enabled state.

        problems_by_uid: {uid: text} for buttons whose rules reference
        Control shape bones the rig no longer has. Those get the text as a
        tooltip (Qt still shows a tooltip on a disabled widget - which is
        exactly when it's needed: the button can't enable because of it) and
        an "actionProblem" property the stylesheet turns into a warning look.
        """
        problems_by_uid = problems_by_uid or {}
        for uid, enabled in enabled_by_uid.items():
            btn = self.quick_multi_buttons.get(uid)
            if btn is None:
                continue
            self._set_overlay_button_enabled(btn, enabled)

            name_label = self.quick_multi_labels.get(uid)
            if name_label is not None:
                name_label.setEnabled(enabled)

            self._apply_action_problem(btn, name_label, problems_by_uid.get(uid, ""))

    def _sync_quick_action_group(self, actions, buttons_dict, labels_dict, key_prefix, run_uid_callback, existing_uids=None):
        """Shared body behind sync_quick_actions() below - reconciles one
        button group (either the Quick multi-command group or the Quick
        single-command group) against exactly the `actions` list passed
        in. Parameterized so it can run twice with different
        data/dicts/key prefixes/click targets instead of being copy-
        pasted. `actions` is assumed to already be the correct visible
        set - sync_quick_actions() below does the rig filtering for the
        multi group before calling this.

        existing_uids: the full set of uids that still exist in the
        underlying scene data, REGARDLESS of rig - only needed when
        `actions` is itself a rig-filtered subset (the multi group).
        A widget whose uid drops out of `actions` but is still in
        existing_uids isn't gone, it's just not for this rig right
        now, so its saved canvas position (self.overlay_positions /
        controller.data / the on-disk json) is left alone - only a
        uid missing from existing_uids entirely (a real Remove Button
        click) gets that position data erased. Without this
        distinction, switching rigs back and forth repeatedly wiped
        and recomputed a fresh default position every time, which
        could land exactly on top of another button and make the
        original look like it had vanished. Defaults to `live_uids`
        itself (every non-live uid counts as really removed) for the
        single group, which is never rig-filtered.
        """
        live_uids = {action.uid for action in actions}
        if existing_uids is None:
            existing_uids = live_uids

        for uid in list(buttons_dict.keys()):
            if uid not in live_uids:
                btn = buttons_dict.pop(uid)
                name_label = labels_dict.pop(uid, None)
                key = f"{key_prefix}{uid}"
                self._unregister_overlay_drag_group(key)

                if uid not in existing_uids:
                    # Genuinely removed (not just hidden for this rig) -
                    # its saved position should go with it.
                    self.overlay_positions.pop(key, None)
                    from .. import json_manager
                    controller = getattr(self.window(), "controller", None)
                    if controller is not None:
                        # Keep the position of the loaded panel before it
                        # goes, so undoing the removal can put the button
                        # back exactly where it was (the stored panels are
                        # remembered by remove_overlay_position() itself).
                        position = controller.data.get(
                            "overlay_positions", {}
                        ).pop(key, None)
                        json_manager.remember_removed_overlay_position(
                            key,
                            controller.armature_name,
                            controller.panel_name,
                            position,
                        )
                        controller.save()
                    json_manager.remove_overlay_position(key)

                for widget in (name_label, btn):
                    if widget is None:
                        continue
                    widget.removeEventFilter(self)
                    widget.hide()
                    widget.deleteLater()

        for action in actions:
            is_toggle = getattr(action, 'action_type', 'BUTTON') == 'TOGGLE'
            label = action.name or ("Toggle" if is_toggle else "Button")

            btn = buttons_dict.get(action.uid)
            if btn is None:
                if is_toggle:
                    name_label = QLabel(label, self)
                    name_label.setObjectName("ikfkOverlayLabel")
                    name_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    name_label.setCursor(Qt.PointingHandCursor)
                    name_label.ensurePolished()
                    name_label.adjustSize()
                    name_label._base_width = name_label.width()
                    name_label._base_height = name_label.height()

                    btn = ToggleSwitch(checked=action.toggle_state, parent=self)
                    btn.ensurePolished()
                    btn._base_width = btn.width()
                    btn._base_height = btn.height()

                    btn.toggled.connect(
                        lambda checked=False, uid=action.uid: run_uid_callback(uid)
                    )

                    labels_dict[action.uid] = name_label
                    buttons_dict[action.uid] = btn
                    key = f"{key_prefix}{action.uid}"
                    self._register_overlay_drag_group(key, (name_label, btn))
                    self._assign_default_custom_action_position(key)
                else:
                    btn = QPushButton(label, self)
                    btn.setObjectName("ikfkOverlayButton")
                    # Same hand cursor the picker's controls and the
                    # Toggle switch show (see CircleControl / ToggleSwitch).
                    btn.setCursor(Qt.PointingHandCursor)

                    btn.ensurePolished()
                    btn.adjustSize()
                    btn._base_width = btn.width() + BUTTON_TEXT_HORIZONTAL_PADDING
                    btn._base_height = self._overlay_button_base_height

                    f = btn.font()
                    if f.pixelSize() > 0:
                        btn._base_font_px = f.pixelSize()
                    else:
                        btn._base_font_px = round(
                            f.pointSizeF() * self.logicalDpiY() / 72
                        )

                    btn.clicked.connect(
                        lambda checked=False, uid=action.uid: run_uid_callback(uid)
                    )

                    buttons_dict[action.uid] = btn
                    key = f"{key_prefix}{action.uid}"
                    self._register_overlay_drag_group(key, (btn,))
                    self._assign_default_custom_action_position(key)
            elif is_toggle:
                name_label = labels_dict.get(action.uid)
                if name_label is not None and name_label.text() != label:
                    name_label.setText(label)
                    name_label.setMinimumSize(0, 0)
                    name_label.setMaximumSize(16777215, 16777215)
                    name_label.adjustSize()
                    name_label._base_width = name_label.width()
                btn.set_checked(action.toggle_state)
            else:
                if btn.text() != label:
                    btn.setText(label)
                    btn._base_width = (
                        btn.sizeHint().width() + BUTTON_TEXT_HORIZONTAL_PADDING
                    )

    def sync_quick_actions(self):
        """Rebuilds this canvas's Quick Action buttons - draggable
        overlay widgets for the two button groups added in custom_actions_quick.py
        (see that module's own docstring). Multi-command buttons are
        filtered here to whichever (rig, panel) pair is currently
        loaded in this window; single-command buttons are never
        filtered - they show on every rig and every panel.
        """
        settings = getattr(bpy.context.scene, "rp_quick_action_settings", None)
        if settings is None:
            return

        for action in settings.multi_actions:
            if not action.uid:
                action.uid = uuid.uuid4().hex
        for action in settings.single_actions:
            if not action.uid:
                action.uid = uuid.uuid4().hex

        from .. import custom_actions_quick

        controller = getattr(self.window(), "controller", None)
        current_rig_name = controller.armature_name if controller is not None else None
        current_panel_name = controller.panel_name if controller is not None else None

        all_multi_uids = {action.uid for action in settings.multi_actions}
        visible_multi = [
            action for action in settings.multi_actions
            if custom_actions_quick._quick_multi_action_visible(
                settings, action, current_rig_name, current_panel_name
            )
        ]

        self._sync_quick_action_group(
            visible_multi, self.quick_multi_buttons, self.quick_multi_labels,
            "custom_action_quick_multi_", self._run_quick_multi_action,
            existing_uids=all_multi_uids,
        )
        self._sync_quick_action_group(
            list(settings.single_actions), self.quick_single_buttons, self.quick_single_labels,
            "custom_action_quick_single_", self._run_quick_single_action,
        )

        self.update_overlay_buttons()

        if controller is not None:
            controller.refresh_quick_single_action_enabled_states()
            controller.refresh_quick_multi_action_enabled_states()

    def _run_quick_multi_action(self, action_uid):
        self._run_quick_action("multi_actions", "quick_run_multi_action", action_uid)

    def _run_quick_single_action(self, action_uid):
        self._run_quick_action("single_actions", "quick_run_single_action", action_uid)

    def _run_quick_action(self, collection_name, operator_name, action_uid):
        """Runs a Quick button's operator on behalf of a click in the picker
        window. The operator reports a failing COMMAND itself; this reports
        anything that goes wrong AROUND invoking it (poll failure, a
        RuntimeError out of bpy.ops, the context override or rig lookup
        raising) via custom_actions_quick.report_quick_invocation_failure()
        - traceback on the console, one-line ERROR in Blender - instead of
        swallowing it so the click just silently does nothing.
        """
        from .. import custom_actions_quick

        settings = getattr(bpy.context.scene, "rp_quick_action_settings", None)
        action_index = None
        action_name = ""
        if settings is not None:
            for i, action in enumerate(getattr(settings, collection_name)):
                if action.uid == action_uid:
                    action_index = i
                    action_name = action.name
                    break
        if action_index is None:
            # The button was removed/renamed between this canvas being drawn
            # and the click (a stale widget) - nothing to run. Redraw so it
            # stops being offered, rather than leave a dead button.
            self.sync_quick_actions()
            return

        try:
            # Same context-override reasoning as _run_custom_action() above:
            # a command that itself calls bpy.ops.pose.* polls against a
            # real VIEW_3D area/region, which this floating picker window
            # doesn't have one of its own - without this override those
            # pose ops fail their poll (e.g. "No object, or not exclusively
            # in pose mode") even though the exact same command runs fine
            # from the N-panel, which already lives inside a VIEW_3D region.
            from .. import backend

            rig = backend.arm()
            override = backend.get_3d_override(bpy.context, rig) if rig else None
            run = getattr(bpy.ops.rp, operator_name)
            if override:
                with bpy.context.temp_override(**override):
                    run(action_index=action_index)
            else:
                run(action_index=action_index)
        except Exception as exc:
            custom_actions_quick.report_quick_invocation_failure(action_name, exc)
        self.sync_quick_actions()

    def update_overlay_buttons(self):

        # Each entry is a row; a row with more than one button lays them
        # out side by side instead of stacking them.
        rows = ()

        # Prototype "Quick" action buttons (see custom_actions_quick.py
        # and sync_quick_actions() above). A TOGGLE action's row is
        # (name label, switch); a BUTTON action's row is just its
        # single QPushButton. Iterates the dicts directly (insertion
        # order, i.e. the order each button was first created in)
        # rather than sorting by key - the keys are uids, not indices,
        # so they don't have a meaningful sort order, and insertion
        # order already stays put across an unrelated removal/rename
        # elsewhere in the list, which is exactly what keeps an
        # un-dragged button's default stacked row from shifting when a
        # *different* action is removed.
        quick_multi_rows = tuple(
            (self.quick_multi_labels[uid], btn) if uid in self.quick_multi_labels else (btn,)
            for uid, btn in self.quick_multi_buttons.items()
        )
        quick_single_rows = tuple(
            (self.quick_single_labels[uid], btn) if uid in self.quick_single_labels else (btn,)
            for uid, btn in self.quick_single_buttons.items()
        )
        rows = rows + quick_multi_rows + quick_single_rows

        all_buttons = [btn for row in rows for btn in row]

        if self.background is None:
            for btn in all_buttons:
                btn.hide()
            self._overlay_visible_widgets = []
            return

        # Buttons created while this panel had no image yet were left
        # without a position (see _assign_default_custom_action_position());
        # now that an image exists, give them the bottom-left default.
        self._assign_pending_default_positions(
            list(self.quick_multi_buttons.keys()),
            list(self.quick_single_buttons.keys()),
        )

        # Snapshot of whatever's actually going to be shown/positioned
        # below, so callers like mouseMoveEvent's image-drag handling can
        # check where these widgets ended up (after this method
        # positions them) without having to re-derive "which buttons are
        # currently visible" themselves.
        self._overlay_visible_widgets = [btn for row in rows for btn in row]

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

        # A TOGGLE action's (label, switch) row reads as one compound
        # control, not two separate ones - so it sits flush together
        # with no gap, unlike the spacing between separate buttons.
        # tight_row_set keys off membership below, so every label+switch
        # row gets the same treatment.
        tight_spacing = 0
        tight_row_set = {
            row for row in quick_multi_rows if len(row) == 2
        } | {
            row for row in quick_single_rows if len(row) == 2
        }

        # Maps each draggable row to the OVERLAY_DRAG_GROUPS key that
        # tracks its custom (dragged) position, so the position pass
        # below can start that row at its saved spot instead of the
        # default stacked one, purely as an alternate starting point -
        # everything above (sizes/fonts/spacing) is computed exactly as
        # before, regardless of which starting point ends up used.
        draggable_rows = {}
        for row, (uid, btn) in zip(quick_multi_rows, self.quick_multi_buttons.items()):
            draggable_rows[row] = f"custom_action_quick_multi_{uid}"
        for row, (uid, btn) in zip(quick_single_rows, self.quick_single_buttons.items()):
            draggable_rows[row] = f"custom_action_quick_single_{uid}"

        x = self.image_x + margin
        y = self.image_y + margin

        for row in rows:

            row_x = x
            row_spacing = tight_spacing if row in tight_row_set else spacing

            # First pass: resize every widget in the row (and fit its
            # font, where applicable) so we know the row's tallest
            # widget before positioning anything.
            sizes = []
            for btn in row:

                if getattr(btn, "_font_locked", False):
                    # Font/height are already locked (see below) - this
                    # button's _base_width now holds the exact final
                    # pixel width for its current (locked, unchanging)
                    # font, not a scale=1 reference to multiply - a
                    # rename writes a fresh value here (measured against
                    # this same locked font) whenever the label changes,
                    # so a longer name still grows the button instead of
                    # getting clipped against a stale, too-small box.
                    width = max(8, btn._base_width)
                else:
                    width = max(8, round(btn._base_width * button_scale))

                # Height and font size are fixed the first time a
                # widget is sized here, then left alone for the rest of
                # its life - not re-derived on every later call (a
                # rename, a zoom change, anything else that runs
                # update_overlay_buttons() again). Only WIDTH keeps
                # tracking the current text, since that's what a
                # rename actually needs to change; height/font staying
                # put means a rename never visibly shrinks a button's
                # text the way re-fitting against a stale/mid-update
                # width could.
                if not hasattr(btn, "_locked_height"):
                    btn._locked_height = max(8, round(btn._base_height * button_scale))
                height = btn._locked_height

                btn.setFixedSize(width, height)

                # ToggleSwitch has no text to size a font for - it's
                # drawn entirely in paintEvent - so skip the QPushButton/
                # QLabel-only font-fitting step for it.
                if hasattr(btn, "text") and not getattr(btn, "_font_locked", False):
                    self.fit_font(btn)
                    btn._font_locked = True
                    # From here on _base_width is read directly above
                    # (no more * button_scale) - see the branch above.
                    btn._base_width = width

                # The label's box width comes from adjustSize() at
                # construction time and doesn't shrink back down after
                # fit_font() picks a smaller font for breathing room -
                # so for the IK/switch/FK compound, and each TOGGLE
                # action's own label/switch compound, ask Qt to
                # recompute the label's ideal box for its now-fitted
                # font (instead of hand-measuring it, which can clip
                # the text if the measurement comes out too tight).
                if row in tight_row_set and isinstance(btn, QLabel):
                    btn.adjustSize()
                    width = btn.width()
                    height = btn.height()

                sizes.append((width, height))

            row_height = max(height for _, height in sizes)

            # If this row has been dragged to a custom spot, start
            # laying it out there instead of at the default stacked
            # position - the row's own internal sizing/spacing (already
            # computed above) is identical either way. A row that's
            # never been dragged (group_key not in overlay_positions,
            # or dragged then reset to None) keeps the default (row_x, y).
            group_key = draggable_rows.get(row)
            saved_pos = (
                self.overlay_positions.get(group_key) if group_key else None
            )

            if saved_pos is not None:
                start_x = self.image_x + saved_pos.x() * scale
                start_y = self.image_y + saved_pos.y() * scale
            else:
                start_x = row_x
                start_y = y

            # Second pass: position each widget, vertically centering it
            # within the row so shorter widgets (e.g. the toggle switch)
            # line up with taller ones (e.g. its flanking labels) instead
            # of sharing a common top edge.
            pos_x = start_x
            for btn, (width, height) in zip(row, sizes):

                btn_y = start_y + (row_height - height) // 2

                btn.move(round(pos_x), round(btn_y))

                btn.raise_()
                btn.show()

                pos_x += width + row_spacing

            y += row_height + spacing

    @safe_override()
    def paintEvent(self, event):

        super().paintEvent(event)

        # Box select needs to draw (see bottom of this method) even with
        # no background image loaded, so the early-return below - which
        # otherwise skips all custom painting - can't gate it. The
        # painter itself is still created lazily: only once something
        # actually needs to draw with it, background or box rect.
        painter = None

        if self.background is not None:

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

                # self.background.height() is the image's NATIVE pixel
                # height; image_scale() is a ratio relative to the
                # load-time DISPLAYED size (see image_scale()'s own
                # docstring), not native pixels - multiplying the two
                # together mixes unrelated units and made the line's
                # bottom endpoint land far past the actual bottom edge
                # of the displayed image (clipped hard at the canvas
                # edge instead), since native resolution is normally
                # much larger than the on-screen display size. `pixmap`
                # (computed above via scaled_background()) is already
                # the image at its correct CURRENT on-screen size, so
                # its own height is the line's real bottom edge.
                line_bottom = self.image_y + pixmap.height()

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

        # Rubber-band box select rectangle (Shift + drag on empty
        # canvas - see mousePressEvent/mouseMoveEvent/mouseReleaseEvent).
        # Drawn on top of everything else, and independent of whether a
        # background image is loaded, so the painter above is created
        # lazily here too if it wasn't already.
        if self.box_select_active and not self.box_select_rect.isNull():

            if painter is None:
                painter = QPainter(self)

            # Antialiasing + a half-pixel-inset QRectF, not the raw
            # integer QRect: drawRect() on a plain QRect with a 1px pen
            # and no antialiasing rasterizes the stroke asymmetrically
            # (QRect's right()/bottom() are x+width-1/y+height-1, not
            # x+width/y+height, so the pen lands differently on the
            # bottom/right edge than the top/left one) - that's what
            # made the border look thicker/offset on one side. Centering
            # the pen on a half-pixel-adjusted float rect with
            # antialiasing on draws all four edges evenly.
            painter.setRenderHint(QPainter.Antialiasing, True)

            # #4f86f7 - not an arbitrary blue, this is the theme's own
            # accent color (see ui/themes/blender_dark.qss: the same
            # shade used for pressed/checked buttons and combo-box
            # selection highlights), so the box reads as part of this
            # UI's existing accent language rather than a clashing
            # one-off color - including against the #2c2c2c panel/
            # disabled-button background this stylesheet already uses.
            pen = QPen(QColor(79, 134, 247, 220), 1)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(QColor(79, 134, 247, 45))

            rect = QRectF(self.box_select_rect).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.drawRect(rect)

    @safe_override()
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

                    # A control the user can't click directly (hidden, or
                    # disabled because its bone/collection is hidden in
                    # Blender) must not be pickable by clicking the empty
                    # canvas space near it either - that path bypassed the
                    # normal widget-level enabled/visible gating.
                    if not control.isVisible() or not control.isEnabled():
                        continue

                    center = control.geometry().center()

                    dx = center.x() - click_pos.x()
                    dy = center.y() - click_pos.y()

                    dist2 = dx * dx + dy * dy

                    if dist2 <= nearest_dist2:
                        nearest = control
                        nearest_dist2 = dist2

                # Click close enough to a control
                if nearest:
                    # shift, not hardcoded False: a near-miss pick (this
                    # branch) should toggle/add into the selection under
                    # Shift exactly the same way a dead-on click on the
                    # control itself already does (see CircleControl's
                    # own mouseReleaseEvent) - hardcoding False here
                    # silently replaced the whole selection instead of
                    # extending it whenever the click landed just
                    # outside the control's actual hit area but within
                    # PICK_RADIUS.
                    nearest.clicked.emit(
                        nearest.bone_name,
                        bool(event.modifiers() & Qt.ShiftModifier),
                        False,  # was_drag: this is a direct pick, never a drag
                    )
                    return

                # Truly empty space
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

                # Shift + drag on empty canvas starts a rubber-band box
                # select instead of the usual click-to-deselect /
                # background-image drag below - deferred to
                # mouseMoveEvent (see box_select_active there) the same
                # way the image drag is, so a plain Shift+click with no
                # movement leaves the current selection untouched rather
                # than clearing or dragging anything.
                if event.modifiers() & Qt.ShiftModifier:
                    self._pending_drag_start = click_pos
                    self._pending_drag_kind = "box_select"
                    return

                window = self.window()
                if hasattr(window, "controller"):
                    window.controller.deselect_all()

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

    def _drag_symmetry_to(self, canvas_x):
        """Moves the symmetry line (and every control with it) so it sits
        at canvas x-coordinate `canvas_x`. Shared by mouseMoveEvent() and
        the G-key grab (begin_grab()).
        """

        image_x = self.canvas_to_image_position(
            QPoint(int(canvas_x), 0)
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

    @safe_override()
    def mouseMoveEvent(self, event):
        hover = self.symmetry_handle_rect().contains(
            event.position().toPoint()
        )
        if hover != self.symmetry_handle_hover:
            self.symmetry_handle_hover = hover
            self.update()
        # A move delivered to the canvas itself means the pointer is not over
        # an overlay widget (those get their own events), so drop any
        # disabled-hover state a missed Leave event could have left behind.
        self._clear_disabled_hover_cursor()
        self._update_symmetry_cursor(hover)

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

            if self._pending_drag_kind == "symmetry":
                controller = getattr(self.window(), "controller", None)
                if controller is not None:
                    controller._record_undo_snapshot()
                self.dragging_symmetry = True
            elif self._pending_drag_kind == "image":
                controller = getattr(self.window(), "controller", None)
                if controller is not None:
                    controller._record_undo_snapshot()
                self.dragging_image = True
                self.drag_start = self._pending_drag_start
            elif self._pending_drag_kind == "box_select":
                # No undo snapshot here (unlike symmetry/image above) -
                # box select is a selection change, not a data edit, and
                # whether it even changes anything at all isn't known
                # until the drag ends and the final rect is checked
                # against every control's geometry (see
                # mouseReleaseEvent). Controller.box_select() takes its
                # own selection_only snapshot then, the same way a plain
                # Shift+click on a single control does in
                # select_control().
                self.box_select_active = True
                self.box_select_origin = self._pending_drag_start

            self._pending_drag_start = None
            self._pending_drag_kind = None

        if self.box_select_active:
            current = event.position().toPoint()
            self.box_select_rect = QRect(
                self.box_select_origin, current
            ).normalized()
            # repaint(), not update() - see mouseReleaseEvent's comment
            # on why this addon needs the synchronous version.
            self.repaint()
            super().mouseMoveEvent(event)
            return

        if self.dragging_symmetry:
            self._drag_symmetry_to(event.position().x())
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

                # Reposition the overlay buttons (custom action buttons/
                # toggles) at this candidate image position so their
                # actual on-canvas geometry can be checked next.
                # Their position always tracks image_x/image_y - either
                # stacked from the image's default top-left corner, or
                # offset from it at whatever spot the user last dragged
                # them to (see update_overlay_buttons()) - so a button
                # sitting outside the image's own edge follows the image
                # as it's dragged.
                self.update_overlay_buttons()

                # Without this, the clamp above (which only keeps the
                # *image* from leaving a gap at the canvas edge) lets a
                # button that sits outside the image - at its default
                # spot or a manually dragged one - get pushed straight
                # off the visible canvas and disappear, since nothing
                # was otherwise stopping the image (and everything
                # anchored to it) from being dragged past that point.
                # Nudge image_x/image_y back just enough to keep every
                # currently-visible overlay button on-canvas instead.
                visible_widgets = [
                    w for w in self._overlay_visible_widgets if w.isVisible()
                ]

                if visible_widgets:

                    widgets_left = min(w.x() for w in visible_widgets)
                    widgets_top = min(w.y() for w in visible_widgets)
                    widgets_right = max(
                        w.x() + w.width() for w in visible_widgets
                    )
                    widgets_bottom = max(
                        w.y() + w.height() for w in visible_widgets
                    )

                    shift_x = 0
                    if widgets_right > canvas_w:
                        shift_x = canvas_w - widgets_right
                    elif widgets_left < 0:
                        shift_x = -widgets_left

                    shift_y = 0
                    if widgets_bottom > canvas_h:
                        shift_y = canvas_h - widgets_bottom
                    elif widgets_top < 0:
                        shift_y = -widgets_top

                    if shift_x or shift_y:
                        self.image_x += shift_x
                        self.image_y += shift_y
                        self.update_overlay_buttons()

                # Save relative position
                available_x = max(1, max_x - min_x)
                available_y = max(1, max_y - min_y)

                self.image_offset_x = (self.image_x - min_x) / available_x
                self.image_offset_y = (self.image_y - min_y) / available_y

            self.layout_controls()
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

    @safe_override()
    def resizeEvent(self, event):

        super().resizeEvent(event)

        if not self.background:
            return

        self.apply_image_offset()

        self.layout_controls()
        self.update_overlay_buttons()
        self.update()

    @safe_override()
    def mouseReleaseEvent(self, event):

        was_dragging_image = self.dragging_image
        was_box_selecting = self.box_select_active

        self.dragging_image = False
        self.dragging_symmetry = False
        self.box_select_active = False

        # Clear any pending drag that never crossed the threshold (i.e.
        # this was a plain click, not a drag) so it doesn't leak into
        # the next press.
        self._pending_drag_start = None
        self._pending_drag_kind = None
        self._update_symmetry_cursor(
            self.symmetry_handle_rect().contains(event.position().toPoint())
        )

        if was_box_selecting:
            select_rect = self.box_select_rect

            self.box_select_origin = None
            self.box_select_rect = QRect()

            # repaint(), not update(): this addon's Qt event loop is
            # pumped periodically from inside Blender (via bqt), not
            # natively - update()'s queued repaint can sit pending until
            # the next pump cycle instead of clearing immediately on
            # release. That's what made the box appear to hang around
            # until some unrelated event (e.g. releasing Shift, which
            # generates its own key event and happens to pump the loop)
            # finally flushed it. repaint() forces the clear synchronously,
            # right here, regardless of Shift or anything else afterward.
            self.repaint()

            controls = (
                self.control_list.controls
                if self.control_list is not None
                else {c.bone_name: c for c in self.findChildren(CircleControl)}
            )

            # isEnabled() check matters here: a control whose bone sits
            # in a currently-hidden collection is setEnabled(False) (see
            # CircleControl.set_bone_collection_visible()), the same way
            # Controller.select_all() explicitly filters those out via
            # is_bone_collection_visible() before building its own
            # selection. Without it, dragging a box over a hidden-
            # collection control's (still on-screen, just dimmed and
            # non-interactive) position would mark it active/selected in
            # the picker UI even though clicking it directly never
            # could, and even though the viewport sync would silently
            # drop it anyway - a mismatch that would only get corrected
            # on the next ~0.15s poll tick instead of never happening.
            hit_bone_names = [
                name for name, control in controls.items()
                if control.isEnabled() and select_rect.intersects(control.geometry())
            ]

            # Empty rect (dragged over nothing) is a no-op, same as a
            # plain Shift+click on empty space - leaves the current
            # selection exactly as it was rather than clearing it.
            if hit_bone_names:
                window = self.window()
                if hasattr(window, "controller"):
                    window.controller.box_select(hit_bone_names)

            super().mouseReleaseEvent(event)
            return

        if was_dragging_image:
            controller = getattr(self.window(), "controller", None)
            if controller is not None:
                controller.data["image_offset_x"] = self.image_offset_x
                controller.data["image_offset_y"] = self.image_offset_y
                controller.save()

        super().mouseReleaseEvent(event)
    

    def _update_symmetry_cursor(self, over_handle):
        """Hand cursor over the symmetry line's grab handle (and for the
        whole of a drag started on it), the plain cursor otherwise - like
        the picker controls. Only while symmetry is on: the handle isn't
        drawn or draggable otherwise."""
        want = bool(self.symmetry_enabled) and (
            bool(over_handle)
            or self.dragging_symmetry
            or self._pending_drag_kind == "symmetry"
        )
        if want == self._symmetry_cursor_on:
            return
        self._symmetry_cursor_on = want
        self._refresh_canvas_cursor()

    def _refresh_canvas_cursor(self):
        """The canvas cursor: the hand while the pointer is over the
        symmetry handle or over a disabled overlay button/toggle (whose own
        cursor Qt ignores), the normal one otherwise."""
        if self._symmetry_cursor_on or self._disabled_hover_widget is not None:
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.unsetCursor()

    # Events an overlay widget's filter uses to track the pointer.
    _OVERLAY_HOVER_EVENTS = (
        QEvent.Enter, QEvent.HoverEnter, QEvent.HoverMove,
        QEvent.Leave, QEvent.HoverLeave,
    )

    def _update_disabled_hover_cursor(self, obj, event_type):
        """Tracks the pointer over disabled overlay widgets (see
        eventFilter()). A widget that became enabled while hovered, or that
        the pointer left, stops counting."""
        leaving = event_type in (QEvent.Leave, QEvent.HoverLeave)
        if not leaving and not obj.isEnabled():
            new = obj
        elif self._disabled_hover_widget is obj:
            new = None
        else:
            return
        if new is not self._disabled_hover_widget:
            self._disabled_hover_widget = new
            self._refresh_canvas_cursor()

    def _clear_disabled_hover_cursor(self):
        if self._disabled_hover_widget is not None:
            self._disabled_hover_widget = None
            self._refresh_canvas_cursor()

    # -----------------------------------------------------------------
    # G-key grab
    # -----------------------------------------------------------------
    # Whatever shows the hand cursor under the pointer (a picker control,
    # a Quick Action button/toggle, or the symmetry line's handle) starts
    # the same drag a mouse press-and-drag would when G is pressed - then
    # follows the pointer with NO button held, like Blender's G:
    #   left click / Enter / G -> confirm      Esc / right click -> cancel
    # The movement itself is the existing drag code (CircleControl.
    # drag_to_global(), _drag_overlay_group(), _drag_symmetry_to()).

    def grab_target_under_cursor(self):
        """(kind, widget_or_key) for whatever the hand cursor would be on
        right now, or None. kind: "control" | "overlay" | "symmetry"."""
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QCursor

        global_pos = QCursor.pos()
        widget = QApplication.widgetAt(global_pos)
        while widget is not None and widget is not self:
            if isinstance(widget, CircleControl):
                # A disabled control shows the arrow, not the hand.
                return ("control", widget) if widget.isEnabled() else None
            key = self._overlay_drag_group_of.get(widget)
            if key is not None:
                return ("overlay", key)
            widget = widget.parentWidget()
        if widget is not self:
            return None
        if self.symmetry_enabled and self.symmetry_handle_rect().contains(
            self.mapFromGlobal(global_pos)
        ):
            return ("symmetry", None)
        return None

    def _drag_in_progress(self):
        return bool(
            self._grab is not None
            or self._overlay_press_pos
            or self._pending_drag_start is not None
            or self.dragging_symmetry
            or self.dragging_image
            or self.box_select_active
            or any(c.dragging or c.drag_offset is not None
                   for c in self.findChildren(CircleControl))
        )

    def begin_grab(self):
        """Called for the G key. Returns True if a grab started."""
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QCursor

        if self._drag_in_progress():
            return False
        target = self.grab_target_under_cursor()

        # Several controls selected (shift-click / box select): G moves
        # them all together - when the pointer is on one of the selected
        # controls, or on empty canvas (like Blender's G, which doesn't
        # care where the pointer is). A control that ISN'T part of the
        # selection, a button/toggle or the symmetry handle is grabbed on
        # its own as before.
        group = self._grab_group_controls()
        if group and (
            target is None
            or (target[0] == "control" and target[1] in group)
        ):
            controller = getattr(self.window(), "controller", None)
            if controller is not None and controller.data.get("symmetry"):
                # Moving several controls at once would fight the mirror
                # partners (each one is dragged along its partner), so the
                # user has to switch Symmetry off first. True = the key was
                # handled, there's just no grab to start.
                self._warn_group_grab_needs_symmetry_off()
                return True
            first = target[1] if target is not None else group[0]
            self._grab = self._make_group_grab(group, first, QCursor.pos())
            self._grab_filter = _GrabFilter(self)
            QApplication.instance().installEventFilter(self._grab_filter)
            return True

        if target is None:
            return False

        kind, ref = target
        start = QCursor.pos()
        grab = {"kind": kind, "ref": ref, "start": start, "moved": False}

        if kind == "control":
            grab["offset"] = ref.mapFromGlobal(start)
        elif kind == "overlay":
            anchor = self.OVERLAY_DRAG_GROUPS[ref][0]
            self._overlay_drag_offset[ref] = self.mapFromGlobal(start) - anchor.pos()

        self._grab = grab
        self._grab_filter = _GrabFilter(self)
        QApplication.instance().installEventFilter(self._grab_filter)
        return True

    def _warn_group_grab_needs_symmetry_off(self):
        """Non-modal warning (a blocking dialog would run a nested Qt event
        loop inside Blender's callback and swallow the key). An already
        open one is reused instead of stacking another."""
        import shiboken6
        from PySide6.QtWidgets import QMessageBox

        box = getattr(self, "_group_symmetry_box", None)
        if box is not None and shiboken6.isValid(box) and box.isVisible():
            box.raise_()
            return
        box = QMessageBox(
            QMessageBox.Warning,
            "Symmetry is on",
            "Several controls can't be moved together while Symmetry is on, "
            "because each control moves with its mirror partner.\n\n"
            "Turn Symmetry off, then press G again.",
            QMessageBox.Ok,
            self.window(),
        )
        box.setAttribute(Qt.WA_DeleteOnClose)
        box.setWindowModality(Qt.NonModal)
        self._group_symmetry_box = box
        box.show()

    def _grab_group_controls(self):
        """The selected picker controls that can be grabbed together (two
        or more, enabled and visible), else []."""
        controller = getattr(self.window(), "controller", None)
        if controller is None or self.control_list is None:
            return []
        group = []
        for name in controller.selected_bones:
            c = self.control_list.controls.get(name)
            if c is not None and c.isEnabled() and not c.isHidden():
                group.append(c)
        return group if len(group) >= 2 else []

    def _make_group_grab(self, group, first, start):
        """State for a G grab of several selected controls moving as one.
        (Not used while Symmetry is on - begin_grab() warns instead, since
        every control would drag its mirror partner along.) Obstacles never
        change during a grab, so they're collected once here. A member
        that already overlaps one at the start may keep that overlap and
        move out of it (like a button starting on a control) but never
        deeper: the separating axes and the overlap along each are
        recorded per (member, obstacle) and _group_blocked() holds each
        as a ceiling (the smallest depth alone would let one dimension
        of the overlap grow while the other stays put). The axis
        DIRECTIONS are stored, not just the depths, because a circle's
        nearest-vertex axis against a polygon changes as it moves; later
        positions are measured along these same directions. The pair is dropped as soon
        as the member is clear of the obstacle (see _group_grab_move()),
        after which it's an ordinary obstacle again.
        """
        drivers = [first] + [c for c in group if c is not first]

        grab = {
            "kind": "group", "ref": first, "start": start, "moved": False,
            "drivers": drivers, "delta": (0, 0),
            "starts": {c: (c.x(), c.y()) for c in drivers},
        }

        members = [c for c, _x, _y in self._group_members_at(grab, 0, 0)]
        member_set = set(members)
        obstacles = []
        for o in self._other_picker_controls(None):
            if o not in member_set and not o.isHidden():
                shape = self._collision_shape(o)
                obstacles.append((shapes.bounds(shape), shape))
        for shape in self._overlay_obstacle_shapes():
            obstacles.append((shapes.bounds(shape), shape))

        # (member, obstacle index) -> (axes, depths): the separating axes
        # SAT used at the start of the grab and the overlap along each
        # (shapes.axis_depths), for every pair that already overlaps.
        # axes is None for two circles (see shapes.pinned_axes()).
        exempt = {}
        for c in members:
            mine = shapes.shape_for(
                getattr(c, "shape", None), c.x(), c.y(), c.width(), c.height()
            )
            for i, (_bb, o) in enumerate(obstacles):
                if shapes.overlaps(mine, o):
                    axes = shapes.pinned_axes(mine, o)
                    exempt[(c, i)] = (axes, shapes.axis_depths(mine, o, axes))

        grab["obstacles"] = obstacles
        grab["exempt"] = exempt
        return grab

    def _group_members_at(self, grab, dx, dy):
        """[(control, x, y)] - every grabbed control at its start position
        + (dx, dy)."""
        return [
            (c, grab["starts"][c][0] + dx, grab["starts"][c][1] + dy)
            for c in grab["drivers"]
        ]

    def _group_blocked(self, grab, dx, dy):
        """True if moving the group by (dx, dy) would put any member
        outside the canvas, onto an obstacle it wasn't already on, or
        deeper into one it was already on - deeper along ANY of the axes
        recorded at the start of the grab, not just the shallowest one.
        Moving out of an existing overlap, or sliding along it without it
        growing, is fine."""
        width, height = self.width(), self.height()
        for c, x, y in self._group_members_at(grab, dx, dy):
            w, h = c.width(), c.height()
            if x < 0 or y < 0 or x + w > width or y + h > height:
                return True
            mine = shapes.shape_for(getattr(c, "shape", None), x, y, w, h)
            for i, (bb, o) in enumerate(grab["obstacles"]):
                if bb[0] >= x + w or bb[2] <= x or bb[1] >= y + h or bb[3] <= y:
                    continue
                if not shapes.overlaps(mine, o):
                    continue
                cap = grab["exempt"].get((c, i))
                if cap is None:
                    return True
                axes, start_depths = cap
                if shapes.overlap_grew(shapes.axis_depths(mine, o, axes), start_depths):
                    return True
        return False

    def _group_grab_move(self, grab, dx, dy):
        """Moves the whole group toward (dx, dy) from where it started.
        Same walk as a single drag (see _sweep_to()) so nothing is jumped
        onto; when blocked on one axis the group slides along the other,
        and it stops where it can't move at all."""

        def resolve(x, y, hold):
            if not self._group_blocked(grab, x, y):
                return x, y
            if not self._group_blocked(grab, x, hold[1]):
                return x, hold[1]
            if not self._group_blocked(grab, hold[0], y):
                return hold[0], y
            return hold

        dx, dy = self._sweep_to(grab["delta"], (dx, dy), resolve)
        if (dx, dy) == grab["delta"]:
            return
        grab["delta"] = (dx, dy)

        # A member that has left an obstacle it started on is done with its
        # exemption: from here that obstacle blocks it like any other, so
        # it can't be dragged back in.
        for (c, i) in list(grab["exempt"]):
            sx, sy = grab["starts"][c]
            mine = shapes.shape_for(
                getattr(c, "shape", None), sx + dx, sy + dy, c.width(), c.height()
            )
            if not shapes.overlaps(mine, grab["obstacles"][i][1]):
                del grab["exempt"][(c, i)]

        for c in grab["drivers"]:
            sx, sy = grab["starts"][c]
            self.move_control_from_canvas(c, QPoint(sx + dx, sy + dy))

    def _grab_move(self, global_pos):
        grab = self._grab
        if grab is None:
            return
        if not grab["moved"]:
            if (global_pos - grab["start"]).manhattanLength() < self.DRAG_THRESHOLD:
                return
            # Real movement: snapshot now (not at G), same as a mouse drag.
            controller = getattr(self.window(), "controller", None)
            if controller is not None:
                controller._record_undo_snapshot()
            grab["moved"] = True
            if grab["kind"] == "control":
                grab["ref"].dragging = True
                grab["ref"].update()
            elif grab["kind"] == "group":
                for c in grab["drivers"]:
                    c.dragging = True
                    c.update()
            elif grab["kind"] == "symmetry":
                self.dragging_symmetry = True

        kind = grab["kind"]
        if kind == "group":
            self._group_grab_move(
                grab, global_pos.x() - grab["start"].x(),
                global_pos.y() - grab["start"].y(),
            )
        elif kind == "control":
            grab["ref"].drag_to_global(global_pos, grab["offset"])
        elif kind == "overlay":
            self._drag_overlay_group(grab["ref"], global_pos)
        else:
            self._drag_symmetry_to(self.mapFromGlobal(global_pos).x())

    def interrupt_for_undo(self):
        """Asked by Controller.undo()/redo() before they do anything.
        Returns True when an operation in progress consumed the request
        and undo/redo must do nothing:

        - G grab: cancelled (its move is taken back, like Esc) - no undo
          or redo step is taken.
        - Mouse drag with a button actually held: ignored; the drag
          carries on and commits on release as usual. (The button check
          keeps a stale drag flag from ever disabling undo for good.)
        """
        from PySide6.QtWidgets import QApplication

        if self._grab is not None:
            self.end_grab(False)
            return True
        if self._drag_in_progress() and QApplication.mouseButtons() != Qt.NoButton:
            return True
        return False

    def end_grab(self, commit):
        grab = self._grab
        if grab is None:
            return
        self._grab = None

        from PySide6.QtWidgets import QApplication
        flt, self._grab_filter = self._grab_filter, None
        if flt is not None:
            QApplication.instance().removeEventFilter(flt)
            flt.deleteLater()

        kind, ref = grab["kind"], grab["ref"]
        controller = getattr(self.window(), "controller", None)

        if kind == "group":
            for c in grab["drivers"]:
                c.dragging = False
                c.update()
        elif kind == "control":
            ref.dragging = False
            ref.update()
        elif kind == "overlay":
            self._overlay_drag_offset.pop(ref, None)
        else:
            self.dragging_symmetry = False

        if grab["moved"] and controller is not None:
            if commit:
                if kind == "group":
                    # Keep the multi-selection as it is (a single control's
                    # commit selects just that control).
                    controller.save()
                elif kind == "control":
                    controller.save()
                    # What releasing a real drag does: select the control.
                    ref.clicked.emit(ref.bone_name, False, True)
                elif kind == "overlay":
                    controller.save()
            else:
                # Cancel: take the whole move back via the snapshot the
                # first movement recorded. discard_redo: a cancelled move
                # was never a committed step, so it must not become
                # redoable.
                controller.undo(discard_redo=True)

        from PySide6.QtGui import QCursor
        self._update_symmetry_cursor(
            self.symmetry_handle_rect().contains(
                self.mapFromGlobal(QCursor.pos())
            )
        )
        self.update()

    def symmetry_handle_rect(self):

        if self.symmetry_x < 0:
            return QRect()

        canvas_x = self.image_x + self.symmetry_x * self.image_scale()

        return QRect(
            int(canvas_x - 12),
            0,
            24,
            28
        )