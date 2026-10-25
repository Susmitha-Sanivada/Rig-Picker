"""
launcher.py

Creates and shows the Rig Picker window.
"""

_window = None

# Whether json_manager's error hook has been wired up to a visible Qt
# warning yet (see _install_error_popup() below). Only needs doing once
# per session - json_manager.set_error_handler() itself just overwrites
# on repeat calls, but there's no reason to reregister every open.
_error_popup_installed = False

# The currently visible json_manager error box, if any - see
# _install_error_popup()._show_error().
_error_box = None


def _install_error_popup():
    """Makes json_manager's load/save failures (disk full, locked file,
    bad permissions, corrupted JSON with no readable backup...) show up
    as an actual popup instead of only a console line nobody's looking
    at. Registered lazily here - the first place in the addon guaranteed
    to have Qt/a QApplication ready - rather than from json_manager
    itself, which is intentionally Qt-agnostic (it also runs before Qt
    is initialized, e.g. at addon register time)."""
    global _error_popup_installed

    if _error_popup_installed:
        return

    from .. import json_manager

    def _show_error(message):
        # NON-blocking and de-duplicated. This hook fires from json_manager
        # load/save failures, which can happen inside bpy.app.timers
        # callbacks and Blender save/load handlers. A blocking
        # QMessageBox.warning() there runs a nested Qt event loop from
        # inside Blender's callback, and a save that keeps failing (disk
        # full, locked file) would otherwise stack up one dialog per
        # attempt. So: show a non-modal box, and while one is already on
        # screen just update its text instead of opening another.
        global _error_box
        import shiboken6
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox

        box = _error_box
        if box is not None and shiboken6.isValid(box) and box.isVisible():
            if box.text() != message:
                box.setText(message)
            return

        parent = _window if (
            _window is not None and shiboken6.isValid(_window)
        ) else None
        box = QMessageBox(
            QMessageBox.Warning, "Rig Picker", message, QMessageBox.Ok, parent
        )
        box.setAttribute(Qt.WA_DeleteOnClose)
        box.setWindowModality(Qt.NonModal)
        _error_box = box
        box.show()

    json_manager.set_error_handler(_show_error)
    _error_popup_installed = True


def show_picker():

    global _window

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()

    _install_error_popup()

    # Import AFTER Qt is ready
    import shiboken6
    from .main_window import RigPickerWindow

    is_new_window = (
        _window is None
        or not shiboken6.isValid(_window)
    )

    if is_new_window:

        _window = RigPickerWindow(
            parent=app.blender_widget
        )

        _window.setObjectName("RigPicker")

        # -----------------------------
        # Position
        # -----------------------------
        # NOTE: intentionally NOT calling _window.adjustSize() here.
        # The window's size was already restored from the file's data.json
        # inside RigPickerWindow.__init__() (self.resize(width, height)).
        # adjustSize() would immediately discard that and snap the window
        # back to its layout's sizeHint(), which is exactly why the saved
        # size never appeared to "stick" on reopen.
        # Saved position of this file (checked against the screens that
        # exist now), else next to the Blender window.
        _window.apply_saved_position()

    else:
        # Window already exists (just hidden) - the active armature may
        # have changed since it was last shown, so make sure the picker
        # reflects it.
        # It was closed (unbound) - bind it to the file that is open now
        # and apply that file's saved geometry.
        _window.bind_to_current_file()
        from ..backend import visible_arm
        _window.controller.load_armature(visible_arm())

    _window.show()
    _window.raise_()
    _window.activateWindow()

    # Blender's own event loop doesn't pump Qt continuously (see the
    # comment on PickerComboBox._apply_style() in main_window.py) - a
    # window that's only just been shown can sit with pending,
    # unpainted content until some unrelated Qt event later happens to
    # force a redraw. Force one immediately so the picker is actually
    # fully drawn the moment it appears, rather than needing an
    # unrelated interaction (like switching armatures) first.
    # Direct synchronous repaint of just this window - not
    # QApplication.processEvents(), which would dispatch every queued Qt
    # event (and Python event overrides) from inside Blender's operator.
    _window.repaint()

    return _window


def close_picker():
    """Closes the picker window if one is currently open, and forgets the
    reference to it.

    Called from __init__.unregister() - without this, disabling the addon
    (or reloading scripts) while the picker is open left the window on
    screen holding a controller wired up to bpy.ops.rp.* operator classes
    that unregister() is about to remove. Every click on it afterward
    would raise AttributeError instead of just... not being clickable, and
    the window itself never got cleaned up at all.
    """
    global _window

    # Nothing to do if the picker was never opened this session - skip
    # the shiboken6 import entirely rather than risk it failing because
    # ensure_qt() (which installs PySide6/shiboken6) never got a chance
    # to run.
    if _window is None:
        return

    import shiboken6

    if shiboken6.isValid(_window):
        _window.close()

    _window = None


def is_picker_open():
    """Whether the Rig Picker window currently exists and is visible on
    screen.

    Used to gate the N-panel's Manage Panels / Multi Command Buttons /
    Single Command Buttons sections (see custom_actions_quick.py's
    RP_PT_quick_* panels' poll()) so they're only shown while the picker
    itself is open, and hide again as soon as it's closed - whether via
    close_picker() above or the user clicking the window's own close
    button (which only hides/destroys it; it doesn't go through
    close_picker(), so _window can still be a stale-but-valid reference
    here - isVisible() is what actually catches that case).
    """
    if _window is None:
        return False

    # PySide6/shiboken6 are installed on first picker open (see
    # dependency.py) - before that's ever happened this session, they
    # may not be importable yet. No window can exist yet either way, so
    # treat that the same as "not open" rather than letting an
    # ImportError bubble up into every N-panel redraw.
    try:
        import shiboken6
    except ImportError:
        return False

    if not shiboken6.isValid(_window):
        return False

    return _window.isVisible()