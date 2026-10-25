"""
main_window.py

Main floating window for Rig Picker.
"""

from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QComboBox,
    QLabel,
    QMenu,
    QApplication,
    QStylePainter,
    QStyleOptionComboBox,
    QStyle
)
from PySide6.QtCore import Qt, QPoint, QRectF, QTimer, QEvent
from PySide6.QtGui import QPainterPath, QRegion, QCursor, QPalette, QColor

from .control_list import ControlList
from .controller import Controller
from .mirror_icon import MirrorIcon

from pathlib import Path
import bpy
import os


# The picker's window/min sizes below were tuned by eye on a 1920x1080
# laptop screen. Rather than hardcoding those pixel values (which look
# right there but end up tiny on a 4K monitor or cramped on a smaller
# one), REFERENCE_SCREEN_SIZE records the screen they were tuned on and
# _scale_to_screen() below re-derives them as the same proportion of
# whatever screen the window is actually opening on - so the picker
# keeps the same on-screen size/shape everywhere, not the same pixel
# count.
# The picker's window/min sizes below were tuned by eye on the laptop
# this addon is developed on - a 1920x1080 physical display running at
# 150% OS scaling. QScreen.availableGeometry() reports LOGICAL pixels
# (physical resolution / scale factor), so that laptop's screen shows
# up to Qt as 1280x672 (1920/1.5 x (1080/1.5 minus the taskbar)), not
# 1920x1080 - recording the reference in physical pixels would compare
# it against availableGeometry()'s logical pixels at runtime and always
# shrink the computed size, which is exactly what was happening before
# this value was corrected. Keep this in whatever units
# screen.availableGeometry() reports, not what the OS display settings
# panel shows.
REFERENCE_SCREEN_SIZE = (1280, 672)
REFERENCE_MIN_SIZE = (330, 550)
REFERENCE_DEFAULT_SIZE = (330, 550)


def _scale_to_screen(base_size, reference=REFERENCE_SCREEN_SIZE):
    """Scales a (width, height) tuple - sized to look right at
    `reference` resolution - proportionally to the primary screen's
    current available geometry (usable area, taskbar/dock excluded).
    Falls back to the unscaled base_size if no screen is available yet
    (shouldn't normally happen, since this only runs once a QApplication
    exists)."""
    screen = QApplication.primaryScreen()
    if screen is None:
        return base_size
    geo = screen.availableGeometry()
    scale_w = geo.width() / reference[0]
    scale_h = geo.height() / reference[1]
    return (
        max(1, round(base_size[0] * scale_w)),
        max(1, round(base_size[1] * scale_h)),
    )


from .safe_qt import safe_override


