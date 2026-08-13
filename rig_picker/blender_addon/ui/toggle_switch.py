"""
toggle_switch.py

A compact sliding on/off switch - the "dark mode toggle" style seen on
websites - for lightweight boolean settings in the picker overlay, as
opposed to a regular button that fires a one-off action.
"""

from PySide6.QtCore import Qt, QRectF, QPropertyAnimation, QEasingCurve, Property, Signal
from PySide6.QtGui import QPainter, QColor, QBrush, QPen
from PySide6.QtWidgets import QWidget


class ToggleSwitch(QWidget):

    toggled = Signal(bool)

    TRACK_OFF = QColor(70, 70, 70)
    TRACK_ON = QColor(58, 142, 88)   # matches CircleControl's "GREEN"
    BORDER = QColor(15, 15, 15, 200)
    KNOB = QColor(232, 232, 232)

    TRACK_OFF_DISABLED = QColor(50, 50, 50)
    TRACK_ON_DISABLED = QColor(60, 80, 66)
    KNOB_DISABLED = QColor(150, 150, 150)

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)

        self._checked = checked
        self._knob_pos = 1.0 if checked else 0.0

        self.setFixedSize(32, 16)
        self.setCursor(Qt.PointingHandCursor)

        # Animates the knob sliding across, rather than snapping instantly,
        # so it reads as a slider rather than a two-state icon swap.
        self._anim = QPropertyAnimation(self, b"knob_pos", self)
        self._anim.setDuration(120)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    # -----------------------------------------------------
    # Animatable knob position (0.0 = off/left, 1.0 = on/right)
    # -----------------------------------------------------

    def _get_knob_pos(self):
        return self._knob_pos

    def _set_knob_pos(self, value):
        self._knob_pos = value
        self.update()

    knob_pos = Property(float, _get_knob_pos, _set_knob_pos)

    # -----------------------------------------------------

    def is_checked(self):
        return self._checked

    def set_checked(self, checked, animate=True):
        """Sets state programmatically without emitting `toggled` - for
        syncing the switch's look to some other source of truth."""
        if checked == self._checked:
            return

        self._checked = checked
        target = 1.0 if checked else 0.0

        if animate:
            self._anim.stop()
            self._anim.setStartValue(self._knob_pos)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._set_knob_pos(target)

    def mousePressEvent(self, event):
        if not self.isEnabled():
            super().mousePressEvent(event)
            return

        if event.button() == Qt.LeftButton:
            self.set_checked(not self._checked)
            self.toggled.emit(self._checked)
            event.accept()
            return
        super().mousePressEvent(event)

    # -----------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = QRectF(0, 0, self.width(), self.height()).adjusted(1, 1, -1, -1)
        radius = rect.height() / 2

        enabled = self.isEnabled()
        track_off = self.TRACK_OFF if enabled else self.TRACK_OFF_DISABLED
        track_on = self.TRACK_ON if enabled else self.TRACK_ON_DISABLED
        knob_color = self.KNOB if enabled else self.KNOB_DISABLED

        # Track color eases between off/on colors as the knob slides,
        # instead of snapping the moment the drag/animation finishes.
        t = self._knob_pos
        track_color = QColor(
            self._lerp(track_off.red(), track_on.red(), t),
            self._lerp(track_off.green(), track_on.green(), t),
            self._lerp(track_off.blue(), track_on.blue(), t),
        )

        painter.setPen(QPen(self.BORDER, max(1.0, rect.height() * 0.05)))
        painter.setBrush(QBrush(track_color))
        painter.drawRoundedRect(rect, radius, radius)

        knob_d = rect.height() - 4
        knob_x = rect.left() + 2 + t * (rect.width() - knob_d - 4)
        knob_y = rect.top() + 2

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(knob_color))
        painter.drawEllipse(QRectF(knob_x, knob_y, knob_d, knob_d))

    @staticmethod
    def _lerp(a, b, t):
        return round(a + (b - a) * t)