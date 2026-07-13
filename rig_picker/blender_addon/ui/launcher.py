"""
launcher.py

Starts the floating Rig Picker window using BQT.
"""

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from .main_window import RigPickerWindow


_window = None


def show_picker():

    global _window

    from PySide6.QtWidgets import QApplication
    import bl_ext.user_default.bqt as bqt

    app = QApplication.instance()

    if app is None:

        # Start BQT manually
        bqt.register()

        app = QApplication.instance()

    if app is None:
        raise RuntimeError("Unable to initialize BQT.")

    parent = app.blender_widget

    if _window is None:

        _window = RigPickerWindow(parent)

        _window.setObjectName("RigPicker")

        _window.setWindowFlag(Qt.Tool, True)

        screen = app.primaryScreen().availableGeometry()

        x = screen.right() - _window.width() - 20
        y = 80

        _window.move(x, y)

    _window.show()

    _window.raise_()

    _window.activateWindow()

    return _window