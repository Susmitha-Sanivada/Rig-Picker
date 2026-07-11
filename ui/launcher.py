"""
launcher.py

Starts the floating Rig Picker window.
"""

import sys

from PySide6.QtWidgets import QApplication

from ui.main_window import RigPickerWindow


_app = None
_window = None


def show_picker():

    global _app
    global _window

    # Reuse existing QApplication if one exists.
    _app = QApplication.instance()

    if _app is None:
        _app = QApplication(sys.argv)

    # Only one picker window.
    if _window is None:
        _window = RigPickerWindow()

    _window.show()
    _window.raise_()
    _window.activateWindow()

    return _window