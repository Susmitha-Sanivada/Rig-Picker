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
        # Position relative to Blender
        # -----------------------------
        _window.adjustSize()

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