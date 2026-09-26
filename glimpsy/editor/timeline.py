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

from glimpsy.editor.media import Thumbnailer
from glimpsy.editor.project import Project
from glimpsy.ui import theme

RULER_H = 24
TEXT_Y = 30          # дорожка текстов
TEXT_H = 22
OVL_Y = 56           # дорожка наложений (картинки/видео поверх ролика)
TRACK_Y = 84         # дорожка видео и фото
TRACK_H = 64
MUSIC_Y = TRACK_Y + TRACK_H + 6    # дорожка музыки
MUSIC_H = 20
EDGE_PX = 8
GAP = 2

COL_BG = QColor(theme.SURFACE)
COL_RULER = QColor(theme.FAINT)
COL_CLIP = QColor("#1C5F68")
COL_IMAGE = QColor("#4B3C7A")
COL_SELECT = QColor(theme.ACCENT_HOVER)
COL_PLAYHEAD = QColor("#FFFFFF")
COL_DROP = QColor(theme.ACCENT)
COL_TEXT = QColor("#D0932F")
COL_OVL = QColor("#7E62D6")
COL_MUSIC = QColor("#2E9A6E")
COL_LANE = QColor(255, 255, 255, 9)
LANE_ICONS = {"text": "type", "overlay": "layers", "music": "music"}
MIN_TEXT_S = 0.2
LANES = ("text", "overlay")


def fmt_time(t: float, precise: bool = False) -> str:
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m):02d}:{s:04.1f}" if precise else f"{int(m):02d}:{int(s):02d}"


