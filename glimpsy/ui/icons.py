"""Иконки рисуются кодом — не нужно хранить картинки для каждого состояния."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap

COLORS = {
    "recording": "#E5484D",   # красный — идёт запись
    "idle": "#F5A524",        # жёлтый — автопауза
    "paused": "#8B8D98",      # серый — пауза
    "private": "#8E4EC6",     # фиолетовый — приватное окно
    "assembling": "#3E63DD",  # синий — сборка
    "error": "#E5484D",
    "stopped": "#8B8D98",
    "starting": "#8B8D98",
    "important": "#F5C518",   # золотая звезда — «важный момент отмечен»
}


def state_icon(state: str, size: int = 64) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    # тёмная «плашка» — видно и на светлом, и на тёмном трее
    p.setBrush(QColor("#1C1C21"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(2, 2, size - 4, size - 4), size * 0.22, size * 0.22)
    color = QColor(COLORS.get(state, "#8B8D98"))
    p.setBrush(color)
    c = size / 2
    if state in ("paused", "idle", "private"):
        w, h = size * 0.12, size * 0.4
        p.drawRoundedRect(QRectF(c - w * 1.6, c - h / 2, w, h), 2, 2)
        p.drawRoundedRect(QRectF(c + w * 0.6, c - h / 2, w, h), 2, 2)
    elif state == "important":
        import math
        path = QPainterPath()
        for i in range(10):                                  # пятиконечная звезда
            r = size * (0.32 if i % 2 == 0 else 0.14)
            a = -math.pi / 2 + i * math.pi / 5
            pt = (c + r * math.cos(a), c + r * math.sin(a))
            path.moveTo(*pt) if i == 0 else path.lineTo(*pt)
        path.closeSubpath()
        p.drawPath(path)
    elif state == "error":
        path = QPainterPath()
        path.moveTo(c, size * 0.24)
        path.lineTo(size * 0.78, size * 0.74)
        path.lineTo(size * 0.22, size * 0.74)
        path.closeSubpath()
        p.drawPath(path)
    else:
        r = size * 0.22
        p.drawEllipse(QRectF(c - r, c - r, 2 * r, 2 * r))
    p.end()
    return QIcon(pm)
