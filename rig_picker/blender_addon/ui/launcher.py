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

    if (
        _window is None
        or not shiboken6.isValid(_window)
    ):

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

    _window.show()
    _window.raise_()
    _window.activateWindow()

    return _window