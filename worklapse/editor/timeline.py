"""Лента фрагментов (таймлайн), как в CapCut.

Что умеет:
  * щелчок — выбрать фрагмент и поставить туда курсор воспроизведения;
  * щелчок по линейке сверху — перемотка;
  * перетащить фрагмент — поменять порядок;
  * потянуть за левый/правый край — обрезать;
  * колёсико — прокрутка, Ctrl+колёсико — масштаб;
  * перетащить файлы из Проводника/Finder — вставить.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from worklapse.editor.media import Thumbnailer
from worklapse.editor.project import Project

RULER_H = 24
TRACK_Y = 34
TRACK_H = 64
EDGE_PX = 8
GAP = 2

COL_BG = QColor("#17171c")
COL_RULER = QColor("#8b8d98")
COL_CLIP = QColor("#1f6f78")
COL_IMAGE = QColor("#6b4fa3")
COL_SELECT = QColor("#ffffff")
COL_PLAYHEAD = QColor("#ffffff")
COL_DROP = QColor("#3e9bff")


def fmt_time(t: float, precise: bool = False) -> str:
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m):02d}:{s:04.1f}" if precise else f"{int(m):02d}:{int(s):02d}"


class TimelineWidget(QWidget):
    seek_requested = Signal(float)
    selection_changed = Signal(object)          # id фрагмента или None
    about_to_change = Signal(str)               # перед правкой (для отмены)
    changed = Signal()                          # после правки
    files_dropped = Signal(list, int)           # пути, позиция вставки

    def __init__(self, project: Project, thumbs: Thumbnailer) -> None:
        super().__init__()
        self.project = project
        self.thumbs = thumbs
        self.thumbs.ready.connect(self.update)
        self.pps = 60.0              # пикселей на секунду
        self.offset = 0.0            # прокрутка, пикс.
        self.playhead = 0.0
        self.selected: str | None = None       # главный выбранный фрагмент
        self.selection: list[str] = []          # все выбранные (Shift/Ctrl+щелчок)
        self._mode: str | None = None  # playhead / trim_l / trim_r / press / drag
        self._press: QPointF | None = None
        self._drag_idx = -1
        self._drop_idx = -1
        self._orig = (0.0, 0.0)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setMinimumHeight(TRACK_Y + TRACK_H + 24)
        self.setMaximumHeight(TRACK_Y + TRACK_H + 60)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # ---------- координаты ----------

    def x_of(self, t: float) -> float:
        return t * self.pps - self.offset + 12

    def t_of(self, x: float) -> float:
        return max(0.0, (x - 12 + self.offset) / self.pps)

    def clip_rects(self) -> list[QRectF]:
        rects, t = [], 0.0
        for c in self.project.clips:
            rects.append(QRectF(self.x_of(t) + GAP / 2, TRACK_Y, max(4.0, c.duration * self.pps - GAP), TRACK_H))
            t += c.duration
        return rects

    def fit(self) -> None:
        """Масштаб «вся лента в окне»."""
        total = max(self.project.total, 1.0)
        self.pps = max(4.0, min(400.0, (self.width() - 40) / total))
        self.offset = 0.0
        self.update()

    def zoom(self, factor: float, anchor_x: float | None = None) -> None:
        anchor_x = self.width() / 2 if anchor_x is None else anchor_x
        t = self.t_of(anchor_x)
        self.pps = max(4.0, min(600.0, self.pps * factor))
        self.offset = max(0.0, t * self.pps - anchor_x + 12)
        self.update()

    def set_playhead(self, t: float, follow: bool = False) -> None:
        self.playhead = t
        if follow:
            x = self.x_of(t)
            if x > self.width() - 40 or x < 12:
                self.offset = max(0.0, t * self.pps - self.width() * 0.25)
        self.update()

    # ---------- рисование ----------

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), COL_BG)
        self._paint_ruler(p)
        rects = self.clip_rects()
        for i, (c, r) in enumerate(zip(self.project.clips, rects)):
            if r.right() < 0 or r.left() > self.width():
                continue
            self._paint_clip(p, c, r, i == self._drag_idx and self._mode == "drag")
        if self._mode == "drag" and self._drop_idx >= 0:
            x = rects[self._drop_idx].left() if self._drop_idx < len(rects) else (rects[-1].right() if rects else 12)
            p.fillRect(QRectF(x - 2, TRACK_Y - 6, 4, TRACK_H + 12), COL_DROP)
        if not self.project.clips:
            p.setPen(COL_RULER)
            p.drawText(QRectF(0, TRACK_Y, self.width(), TRACK_H), Qt.AlignmentFlag.AlignCenter,
                       "Перетащите сюда видео или фото, или нажмите «Добавить медиа»")
        # курсор воспроизведения
        x = self.x_of(self.playhead)
        p.setPen(QPen(COL_PLAYHEAD, 2))
        p.drawLine(QPointF(x, 4), QPointF(x, self.height() - 4))
        p.setBrush(COL_PLAYHEAD)
        p.drawPolygon([QPointF(x - 6, 2), QPointF(x + 6, 2), QPointF(x, 10)])

    def _paint_ruler(self, p: QPainter) -> None:
        # шаг подписей подбираем так, чтобы они не слипались
        step = next(s for s in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600) if s * self.pps >= 70)
        p.setPen(COL_RULER)
        f = QFont(self.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1.5))
        p.setFont(f)
        t = (self.t_of(0) // step) * step
        while self.x_of(t) < self.width():
            x = self.x_of(t)
            p.drawLine(QPointF(x, RULER_H - 6), QPointF(x, RULER_H))
            p.drawText(QPointF(x + 3, RULER_H - 9), fmt_time(t, precise=step < 1))
            for k in range(1, 5):
                xm = self.x_of(t + step * k / 5)
                p.drawLine(QPointF(xm, RULER_H - 3), QPointF(xm, RULER_H))
            t += step

    def _paint_clip(self, p: QPainter, c, r: QRectF, ghost: bool) -> None:
        path = QPainterPath()
        path.addRoundedRect(r, 6, 6)
        p.save()
        p.setClipPath(path)
        p.fillRect(r, COL_IMAGE if c.kind == "image" else COL_CLIP)
        # миниатюры кадров по всей длине фрагмента
        th_h = int(TRACK_H)
        src = self.project.path_of(c)
        first = self.thumbs.get(src, c.in_s, th_h, c.kind == "image")
        tile_w = (first.width() if first is not None and not first.isNull() else th_h * 16 / 9) or th_h
        x = r.left()
        while x < r.right() and x < self.width():
            if x + tile_w >= 0:
                frac = (x - r.left()) / max(1.0, r.width())
                t_src = c.in_s + frac * (c.out_s - c.in_s)
                img = first if c.kind == "image" else self.thumbs.get(src, round(t_src, 0), th_h)
                if img is not None and not img.isNull():
                    p.drawImage(QPointF(x, r.top()), img)
            x += tile_w
        if ghost:
            p.fillRect(r, QColor(0, 0, 0, 150))
        # подписи
        p.fillRect(QRectF(r.left(), r.top(), r.width(), 16), QColor(0, 0, 0, 120))
        p.fillRect(QRectF(r.left(), r.bottom() - 16, r.width(), 16), QColor(0, 0, 0, 120))
        p.setPen(QColor("#ffffff"))
        f = QFont(self.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1.5))
        p.setFont(f)
        badges = ("⭐ " if c.priority else "") + ("🔇 " if c.muted and c.has_audio else "")
        if abs(c.speed - 1.0) > 1e-3:
            badges += f"×{c.speed:g} "
        p.drawText(QRectF(r.left() + 5, r.top(), r.width() - 10, 16),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, badges + (c.label or ""))
        p.drawText(QRectF(r.left() + 5, r.bottom() - 16, r.width() - 10, 16),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, f"{c.duration:.1f} с")
        p.restore()
        if c.id in self.selection:
            p.setPen(QPen(COL_SELECT if c.id == self.selected else COL_DROP, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 6, 6)
            # «ручки» обрезки
            p.setBrush(COL_SELECT)
            for hx in (r.left() + 1, r.right() - 7):
                p.drawRoundedRect(QRectF(hx, r.top() + 1, 6, r.height() - 2), 3, 3)

    # ---------- мышь ----------

    def _hit(self, pos: QPointF) -> tuple[int, str]:
        if pos.y() < TRACK_Y - 4 or pos.y() > TRACK_Y + TRACK_H + 4:
            return -1, ""
        for i, r in enumerate(self.clip_rects()):
            if r.left() - 2 <= pos.x() <= r.right() + 2:
                if pos.x() - r.left() <= EDGE_PX:
                    return i, "trim_l"
                if r.right() - pos.x() <= EDGE_PX:
                    return i, "trim_r"
                return i, "body"
        return -1, ""

    def mousePressEvent(self, e) -> None:
        if e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position()
        self._press = pos
        if pos.y() < RULER_H + 4:
            self._mode = "playhead"
            self.seek_requested.emit(self.t_of(pos.x()))
            return
        idx, part = self._hit(pos)
        if idx < 0:
            self._select(None)
            self._mode = "playhead"
            self.seek_requested.emit(min(self.t_of(pos.x()), self.project.total))
            return
        c = self.project.clips[idx]
        if e.modifiers() & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier):
            self._toggle(c.id)
            self._mode = None
            return
        self._select(c.id)
        self._drag_idx = idx
        if part in ("trim_l", "trim_r"):
            self._mode = part
            self._orig = (c.in_s, c.out_s)
            self.about_to_change.emit(f"trim:{c.id}")
        else:
            self._mode = "press"
            self.seek_requested.emit(self.t_of(pos.x()))

    def mouseMoveEvent(self, e) -> None:
        pos = e.position()
        if self._mode is None:
            _, part = self._hit(pos)
            self.setCursor(Qt.CursorShape.SizeHorCursor if part in ("trim_l", "trim_r")
                           else Qt.CursorShape.ArrowCursor)
            return
        if self._mode == "playhead":
            self.seek_requested.emit(min(self.t_of(pos.x()), self.project.total))
        elif self._mode in ("trim_l", "trim_r"):
            c = self.project.clips[self._drag_idx]
            d_src = (pos.x() - self._press.x()) / self.pps * c.speed
            in0, out0 = self._orig
            if self._mode == "trim_l":
                self.project.trim(self._drag_idx, in0 + d_src, out0)
            else:
                self.project.trim(self._drag_idx, in0, out0 + d_src)
            self.changed.emit()
        elif self._mode in ("press", "drag"):
            if self._mode == "press" and abs(pos.x() - self._press.x()) > 6:
                self._mode = "drag"
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            if self._mode == "drag":
                self._drop_idx = self._drop_index(pos.x())
                self.update()

    def mouseReleaseEvent(self, _e) -> None:
        if self._mode == "drag" and self._drop_idx not in (-1, self._drag_idx, self._drag_idx + 1):
            self.about_to_change.emit("move")
            self.project.move(self._drag_idx, self._drop_idx)
            self.changed.emit()
        self._mode = None
        self._drop_idx = -1
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def _drop_index(self, x: float) -> int:
        for i, r in enumerate(self.clip_rects()):
            if x < r.center().x():
                return i
        return len(self.project.clips)

    def _select(self, clip_id: str | None) -> None:
        changed = clip_id != self.selected or self.selection != ([clip_id] if clip_id else [])
        self.selected = clip_id
        self.selection = [clip_id] if clip_id else []
        if changed:
            self.selection_changed.emit(clip_id)
        self.update()

    def _toggle(self, clip_id: str) -> None:
        """Shift/Ctrl+щелчок: добавить фрагмент к выбранным или убрать из них."""
        if clip_id in self.selection:
            self.selection.remove(clip_id)
            self.selected = self.selection[-1] if self.selection else None
        else:
            self.selection.append(clip_id)
            self.selected = clip_id
        self.selection_changed.emit(self.selected)
        self.update()

    def select_all(self) -> None:
        self.selection = [c.id for c in self.project.clips]
        self.selected = self.selection[-1] if self.selection else None
        self.selection_changed.emit(self.selected)
        self.update()

    def prune_selection(self) -> None:
        """Убрать из выбора фрагменты, которых больше нет (после удаления/отмены)."""
        ids = {c.id for c in self.project.clips}
        self.selection = [i for i in self.selection if i in ids]
        if self.selected not in ids:
            self.selected = self.selection[-1] if self.selection else None

    def select(self, clip_id: str | None) -> None:
        self._select(clip_id)

    def wheelEvent(self, e) -> None:
        delta = e.angleDelta().y() or e.angleDelta().x()
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1.15 if delta > 0 else 1 / 1.15, e.position().x())
        else:
            max_off = max(0.0, self.project.total * self.pps - self.width() + 60)
            self.offset = max(0.0, min(max_off, self.offset - delta))
            self.update()

    # ---------- перетаскивание файлов ----------

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e) -> None:
        self._mode, self._drop_idx = "drag", self._drop_index(e.position().x())
        self._drag_idx = -1
        self.update()
        e.acceptProposedAction()

    def dragLeaveEvent(self, _e) -> None:
        self._mode, self._drop_idx = None, -1
        self.update()

    def dropEvent(self, e) -> None:
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        idx = self._drop_index(e.position().x())
        self._mode, self._drop_idx = None, -1
        self.update()
        if files:
            self.files_dropped.emit([str(Path(f)) for f in files], idx)

