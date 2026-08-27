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
    QSizePolicy,
    QCheckBox
)
from PySide6.QtCore import Qt, QPoint, QRectF, QTimer
from PySide6.QtGui import QPainterPath, QRegion

from .control_list import ControlList
from .controller import Controller

from pathlib import Path
import bpy


class PickerComboBox(QComboBox):
    """A combo box whose full option list opens directly below the field."""

    def showPopup(self):
        menu = QMenu(self)
        menu.setObjectName("pickerComboPopup")
        menu.setWindowFlags(
            Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
        )
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
                background-color: #353535;
                border: 1px solid #4d4d4d;
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
                padding: 4px 10px;
            }
            QMenu#pickerComboPopup::item:checked {
                background-color: #4f86f7;
                color: white;
            }
            QMenu#pickerComboPopup::item:selected {
                background-color: #505050;
            }
            QMenu#pickerComboPopup::item:checked:selected {
                background-color: #4f86f7;
            }
        """)

        for index in range(self.count()):
            action = menu.addAction(self.itemText(index))
            action.setCheckable(True)
            action.setChecked(index == self.currentIndex())
            action.triggered.connect(
                lambda checked=False, value=index: self.setCurrentIndex(value)
            )

        menu.adjustSize()
        rounded_path = QPainterPath()
        rounded_path.addRoundedRect(QRectF(menu.rect()), 4, 4)
        menu.setMask(QRegion(rounded_path.toFillPolygon().toPolygon()))

        menu.exec(self.mapToGlobal(QPoint(0, self.height())))

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
        self.setMinimumSize(330, 580)

        self.controller = Controller()
        self.controller.set_window(self)

        # Created before build_ui()/resize() below, since either of those
        # can trigger resizeEvent()/moveEvent() - which reference these
        # timers - before the window is ever shown.
        self.resize_timer = QTimer(self)
        self.resize_timer.setSingleShot(True)
        self.resize_timer.timeout.connect(self.save_window_size)

        self.move_timer = QTimer(self)
        self.move_timer.setSingleShot(True)
        self.move_timer.timeout.connect(self.save_window_position)

        self.build_ui()
        from .. import json_manager

        width, height = json_manager.get_window_size()

        self.resize(width, height)

        # Make this the addon's single active controller, so the
        # depsgraph handler in backend.py can refresh it automatically
        # whenever the active armature changes.
        from .. import backend
        backend._ACTIVE_CONTROLLER = self.controller
        backend._ACTIVE_WINDOW = self

        # Load the currently active armature's picker from JSON
        self.controller.load_armature(backend.arm())

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
        armature_row.setContentsMargins(0, 0, 0, 0)

        self.armature_combo = PickerComboBox()
        self.armature_combo.setObjectName("pickerCombo")
        # bpy.context.view_layer.objects, not bpy.data.objects - the
        # latter includes armatures from other scenes and orphaned
        # datablocks that aren't actually selectable/active-able here
        # (see the matching note in controller.py's
        # _sync_armature_combo_items, which rebuilds this same list).
        for obj in bpy.context.view_layer.objects:
            if obj.type == "ARMATURE":
                self.armature_combo.addItem(obj.name)
                # Grey out rigs already hidden at window-creation time,
                # rather than leaving every entry enabled until the next
                # poll tick calls controller._sync_armature_combo_items()
                # and corrects it a moment later.
                if obj.hide_get():
                    index = self.armature_combo.count() - 1
                    item = self.armature_combo.model().item(index)
                    if item is not None:
                        item.setEnabled(False)

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

        armature_row.addWidget(self.armature_combo)
        armature_row.addStretch()
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

        self.symmetry_checkbox = QCheckBox("Symmetry")
        self.ik_fk_checkbox = QCheckBox("IK-FK")
        self.motion_paths_checkbox = QCheckBox("Motion Paths")
        checkbox_row = QHBoxLayout()
        checkbox_row.setContentsMargins(0, 0, 0, 0)
        checkbox_row.setSpacing(14)
        checkbox_row.addWidget(self.symmetry_checkbox)
        checkbox_row.addWidget(self.ik_fk_checkbox)
        checkbox_row.addWidget(self.motion_paths_checkbox)
        checkbox_row.addStretch()
        layout.addLayout(checkbox_row)

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

        combo_style = """
            QComboBox {
                min-height: 18px;
                max-height: 20px;
                margin: 0;
                padding: 1px 6px;
                border: 1px solid #5b5b5b;
                border-radius: 4px;
                background: #292929;
                color: #d8d8d8;
            }
            QComboBox:hover {
                background: #464646;
                border: 1px solid #7a7a7a;
            }
            QComboBox:pressed {
                background: #353535;
            }
            QComboBox:disabled {
                color: #777777;
                background: #292929;
                border: 1px solid #454545;
            }
            QComboBox::drop-down {
                width: 0px;
                border: 0;
                background: transparent;
            }
            QComboBox::down-arrow { image: none; width: 0px; height: 0px; }
        """
        self.armature_combo.setFixedWidth(180)
        self.size_combo.setFixedWidth(55)
        self.shape_combo.setFixedWidth(75)
        self.color_combo.setFixedWidth(60)
        self.armature_combo.setFixedHeight(20)
        self.size_combo.setFixedHeight(20)
        self.shape_combo.setFixedHeight(20)
        self.color_combo.setFixedHeight(20)
        self.armature_combo.setStyleSheet(combo_style)
        self.size_combo.setStyleSheet(combo_style)
        self.shape_combo.setStyleSheet(combo_style)
        self.color_combo.setStyleSheet(combo_style)

        # ----------------------------------------------------
        # Picker Area
        # ----------------------------------------------------

        self.control_list = ControlList()

        layout.addWidget(self.control_list)
        self.control_list.container.connect_controller(self.controller)

        # ----------------------------------------------------
        # Connections
        # ----------------------------------------------------

        self.armature_combo.currentTextChanged.connect(
            self.controller.change_armature
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

        self.symmetry_checkbox.toggled.connect(
            self.controller.toggle_symmetry
        )

        self.ik_fk_checkbox.toggled.connect(
            self.controller.toggle_ik_fk_setting
        )

        self.motion_paths_checkbox.toggled.connect(
            self.controller.toggle_motion_paths_setting
        )

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

        if event.key() == Qt.Key_A:
            self.controller.select_all()
            event.accept()
            return

        if (
            event.key() == Qt.Key_H
            and (event.modifiers() & Qt.AltModifier)
        ):
            self.controller.show_all()
            event.accept()
            return

        if event.key() == Qt.Key_H:
            self.controller.hide_all()
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

    def enterEvent(self, event):

        self.activateWindow()
        self.raise_()
        self.setFocus(Qt.ActiveWindowFocusReason)

        super().enterEvent(event)


    def leaveEvent(self, event):

        self.clearFocus()

        super().leaveEvent(event)
    
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resize_timer.start(200)

    def showEvent(self, event):
        super().showEvent(event)

        # See the comment on _needs_post_show_relayout in __init__: the
        # very first load_armature() ran before this window had ever
        # been shown, so its canvas may have been laid out at a smaller,
        # not-yet-final size - re-run it now that the window is shown.
        # Only needed once; later show()s (reopening an already-
        # constructed, previously-shown window) already have a
        # correctly-sized canvas.
        #
        # Deferred with a 0ms singleShot rather than called directly
        # here: showEvent() firing doesn't guarantee Qt has finished
        # computing the central widget's final layout geometry at that
        # exact instant (QMainWindow's layout pass can still be
        # pending) - a direct call could still see the same
        # not-yet-final size it's trying to correct. Queuing this to
        # run right after the current event loop iteration lets Qt
        # finish that pending layout first.
        if getattr(self, "_needs_post_show_relayout", False):
            self._needs_post_show_relayout = False
            QTimer.singleShot(0, self.controller.refresh)

    def moveEvent(self, event):
        super().moveEvent(event)
        self.move_timer.start(200)

    def closeEvent(self, event):
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

        super().closeEvent(event)

    def save_window_size(self):
        from .. import json_manager

        json_manager.save_window_size(
            self.width(),
            self.height(),
        )

    def save_window_position(self):
        from .. import json_manager

        json_manager.save_window_position(
            self.x(),
            self.y(),
        )