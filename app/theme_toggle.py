"""
theme_toggle.py
----------------
A small, custom-drawn pill/switch widget used to flip between light and
dark mode from the toolbar.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

_TRACK_W = 44
_TRACK_H = 22
_THUMB_MARGIN = 2

_TRACK_OFF = QColor("#D3DAE6")
_TRACK_ON = QColor("#4C7DF0")
_THUMB_COLOR = QColor("#FFFFFF")
_SUN_COLOR = QColor("#F5A623")
_MOON_COLOR = QColor("#4C5B7A")


class ThemeToggle(QWidget):
    toggled = Signal(bool)

    def __init__(self, parent=None, checked: bool = False):
        super().__init__(parent)
        self._checked = checked
        self.setFixedSize(_TRACK_W, _TRACK_H)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Switch between light and dark mode")

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool) -> None:
        if checked == self._checked:
            return
        self._checked = checked
        self.update()

    def sizeHint(self) -> QSize:  # pragma: no cover
        return QSize(_TRACK_W, _TRACK_H)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._checked = not self._checked
            self.update()
            self.toggled.emit(self._checked)
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:  # pragma: no cover
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        track_rect = QRectF(0, 0, _TRACK_W, _TRACK_H)
        p.setPen(Qt.NoPen)
        p.setBrush(_TRACK_ON if self._checked else _TRACK_OFF)
        p.drawRoundedRect(track_rect, _TRACK_H / 2, _TRACK_H / 2)

        thumb_d = _TRACK_H - _THUMB_MARGIN * 2
        thumb_x = (_TRACK_W - _THUMB_MARGIN - thumb_d) if self._checked else _THUMB_MARGIN
        thumb_rect = QRectF(thumb_x, _THUMB_MARGIN, thumb_d, thumb_d)
        p.setBrush(_THUMB_COLOR)
        p.drawEllipse(thumb_rect)

        cx, cy = thumb_rect.center().x(), thumb_rect.center().y()
        radius = thumb_d * 0.22
        p.setPen(Qt.NoPen)

        if not self._checked:
            p.setBrush(_SUN_COLOR)
            p.drawEllipse(QPointF(cx, cy), radius, radius)
        else:
            p.setBrush(_MOON_COLOR)
            p.drawEllipse(QPointF(cx, cy), radius, radius)
            p.setCompositionMode(QPainter.CompositionMode_DestinationOut)
            p.setBrush(QColor("#FFFFFF"))
            p.drawEllipse(QPointF(cx + radius * 0.55, cy - radius * 0.35), radius * 0.85, radius * 0.85)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)

        p.end()
