"""Иконки рисуются кодом — не нужно хранить картинки для каждого состояния."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap

COLORS = {
    "recording": "#E5484D",   # красный — идёт запись
    "idle": "#F5A524",        # жёлтый — автопауза
    "paused": "#8B8D98",      # серый — пауза
    "private": "#8E4EC6",     # фиолетовый — приватное окно
    "waiting": "#22AEBB",     # бирюзовый — поток ждёт своего окна
    "assembling": "#3E63DD",  # синий — сборка
    "error": "#E5484D",
    "stopped": "#8B8D98",
    "starting": "#8B8D98",
    "important": "#F5C518",   # золотая звезда — «важный момент отмечен»
}


def state_icon(state: str, size: int = 64, voice: bool = False) -> QIcon:
    """Значок в трее: знак Glimpsy, в центре — состояние (запись, пауза, сборка…).

    voice — пишется речь: красная точка в левом верхнем углу.
    """
    import math

    from PySide6.QtGui import QPen

    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#161A22"))
    p.drawRoundedRect(QRectF(1, 1, size - 2, size - 2), size * 0.26, size * 0.26)
    c = size / 2
    active = state in ("recording", "starting", "assembling", "important")
    ring = QPen(QColor("#22AEBB" if active else "#6B7280"), size * 0.1)
    ring.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(ring)
    r = size * 0.3
    p.drawArc(QRectF(c - r, c - r, 2 * r, 2 * r), 25 * 16, 290 * 16)
    p.setPen(Qt.PenStyle.NoPen)
    color = QColor(COLORS.get(state, "#8B8D98"))
    p.setBrush(color)
    if state in ("paused", "idle", "private", "waiting"):
        w, h = size * 0.08, size * 0.24
        p.drawRoundedRect(QRectF(c - w * 1.5, c - h / 2, w, h), 1.5, 1.5)
        p.drawRoundedRect(QRectF(c + w * 0.5, c - h / 2, w, h), 1.5, 1.5)
    elif state == "error":
        path = QPainterPath()
        path.moveTo(c, c - size * 0.13)
        path.lineTo(c + size * 0.13, c + size * 0.1)
        path.lineTo(c - size * 0.13, c + size * 0.1)
        path.closeSubpath()
        p.drawPath(path)
    elif state == "important":
        path = QPainterPath()
        for i in range(10):                                  # пятиконечная звезда
            rr = size * (0.17 if i % 2 == 0 else 0.075)
            a = -math.pi / 2 + i * math.pi / 5
            pt = (c + rr * math.cos(a), c + rr * math.sin(a))
            path.moveTo(*pt) if i == 0 else path.lineTo(*pt)
        path.closeSubpath()
        p.drawPath(path)
    else:
        d = size * 0.2
        p.drawEllipse(QRectF(c - d / 2, c - d / 2, d, d))
    if voice:
        d = size * 0.36
        p.setPen(QPen(QColor("#161A22"), size * 0.05))       # тёмная обводка — видна на любом фоне
        p.setBrush(QColor("#FF3B30"))
        p.drawEllipse(QRectF(size * 0.03, size * 0.03, d, d))
    p.end()
    return QIcon(pm)


def draw_logo(p: QPainter, size: float, record_color: str = "#F0565B") -> None:
    """Знак Glimpsy: бирюзовое кольцо-«G» с просветом (кольцо таймлапса) и точка записи в центре."""
    from PySide6.QtGui import QLinearGradient, QPen

    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    bg = QLinearGradient(0, 0, size, size)
    bg.setColorAt(0, QColor("#1D2331"))
    bg.setColorAt(1, QColor("#0B0E13"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(bg)
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.24, size * 0.24)
    c = size / 2
    r = size * 0.27
    ring = QLinearGradient(c - r, c - r, c + r, c + r)
    ring.setColorAt(0.0, QColor("#7BE6EE"))
    ring.setColorAt(0.55, QColor("#22AEBB"))
    ring.setColorAt(1.0, QColor("#177F8B"))
    pen = QPen(ring, size * 0.105)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(QRectF(c - r, c - r, 2 * r, 2 * r), 30 * 16, 300 * 16)     # просвет справа — буква G
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(record_color))
    d = size * 0.17
    p.drawEllipse(QRectF(c - d / 2, c - d / 2, d, d))


def app_logo(size: int = 64) -> QPixmap:
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    draw_logo(p, size * 2)
    p.end()
    pm.setDevicePixelRatio(2)
    return pm