class PickerComboBox(QComboBox):
    """A combo box whose full option list opens directly below the field."""

    # Three full stylesheets - identical except for which chevron PNG the
    # down-arrow rule points at, and (for _pressed_style) which shade the
    # box itself uses. Swapping the *entire* stylesheet at each stage
    # (instead of leaning on QSS's own :hover/:pressed/:on pseudo-states)
    # avoids two problems: (1) Qt layering a second `::down-arrow` rule on
    # top of the base one and painting both icons at once, and (2) the
    # pressed/blue state getting stuck after the popup closes, because our
    # popup is a plain QMenu rather than QComboBox's real internal popup,
    # so Qt's own :pressed/:on tracking doesn't reliably clear on its own.
    _normal_style = ""
    _hover_style = ""
    _pressed_style = ""
    # True for the whole time the popup menu is open. While it's open we
    # ignore enterEvent/leaveEvent: opening the menu.exec() nested loop and
    # moving the mouse toward the popup's items both generate real
    # enter/leave events for this widget, which would otherwise clobber
    # the pressed/blue style mid-popup. The style is only resolved once,
    # authoritatively, right after the popup actually closes.
    _popup_open = False
    # Set the instant the popup closes, and cleared again on the next
    # event-loop tick. A second click on this combo box while the popup
    # is open doesn't reach our mousePressEvent as a normal click - the
    # popup (a Qt::Popup) intercepts it first as an "outside click" and
    # closes itself, and Qt can then immediately redeliver that same
    # click to this widget, which would otherwise reopen the popup right
    # back up (looking like the second click did nothing). This flag lets
    # mousePressEvent recognize and swallow that redelivered click so a
    # second click actually toggles the dropdown closed.
    _just_closed = False
    _active_menu = None

    def set_hoverable_style(self, normal_style, hover_style, pressed_style=""):
        self._normal_style = normal_style
        self._hover_style = hover_style
        self._pressed_style = pressed_style or hover_style
        self.setStyleSheet(self._normal_style)

    def _choose_index(self, index):
        """The user picked entry `index` from the popup menu. Because this
        combo uses its own QMenu instead of Qt's internal popup, Qt never
        emits `activated` itself, so do what a normal QComboBox does:
        setCurrentIndex() (emits currentIndexChanged only if it actually
        changed), then emit `activated` - which fires even when the user
        re-picks the entry that is already shown.
        """
        self.setCurrentIndex(index)
        self.activated.emit(index)

    def _current_pressed_style(self):
        """The stylesheet to use while this combo is actively pressed/open.

        _pressed_style's solid blue (#4f86f7, see build_combo_style's
        "pressed" call in build_ui) is meant to read as "this field has
        a real value" - it's the same blue the popup's own
        ::item:checked rule uses to mark the actual current item, and
        what QComboBox:focus's border would normally imply too. Using
        it unconditionally meant clicking this combo while it's showing
        its empty-selection placeholder (currentIndex() == -1 - "Select
        Rig", "Select") turned solid blue around that placeholder text,
        which reads as if "Select Rig" were itself a chosen value
        rather than a prompt for one. Falling back to the neutral hover
        style in that case keeps the blue meaningful: it only ever
        appears once this combo actually has something selected.
        """
        return self._pressed_style if self.currentIndex() != -1 else self._hover_style

    def _apply_style(self, stylesheet):
        if not stylesheet:
            return
        self.setStyleSheet(stylesheet)
        # Forces Qt to immediately recompute and repaint with the new
        # sheet. update() alone only *schedules* a repaint for whenever
        # the event loop next processes paint events - inside Blender's
        # own event loop (which doesn't pump Qt continuously) that can
        # sit pending indefinitely until some unrelated event (like the
        # next hover) forces a redraw, which is why the color looked
        # "stuck" until then. repaint() forces it synchronously, now.
        self.style().unpolish(self)
        self.style().polish(self)
        # repaint() already paints synchronously. A nested
        # QApplication.processEvents() here ran a second event loop from
        # inside enterEvent/leaveEvent/mousePressEvent (re-entrant event
        # dispatch through Python overrides), which is a native-crash risk.
        self.repaint()

    @safe_override()
    def mousePressEvent(self, event):
        if self._just_closed:
            # This is the redelivered click that closed the popup a
            # moment ago - consume it so it doesn't immediately reopen.
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            self._apply_style(self._current_pressed_style())
        super().mousePressEvent(event)

    @safe_override(False)
    def eventFilter(self, obj, event):
        # Installed application-wide only while the popup is open (see
        # showPopup). Explicitly closing the menu ourselves on any press
        # outside it - rather than counting on QMenu's own native
        # outside-click dismissal - sidesteps whatever quirk (likely
        # related to WA_TranslucentBackground + the rounded-rect mask)
        # was letting a second click on this combo box either fail to
        # close the popup or close-then-immediately-reopen it.
        if event.type() == QEvent.MouseButtonPress and self._active_menu is not None:
            global_pos = event.globalPosition().toPoint()
            if not self._active_menu.geometry().contains(global_pos):
                self._active_menu.close()
                if self.rect().contains(self.mapFromGlobal(global_pos)):
                    # The click that closed it landed on this combo box -
                    # swallow it so it can't also reopen the popup.
                    return True
        return super().eventFilter(obj, event)

    @safe_override()
    def enterEvent(self, event):
        if not self._popup_open:
            self._apply_style(self._hover_style)
        super().enterEvent(event)

    @safe_override()
    def leaveEvent(self, event):
        if not self._popup_open:
            self._apply_style(self._normal_style)
        super().leaveEvent(event)

    @safe_override()
    def paintEvent(self, event):
        # QComboBox's own CE_ComboBoxLabel draw doesn't reliably elide
        # the current text against this widget's QSS-driven chrome
        # (fixed width, custom padding/arrow), so a long rig/panel name
        # just gets clipped hard against the edge with no "...". Draw
        # the frame/arrow/background via the normal style/stylesheet
        # path as usual, then paint the label ourselves, elided to
        # whatever width is actually left for text.
        painter = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        opt.currentText = ""  # let the style draw frame/arrow only, no label
        painter.drawComplexControl(QStyle.CC_ComboBox, opt)

        text_rect = self.style().subControlRect(
            QStyle.CC_ComboBox, opt, QStyle.SC_ComboBoxEditField, self
        )

        # currentText() is just "" at index -1 ("no selection" - e.g.
        # no rig active, or no panel to show one for) - drawing that
        # directly, as this used to do unconditionally, painted nothing
        # at all instead of the placeholderText() set via
        # setPlaceholderText() (main_window.py's "Select Rig"/"Select").
        # A first attempt at fixing that fell back to
        # QPalette.PlaceholderText here - technically correct Qt API,
        # but this combo's palette is whatever setStyleSheet()'s QSS
        # happened to compute for that specific role, and the QSS here
        # only ever sets "color" (which maps to QPalette::Text/
        # WindowText), never anything PlaceholderText-specific - so
        # that role was left at its unstyled default, which rendered
        # as good as invisible against this dark theme. An explicit,
        # always-legible grey - dimmer than normal text so it still
        # reads as a prompt rather than a real value, and dimmer still
        # while the combo is disabled - sidesteps trusting that
        # palette role at all. This is also what makes a disabled-but-
        # empty combo (e.g. the Panel dropdown with no rig loaded) and
        # an enabled-but-empty one look different again, instead of
        # both just being blank.
        showing_placeholder = self.currentIndex() == -1 and bool(self.placeholderText())
        full_text = self.placeholderText() if showing_placeholder else self.currentText()
        elided = self.fontMetrics().elidedText(
            full_text, Qt.ElideRight, text_rect.width()
        )
        self.setToolTip(full_text if elided != full_text else "")

        if showing_placeholder:
            painter.setPen(
                QColor("#9a9a9a") if self.isEnabled() else QColor("#5a5a5a")
            )
            painter.drawText(text_rect, int(Qt.AlignVCenter | Qt.AlignLeft), elided)
        else:
            painter.drawItemText(
                text_rect,
                Qt.AlignVCenter | Qt.AlignLeft,
                self.palette(),
                self.isEnabled(),
                elided,
                QPalette.Text,
            )

    @safe_override()
    def showPopup(self):
        self._popup_open = True
        # mousePressEvent (for a real mouse click) already applied the
        # pressed style just before calling super(), which is what led
        # here via Qt's normal click handling - applying it again here
        # unconditionally meant two separate setStyleSheet+repaint passes
        # back to back for the same target state, which is what made the
        # icon and background appear to update at slightly different
        # moments instead of together. Only apply here if it isn't
        # already showing (e.g. popup opened via keyboard, not a click).
        if self.styleSheet() != self._current_pressed_style():
            self._apply_style(self._current_pressed_style())
        menu = QMenu(self)
        menu.setObjectName("pickerComboPopup")
        menu.setWindowFlags(
            Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
        )
        # Without this, Qt/the OS still paints an opaque (and sometimes
        # bordered) rectangular background behind the menu before our
        # rounded QSS background and the corner mask below get applied.
        # Whatever sliver isn't covered by either shows up as a faint
        # "ring" outline around the popup - WA_TranslucentBackground makes
        # everything outside our own painted rounded rect truly
        # transparent instead, which removes that ring.
        menu.setAttribute(Qt.WA_TranslucentBackground)
        menu.setMinimumWidth(self.width())
        menu.setContentsMargins(0, 0, 0, 0)

        # Actions stay checkable so we still know which one is "current",
        # but the checkmark/arrow indicator glyph Qt normally draws next
        # to a checked action is hidden - Blender's own dropdowns mark the
        # current item with a plain background highlight instead, with no
        # icon taking up space next to the label. Colors/radius match this
        # theme's QListWidget and QComboBox rules in blender_dark.qss, so
        # the popup looks like a native Blender dropdown rather than a
        # generic Qt menu.
        menu.setStyleSheet(self.window().styleSheet() + """
            QMenu#pickerComboPopup {
                background-color: #1d1d1d;
                border: 1px solid #1d1d1d;
                border-radius: 4px;
            }
            QMenu#pickerComboPopup::indicator {
                width: 0px;
                height: 0px;
                image: none;
            }
            QMenu#pickerComboPopup::item {
                background-color: transparent;
                color: #d8d8d8;
                min-height: 18px;
                padding: 1px 10px;
                margin: 1px 0px;
                border-radius: 4px;
            }
            QMenu#pickerComboPopup::item:checked {
                background-color: #4f86f7;
                color: white;
                border-radius: 4px;
            }
            QMenu#pickerComboPopup::item:selected {
                background-color: #505050;
                border-radius: 4px;
            }
            QMenu#pickerComboPopup::item:checked:selected {
                background-color: #4f86f7;
                border-radius: 4px;
            }
        """)

        for index in range(self.count()):
            # Disabled items (rigs hidden in the viewport) are not offered.
            model_item = self.model().item(index)
            if model_item is not None and not model_item.isEnabled():
                continue
            action = menu.addAction(self.itemText(index))
            action.setCheckable(True)
            action.setChecked(index == self.currentIndex())
            action.triggered.connect(
                lambda checked=False, value=index: self._choose_index(value)
            )

        if menu.isEmpty():
            # Nothing selectable (e.g. every rig is hidden) - don't open an
            # empty popup, and put the combo's style back.
            self._just_closed = False
            if self.rect().contains(self.mapFromGlobal(QCursor.pos())):
                self._apply_style(self._hover_style)
            else:
                self._apply_style(self._normal_style)
            self._popup_open = False
            return

        menu.adjustSize()
        rounded_path = QPainterPath()
        rounded_path.addRoundedRect(QRectF(menu.rect()), 4, 4)
        menu.setMask(QRegion(rounded_path.toFillPolygon().toPolygon()))

        # menu.exec() blocks until the popup closes. The mouse-release
        # that closes it - whether from picking an item, pressing Escape,
        # clicking outside the popup, or the window losing focus - happens
        # on the QMenu, not on this QComboBox, so Qt's own hover/pressed
        # tracking for this widget never gets a matching event and can
        # otherwise leave it stuck showing the pressed (blue) style.
        # aboutToHide fires for all of those cases uniformly, so resync
        # there rather than relying only on exec() having returned.
        def resync_style():
            # _popup_open stays True until after _apply_style below runs
            # (and its internal processEvents() call settles) so that any
            # queued enterEvent/leaveEvent from mouse movement during the
            # popup's lifetime can't sneak in and trigger a second,
            # separate style application on top of this one - the same
            # double-repaint stagger as the mousePressEvent/showPopup
            # case above, just on the closing side instead of opening.
            self._just_closed = True
            if self.rect().contains(self.mapFromGlobal(QCursor.pos())):
                self._apply_style(self._hover_style)
            else:
                self._apply_style(self._normal_style)
            self._popup_open = False
            QTimer.singleShot(0, lambda: setattr(self, "_just_closed", False))

        menu.aboutToHide.connect(resync_style)
        self._active_menu = menu
        app = QApplication.instance()
        app.installEventFilter(self)
        try:
            menu.exec(self.mapToGlobal(QPoint(0, self.height())))
        finally:
            app.removeEventFilter(self)
            self._active_menu = None
        # Fallback in case aboutToHide didn't fire for some reason (belt
        # and suspenders - resync_style is idempotent either way).
        resync_style()

