"""
icons.py
--------
Every toolbar / tree icon in ISOForge(ZR) is drawn programmatically with
QPainter instead of being loaded from bitmap assets.

Container-aware logos
======================
The "what am I editing" logo shown in the tree root and window icon
adapts to the open format: a blue disc for ISO, and a package/box for
the archive formats, tinted differently per format so they stay visually
distinguishable at a glance.

A self-extracting archive gets the package too, but with a small "run"
triangle badged onto it -- it is the one format that is also a program,
and that distinction is worth seeing before double-clicking something.

Use `container_logo(fmt)` rather than picking one directly, so the
choice stays in one place.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

ACCENT = QColor("#4C7DF0")
ACCENT_DARK = QColor("#2F5FD6")
INK = QColor("#3A4150")
PAPER = QColor("#FFFFFF")
MUTED = QColor("#8A93A6")
DANGER = QColor("#E5566D")
OK = QColor("#39B37E")
WARN = QColor("#E0A030")

# Per-format accent colours for the archive logos.
ZIP_COLOR = QColor("#E0A030")      # amber
SEVENZ_COLOR = QColor("#39B37E")   # green
RAR_COLOR = QColor("#9B6BD6")      # violet
SFX_COLOR = QColor("#2FA8A0")      # teal

_SIZE = 28
_is_dark = False


def set_theme(dark: bool) -> None:
    global INK, _is_dark
    _is_dark = dark
    INK = QColor("#D7DCE6") if dark else QColor("#3A4150")


def is_dark() -> bool:
    return _is_dark


def _canvas():
    pm = QPixmap(_SIZE, _SIZE)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    return pm, p


def _finish(pm, p) -> QIcon:
    p.end()
    return QIcon(pm)


def _folder_path(rect: QRectF, tab_w=11, tab_h=4) -> QPainterPath:
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    r = 2.2
    path = QPainterPath()
    path.moveTo(x, y + tab_h + r)
    path.arcTo(x, y + tab_h, r * 2, r * 2, 180, -90)
    path.lineTo(x + tab_w - r, y + tab_h)
    path.lineTo(x + tab_w + 2, y + tab_h - 3)
    path.lineTo(x + w - r, y + tab_h - 3)
    path.arcTo(x + w - r * 2, y + tab_h - 3, r * 2, r * 2, 90, -90)
    path.lineTo(x + w, y + h - r)
    path.arcTo(x + w - r * 2, y + h - r * 2, r * 2, r * 2, 0, -90)
    path.lineTo(x + r, y + h)
    path.arcTo(x, y + h - r * 2, r * 2, r * 2, -90, -90)
    path.closeSubpath()
    return path


def _draw_folder(p: QPainter, rect: QRectF, fill=ACCENT):
    p.setPen(Qt.NoPen)
    p.setBrush(fill)
    p.drawPath(_folder_path(rect))


def _draw_doc(p: QPainter, rect: QRectF, fill=PAPER, border=MUTED, fold=6):
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    path = QPainterPath()
    path.moveTo(x, y)
    path.lineTo(x + w - fold, y)
    path.lineTo(x + w, y + fold)
    path.lineTo(x + w, y + h)
    path.lineTo(x, y + h)
    path.closeSubpath()
    p.setPen(QPen(border, 1.4))
    p.setBrush(fill)
    p.drawPath(path)
    fold_path = QPainterPath()
    fold_path.moveTo(x + w - fold, y)
    fold_path.lineTo(x + w - fold, y + fold)
    fold_path.lineTo(x + w, y + fold)
    p.setBrush(border.lighter(160))
    p.drawPath(fold_path)


def _arrow(p: QPainter, cx, cy, length, direction, color, width=2.4):
    p.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    half = length / 2
    if direction == "down":
        p.drawLine(QPointF(cx, cy - half), QPointF(cx, cy + half))
        p.drawLine(QPointF(cx, cy + half), QPointF(cx - half * 0.55, cy + half * 0.15))
        p.drawLine(QPointF(cx, cy + half), QPointF(cx + half * 0.55, cy + half * 0.15))
    elif direction == "up":
        p.drawLine(QPointF(cx, cy - half), QPointF(cx, cy + half))
        p.drawLine(QPointF(cx, cy - half), QPointF(cx - half * 0.55, cy - half * 0.15))
        p.drawLine(QPointF(cx, cy - half), QPointF(cx + half * 0.55, cy - half * 0.15))
    elif direction == "left":
        p.drawLine(QPointF(cx - half, cy), QPointF(cx + half, cy))
        p.drawLine(QPointF(cx - half, cy), QPointF(cx - half * 0.15, cy - half * 0.55))
        p.drawLine(QPointF(cx - half, cy), QPointF(cx - half * 0.15, cy + half * 0.55))
    elif direction == "right":
        p.drawLine(QPointF(cx - half, cy), QPointF(cx + half, cy))
        p.drawLine(QPointF(cx + half, cy), QPointF(cx + half * 0.15, cy - half * 0.55))
        p.drawLine(QPointF(cx + half, cy), QPointF(cx + half * 0.15, cy + half * 0.55))


def _plus(p: QPainter, cx, cy, r, color, bg=None):
    if bg is not None:
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawEllipse(QPointF(cx, cy), r + 2, r + 2)
    p.setPen(QPen(color, 2.4, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(cx - r, cy), QPointF(cx + r, cy))
    p.drawLine(QPointF(cx, cy - r), QPointF(cx, cy + r))


def icon_new_disc() -> QIcon:
    pm, p = _canvas()
    _draw_doc(p, QRectF(6, 3, 14, 19))
    p.setPen(Qt.NoPen)
    p.setBrush(ACCENT)
    _plus(p, 20, 20, 5, PAPER, bg=ACCENT)
    return _finish(pm, p)


def icon_open_folder() -> QIcon:
    pm, p = _canvas()
    _draw_folder(p, QRectF(3, 8, 22, 15), fill=ACCENT)
    p.setBrush(QColor(255, 255, 255, 235))
    p.setPen(Qt.NoPen)
    p.drawRect(QRectF(6, 12, 16, 8))
    _draw_folder(p, QRectF(3, 8, 22, 15), fill=QColor(76, 125, 240, 60))
    return _finish(pm, p)


def icon_save() -> QIcon:
    pm, p = _canvas()
    p.setPen(Qt.NoPen)
    p.setBrush(ACCENT)
    p.drawRoundedRect(QRectF(4, 4, 20, 20), 3, 3)
    p.setBrush(PAPER)
    p.drawRect(QRectF(7, 4, 10, 6))
    p.setBrush(ACCENT_DARK)
    p.drawRect(QRectF(9, 4, 3, 5))
    p.setBrush(PAPER)
    p.drawRoundedRect(QRectF(7, 14, 14, 8), 1, 1)
    return _finish(pm, p)


def icon_save_as() -> QIcon:
    pm, p = _canvas()
    p.setPen(Qt.NoPen)
    p.setBrush(ACCENT)
    p.drawRoundedRect(QRectF(3, 4, 17, 18), 3, 3)
    p.setBrush(PAPER)
    p.drawRect(QRectF(6, 4, 9, 5))
    p.setBrush(PAPER)
    p.drawRoundedRect(QRectF(6, 12, 11, 7), 1, 1)
    _plus(p, 21, 21, 4.5, PAPER, bg=OK)
    return _finish(pm, p)


def icon_add_files() -> QIcon:
    pm, p = _canvas()
    _draw_doc(p, QRectF(5, 3, 13, 18))
    _plus(p, 20, 20, 5, PAPER, bg=OK)
    return _finish(pm, p)


def icon_add_folder() -> QIcon:
    pm, p = _canvas()
    _draw_folder(p, QRectF(2, 8, 20, 15), fill=ACCENT)
    _plus(p, 21, 20, 5, PAPER, bg=OK)
    return _finish(pm, p)


def icon_new_folder() -> QIcon:
    pm, p = _canvas()
    _draw_folder(p, QRectF(2, 8, 20, 15), fill=MUTED)
    _plus(p, 21, 20, 5, PAPER, bg=ACCENT)
    return _finish(pm, p)


def icon_extract() -> QIcon:
    pm, p = _canvas()
    _draw_folder(p, QRectF(2, 10, 20, 14), fill=ACCENT)
    _arrow(p, 21, 11, 12, "up", INK, width=2.6)
    return _finish(pm, p)


def icon_delete() -> QIcon:
    pm, p = _canvas()
    p.setPen(QPen(DANGER, 2))
    p.setBrush(Qt.NoBrush)
    p.drawLine(QPointF(6, 8), QPointF(22, 8))
    p.drawRoundedRect(QRectF(8, 8, 12, 16), 1.5, 1.5)
    p.drawLine(QPointF(11, 5), QPointF(17, 5))
    p.setPen(QPen(DANGER, 1.6))
    p.drawLine(QPointF(11.5, 11), QPointF(11.5, 21))
    p.drawLine(QPointF(14, 11), QPointF(14, 21))
    p.drawLine(QPointF(16.5, 11), QPointF(16.5, 21))
    return _finish(pm, p)


def icon_rename() -> QIcon:
    pm, p = _canvas()
    p.setPen(QPen(INK, 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawLine(QPointF(6, 22), QPointF(9, 21))
    p.drawLine(QPointF(9, 21), QPointF(20, 10))
    p.setBrush(ACCENT)
    p.setPen(Qt.NoPen)
    p.save()
    p.translate(20, 10)
    p.rotate(45)
    p.drawRoundedRect(QRectF(-2, -5, 4, 7), 1, 1)
    p.restore()
    return _finish(pm, p)


def icon_properties() -> QIcon:
    pm, p = _canvas()
    p.setPen(Qt.NoPen)
    p.setBrush(ACCENT)
    p.drawEllipse(QPointF(14, 14), 11, 11)
    p.setBrush(PAPER)
    p.drawEllipse(QPointF(14, 9.5), 1.6, 1.6)
    p.drawRoundedRect(QRectF(12.4, 12.5, 3.2, 8), 1, 1)
    return _finish(pm, p)


def icon_convert() -> QIcon:
    pm, p = _canvas()
    _draw_folder(p, QRectF(1, 12, 12, 10), fill=MUTED)
    _draw_doc(p, QRectF(16, 4, 11, 14), fill=PAPER, border=ACCENT_DARK, fold=4)
    _arrow(p, 13.5, 10, 9, "right", ACCENT, width=2.4)
    return _finish(pm, p)


def icon_settings() -> QIcon:
    """A gear, used for Tools > Settings.

    Drawn as a filled disc with trapezoidal teeth radiating from the
    centre, then a hole punched through the middle with
    CompositionMode_Clear so the ring reads correctly against both the
    light and dark toolbar backgrounds -- painting a background-coloured
    circle instead would leave a visible disc on whichever theme it
    wasn't matched to.
    """
    pm, p = _canvas()
    cx, cy = 14.0, 14.0
    teeth = 8
    outer, inner = 12.0, 8.6

    p.setPen(Qt.NoPen)
    p.setBrush(ACCENT)

    for i in range(teeth):
        angle = (360.0 / teeth) * i
        p.save()
        p.translate(cx, cy)
        p.rotate(angle)
        # Tooth sits just beyond the body and is slightly tapered.
        tooth = QPainterPath()
        tooth.moveTo(-3.1, -inner)
        tooth.lineTo(3.1, -inner)
        tooth.lineTo(2.2, -outer)
        tooth.lineTo(-2.2, -outer)
        tooth.closeSubpath()
        p.drawPath(tooth)
        p.restore()

    p.drawEllipse(QPointF(cx, cy), inner, inner)

    p.setCompositionMode(QPainter.CompositionMode_Clear)
    p.setBrush(QColor(0, 0, 0, 255))
    p.drawEllipse(QPointF(cx, cy), 4.1, 4.1)
    p.setCompositionMode(QPainter.CompositionMode_SourceOver)

    return _finish(pm, p)


def icon_help() -> QIcon:
    pm, p = _canvas()
    p.setPen(Qt.NoPen)
    p.setBrush(ACCENT)
    p.drawEllipse(QPointF(14, 14), 12, 12)
    p.setPen(QPen(PAPER, 2.2, Qt.SolidLine, Qt.RoundCap))
    p.setBrush(Qt.NoBrush)
    path = QPainterPath()
    path.moveTo(10.8, 11)
    path.cubicTo(10.8, 7.6, 17.3, 7.4, 17.3, 11.4)
    path.cubicTo(17.3, 14, 14, 13.6, 14, 17)
    p.drawPath(path)
    p.setPen(Qt.NoPen)
    p.setBrush(PAPER)
    p.drawEllipse(QPointF(14, 20.4), 1.5, 1.5)
    return _finish(pm, p)


def icon_nav(direction: str) -> QIcon:
    pm, p = _canvas()
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0, 0))
    _arrow(p, 14, 14, 14, direction, INK, width=2.6)
    return _finish(pm, p)


def icon_refresh() -> QIcon:
    pm, p = _canvas()
    p.setPen(QPen(INK, 2.4, Qt.SolidLine, Qt.RoundCap))
    p.setBrush(Qt.NoBrush)
    p.drawArc(QRectF(5, 5, 18, 18), 40 * 16, 260 * 16)
    p.setBrush(INK)
    p.setPen(Qt.NoPen)
    path = QPainterPath()
    path.moveTo(20.5, 4.5)
    path.lineTo(24.5, 8)
    path.lineTo(19, 9.5)
    path.closeSubpath()
    p.drawPath(path)
    return _finish(pm, p)


def icon_warning() -> QIcon:
    pm, p = _canvas()
    path = QPainterPath()
    path.moveTo(14, 4)
    path.lineTo(25, 23)
    path.lineTo(3, 23)
    path.closeSubpath()
    p.setPen(Qt.NoPen)
    p.setBrush(WARN)
    p.drawPath(path)
    p.setPen(QPen(PAPER, 2.2, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(14, 11), QPointF(14, 17))
    p.setPen(Qt.NoPen)
    p.setBrush(PAPER)
    p.drawEllipse(QPointF(14, 20.3), 1.4, 1.4)
    return _finish(pm, p)


# --------------------------------------------------------------------- #
# Lock / encryption indicator
# --------------------------------------------------------------------- #
def _draw_padlock(p: QPainter, cx: float, cy: float, scale: float,
                  body=WARN, shackle=None):
    """Shared padlock glyph, used both as a standalone toolbar icon and
    as the small badge overlaid on an encrypted archive's logo."""
    shackle = shackle or body.darker(130)
    bw = 11 * scale
    bh = 9 * scale
    p.setPen(QPen(shackle, max(1.2, 2.0 * scale), Qt.SolidLine, Qt.RoundCap))
    p.setBrush(Qt.NoBrush)
    p.drawArc(QRectF(cx - bw * 0.31, cy - bh * 0.98, bw * 0.62, bh * 0.95),
              0, 180 * 16)
    p.setPen(Qt.NoPen)
    p.setBrush(body)
    p.drawRoundedRect(QRectF(cx - bw / 2, cy - bh * 0.18, bw, bh),
                      2 * scale, 2 * scale)
    p.setBrush(PAPER)
    p.drawEllipse(QPointF(cx, cy + bh * 0.28), 1.4 * scale, 1.4 * scale)


