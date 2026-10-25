"""
mirror_icon.py

Renders the mirror/symmetry glyph (a small PNG shipped in ui/icons/) that
sits directly to the left of the Symmetry "X" axis-toggle button in the
main window's checkbox row (see main_window.py). The source PNG is a
plain black-on-transparent graphic; it's recolored at paint time to match
whatever color the rest of the checkbox-row UI uses for enabled/disabled
text, the same way the previous hand-drawn version behaved.

This widget never receives clicks itself - toggling symmetry on/off is
entirely the adjacent QPushButton's job (see PickerWindow's
symmetry_toggle). Keeping the glyph non-interactive matches Blender's
own layout, where the axis icon is a label and the letter buttons next
to it are what you actually click.
"""

import os

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPainter, QPixmap, QColor
from PySide6.QtWidgets import QWidget


from .safe_qt import safe_override


class MirrorIcon(QWidget):

    ENABLED_COLOR = QColor(215, 215, 215)   # matches QWidget's default text color (#d7d7d7)
    DISABLED_COLOR = QColor(111, 111, 111)  # matches QLabel#ikfkOverlayLabel:disabled (#6f6f6f)

    _SOURCE = None  # unrecolored source pixmap, loaded once and shared

    def __init__(self, parent=None):
        super().__init__(parent)
        # A little smaller than the old hand-drawn glyph's 26x18 box.
        self.setFixedSize(20, 20)

    @classmethod
    def _source_pixmap(cls):
        if cls._SOURCE is None:
            icon_path = os.path.join(os.path.dirname(__file__), "icons", "mirror_icon.png")
            cls._SOURCE = QPixmap(icon_path)
        return cls._SOURCE

    def _tinted_pixmap(self, color):
        """Recolor the source glyph's opaque pixels to `color`, keeping
        its existing alpha as a mask (so it stays anti-aliased)."""
        source = self._source_pixmap()
        tinted = QPixmap(source.size())
        tinted.fill(Qt.transparent)
        painter = QPainter(tinted)
        painter.drawPixmap(0, 0, source)
        painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
        painter.fillRect(tinted.rect(), color)
        painter.end()
        return tinted

    @safe_override()
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        color = self.ENABLED_COLOR if self.isEnabled() else self.DISABLED_COLOR
        pixmap = self._tinted_pixmap(color)

        # Source art is square - fit it inside the widget with a small
        # margin, preserving aspect ratio and centering it.
        margin = 1.0
        side = min(self.width(), self.height()) - margin * 2
        x = (self.width() - side) / 2.0
        y = (self.height() - side) / 2.0
        painter.drawPixmap(QRectF(x, y, side, side), pixmap, QRectF(pixmap.rect()))
