"""Окно просмотра: кадр в рамке выбранного формата (16:9 или 9:16).

Кадрирование прямо мышью, как в CapCut:
  * перетащить кадр — сдвинуть (у центра он «прилипает» к середине);
  * потянуть за уголок или покрутить колёсико — увеличить/уменьшить.
Если кадр не закрывает весь экран, фон заполняется его размытой копией (как и при экспорте).
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

import json

from worklapse.editor.project import ASPECTS, DEFAULT_FRAME, frame_rect
from worklapse.editor.text import anim_state, placement, render_text

HANDLE = 10        # размер уголка, пикс.
SNAP = 0.015       # насколько близко к центру кадр «прилипает»


class PreviewWidget(QWidget):
    edit_started = Signal()
    frame_delta = Signal(float, float, float)   # множитель масштаба, сдвиг X, сдвиг Y (от начала жеста)
    edit_finished = Signal()
    wheel_zoom = Signal(float)
    text_pressed = Signal(str)                   # щёлкнули по тексту
    text_moved = Signal(str, float, float)       # новый центр текста (доли кадра)

    def __init__(self) -> None:
        super().__init__()
        self.image = QImage()
        self.aspect = "16:9"
        self.frame = DEFAULT_FRAME
        self.editable = False
        self._bg_cache: tuple[int, QImage] | None = None
        self._drag: str | None = None      # move / scale
        self._press = QPointF()
        self._start_frame = DEFAULT_FRAME
        self._guides = (False, False)
        self.texts: list = []          # [(TextItem, стиль)] — видимые сейчас
        self.t = 0.0
        self.selected_text: str | None = None
        self._text_cache: dict[str, QImage] = {}
        self._text_drag: tuple[str, float, float] | None = None   # id, центр x, y в начале
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 220)
        self.setMouseTracking(True)

    def sizeHint(self) -> QSize:
        return QSize(800, 450)

    # ---------- данные ----------

    def set_image(self, img: QImage) -> None:
        self.image = img
        self._bg_cache = None
        self.update()

    def set_aspect(self, aspect: str) -> None:
        self.aspect = aspect
        self.update()

    def set_frame(self, frame: tuple[float, float, float], editable: bool) -> None:
        if self._drag is None:          # во время перетаскивания рамку ведёт мышь
            self.frame = frame
        self.editable = editable
        self.update()

    def set_texts(self, texts: list, t: float, selected: str | None) -> None:
        self.texts, self.t, self.selected_text = texts, t, selected
        self.update()

    def _text_image(self, item, style) -> QImage:
        c = self.canvas_rect()
        key = json.dumps([item.text, style, int(c.width()), int(c.height())], sort_keys=True, ensure_ascii=False)
        img = self._text_cache.get(key)
        if img is None:
            if len(self._text_cache) > 200:
                self._text_cache.clear()
            img = self._text_cache[key] = render_text(item.text, style, int(c.width()), int(c.height()))
        return img

    def _text_rect(self, item, style) -> QRectF:
        c = self.canvas_rect()
        img = self._text_image(item, style)
        x, y = placement(img.width(), img.height(), item.pos_for(self.aspect), int(c.width()), int(c.height()))
        return QRectF(c.x() + x, c.y() + y, img.width(), img.height())

    def _paint_texts(self, p: QPainter) -> None:
        canvas = self.canvas_rect()
        p.save()
        p.setClipRect(canvas)
        for item, style in self.texts:
            if not item.text.strip():
                continue
            img = self._text_image(item, style)
            r = self._text_rect(item, style)
            op, dy, sc = anim_state(style.get("anim", "fade"), self.t - item.start, item.duration)
            if item.id == self.selected_text:
                op, dy, sc = 1.0, 0.0, 1.0      # выбранный текст показываем целиком, без анимации
            p.save()
            p.setOpacity(op)
            center = r.center()
            p.translate(center.x(), center.y() + dy * canvas.height())
            p.scale(sc, sc)
            p.drawImage(QRectF(-r.width() / 2, -r.height() / 2, r.width(), r.height()), img)
            p.restore()
            if item.id == self.selected_text:
                p.setPen(QPen(QColor("#ffd166"), 1, Qt.PenStyle.DashLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(r.adjusted(-2, -2, 2, 2))
        p.restore()

    def _hit_text(self, pos: QPointF):
        for item, style in reversed(self.texts):
            if item.text.strip() and self._text_rect(item, style).contains(pos):
                return item
        return None

    # ---------- геометрия ----------

    def canvas_rect(self) -> QRectF:
        W, H = ASPECTS[self.aspect]
        scale = min((self.width() - 24) / W, (self.height() - 24) / H)
        w, h = W * scale, H * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _frame_rect(self, frame=None) -> QRectF:
        canvas = self.canvas_rect()
        iw, ih = (self.image.width(), self.image.height()) if not self.image.isNull() else (16, 9)
        x, y, w, h = frame_rect(iw, ih, canvas.width(), canvas.height(), frame or self.frame)
        return QRectF(canvas.x() + x, canvas.y() + y, w, h)

    def _corners(self, r: QRectF) -> list[QPointF]:
        return [r.topLeft(), r.topRight(), r.bottomLeft(), r.bottomRight()]

    # ---------- рисование ----------

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), self.palette().window())
        canvas = self.canvas_rect()
        p.fillRect(canvas, QColor("#000000"))
        img = self.image
        if img.isNull():
            p.setPen(QColor("#777"))
            p.drawText(canvas, Qt.AlignmentFlag.AlignCenter, "Нет кадра")
            self._paint_texts(p)
            return
        fr = self._frame_rect()
        if not fr.contains(canvas.adjusted(1, 1, -1, -1)):
            # размытая подложка: уменьшаем кадр в 16 раз и растягиваем обратно
            key = img.cacheKey()
            if not self._bg_cache or self._bg_cache[0] != key:
                small = img.scaled(max(1, img.width() // 16), max(1, img.height() // 16),
                                   Qt.AspectRatioMode.IgnoreAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
                self._bg_cache = (key, small)
            p.save()
            p.setClipRect(canvas)
            p.drawImage(_cover(img.width(), img.height(), canvas), self._bg_cache[1])
            p.fillRect(canvas, QColor(0, 0, 0, 40))
            p.restore()
        p.save()
        p.setClipRect(canvas)
        p.drawImage(fr, img)
        p.restore()
        self._paint_texts(p)
        if self.editable and self.selected_text is None:
            # рамка и уголки выбранного фрагмента (за пределами экрана видны пунктиром)
            p.setPen(QPen(QColor("#ffffff"), 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(fr)
            p.setPen(QPen(QColor("#1f6f78"), 1))
            p.setBrush(QColor("#ffffff"))
            for c in self._corners(fr):
                p.drawRect(QRectF(c.x() - HANDLE / 2, c.y() - HANDLE / 2, HANDLE, HANDLE))
        # направляющие «прилипания» к центру
        gx, gy = self._guides
        p.setPen(QPen(QColor("#ff4fa3"), 1))
        if gx:
            p.drawLine(QPointF(canvas.center().x(), canvas.top()), QPointF(canvas.center().x(), canvas.bottom()))
        if gy:
            p.drawLine(QPointF(canvas.left(), canvas.center().y()), QPointF(canvas.right(), canvas.center().y()))

    # ---------- мышь ----------

    def _hit_corner(self, pos: QPointF) -> bool:
        return any(abs(pos.x() - c.x()) <= HANDLE and abs(pos.y() - c.y()) <= HANDLE
                   for c in self._corners(self._frame_rect()))

    def mousePressEvent(self, e) -> None:
        if e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position()
        hit = self._hit_text(pos)
        if hit is not None:                 # тексты лежат поверх видео — они в приоритете
            self.text_pressed.emit(hit.id)
            self._press = pos
            x, y = hit.pos_for(self.aspect)
            self._text_drag = (hit.id, x, y)
            return
        if self.image.isNull():
            return
        if not self.canvas_rect().contains(pos) and not self._hit_corner(pos):
            return
        self.edit_started.emit()          # окно выберет показанный фрагмент и запомнит «до»
        self._press = pos
        self._start_frame = self.frame
        self._drag = "scale" if self.editable and self._hit_corner(pos) else "move"

    def mouseMoveEvent(self, e) -> None:
        pos = e.position()
        if self._text_drag is not None:
            tid, x0, y0 = self._text_drag
            c = self.canvas_rect()
            nx = x0 + (pos.x() - self._press.x()) / c.width()
            ny = y0 + (pos.y() - self._press.y()) / c.height()
            snap = abs(nx - 0.5) < SNAP
            self._guides = (snap, False)
            self.text_moved.emit(tid, 0.5 if snap else nx, ny)
            return
        if self._drag is None:
            if self._hit_text(pos) is not None:
                self.setCursor(Qt.CursorShape.SizeAllCursor)
                return
            self.setCursor(Qt.CursorShape.SizeFDiagCursor if self.editable and self._hit_corner(pos)
                           else Qt.CursorShape.OpenHandCursor if self.canvas_rect().contains(pos)
                           else Qt.CursorShape.ArrowCursor)
            return
        canvas = self.canvas_rect()
        z0, x0, y0 = self._start_frame
        if self._drag == "move":
            nx = x0 + (pos.x() - self._press.x()) / canvas.width()
            ny = y0 + (pos.y() - self._press.y()) / canvas.height()
            snap_x, snap_y = abs(nx) < SNAP, abs(ny) < SNAP
            nx, ny = (0.0 if snap_x else nx), (0.0 if snap_y else ny)
            self._guides = (snap_x, snap_y)
            self.frame = (z0, nx, ny)
        else:
            center = self._frame_rect(self._start_frame).center()
            d0 = math.dist((self._press.x(), self._press.y()), (center.x(), center.y())) or 1.0
            d1 = math.dist((pos.x(), pos.y()), (center.x(), center.y()))
            self.frame = (z0 * d1 / d0, x0, y0)
        zm = self.frame[0] / z0 if z0 else 1.0
        self.frame_delta.emit(zm, self.frame[1] - x0, self.frame[2] - y0)
        self.update()

    def mouseReleaseEvent(self, _e) -> None:
        if self._text_drag is not None:
            self._text_drag = None
            self._guides = (False, False)
            self.edit_finished.emit()
            self.update()
            return
        if self._drag is not None:
            self._drag = None
            self._guides = (False, False)
            self.edit_finished.emit()
            self.update()

    def wheelEvent(self, e) -> None:
        if self.image.isNull():
            return
        steps = (e.angleDelta().y() or e.angleDelta().x()) / 120
        if steps:
            self.wheel_zoom.emit(1.06 ** steps)


def _cover(w: int, h: int, box: QRectF) -> QRectF:
    s = max(box.width() / w, box.height() / h)
    fw, fh = w * s, h * s
    return QRectF(box.x() + (box.width() - fw) / 2, box.y() + (box.height() - fh) / 2, fw, fh)