class RigPickerWindow(QMainWindow):

    def __init__(self, parent=None):
        super().__init__(parent)

        theme = (
            Path(__file__).parent /
            "themes" /
            "blender_dark.qss"
        )

        theme_dir = Path(theme).parent

        with open(theme, encoding="utf8") as f:
            style = f.read()

        # The stylesheet references the drop-down arrow via a Qt resource
        # path (":/icons/down_arrow.svg"), which only resolves if a .qrc
        # resource file was compiled in - this addon doesn't do that, so
        # rewrite it to an absolute path pointing at the actual .svg file
        # shipped alongside this theme instead.
        style = style.replace(
            "url(:/icons/down_arrow.svg)",
            f'url("{(theme_dir / "down_arrow.svg").as_posix()}")'
        )
        style = style.replace(
            "url(:/icons/checkmark.svg)",
            f'url("{(theme_dir / "checkmark.svg").as_posix()}")'
        )

        self.setStyleSheet(style)

        self.setWindowTitle("Rig Picker")
        min_width, min_height = _scale_to_screen(REFERENCE_MIN_SIZE)
        self.setMinimumSize(min_width, min_height)

        self.controller = Controller()
        self.controller.set_window(self)

        # Created before build_ui()/resize() below, since either of these
        # can trigger resizeEvent()/moveEvent() - which reference these
        # timers - before the window is ever shown.
        self.resize_timer = QTimer(self)
        self.resize_timer.setSingleShot(True)
        self.resize_timer.timeout.connect(self.save_window_size)

        self.move_timer = QTimer(self)
        self.move_timer.setSingleShot(True)
        self.move_timer.timeout.connect(self.save_window_position)

        # While False, resize/move debounce timers don't write anything.
        # Held False while a file's saved geometry is being applied (see
        # apply_saved_geometry()), so applying it never counts as the
        # user changing it.
        self._geometry_save_enabled = True

        from .. import json_manager

        # Bind this window to the .blend it was opened from: every write
        # it causes goes to that file's own data folder, and is refused if
        # Blender has a different file open (see json_manager's WINDOW
        # BINDING section).
        json_manager.bind_window()
        self._update_title()

        self.build_ui()

        # A previously-saved size (the user dragged the window to a size
        # they liked) always wins; only a first-ever run - nothing saved
        # yet - falls back to the screen-scaled default rather than the
        # fixed 330x580 that was tuned for REFERENCE_SCREEN_SIZE.
        default_width, default_height = _scale_to_screen(REFERENCE_DEFAULT_SIZE)
        width, height = json_manager.get_window_size(default_width, default_height)

        self.resize(width, height)

        # Make this the addon's single active controller, so the
        # depsgraph handler in backend.py can refresh it automatically
        # whenever the active armature changes.
        from .. import backend
        backend._ACTIVE_CONTROLLER = self.controller
        backend._ACTIVE_WINDOW = self

        # Load the currently active armature's picker from JSON. This
        # also builds the Quick Action buttons as canvas overlays now,
        # internally, before its own refresh() runs - see the comment
        # in Controller.load_armature() for why that ordering matters
        # (a stale-then-correct two-pass repaint otherwise). A second
        # sync_quick_actions() call here would just rebuild the same,
        # now-correct set a second time.
        self.controller.load_armature(backend.visible_arm())

        # Match the combo's displayed selection to whatever actually
        # got loaded above, immediately - without this the placeholder
        # set just above would still show for the one poll tick
        # (~0.15s) it'd otherwise take _sync_armature_combo_selection()
        # to catch up, even when a real armature loaded correctly on
        # the very first try.
        target_text = self.controller.armature_name or ""
        if target_text:
            self.armature_combo.setCurrentIndex(self.armature_combo.findText(target_text))

        # This first load_armature() ran before the window has ever been
        # shown (show() happens later, in launcher.py) - the canvas
        # widget hasn't been through Qt's show/layout-activation cycle
        # yet, so its size() here can be smaller than the size it will
        # actually have once shown, even though resize(width, height)
        # above already set the *window's* final geometry. layout_controls()
        # computes image_scale() from that not-yet-final canvas size, so
        # controls loaded with a background at this point get laid out
        # at a too-small scale - and since the window's geometry doesn't
        # change again once actually shown (it was already set), no
        # later resizeEvent() ever fires to recompute it, so the wrong
        # scale sticks for the rest of the session. Re-run layout once
        # the window's first real showEvent() confirms the canvas has
        # its true, final size.
        self._needs_post_show_relayout = True

        self.setFocusPolicy(Qt.StrongFocus)
        self.setFocus()

        # Installed on the whole application, not just this window, so
        # Shift+Tab cycles the Rig dropdown and Ctrl+Tab cycles the
        # Panel dropdown no matter which child widget (a combo box, a
        # button, the canvas) currently has keyboard focus. A plain
        # keyPressEvent() override here wouldn't work for this specific
        # key: Tab (with or without modifiers) is intercepted for normal
        # focus-navigation by whichever widget the key event is
        # actually delivered to (its default keyPressEvent calls
        # focusNextPrevChild() and consumes the event) before it would
        # ever bubble up to this window's own keyPressEvent.
        self._install_key_filter()

    def _install_key_filter(self):
        """(Re)installs the application-wide event filter behind the
        Shift+Tab (next rig) and Ctrl+Tab (next panel) shortcuts.

        closeEvent() removes it so a closed window doesn't keep seeing
        every key press in Blender - but closing only HIDES this window
        (launcher.show_picker() reuses it), so it must be installed again
        every time the window is shown, not just once in __init__(). Qt
        doesn't add a filter twice if it is already installed.
        """
        QApplication.instance().installEventFilter(self)

    # --------------------------------------------------------

    def build_ui(self):

        central = QWidget()
        central.setObjectName("rigPickerCentral")
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(5)

        # ----------------------------------------------------
        # Toolbar
        # ----------------------------------------------------

        armature_row = QHBoxLayout()
        # Right margin reserved here (rather than 0, matching the other
        # rows) purely for visual breathing room against the window's
        # true edge - the symmetry "X" button itself now sits right
        # after the Rig/Panel dropdowns (fixed distance from the left),
        # with the trailing addStretch() below absorbing any extra
        # width instead of pushing the button around, so it stays put
        # on screen when the window is resized.
        armature_row.setContentsMargins(0, 0, 4, 0)
        armature_row.setSpacing(0)

        self.armature_label = QLabel("Rig")
        self.armature_label.setObjectName("pickerSelectorLabel")

        self.armature_combo = PickerComboBox()
        self.armature_combo.setObjectName("pickerCombo")
        # Shown whenever no armature is the current picker's rig (index
        # -1, no real item selected) - see
        # Controller._sync_armature_combo_selection() for how that state
        # is kept in sync with self.armature_name on every poll tick,
        # rather than this combo silently defaulting to whichever
        # armature happens to be first once items exist.
        self.armature_combo.setPlaceholderText("Select Rig")
        # bpy.context.view_layer.objects, not bpy.data.objects - the
        # latter includes armatures from other scenes and orphaned
        # datablocks that aren't actually selectable/active-able here
        # (see the matching note in controller.py's
        # _sync_armature_combo_items, which rebuilds this same list).
        # Hidden rigs stay in the model but disabled - PickerComboBox.
        # showPopup() skips disabled items, so they are never listed.
        from .. import backend
        for obj in bpy.context.view_layer.objects:
            if obj.type == "ARMATURE":
                self.armature_combo.addItem(obj.name)
                if not backend.is_rig_visible(obj):
                    index = self.armature_combo.count() - 1
                    item = self.armature_combo.model().item(index)
                    if item is not None:
                        item.setEnabled(False)
        # Qt auto-selects index 0 the instant the first addItem() above
        # runs, regardless of whether an armature is actually active -
        # explicitly go back to "nothing selected" here so the combo
        # starts out matching backend.arm()'s answer (checked right
        # after this in load_armature()) rather than showing an
        # arbitrary rig no one picked.
        self.armature_combo.setCurrentIndex(-1)

        # "Panel" dropdown - two-way synced with the Manage Panels
        # N-panel section's add/rename/remove list
        # (Scene.rp_quick_action_settings.panels/panels_index), the
        # same way the Rig dropdown above is synced with that panel's
        # Rig dropdown. See Controller.change_panel() (selection) and
        # Controller.sync_panel_combo_items() (item list, refreshed
        # every poll tick to pick up N-panel add/rename/remove).
        self.stale_label = QLabel("Panel")
        self.stale_label.setObjectName("pickerSelectorLabel")

        self.stale_combo = PickerComboBox()
        self.stale_combo.setObjectName("pickerCombo")
        # Panels are scoped per rig (each rig keeps its own set), so
        # with no rig loaded there's nothing to pick a panel *for* -
        # placeholder reads "Select" and the combo starts disabled;
        # Controller.sync_panel_combo_items() flips both back once a
        # rig is actually loaded (also covers "No Panel" for a rig
        # that itself has none), on every poll tick.
        self.stale_combo.setPlaceholderText("Select")
        self.stale_combo.setEnabled(bool(self.controller.armature_name))

        # Populated from the N-panel's list rather than hardcoded
        # placeholder items, filtered to whichever rig load_armature()
        # above just loaded - panels are rig-independent (each rig
        # keeps its own set, Face/Body/etc.), the same way Section 1's
        # multi-command buttons are scoped per rig - and starting on
        # whichever row is currently selected there for THIS rig,
        # covering reopening the picker after managing the list from
        # the N-panel while the window was closed.
        try:
            scene = bpy.context.scene
            quick_settings = getattr(scene, "rp_quick_action_settings", None) if scene else None
            if quick_settings is not None:
                current_rig_name = self.controller.armature_name or ""
                for panel_item in quick_settings.panels:
                    if panel_item.armature_name == current_rig_name:
                        self.stale_combo.addItem(panel_item.name)
                if 0 <= quick_settings.panels_index < len(quick_settings.panels):
                    selected_panel = quick_settings.panels[quick_settings.panels_index]
                    if selected_panel.armature_name == current_rig_name:
                        self.stale_combo.setCurrentText(selected_panel.name)
        except Exception:
            pass


        self.capture_button = QPushButton("Capture View")
        self.add_button = QPushButton("Add Selected")
        self.clear_button = QPushButton("Clear All")
        self.delete_button = QPushButton("Delete")
        for button, width in (
            (self.capture_button, 87),
            (self.add_button, 87),
            (self.clear_button, 72),
            (self.delete_button, 52),
        ):
            # Toolbar row only (Capture View / Add Selected / Clear All /
            # Delete) - lets the stylesheet target just these buttons'
            # background without touching every other QPushButton.
            button.setObjectName("toolbarButton")
            button.setFixedWidth(width)
            button.setFixedHeight(20)
        self.size_combo = PickerComboBox()
        self.size_combo.setObjectName("pickerCombo")
        self.size_combo.addItem("L", 22)
        self.size_combo.addItem("M", 18)
        self.size_combo.addItem("S", 14)
        self.shape_combo = PickerComboBox()
        self.shape_combo.setObjectName("pickerCombo")
        self.shape_combo.addItem("Circle", "CIRCLE")
        self.shape_combo.addItem("Rectangle", "RECTANGLE")
        self.shape_combo.addItem("Triangle", "TRIANGLE")
        self.shape_combo.addItem("Square", "SQUARE")
        self.color_combo = PickerComboBox()
        self.color_combo.setObjectName("pickerCombo")
        self.color_combo.addItem("Red", "RED")
        self.color_combo.addItem("Green", "GREEN")
        self.color_combo.addItem("Blue", "BLUE")
        self.color_combo.addItem("Yellow", "YELLOW")

        for combo in (self.size_combo, self.shape_combo, self.color_combo):
            combo.setMaxVisibleItems(combo.count())
            combo.view().setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            combo.view().setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # No control is selected yet on initial build, same as after a
        # deselect - show the pickers enabled with the default-control
        # values rather than disabled/blank.
        self.set_no_selection_defaults()

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(6)

        # Same "pickerSelectorField" wrapper pattern as size_field/
        # shape_field/color_field below (0 margins, 0 spacing between
        # the label and its dropdown) so the Rig/Panel label-to-
        # dropdown spacing matches theirs exactly, instead of falling
        # back to armature_row's own unset (Qt-default) layout
        # spacing between two separately-added widgets.
        rig_field = QWidget()
        rig_field.setObjectName("pickerSelectorField")
        rig_layout = QHBoxLayout(rig_field)
        rig_layout.setContentsMargins(0, 0, 0, 0)
        rig_layout.setSpacing(0)
        rig_layout.addWidget(self.armature_label)
        rig_layout.addWidget(self.armature_combo)
        armature_row.addWidget(rig_field)

        # Same between-fields gap row 3 (selection_row below) uses
        # between its own fields (shape_field/color_field), rather
        # than the earlier one-off 16px spacing.
        armature_row.addSpacing(1)

        panel_field = QWidget()
        panel_field.setObjectName("pickerSelectorField")
        panel_layout = QHBoxLayout(panel_field)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(0)
        panel_layout.addWidget(self.stale_label)
        panel_layout.addWidget(self.stale_combo)
        armature_row.addWidget(panel_field)

        layout.addLayout(armature_row)

        toolbar.addWidget(self.capture_button)
        toolbar.addWidget(self.add_button)
        toolbar.addWidget(self.clear_button)
        toolbar.addWidget(self.delete_button)
        toolbar.addStretch()

        layout.addLayout(toolbar)

        # Keep selection settings together rather than letting the two
        # dropdowns wrap independently in the action-button flow layout.
        selection_row = QHBoxLayout()
        selection_row.setContentsMargins(0, 0, 0, 0)
        selection_row.setSpacing(0)
        size_field = QWidget()
        size_field.setObjectName("pickerSelectorField")
        size_layout = QHBoxLayout(size_field)
        size_layout.setContentsMargins(0, 0, 0, 0)
        size_layout.setSpacing(0)
        size_label = QLabel("Size")
        size_label.setObjectName("pickerSelectorLabel")
        size_layout.addWidget(size_label)
        size_layout.addWidget(self.size_combo)
        selection_row.addWidget(size_field)

        # Symmetry is a mirror-axis toggle (icon + "X" button) rather
        # than a checkbox - matches Blender's own mirror-axis buttons
        # (an axis glyph next to the letter you click to enable it).
        # The MirrorIcon is purely decorative; symmetry_toggle is the
        # actual checkable button and turns blue via QPushButton:checked
        # in the stylesheet when symmetry is on.
        self.symmetry_icon = MirrorIcon()
        self.symmetry_toggle = QPushButton("X")
        self.symmetry_toggle.setObjectName("symmetryToggle")
        self.symmetry_toggle.setCheckable(True)
        # QPushButton text otherwise renders in the platform style's own
        # control font, which can differ from the rest of the UI's plain
        # widget font - match it explicitly to a sibling label's font so
        # the "X" reads at the same size as everything else in this row.
        self.symmetry_toggle.setFont(size_label.font())

        symmetry_field = QHBoxLayout()
        symmetry_field.setContentsMargins(0, 0, 0, 0)
        symmetry_field.setSpacing(4)
        symmetry_field.addWidget(self.symmetry_icon)
        symmetry_field.addWidget(self.symmetry_toggle)
        # Stretch placed AFTER the symmetry field (not before) so the X
        # button sits at a fixed distance right after the Rig/Panel
        # dropdowns and never shifts on screen when the window is
        # resized - it's the empty space to the right of the button,
        # not the button's own position, that grows/shrinks with the
        # window now.
        armature_row.addSpacing(8)
        armature_row.addLayout(symmetry_field)
        armature_row.addStretch()

        selection_row.addSpacing(1)
        shape_field = QWidget()
        shape_field.setObjectName("pickerSelectorField")
        shape_layout = QHBoxLayout(shape_field)
        shape_layout.setContentsMargins(0, 0, 0, 0)
        shape_layout.setSpacing(0)
        shape_label = QLabel("Shape")
        shape_label.setObjectName("pickerSelectorLabel")
        shape_layout.addWidget(shape_label)
        shape_layout.addWidget(self.shape_combo)
        selection_row.addWidget(shape_field)

        selection_row.addSpacing(1)
        color_field = QWidget()
        color_field.setObjectName("pickerSelectorField")
        color_layout = QHBoxLayout(color_field)
        color_layout.setContentsMargins(0, 0, 0, 0)
        color_layout.setSpacing(0)
        color_label = QLabel("Color")
        color_label.setObjectName("pickerSelectorLabel")
        color_layout.addWidget(color_label)
        color_layout.addWidget(self.color_combo)
        selection_row.addWidget(color_field)
        selection_row.addStretch()
        layout.addLayout(selection_row)

        # QSS url() needs forward slashes even on Windows.
        chevron_icon_path = Path(
            os.path.dirname(__file__), "icons", "chevron_down.png"
        ).as_posix()
        chevron_icon_hover_path = Path(
            os.path.dirname(__file__), "icons", "chevron_down_hover.png"
        ).as_posix()
        chevron_icon_pressed_path = Path(
            os.path.dirname(__file__), "icons", "chevron_down_pressed.png"
        ).as_posix()

        def build_combo_style(icon_path, base_background="#1d1d1d", border_color="#3d3d3d"):
            return f"""
                QComboBox {{
                    min-height: 16px;
                    max-height: 16px;
                    margin: 0;
                    padding: 1px 6px;
                    border: 1px solid {border_color};
                    border-radius: 4px;
                    background: {base_background};
                    color: #d8d8d8;
                }}
                QComboBox:hover {{
                    background: #232323;
                    border: 1px solid {border_color};
                }}
                QComboBox:disabled {{
                    color: #777777;
                    background: #1d1d1d;
                    border: 1px solid #333333;
                }}
                QComboBox:focus {{
                    outline: none;
                    border: 1px solid {border_color};
                }}
                QComboBox::drop-down {{
                    width: 16px;
                    border: 0;
                    background: transparent;
                }}
                QComboBox::down-arrow {{
                    image: url({icon_path});
                    width: 10px;
                    height: 10px;
                }}
            """

        # Three complete stylesheets, differing only in which chevron PNG
        # the down-arrow rule points at (and the pressed one's base
        # background/border) - see PickerComboBox for why they're swapped
        # as whole stylesheets rather than left to QSS pseudo-states.
        combo_style = build_combo_style(chevron_icon_path)
        combo_style_hover = build_combo_style(chevron_icon_hover_path)
        combo_style_pressed = build_combo_style(
            chevron_icon_pressed_path, base_background="#4f86f7", border_color="#4f86f7"
        )


        self.armature_combo.setFixedWidth(115)
        self.stale_combo.setFixedWidth(70)
        self.size_combo.setFixedWidth(40)
        self.shape_combo.setFixedWidth(85)
        self.color_combo.setFixedWidth(65)
        self.armature_combo.setFixedHeight(16)
        self.stale_combo.setFixedHeight(16)
        self.size_combo.setFixedHeight(16)
        self.shape_combo.setFixedHeight(16)
        self.color_combo.setFixedHeight(16)
        self.armature_combo.set_hoverable_style(combo_style, combo_style_hover, combo_style_pressed)
        self.stale_combo.set_hoverable_style(combo_style, combo_style_hover, combo_style_pressed)
        self.size_combo.set_hoverable_style(combo_style, combo_style_hover, combo_style_pressed)
        self.shape_combo.set_hoverable_style(combo_style, combo_style_hover, combo_style_pressed)
        self.color_combo.set_hoverable_style(combo_style, combo_style_hover, combo_style_pressed)
        # Belt-and-suspenders on top of the QComboBox:focus{outline:none}
        # rule above - some platforms/styles draw their native focus glow
        # regardless of the stylesheet, so also stop these from taking
        # keyboard focus in the first place.
        self.armature_combo.setFocusPolicy(Qt.NoFocus)
        self.stale_combo.setFocusPolicy(Qt.NoFocus)
        self.size_combo.setFocusPolicy(Qt.NoFocus)
        self.shape_combo.setFocusPolicy(Qt.NoFocus)
        self.color_combo.setFocusPolicy(Qt.NoFocus)

        # ----------------------------------------------------
        # Picker Area
        # ----------------------------------------------------

        self.control_list = ControlList()

        layout.addWidget(self.control_list)

        # ----------------------------------------------------
        # Connections
        # ----------------------------------------------------

        self.armature_combo.currentTextChanged.connect(
            self.controller.change_armature
        )

        # Two-way synced with the Manage Panels N-panel section's list
        # (Scene.rp_quick_action_settings.panels/panels_index) - see
        # Controller.change_panel() for the guarded push in both
        # directions.
        self.stale_combo.currentTextChanged.connect(
            self.controller.change_panel
        )

        self.capture_button.clicked.connect(
            self.controller.capture_view
        )

        self.add_button.clicked.connect(
            self.controller.add_selected
        )

        self.clear_button.clicked.connect(
            self.controller.clear_all
        )

        self.delete_button.clicked.connect(
            self.controller.delete_selected
        )
        self.size_combo.currentIndexChanged.connect(
            lambda index: self.controller.set_selected_size(self.size_combo.itemData(index))
        )
        self.shape_combo.currentIndexChanged.connect(
            lambda index: self.controller.set_selected_shape(self.shape_combo.itemData(index))
        )
        self.color_combo.currentIndexChanged.connect(
            lambda index: self.controller.set_selected_color(self.color_combo.itemData(index))
        )
        # `activated` also fires when the user re-picks the color that is
        # already shown (currentIndexChanged doesn't): with Symmetry on,
        # that re-pick syncs a mirror partner whose color differs.
        self.color_combo.activated.connect(
            lambda index: self.controller.sync_mirror_color(self.color_combo.itemData(index))
        )

        self.symmetry_toggle.toggled.connect(
            self.controller.toggle_symmetry
        )

    # --------------------------------------------------------

    def sync_quick_actions(self):
        """Delegates to the canvas - kept as a method on the window too
        since custom_actions_quick.py's _sync_quick_picker_ui() calls
        backend._ACTIVE_WINDOW.sync_quick_actions() without needing to
        know that these buttons live on the canvas rather than the
        window itself.
        """
        self.control_list.container.sync_quick_actions()

    # --------------------------------------------------------

    def connect_item(self, item):

        item.clicked.connect(
            self.controller.select_control
        )

    def set_no_selection_defaults(self):
        """Called whenever no control is selected (deselect, delete,
        armature switch, undo to an empty selection, etc). The
        size/shape/color pickers stay enabled and show the controller's
        current default appearance - what a newly-added control will
        get (see controller.py's default_control_size/shape/color) -
        instead of going blank and disabled. That default starts at
        18 px / Circle / Green but is itself live-editable through
        these same combos while nothing is selected, so it's read from
        the controller rather than hardcoded here.
        """
        self.set_selected_control(
            self.controller.default_control_size,
            self.controller.default_control_shape,
            self.controller.default_control_color,
        )

    def set_selected_control(self, size, shape, color):
        # These combos edit either a selected control's appearance or
        # (when nothing is selected) the default appearance new controls
        # will get - neither is meaningful before this rig has a
        # background image, since "Add Selected" itself is disabled
        # until then (see controller.py's _update_action_controls_enabled())
        # and there's nothing to place a control onto anyway.
        #
        # Checked against the canvas's actual loaded image (like
        # _update_action_controls_enabled() does), not
        # self.controller.data["background"]'s raw path string - that
        # string can be non-empty while pointing at a file that failed
        # to load (missing/corrupted/moved), which would leave these
        # combos enabled and out of sync with every other action control
        # that already correctly disables itself in that situation.
        #
        # self.control_list doesn't exist yet the very first time this
        # runs - build_ui() calls set_no_selection_defaults() (which
        # calls this) BEFORE creating self.control_list further down, to
        # get the combos showing sane default values as soon as they're
        # built. getattr(..., None) treats that one early call as "no
        # background yet" (correct - nothing has loaded at that point)
        # instead of crashing.
        control_list = getattr(self, "control_list", None)
        has_background = (
            control_list is not None
            and control_list.container.background is not None
        )
        self.size_combo.setEnabled(has_background)
        self.shape_combo.setEnabled(has_background)
        self.color_combo.setEnabled(has_background)

        size_index = self.size_combo.findData(size)
        if size_index >= 0:
            self.size_combo.blockSignals(True)
            self.size_combo.setCurrentIndex(size_index)
            self.size_combo.blockSignals(False)

        shape_index = self.shape_combo.findData(shape)
        if shape_index >= 0:
            self.shape_combo.blockSignals(True)
            self.shape_combo.setCurrentIndex(shape_index)
            self.shape_combo.blockSignals(False)

        color_index = self.color_combo.findData(color)
        if color_index >= 0:
            self.color_combo.blockSignals(True)
            self.color_combo.setCurrentIndex(color_index)
            self.color_combo.blockSignals(False)

    @safe_override(False)
    def eventFilter(self, obj, event):
        if (
            event.type() == QEvent.KeyPress
            and event.key() in (Qt.Key_Tab, Qt.Key_Backtab)
            and (event.modifiers() & Qt.ShiftModifier)
            and not (event.modifiers() & Qt.ControlModifier)
            and not event.isAutoRepeat()
        ):
            # Only react while this picker window (or one of its child
            # widgets) is the thing actually focused/active - otherwise
            # Shift+Tab typed into some other completely unrelated
            # Blender/OS window would also cycle this dropdown, since an
            # application-wide filter sees every window's key events.
            if QApplication.activeWindow() is self:
                count = self.armature_combo.count()
                model = self.armature_combo.model()
                for step in range(1, count + 1):
                    next_index = (self.armature_combo.currentIndex() + step) % count
                    item = model.item(next_index)
                    # Skip rigs hidden in the viewport (disabled items).
                    if item is None or item.isEnabled():
                        self.armature_combo.setCurrentIndex(next_index)
                        break
                return True

        if (
            event.type() == QEvent.KeyPress
            and event.key() == Qt.Key_Tab
            and (event.modifiers() & Qt.ControlModifier)
            and not (event.modifiers() & Qt.ShiftModifier)
            and not event.isAutoRepeat()
        ):
            # Panel-level counterpart to Shift+Tab above - same
            # app-wide event filter, for the same reason: Tab (with or
            # without modifiers) is intercepted by whichever child
            # widget has focus for its own focusNextPrevChild()
            # handling before a plain keyPressEvent() override here
            # would ever see it.
            if QApplication.activeWindow() is self:
                count = self.stale_combo.count()
                if count > 0 and self.stale_combo.isEnabled():
                    next_index = (self.stale_combo.currentIndex() + 1) % count
                    self.stale_combo.setCurrentIndex(next_index)
                return True

        return super().eventFilter(obj, event)

    @safe_override()
    def keyPressEvent(self, event):

        if event.isAutoRepeat():
            # Ignore key-repeat events entirely. Each shortcut below calls
            # a controller method that records an undo snapshot right
            # before it runs (self.controller._record_undo_snapshot()) -
            # so if a key is held even briefly, Qt's autorepeat would fire
            # that method again and again, each time overwriting the
            # snapshot with the state the previous repeat already
            # produced. The result: Ctrl+Z has nothing left to undo back
            # to. Requiring a fresh physical press keeps one call per
            # press, matching what a single button click does.
            event.accept()
            return

        if event.key() == Qt.Key_A and not (
            event.modifiers()
            & (
                Qt.ControlModifier
                | Qt.ShiftModifier
                | Qt.AltModifier
                | Qt.MetaModifier
            )
        ):
            # Select All is the plain A key only, like G below. Ctrl+A,
            # Shift+A and Alt+A are different shortcuts (in Blender
            # itself Shift+A is "Add" and Alt+A is "deselect"), so a
            # modified A is left unhandled instead of silently selecting
            # every control. (Meta covers macOS, where Qt reports the
            # physical Ctrl key as MetaModifier.)
            self.controller.select_all()
            event.accept()
            return

        if event.key() == Qt.Key_G and not (
            event.modifiers()
            & (
                Qt.ControlModifier
                | Qt.ShiftModifier
                | Qt.AltModifier
                | Qt.MetaModifier
            )
        ):
            # Plain G only, same as A above: Meta+G (Cmd on macOS, Win on
            # Windows/Linux) is a different shortcut and must not start a
            # grab. Same as pressing and dragging whatever the hand cursor
            # is on (a control, a Quick Action button/toggle, the symmetry
            # handle) - it then follows the mouse until click/Enter/G
            # (confirm) or Esc/right-click (cancel).
            if self.control_list.container.begin_grab():
                event.accept()
                return

        if (
            event.key() == Qt.Key_I
            and (event.modifiers() & Qt.ControlModifier)
            and not (event.modifiers() & (Qt.ShiftModifier | Qt.AltModifier))
        ):
            # Invert the picker's selection (Ctrl+I, as in Blender).
            self.controller.invert_selection()
            event.accept()
            return

        if (
            event.key() == Qt.Key_Z
            and (event.modifiers() & Qt.ControlModifier)
            and (event.modifiers() & Qt.ShiftModifier)
        ):
            self.controller.redo()
            event.accept()
            return

        if (
            event.key() == Qt.Key_Z
            and (event.modifiers() & Qt.ControlModifier)
            and not (event.modifiers() & Qt.ShiftModifier)
        ):
            self.controller.undo()
            event.accept()
            return

        super().keyPressEvent(event)

    @safe_override()
    def enterEvent(self, event):

        self.activateWindow()
        self.raise_()
        self.setFocus(Qt.ActiveWindowFocusReason)

        super().enterEvent(event)


    @safe_override()
    def leaveEvent(self, event):

        self.clearFocus()

        super().leaveEvent(event)
    
    @safe_override()
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resize_timer.start(200)

    @safe_override()
    def showEvent(self, event):
        super().showEvent(event)
        self._install_key_filter()

        # See the comment on _needs_post_show_relayout in __init__: the
        # very first load_armature() ran before this window had ever
        # been shown, so its canvas may have been laid out at a smaller,
        # not-yet-final size - re-run it now that the window is shown.
        # Only needed once; later show()s (reopening an already-
        # constructed, previously-shown window) already have a
        # correctly-sized canvas.
        #
        # Deferred rather than called directly here: showEvent() firing
        # doesn't guarantee Qt (or, running inside Blender, bqt's own
        # periodic pump of Qt's event loop) has actually finished
        # settling the central widget's final layout geometry at that
        # exact instant - a direct call, or even a single queued 0ms
        # call, could still see the same not-yet-final size it's trying
        # to correct, on some platforms and window managers a bit more
        # than others. _try_post_show_relayout() below polls the
        # canvas's size a few times a beat apart instead of trusting any
        # single moment, and only runs the corrective refresh() once
        # that size has actually stopped changing between two checks.
        if getattr(self, "_needs_post_show_relayout", False):
            self._needs_post_show_relayout = False
            self._post_show_relayout_attempts = 0
            self._post_show_relayout_last_size = None
            QTimer.singleShot(0, self._try_post_show_relayout)

    def _try_post_show_relayout(self):
        """Polls the picker canvas's size every 30ms (capped at 10 tries,
        ~300ms) until it stops changing between two consecutive checks,
        then runs the corrective controller.refresh() exactly once -
        see the comment in showEvent() above for why a single queued
        call isn't reliable enough on its own.
        """
        from .. import backend

        canvas = self.control_list.container
        current_size = canvas.size()
        self._post_show_relayout_attempts += 1

        settled = current_size == self._post_show_relayout_last_size
        if settled or self._post_show_relayout_attempts >= 10:
            # The very first load_armature() call in __init__ can run
            # before backend._CACHED_ARM has settled to match the real
            # active object (e.g. ensure_qt() blocking a long time on
            # its very first pip install). Re-resolve now that the
            # window is fully up and running, and do a full reload (not
            # just refresh()) if it doesn't match what got loaded the
            # first time - including reloading to *no* rig if that's
            # genuinely what's active (a camera, nothing, etc.) rather
            # than ever substituting some other armature the user
            # didn't pick. Safe to do even when armature_name is still
            # None, since save() no-ops with nothing to accidentally
            # overwrite in that case.
            current_rig = backend.arm()
            current_rig_name = current_rig.name if current_rig is not None else None
            if self.controller.armature_name != current_rig_name:
                self.controller.load_armature(current_rig)
            else:
                self.controller.refresh()

            # Match the combo's displayed selection immediately rather
            # than waiting for the next poll tick's
            # _sync_armature_combo_selection() to catch up - same
            # reasoning as the matching block right after the first
            # load_armature() call in __init__.
            self.armature_combo.blockSignals(True)
            try:
                self.armature_combo.setCurrentIndex(
                    self.armature_combo.findText(current_rig_name) if current_rig_name else -1
                )
            finally:
                self.armature_combo.blockSignals(False)

            # refresh() above only *schedules* the corrected repaint via
            # canvas.update() - inside Blender's own event loop (which
            # doesn't pump Qt continuously) that can sit pending
            # indefinitely until some unrelated Qt event forces a
            # redraw, same as PickerComboBox._apply_style()'s comment
            # above describes. That's exactly why switching to another
            # armature and back "fixed" it: the combo interaction
            # itself was the unrelated event forcing the redraw, not
            # anything about the switch. Force it here instead, the
            # same way that method does, so the corrected layout is
            # actually visible immediately rather than waiting for the
            # user to stumble into some other event that happens to
            # trigger it.
            canvas.repaint()
            self.repaint()
            return

        self._post_show_relayout_last_size = current_size
        QTimer.singleShot(30, self._try_post_show_relayout)

    @safe_override()
    def moveEvent(self, event):
        super().moveEvent(event)
        self.move_timer.start(200)

    @safe_override()
    def closeEvent(self, event):
        # Remove the application-wide event filter installed in
        # __init__ for the Shift+Tab rig-cycling/Ctrl+Tab panel-cycling
        # shortcuts - otherwise
        # QApplication would keep calling eventFilter() on this window
        # for every key press anywhere, even after it's closed.
        QApplication.instance().removeEventFilter(self)

        # Save the window's current size and position the moment the
        # picker is closed - unconditionally, not just when a resize/move
        # debounce timer happens to still be pending. This guarantees the
        # state on disk is up to date as soon as the UI window closes,
        # rather than only ending up correct incidentally (e.g. whenever
        # the .blend file is next saved).
        self.resize_timer.stop()
        self.move_timer.stop()

        self.save_window_size()
        self.save_window_position()

        # Any background image clear_all() staged for deletion (kept
        # around only in case that clear got undone), across every rig's
        # undo history, is now safe to actually remove, since the picker
        # is closing and there's no more chance of an undo for any of
        # them. Same for any capture_view() backups kept in case a
        # capture got undone.
        self.controller.settle_all_pending_images()

        # The N-panel's Manage Panels / Multi Command Buttons / Single
        # Command Buttons sections are gated on is_picker_open() (see
        # launcher.py) so they disappear once the picker's closed -
        # but Blender only re-evaluates a Panel's poll() on that area's
        # next redraw, which closing this Qt window (via its own X
        # button, outside Blender's own event handling) doesn't trigger
        # by itself. Tag every UI region redraw now so those sections
        # actually vanish immediately instead of lingering until some
        # unrelated viewport interaction happens to redraw them.
        try:
            import bpy
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == 'VIEW_3D':
                        for region in area.regions:
                            if region.type == 'UI':
                                region.tag_redraw()
        except Exception:
            pass

        # Closed: no longer bound to any file. show_picker() binds it to
        # whatever file is open when it is shown again.
        from .. import json_manager
        json_manager.unbind_window()

        super().closeEvent(event)

    def save_window_size(self):
        if not self._geometry_save_enabled:
            return
        from .. import json_manager

        json_manager.save_window_size(
            self.width(),
            self.height(),
        )

    def save_window_position(self):
        if not self._geometry_save_enabled:
            return
        from .. import json_manager

        json_manager.save_window_position(
            self.x(),
            self.y(),
        )

    # -----------------------------------------------------
    # Per-file binding and geometry
    # -----------------------------------------------------

    def _update_title(self):
        """Shows which .blend this window belongs to, so two Blender
        instances are easy to tell apart."""
        from .. import json_manager
        self.setWindowTitle(f"Rig Picker - {json_manager.get_bound_file_label()}")

    def flush_geometry(self):
        """load_pre: writes the current size/position to the OUTGOING
        file's store and stops the debounce timers, so no pending timer
        can fire after the file has changed. Does nothing for a closed
        (unbound) window."""
        from .. import json_manager
        self.resize_timer.stop()
        self.move_timer.stop()
        if json_manager.is_window_bound():
            self.save_window_size()
            self.save_window_position()

    def _position_is_on_screen(self, x, y):
        # A saved position may come from another monitor setup (the data
        # folder travels with the project), so it is checked against the
        # screens that exist now before it is trusted.
        for screen in QApplication.instance().screens():
            if screen.availableGeometry().adjusted(-50, -50, 50, 50).contains(x, y):
                return True
        return False

    def apply_saved_position(self):
        """Moves to the saved position of the current file, or - if none
        is saved, or it would be off-screen - to the default placement
        next to the Blender window."""
        from .. import json_manager
        saved = json_manager.get_window_position()
        if saved is not None and self._position_is_on_screen(*saved):
            self.move(*saved)
            return
        blender_rect = QApplication.instance().blender_widget.frameGeometry()
        self.move(
            blender_rect.right() - self.width() - 10,
            blender_rect.top() + 30,
        )

    def apply_saved_geometry(self):
        """Applies the current file's saved size and position (or the
        defaults) under a no-save flag, so applying them is never written
        back as a user change. Called after the store has been swapped to a
        new file, and when a closed window is shown again."""
        from .. import json_manager
        self._geometry_save_enabled = False
        self.resize_timer.stop()
        self.move_timer.stop()
        try:
            default_width, default_height = _scale_to_screen(REFERENCE_DEFAULT_SIZE)
            width, height = json_manager.get_window_size(default_width, default_height)
            self.resize(width, height)
            self.apply_saved_position()
        finally:
            # The resize/move above may start the debounce timers; they are
            # stopped again now and the flag stays off a little longer than
            # their delay, so nothing from this apply is ever saved.
            self.resize_timer.stop()
            self.move_timer.stop()
            QTimer.singleShot(
                400, lambda: setattr(self, "_geometry_save_enabled", True)
            )
        self._update_title()

    def bind_to_current_file(self):
        """A window that was closed (hidden) is being shown again: bind it
        to the file that is open now and apply that file's geometry."""
        from .. import json_manager
        json_manager.bind_window()
        self.apply_saved_geometry()