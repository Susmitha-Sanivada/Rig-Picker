"""
launcher.py

Starts the floating Rig Picker window.
"""

import sys
import site

# Add the user's site-packages (where pip installed PySide6)
user_site = site.getusersitepackages()
if user_site not in sys.path:
    site.addsitedir(user_site)

from PySide6.QtWidgets import QApplication

from .main_window import RigPickerWindow

from PySide6.QtCore import Qt


_app = None
_window = None


def show_picker():

    global _app
    global _window

    _app = QApplication.instance()

    if _app is None:
        _app = QApplication(sys.argv)

    if _window is None:
        _window = RigPickerWindow()

        # Set the window flags only once
        _window.setWindowFlag(Qt.Window, True)
        _window.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        # Apply the flags
        _window.hide()
        
        # Position the window on the far right
        screen = _app.primaryScreen().availableGeometry()

        x = screen.right() - _window.width() - 20   # 20 px from the right edge
        y = 80                                      # Distance from the top

        _window.move(x, y)

    _window.show()
    _window.raise_()
    _window.activateWindow()

    return _window