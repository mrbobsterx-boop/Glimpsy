"""Окно просмотра: кадр в рамке выбранного формата (16:9 или 9:16).

Если кадр другой формы — фон заполняется его размытой копией (как и при экспорте).
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from worklapse.editor.project import ASPECTS


class PreviewWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.image = QImage()
        self.aspect = "16:9"
        self._bg_cache: tuple[int, QImage] | None = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 220)

    def sizeHint(self) -> QSize:
        return QSize(800, 450)

    def set_image(self, img: QImage) -> None:
        self.image = img
        self._bg_cache = None
        self.update()

    def set_aspect(self, aspect: str) -> None:
        self.aspect = aspect
        self.update()

    def canvas_rect(self) -> QRectF:
        W, H = ASPECTS[self.aspect]
        avail_w, avail_h = self.width() - 24, self.height() - 24
        scale = min(avail_w / W, avail_h / H)
        w, h = W * scale, H * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.rect(), self.palette().window())
        canvas = self.canvas_rect()
        p.fillRect(canvas, QColor("#000000"))
        img = self.image
        if img.isNull():
            p.setPen(QColor("#777"))
            p.drawText(canvas, Qt.AlignmentFlag.AlignCenter, "Нет кадра")
            return
        fit = _fit(img.width(), img.height(), canvas, cover=False)
        if abs(fit.width() - canvas.width()) > 2 or abs(fit.height() - canvas.height()) > 2:
            # размытая подложка: уменьшаем кадр в 16 раз и растягиваем обратно
            key = img.cacheKey()
            if not self._bg_cache or self._bg_cache[0] != key:
                small = img.scaled(max(1, img.width() // 16), max(1, img.height() // 16),
                                   Qt.AspectRatioMode.IgnoreAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
                self._bg_cache = (key, small)
            p.save()
            p.setClipRect(canvas)
            p.drawImage(_fit(img.width(), img.height(), canvas, cover=True), self._bg_cache[1])
            p.fillRect(canvas, QColor(0, 0, 0, 40))
            p.restore()
        p.drawImage(fit, img)


def _fit(w: int, h: int, box: QRectF, cover: bool) -> QRectF:
    s = (max if cover else min)(box.width() / w, box.height() / h)
    fw, fh = w * s, h * s
    return QRectF(box.x() + (box.width() - fw) / 2, box.y() + (box.height() - fh) / 2, fw, fh)