def icon_lock() -> QIcon:
    pm, p = _canvas()
    _draw_padlock(p, 14, 15, 1.6)
    return _finish(pm, p)


# --------------------------------------------------------------------- #
# Container logos
# --------------------------------------------------------------------- #
def icon_disc_logo(size: int = 64) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    s = size
    p.setPen(Qt.NoPen)
    p.setBrush(ACCENT)
    p.drawEllipse(QRectF(s * 0.06, s * 0.06, s * 0.88, s * 0.88))
    p.setBrush(PAPER)
    p.drawEllipse(QRectF(s * 0.30, s * 0.30, s * 0.40, s * 0.40))
    p.setBrush(ACCENT_DARK)
    p.drawEllipse(QRectF(s * 0.44, s * 0.44, s * 0.12, s * 0.12))
    p.setPen(QPen(PAPER, s * 0.03))
    p.drawArc(QRectF(s * 0.12, s * 0.12, s * 0.76, s * 0.76), 200 * 16, 90 * 16)
    p.end()
    return QIcon(pm)


def icon_archive_logo(size: int = 64, color: QColor = None,
                      locked: bool = False,
                      runnable: bool = False) -> QIcon:
    """A package/box glyph used for the archive formats.

    Two optional badges: a padlock when the archive is encrypted, and a
    "run" triangle when the archive is also an executable. They sit on
    opposite corners so an encrypted self-extracting archive can show
    both without them colliding.
    """
    color = color or ZIP_COLOR
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    s = size

    body = QRectF(s * 0.12, s * 0.26, s * 0.76, s * 0.60)
    lid = QRectF(s * 0.08, s * 0.14, s * 0.84, s * 0.16)

    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawRoundedRect(body, s * 0.05, s * 0.05)
    p.setBrush(color.darker(115))
    p.drawRoundedRect(lid, s * 0.04, s * 0.04)

    # Centre seam plus a clasp, reading as a closed container.
    p.setBrush(QColor(255, 255, 255, 210))
    p.drawRect(QRectF(s * 0.45, s * 0.26, s * 0.10, s * 0.60))
    p.setBrush(color.darker(140))
    p.drawRoundedRect(QRectF(s * 0.42, s * 0.44, s * 0.16, s * 0.13),
                      s * 0.03, s * 0.03)

    if runnable:
        # Bottom-LEFT so it never overlaps the padlock badge.
        cx, cy, r = s * 0.26, s * 0.72, s * 0.13
        p.setBrush(QColor("#2B303B"))
        p.drawEllipse(QPointF(cx, cy), r, r)
        play = QPainterPath()
        play.moveTo(cx - r * 0.34, cy - r * 0.46)
        play.lineTo(cx + r * 0.50, cy)
        play.lineTo(cx - r * 0.34, cy + r * 0.46)
        play.closeSubpath()
        p.setBrush(PAPER)
        p.drawPath(play)

    if locked:
        _draw_padlock(p, s * 0.76, s * 0.74, s / 28.0 * 1.35,
                      body=QColor("#2B303B"), shackle=QColor("#2B303B"))

    p.end()
    return QIcon(pm)


def container_logo(fmt: str = None, size: int = 64, locked: bool = False) -> QIcon:
    """Single entry point for "the logo for whatever is open". Keeps the
    disc-vs-package decision (and the per-format tint) in one place
    instead of scattered across the UI."""
    from . import formats
    if fmt == formats.ZIP:
        return icon_archive_logo(size, ZIP_COLOR, locked)
    if fmt == formats.SEVENZ:
        return icon_archive_logo(size, SEVENZ_COLOR, locked)
    if fmt == formats.RAR:
        return icon_archive_logo(size, RAR_COLOR, locked)
    if fmt == formats.SFX:
        return icon_archive_logo(size, SFX_COLOR, locked, runnable=True)
    return icon_disc_logo(size)


def folder_pixmap(open_: bool = False, size: int = 18) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    fill = ACCENT if not open_ else ACCENT.lighter(115)
    _draw_folder(p, QRectF(1, size * 0.28, size - 2, size * 0.62), fill=fill)
    p.end()
    return pm


def file_pixmap(ext: str = "", size: int = 18) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    _draw_doc(p, QRectF(3, 1, size - 6, size - 2), fold=size * 0.28)
    p.end()
    return pm
