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

from glimpsy.editor.project import ASPECTS, DEFAULT_FRAME, frame_rect
from glimpsy.editor.overlay import overlay_rect, shadow_pad, styled_image
from glimpsy.editor.text import anim_state, placement, render_text

HANDLE = 10        # размер уголка, пикс.
SNAP = 0.015       # насколько близко к центру кадр «прилипает»


class PreviewWidget(QWidget):
    edit_started = Signal()
    frame_delta = Signal(float, float, float)   # множитель масштаба, сдвиг X, сдвиг Y (от начала жеста)
    edit_finished = Signal()
    wheel_zoom = Signal(float)
    text_pressed = Signal(str)                   # щёлкнули по тексту
    text_moved = Signal(str, float, float)       # новый центр текста (доли кадра)
    overlay_pressed = Signal(str)
    overlay_changed = Signal(str, float, float, float)   # id, центр X, центр Y, ширина (доли кадра)
    overlay_wheel = Signal(str, float)

    def __init__(self) -> None:
        super().__init__()
        self.image = QImage()
        self.aspect = "16:9"
        self.frame = DEFAULT_FRAME
        self.editable = False
        self.src_crop = (0.0, 0.0, 1.0, 1.0)   # какая часть исходного кадра видна (автозум/слежение)
        self.ripples: list = []                  # круги кликов: [(x, y, прогресс)] в долях исходного кадра
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
        self.overlays: list = []       # [(OverlayItem, QImage источника)] — видимые сейчас
        self.selected_overlay: str | None = None
        self._ov_cache: dict[str, QImage] = {}
        self._ov_drag: tuple | None = None   # (id, режим, cx, cy, ширина)
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

    def set_src_crop(self, crop: tuple[float, float, float, float]) -> None:
        if crop != self.src_crop:
            self.src_crop = crop
            self.update()

    def set_ripples(self, ripples: list) -> None:
        if ripples or self.ripples:
            self.ripples = ripples
            self.update()

    def _paint_ripples(self, p: QPainter, fr: QRectF) -> None:
        """Круги кликов — те же формулы, что у FFmpeg при экспорте (glimpsy/editor/clicks.py)."""
        from glimpsy.editor import clicks as ck

        sx, sy, sw, sh = self.src_crop
        size = ck.SIZE * fr.width() / sw               # размер круга в пикселях просмотра
        r, g, b = ck.COLOR
        for x, y, prog in self.ripples:
            cx = fr.x() + (x - sx) / sw * fr.width()
            cy = fr.y() + (y - sy) / sh * fr.height()
            rad = size / 2 * ck.radius_at(prog)
            fade = 1 - prog
            th = max(1.5, size * 0.06)
            p.setBrush(QColor(r, g, b, int(ck.FILL_ALPHA * fade)))
            p.setPen(QPen(QColor(25, 20, 15, int(150 * fade)), th * 2.4))
            p.drawEllipse(QPointF(cx, cy), rad, rad)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(r, g, b, int(255 * fade)), th))
            p.drawEllipse(QPointF(cx, cy), rad, rad)

    def set_frame(self, frame: tuple[float, float, float], editable: bool) -> None:
        if self._drag is None:          # во время перетаскивания рамку ведёт мышь
            self.frame = frame
        self.editable = editable
        self.update()

    def set_overlays(self, overlays: list, selected: str | None) -> None:
        self.overlays, self.selected_overlay = overlays, selected
        self.update()

    def _ov_rect(self, item) -> QRectF:
        c = self.canvas_rect()
        x, y, w, h = overlay_rect(item, self.aspect, c.width(), c.height())
        return QRectF(c.x() + x, c.y() + y, w, h)

    def _paint_overlays(self, p: QPainter) -> None:
        canvas = self.canvas_rect()
        p.save()
        p.setClipRect(canvas)
        for item, src in self.overlays:
            r = self._ov_rect(item)
            w, h = max(2, int(r.width())), max(2, int(r.height()))
            key = f"{item.id}|{src.cacheKey() if src is not None else 0}|{w}|{h}|{item.radius}|{item.shadow}|{item.opacity}"
            img = self._ov_cache.get(key)
            if img is None:
                if len(self._ov_cache) > 60:
                    self._ov_cache.clear()
                img = self._ov_cache[key] = styled_image(src if src is not None else QImage(), w, h,
                                                         item.radius, item.shadow, item.opacity)
            pad = shadow_pad(w, h)
            p.drawImage(QPointF(r.x() - pad, r.y() - pad), img)
        p.restore()
        for item, _src in self.overlays:
            if item.id == self.selected_overlay:
                r = self._ov_rect(item)
                p.setPen(QPen(QColor("#c9a7ff"), 1, Qt.PenStyle.DashLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(r)
                p.setPen(QPen(QColor("#7d5fb2"), 1))
                p.setBrush(QColor("#ffffff"))
                for c in self._corners(r):
                    p.drawRect(QRectF(c.x() - HANDLE / 2, c.y() - HANDLE / 2, HANDLE, HANDLE))

    def _hit_overlay(self, pos: QPointF):
        """(наложение, «scale» если за уголок выбранного, иначе «move») или (None, "")."""
        for item, _src in reversed(self.overlays):
            r = self._ov_rect(item)
            if item.id == self.selected_overlay and any(
                    abs(pos.x() - c.x()) <= HANDLE and abs(pos.y() - c.y()) <= HANDLE for c in self._corners(r)):
                return item, "scale"
            if r.contains(pos):
                return item, "move"
        return None, ""

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
        iw, ih = iw * self.src_crop[2], ih * self.src_crop[3]      # движение по курсору: видна часть кадра
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
            self._paint_overlays(p)
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
        sx, sy, sw, sh = self.src_crop
        p.drawImage(fr, img, QRectF(sx * img.width(), sy * img.height(), sw * img.width(), sh * img.height()))
        if self.ripples:
            self._paint_ripples(p, fr)
        p.restore()
        self._paint_overlays(p)
        self._paint_texts(p)
        if self.editable and self.selected_text is None and self.selected_overlay is None:
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
        ov, mode = self._hit_overlay(pos)
        if ov is not None:                  # наложения — поверх видео, под текстами
            self.overlay_pressed.emit(ov.id)
            self._press = pos
            self._ov_drag = (ov.id, mode, *ov.layout_for(self.aspect), self._ov_rect(ov).center())
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
        if self._ov_drag is not None:
            oid, mode, cx, cy, sc, center = self._ov_drag
            c = self.canvas_rect()
            if mode == "move":
                nx = cx + (pos.x() - self._press.x()) / c.width()
                ny = cy + (pos.y() - self._press.y()) / c.height()
                snap_x, snap_y = abs(nx - 0.5) < SNAP, abs(ny - 0.5) < SNAP
                self._guides = (snap_x, snap_y)
                self.overlay_changed.emit(oid, 0.5 if snap_x else nx, 0.5 if snap_y else ny, sc)
            else:
                d0 = math.dist((self._press.x(), self._press.y()), (center.x(), center.y())) or 1.0
                d1 = math.dist((pos.x(), pos.y()), (center.x(), center.y()))
                self.overlay_changed.emit(oid, cx, cy, sc * d1 / d0)
            return
        if self._drag is None:
            if self._hit_text(pos) is not None:
                self.setCursor(Qt.CursorShape.SizeAllCursor)
                return
            ov, mode = self._hit_overlay(pos)
            if ov is not None:
                self.setCursor(Qt.CursorShape.SizeFDiagCursor if mode == "scale" else Qt.CursorShape.SizeAllCursor)
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
        if self._ov_drag is not None:
            self._ov_drag = None
            self._guides = (False, False)
            self.edit_finished.emit()
            self.update()
            return
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
        steps = (e.angleDelta().y() or e.angleDelta().x()) / 120
        if not steps:
            return
        ov, _ = self._hit_overlay(e.position())
        if ov is not None and ov.id == self.selected_overlay:
            self.overlay_wheel.emit(ov.id, 1.06 ** steps)     # колёсико над наложением — его размер
            return
        if not self.image.isNull():
            self.wheel_zoom.emit(1.06 ** steps)


def _cover(w: int, h: int, box: QRectF) -> QRectF:
    s = max(box.width() / w, box.height() / h)
    fw, fh = w * s, h * s
    return QRectF(box.x() + (box.width() - fw) / 2, box.y() + (box.height() - fh) / 2, fw, fh)
