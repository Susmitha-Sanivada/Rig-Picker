"""
launcher.py

Creates and shows the Rig Picker window.
"""

from ..dependency import ensure_qt

_window = None


def show_picker():

    global _window

    app = ensure_qt()

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

    _window.show()
    _window.raise_()
    _window.activateWindow()

    return _window