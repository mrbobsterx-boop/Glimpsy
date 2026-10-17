"""Выбор области экрана для записи: экран затемняется, мышью выделяете прямоугольник.

На каждом мониторе — своё прозрачное окно во весь экран. Нарисованную рамку можно двигать
(взять внутри) и менять ей размер (тянуть за края и углы), стрелками — сдвигать точно.
«Записывать эту область», Enter или двойной щелчок — готово (в долях монитора, как вырезка окна
у потоков), Esc — отмена. Если область уже выбрана — она сразу показана, её можно поправить.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from glimpsy.platform.base import Monitor

MIN_PX = 40                     # меньше — скорее случайный щелчок, чем область
GRIP = 10                       # за сколько пикселей от края/угла рамку можно тянуть

EDGE_CURSORS = {"l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
                "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
                "tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
                "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
                "move": Qt.CursorShape.SizeAllCursor}


def grip_at(r: tuple[int, int, int, int], px: int, py: int) -> str:
    """За что взялись: угол (tl, tr, bl, br), край (l, r, t, b), «move» — внутри, "" — мимо рамки."""
    x, y, w, h = r
    if w <= 0 or h <= 0:
        return ""
    near_l, near_r = abs(px - x) <= GRIP, abs(px - (x + w)) <= GRIP
    near_t, near_b = abs(py - y) <= GRIP, abs(py - (y + h)) <= GRIP
    in_x, in_y = x - GRIP <= px <= x + w + GRIP, y - GRIP <= py <= y + h + GRIP
    v = "t" if near_t and in_x else "b" if near_b and in_x else ""
    hz = "l" if near_l and in_y else "r" if near_r and in_y else ""
    if v or hz:
        return v + hz if v and hz else v or hz
    return "move" if x < px < x + w and y < py < y + h else ""


def drag_rect(r: tuple[int, int, int, int], mode: str, dx: int, dy: int,
              bounds: tuple[int, int]) -> tuple[int, int, int, int]:
    """Рамка после перетаскивания: move — целиком (не выходя за экран), иначе — тянем края."""
    x, y, w, h = r
    W, H = bounds
    if mode == "move":
        return min(max(0, x + dx), max(0, W - w)), min(max(0, y + dy), max(0, H - h)), w, h
    x0, y0, x1, y1 = x, y, x + w, y + h
    if "l" in mode:
        x0 = min(max(0, x0 + dx), x1 - MIN_PX)
    if "r" in mode:
        x1 = max(min(W, x1 + dx), x0 + MIN_PX)
    if "t" in mode:
        y0 = min(max(0, y0 + dy), y1 - MIN_PX)
    if "b" in mode:
        y1 = max(min(H, y1 + dy), y0 + MIN_PX)
    return x0, y0, x1 - x0, y1 - y0


def rect_fraction(r: tuple[int, int, int, int], screen: tuple[int, int, int, int]) -> list[float] | None:
    """Прямоугольник на экране → доли [x, y, ширина, высота]. None — слишком мелко или почти весь экран."""
    x, y, w, h = r
    sx, sy, sw, sh = screen
    if w < MIN_PX or h < MIN_PX or sw <= 0 or sh <= 0:
        return None
    x0, y0 = max(0.0, (x - sx) / sw), max(0.0, (y - sy) / sh)
    x1, y1 = min(1.0, (x + w - sx) / sw), min(1.0, (y + h - sy) / sh)
    if x1 - x0 > 0.98 and y1 - y0 > 0.98:
        return None
    return [round(x0, 4), round(y0, 4), round(x1 - x0, 4), round(y1 - y0, 4)]


def match_monitor(screens: list[tuple[int, int, int, int]], monitors: list[Monitor], i: int) -> Monitor | None:
    """Какой монитор записи соответствует экрану Qt номер i (screens — левый верхний угол как у системы,
    размер в настоящих пикселях).

    Qt при масштабе экрана уменьшает размер, но левый верхний угол оставляет системным — по нему
    монитор находится точно. Не нашёлся — по порядку слева направо (он у Qt и у захвата один),
    а если мониторов разное число — ближайший по середине.
    """
    if not monitors or not 0 <= i < len(screens):
        return None
    exact = [m for m in monitors if (m.x, m.y) == (screens[i][0], screens[i][1])]
    if len(exact) == 1:
        return exact[0]
    if len(screens) == len(monitors):
        order = sorted(range(len(screens)), key=lambda k: (screens[k][0], screens[k][1]))
        mons = sorted(monitors, key=lambda m: (m.x, m.y))
        return mons[order.index(i)]
    x, y, w, h = screens[i]
    cx, cy = x + w / 2, y + h / 2
    return min(monitors, key=lambda m: (m.x + m.width / 2 - cx) ** 2 + (m.y + m.height / 2 - cy) ** 2)


class _Shade(QWidget):
    """Затемнение одного монитора, на нём рисуется и правится рамка."""

    def __init__(self, picker: RegionPicker, index: int, screen) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.picker, self.index = picker, index
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self.sel = QRect()
        self.mode = ""                         # new — рисуем новую, move / края — правим
        self.press: QPoint | None = None
        self.start_rect = QRect()
        self.bar = QWidget(self)
        self.bar.setStyleSheet("QPushButton { padding: 6px 14px; font-size: 14px; }")
        row = QHBoxLayout(self.bar)
        row.setContentsMargins(0, 0, 0, 0)
        self.ok = QPushButton("✓  Записывать эту область", self.bar)
        self.ok.setStyleSheet("background: #E5484D; color: white; font-weight: 700; border-radius: 6px;")
        self.ok.clicked.connect(self.confirm)
        self.no = QPushButton("Отмена", self.bar)
        self.no.clicked.connect(lambda _=False: self.picker.cancel())
        row.addWidget(self.ok)
        row.addWidget(self.no)
        self.bar.adjustSize()
        self.bar.hide()

    def set_selection(self, r: QRect) -> None:
        self.sel = r.normalized().intersected(self.rect())
        self._place_bar()
        self.update()

    def _place_bar(self) -> None:
        if self.sel.isNull() or self.mode:
            self.bar.hide()
            return
        self.bar.adjustSize()
        bw, bh = self.bar.width(), self.bar.height()
        x = min(max(0, self.sel.right() - bw), self.width() - bw)
        y = self.sel.bottom() + 10
        if y + bh > self.height():
            y = self.sel.top() - bh - 10
        if y < 0:
            y = self.sel.bottom() - bh - 10
        self.bar.move(x, y)
        self.bar.show()
        self.bar.raise_()

    def _r(self) -> tuple[int, int, int, int]:
        return self.sel.x(), self.sel.y(), self.sel.width(), self.sel.height()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 120))
        p.setPen(QColor("white"))
        if not self.sel.isNull():
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.fillRect(self.sel, Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            p.setPen(QPen(QColor("#E5484D"), 2))
            p.drawRect(self.sel.adjusted(0, 0, -1, -1))
            p.setBrush(QColor("#E5484D"))
            x, y, w, h = self._r()
            for hx, hy in ((x, y), (x + w // 2, y), (x + w, y), (x, y + h // 2), (x + w, y + h // 2),
                           (x, y + h), (x + w // 2, y + h), (x + w, y + h)):
                p.drawRect(hx - 4, hy - 4, 8, 8)
            dpr = self.devicePixelRatioF() or 1.0
            p.setPen(QColor("white"))
            p.drawText(self.sel.topLeft() + QPoint(6, -8 if self.sel.top() > 24 else 18),
                       f"{round(w * dpr)}×{round(h * dpr)}   ·   двигайте рамку и тяните за края")
        else:
            f = p.font()
            f.setPointSize(16)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "Выделите мышью часть экрана, которую записывать\nEsc — отмена")

    def mousePressEvent(self, e) -> None:
        if e.button() != Qt.MouseButton.LeftButton:
            self.picker.cancel()
            return
        pos = e.position().toPoint()
        self.picker.focus(self)
        self.press, self.start_rect = pos, QRect(self.sel)
        self.mode = grip_at(self._r(), pos.x(), pos.y()) if not self.sel.isNull() else ""
        if not self.mode:                      # мимо рамки — рисуем новую
            self.mode = "new"
            self.sel = QRect(pos, pos)
        self.bar.hide()
        self.update()

    def mouseMoveEvent(self, e) -> None:
        pos = e.position().toPoint()
        if self.press is None:
            g = grip_at(self._r(), pos.x(), pos.y()) if not self.sel.isNull() else ""
            self.setCursor(EDGE_CURSORS.get(g, Qt.CursorShape.CrossCursor))
            return
        if self.mode == "new":
            a, b = self.press, pos             # ширина — ровно разница (QRect(точка, точка) даёт +1)
            self.sel = QRect(min(a.x(), b.x()), min(a.y(), b.y()),
                             abs(b.x() - a.x()), abs(b.y() - a.y())).intersected(self.rect())
        else:
            s = self.start_rect
            x, y, w, h = drag_rect((s.x(), s.y(), s.width(), s.height()), self.mode,
                                   pos.x() - self.press.x(), pos.y() - self.press.y(), (self.width(), self.height()))
            self.sel = QRect(x, y, w, h)
        self.update()

    def mouseReleaseEvent(self, e) -> None:
        if self.press is None or e.button() != Qt.MouseButton.LeftButton:
            return
        self.press, mode, self.mode = None, self.mode, ""
        if mode == "new" and (self.sel.width() < MIN_PX or self.sel.height() < MIN_PX):
            self.sel = QRect(self.start_rect)       # случайный щелчок — прежняя рамка остаётся
            if self.sel.isNull() and self.picker.on_too_small:
                self.picker.on_too_small()
        self._place_bar()
        self.update()

    def mouseDoubleClickEvent(self, e) -> None:
        if not self.sel.isNull() and self.sel.contains(e.position().toPoint()):
            self.confirm()

    def keyPressEvent(self, e) -> None:
        k = e.key()
        if k == Qt.Key.Key_Escape:
            self.picker.cancel()
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not self.sel.isNull():
            self.confirm()
        elif k in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down) and not self.sel.isNull():
            step = 1 if e.modifiers() & Qt.KeyboardModifier.ShiftModifier else 10
            dx = {Qt.Key.Key_Left: -step, Qt.Key.Key_Right: step}.get(k, 0)
            dy = {Qt.Key.Key_Up: -step, Qt.Key.Key_Down: step}.get(k, 0)
            # Ctrl + стрелки — меняют размер (правый нижний угол), просто стрелки — двигают
            mode = "br" if e.modifiers() & Qt.KeyboardModifier.ControlModifier else "move"
            x, y, w, h = drag_rect(self._r(), mode, dx, dy, (self.width(), self.height()))
            self.set_selection(QRect(x, y, w, h))

    def confirm(self) -> None:
        # система могла поставить затемнение не на тот экран, куда просили, — берём тот, где оно на самом деле
        actual = self.screen()
        index = self.picker.screens.index(actual) if actual in self.picker.screens else self.index
        frac = rect_fraction(self._r(), (0, 0, self.width(), self.height()))
        if frac is None:
            if self.sel.width() >= MIN_PX and self.sel.height() >= MIN_PX:
                self.picker.choose_whole()          # рамку растянули на весь экран
            elif self.picker.on_too_small:
                self.picker.on_too_small()
            return
        self.picker.choose(index, frac)


class RegionPicker(QObject):
    """picked(монитор записи, [x, y, w, h] в долях), whole() — растянули на весь экран, или cancelled.

    current — уже выбранная область (номер монитора, доли): её рамка сразу на экране, можно поправить.
    """

    picked = Signal(object, list)
    whole = Signal()
    cancelled = Signal()

    def __init__(self, monitors: list[Monitor], parent: QObject | None = None,
                 current: tuple[int, list[float]] | None = None) -> None:
        super().__init__(parent)
        self.monitors = list(monitors)
        self.screens = QGuiApplication.screens()
        self.shades: list[_Shade] = []
        self.current = current
        self.on_too_small = None

    def _rects(self) -> list[tuple[int, int, int, int]]:
        out = []
        for sc in self.screens:
            g, k = sc.geometry(), sc.devicePixelRatio() or 1.0
            out.append((g.x(), g.y(), round(g.width() * k), round(g.height() * k)))
        return out

    def start(self) -> None:
        for i, sc in enumerate(self.screens):
            w = _Shade(self, i, sc)
            self.shades.append(w)
            w.showFullScreen()
        first = self.shades[0] if self.shades else None
        if self.current and self.shades:
            idx, (fx, fy, fw, fh) = self.current
            rects = self._rects()
            for i, w in enumerate(self.shades):
                mon = match_monitor(rects, self.monitors, i)
                if mon is not None and mon.index == idx:
                    W, H = w.width(), w.height()
                    w.set_selection(QRect(round(fx * W), round(fy * H), round(fw * W), round(fh * H)))
                    first = w
                    break
        if first is not None:
            first.activateWindow()
            first.setFocus()

    def focus(self, shade: _Shade) -> None:
        """Рамка одна на все мониторы: начали на другом — на прежнем она пропадает."""
        for w in self.shades:
            if w is not shade and not w.sel.isNull():
                w.sel = QRect()
                w._place_bar()
                w.update()

    def _close(self) -> None:
        for w in self.shades:
            w.close()
        self.shades = []

    def choose(self, i: int, frac: list[float]) -> None:
        mon = match_monitor(self._rects(), self.monitors, i)
        self._close()
        if mon is None:
            self.cancelled.emit()
        else:
            self.picked.emit(mon, frac)

    def choose_whole(self) -> None:
        self._close()
        self.whole.emit()

    def cancel(self) -> None:
        self._close()
        self.cancelled.emit()