class TimelineWidget(QWidget):
    seek_requested = Signal(float)
    selection_changed = Signal(object)          # id фрагмента или None
    about_to_change = Signal(str)               # перед правкой (для отмены)
    changed = Signal()                          # после правки
    files_dropped = Signal(list, int)           # пути, позиция вставки
    text_selected = Signal(object)              # id текста или None
    overlay_selected = Signal(object)           # id наложения или None
    overlay_files_dropped = Signal(list, float) # пути, время начала
    music_selected = Signal(bool)

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
        self.selected_text: str | None = None
        self.selected_overlay: str | None = None
        self.music_active = False
        self._lane = "text"
        self._lane_orig = (0.0, 0.0, 0.0)       # начало, длительность, in_s — в момент нажатия
        self._mode: str | None = None  # playhead / trim_l / trim_r / press / drag
        self._press: QPointF | None = None
        self._drag_idx = -1
        self._drop_idx = -1
        self._orig = (0.0, 0.0)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setMinimumHeight(MUSIC_Y + MUSIC_H + 10)
        self.setMaximumHeight(MUSIC_Y + MUSIC_H + 40)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # ---------- координаты ----------

    def x_of(self, t: float) -> float:
        return t * self.pps - self.offset + 12

    def t_of(self, x: float) -> float:
        return max(0.0, (x - 12 + self.offset) / self.pps)

    def _lane_items(self, kind: str) -> list:
        return self.project.texts if kind == "text" else self.project.overlays

    @staticmethod
    def _lane_y(kind: str) -> float:
        return TEXT_Y if kind == "text" else OVL_Y

    def lane_rects(self, kind: str) -> list[QRectF]:
        y = self._lane_y(kind)
        return [QRectF(self.x_of(t.start) + 1, y, max(6.0, t.duration * self.pps - 2), TEXT_H)
                for t in self._lane_items(kind)]

    def text_rects(self) -> list[QRectF]:
        return self.lane_rects("text")

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
        self._paint_lane(p, "text")
        self._paint_lane(p, "overlay")
        self._paint_music(p)
        if not self.project.clips:
            p.setPen(COL_RULER)
            p.drawText(QRectF(0, TRACK_Y, self.width(), TRACK_H), Qt.AlignmentFlag.AlignCenter,
                       "Перетащите сюда видео или фото, или нажмите «Медиа» слева")
        # курсор воспроизведения
        x = self.x_of(self.playhead)
        p.setPen(QPen(COL_PLAYHEAD, 2))
        p.drawLine(QPointF(x, 8), QPointF(x, self.height() - 4))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(COL_PLAYHEAD)
        p.drawRoundedRect(QRectF(x - 6, 2, 12, 12), 4, 4)

    def _paint_lane(self, p: QPainter, kind: str) -> None:
        f = QFont(self.font())
        f.setPixelSize(11)
        p.setFont(f)
        y = self._lane_y(kind)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(COL_LANE)
        p.drawRoundedRect(QRectF(4, y, self.width() - 8, TEXT_H), 6, 6)
        items = self._lane_items(kind)
        if not items:
            hint = ("Текст — кнопка «Текст» слева или клавиша T" if kind == "text"
                    else "Наложение — кнопка слева или перетащите сюда картинку или видео")
            self._lane_hint(p, kind, y, TEXT_H, hint)
            return
        selected = self.selected_text if kind == "text" else self.selected_overlay
        for t, r in zip(items, self.lane_rects(kind)):
            if r.right() < 0 or r.left() > self.width():
                continue
            p.setPen(QPen(QColor("#FFFFFF"), 2) if t.id == selected else Qt.PenStyle.NoPen)
            p.setBrush(COL_TEXT if kind == "text" else COL_OVL)
            p.drawRoundedRect(r, 6, 6)
            label = (t.text.strip().splitlines() or [""])[0] if kind == "text" else t.label
            self._chip_text(p, r, kind if kind == "text" else ("overlay" if t.kind == "image" else "video"), label)

    def music_rect(self) -> QRectF:
        return QRectF(self.x_of(0) + 1, MUSIC_Y, max(6.0, self.project.total * self.pps - 2), MUSIC_H)

    def _paint_music(self, p: QPainter) -> None:
        f = QFont(self.font())
        f.setPixelSize(11)
        p.setFont(f)
        m = self.project.music
        if m is None:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(COL_LANE)
            p.drawRoundedRect(QRectF(4, MUSIC_Y, self.width() - 8, MUSIC_H), 6, 6)
            self._lane_hint(p, "music", MUSIC_Y, MUSIC_H, "Музыка — кнопка «Музыка» слева или перетащите сюда mp3")
            return
        r = self.music_rect()
        p.setPen(QPen(QColor("#FFFFFF"), 2) if self.music_active else Qt.PenStyle.NoPen)
        p.setBrush(COL_MUSIC)
        p.drawRoundedRect(r, 6, 6)
        self._chip_text(p, r, "music", f"{m.label}  ·  громкость {int(m.volume * 100)}%")

    def _lane_hint(self, p: QPainter, kind: str, y: float, h: float, text: str) -> None:
        p.drawPixmap(QPointF(14, y + (h - 12) / 2), theme.pixmap(LANE_ICONS[kind], theme.FAINT, 12))
        p.setPen(COL_RULER)
        p.drawText(QRectF(32, y, self.width(), h), Qt.AlignmentFlag.AlignVCenter, text)

    def _chip_text(self, p: QPainter, r: QRectF, kind: str, text: str) -> None:
        """Иконка и подпись внутри элемента дорожки."""
        ic = {"text": "type", "overlay": "image-plus", "video": "film", "music": "music"}[kind]
        p.save()
        p.setClipRect(r.adjusted(2, 0, -2, 0))
        p.drawPixmap(QPointF(r.left() + 6, r.center().y() - 6), theme.pixmap(ic, "#FFFFFF", 12))
        p.setPen(QColor("#FFFFFF"))
        p.drawText(r.adjusted(22, 0, -4, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        p.restore()

    def select_music(self, on: bool) -> None:
        if on:
            self._select(None)
            self._set_lane_selection("text", None)
            self._set_lane_selection("overlay", None)
        if on != self.music_active:
            self.music_active = on
            self.music_selected.emit(on)
        self.update()

    def _paint_ruler(self, p: QPainter) -> None:
        # шаг подписей подбираем так, чтобы они не слипались
        step = next(s for s in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600) if s * self.pps >= 70)
        p.setPen(COL_RULER)
        f = QFont(self.font())
        f.setPixelSize(10)
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
        path.addRoundedRect(r, 8, 8)
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
        # подписи — «пилюли» поверх кадров
        f = QFont(self.font())
        f.setPixelSize(10)
        p.setFont(f)
        fm = p.fontMetrics()
        badges = ("★ " if c.priority else "") + ("без звука · " if c.muted and c.has_audio else "")
        if round(c.speed, 2) != 1.0:
            badges += f"×{round(c.speed, 2):g} · "
        mode = c.motion_for(self.project.aspect) if self.project is not None else "none"
        if mode != "none":
            badges += {"pushin": "наезд · ", "cursor_zoom": "к стрелке · ", "region": "область · "}.get(
                mode, "прокрутка · " if mode.startswith("scroll") else
                "за курсором · " if mode.startswith("follow") else "зум · ")
        top = badges + (c.label or "")
        for text, right in ((top, False), (f"{c.duration:.1f} с", True)):
            w = min(r.width() - 8, fm.horizontalAdvance(text) + 12)
            if w < 20:
                continue
            x = r.right() - 4 - w if right else r.left() + 4
            y = r.bottom() - 19 if right else r.top() + 4
            pill = QRectF(x, y, w, 15)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 150))
            p.drawRoundedRect(pill, 7, 7)
            p.setPen(QColor("#FFFFFF"))
            p.drawText(pill.adjusted(6, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter,
                       fm.elidedText(text, Qt.TextElideMode.ElideRight, int(pill.width() - 12)))
        p.restore()
        if c.id in self.selection:
            p.setPen(QPen(COL_SELECT if c.id == self.selected else QColor(theme.ACCENT_DOWN), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 8, 8)
            # «ручки» обрезки
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(COL_SELECT)
            for hx in (r.left() + 1, r.right() - 7):
                p.drawRoundedRect(QRectF(hx, r.top() + 1, 6, r.height() - 2), 3, 3)
            p.setBrush(QColor(0, 0, 0, 120))
            for hx in (r.left() + 3, r.right() - 5):
                p.drawRoundedRect(QRectF(hx, r.center().y() - 7, 2, 14), 1, 1)

    # ---------- мышь ----------

    def _hit_lane(self, pos: QPointF) -> tuple[str, int, str]:
        for kind in LANES:
            y = self._lane_y(kind)
            if not (y - 2 <= pos.y() <= y + TEXT_H + 2):
                continue
            rects = self.lane_rects(kind)
            for i in range(len(rects) - 1, -1, -1):          # последние — сверху
                r = rects[i]
                if r.left() - 2 <= pos.x() <= r.right() + 2:
                    if pos.x() - r.left() <= EDGE_PX:
                        return kind, i, "l_left"
                    if r.right() - pos.x() <= EDGE_PX:
                        return kind, i, "l_right"
                    return kind, i, "l_move"
        return "", -1, ""

    def _select_layer(self, kind: str, item_id: str | None) -> None:
        """Выбрать текст или наложение (выбор фрагментов и другой дорожки снимается)."""
        if item_id is not None and self.music_active:
            self.music_active = False
            self.music_selected.emit(False)
        if item_id is not None and self.selection:
            self.selected, self.selection = None, []
            self.selection_changed.emit(None)
        other = "overlay" if kind == "text" else "text"
        if item_id is not None:
            self._set_lane_selection(other, None)
        self._set_lane_selection(kind, item_id)
        self.update()

    def _set_lane_selection(self, kind: str, item_id: str | None) -> None:
        if kind == "text" and item_id != self.selected_text:
            self.selected_text = item_id
            self.text_selected.emit(item_id)
        elif kind == "overlay" and item_id != self.selected_overlay:
            self.selected_overlay = item_id
            self.overlay_selected.emit(item_id)

    def select_text(self, text_id: str | None) -> None:
        self._select_layer("text", text_id)

    def select_overlay(self, overlay_id: str | None) -> None:
        self._select_layer("overlay", overlay_id)

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
        if self.project.music is not None and MUSIC_Y - 2 <= pos.y() <= MUSIC_Y + MUSIC_H + 2:
            self.select_music(True)
            self._mode = "playhead"
            self.seek_requested.emit(min(self.t_of(pos.x()), self.project.total))
            return
        if self.music_active:
            self.select_music(False)
        kind, li, lpart = self._hit_lane(pos)
        if li >= 0:
            t = self._lane_items(kind)[li]
            self._select_layer(kind, t.id)
            self._lane, self._drag_idx, self._mode = kind, li, lpart
            self._lane_orig = (t.start, t.duration, getattr(t, "in_s", 0.0))
            self.about_to_change.emit(f"lanemove:{t.id}")
            self.seek_requested.emit(min(self.t_of(pos.x()), self.project.total))
            return
        idx, part = self._hit(pos)
        if idx < 0:
            self._select(None)
            self._select_layer("text", None)
            self._select_layer("overlay", None)
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
            _, _, lpart = self._hit_lane(pos)
            self.setCursor(Qt.CursorShape.SizeHorCursor if part in ("trim_l", "trim_r") or
                           lpart in ("l_left", "l_right") else Qt.CursorShape.ArrowCursor)
            return
        if self._mode in ("l_move", "l_left", "l_right"):
            self._drag_lane_item(self._lane_items(self._lane)[self._drag_idx],
                                 (pos.x() - self._press.x()) / self.pps)
            self.changed.emit()
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

    def _drag_lane_item(self, t, dt: float) -> None:
        """Сдвиг текста/наложения по времени или изменение длины за край."""
        s0, d0, in0 = self._lane_orig
        video = getattr(t, "kind", "") == "video"
        limit = (t.src_duration - in0) if video and t.src_duration else float("inf")
        if self._mode == "l_move":
            t.start = max(0.0, s0 + dt)
        elif self._mode == "l_left":
            end = s0 + d0
            lo = max(0.0, s0 - in0) if video else 0.0      # у видео нельзя уйти раньше его начала
            t.start = max(lo, min(end - MIN_TEXT_S, s0 + dt))
            t.duration = end - t.start
            if video:
                t.in_s = max(0.0, in0 + (t.start - s0))
        else:
            t.duration = max(MIN_TEXT_S, min(limit, d0 + dt))

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
        if clip_id is not None:
            self._set_lane_selection("text", None)
            self._set_lane_selection("overlay", None)
            if self.music_active:
                self.music_active = False
                self.music_selected.emit(False)
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
        if self.selected_text and self.project.text_by_id(self.selected_text) is None:
            self._set_lane_selection("text", None)
        if self.selected_overlay and self.project.overlay_by_id(self.selected_overlay) is None:
            self._set_lane_selection("overlay", None)
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
        if files and e.position().y() < TRACK_Y - 4:        # на дорожку наложений/текстов
            self._mode, self._drop_idx = None, -1
            self.update()
            self.overlay_files_dropped.emit([str(Path(f)) for f in files], self.t_of(e.position().x()))
            return
        idx = self._drop_index(e.position().x())
        self._mode, self._drop_idx = None, -1
        self.update()
        if files:
            self.files_dropped.emit([str(Path(f)) for f in files], idx)

