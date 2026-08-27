"""
launcher.py

Creates and shows the Rig Picker window.
"""

from ..dependency import ensure_qt

_window = None


def show_picker():

    global _window

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()

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
        # The window's size was already restored from rig_picker_data.json
        # inside RigPickerWindow.__init__() (self.resize(width, height)).
        # adjustSize() would immediately discard that and snap the window
        # back to its layout's sizeHint(), which is exactly why the saved
        # size never appeared to "stick" on reopen.
        from .. import json_manager

        saved_pos = json_manager.get_window_position()
        restored = False

        if saved_pos is not None:
            x, y = saved_pos

            # Sanity-check the saved position against currently available
            # screens before trusting it - e.g. it may have been saved on
            # a second monitor that isn't connected this time, which would
            # otherwise place the window somewhere the user can't see it.
            for screen in app.screens():
                if screen.availableGeometry().adjusted(
                    -50, -50, 50, 50
                ).contains(x, y):
                    _window.move(x, y)
                    restored = True
                    break

        if not restored:
            # First-ever launch, or saved position is off-screen: fall
            # back to positioning relative to the Blender window.
            blender_rect = app.blender_widget.frameGeometry()

            x = blender_rect.right() - _window.width() - 10
            y = blender_rect.top() + 30

            _window.move(x, y)

    else:
        # Window already exists (just hidden) - the active armature may
        # have changed since it was last shown, so make sure the picker
        # reflects it.
        from ..backend import arm
        _window.controller.load_armature(arm())

    _window.show()
    _window.raise_()
    _window.activateWindow()

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