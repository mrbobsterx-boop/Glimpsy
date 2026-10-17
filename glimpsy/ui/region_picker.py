"""Выбор области экрана для записи: экран затемняется, мышью выделяете прямоугольник.

На каждом мониторе — своё прозрачное окно во весь экран. Отпустили кнопку мыши — область выбрана
(в долях монитора, как вырезка окна у потоков), Esc или щелчок без выделения — отмена.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

from glimpsy.platform.base import Monitor

MIN_PX = 40                     # меньше — скорее случайный щелчок, чем область


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
    """Какой монитор записи соответствует экрану Qt номер i.

    У Qt и у захвата координаты могут отличаться масштабом, но порядок мониторов слева направо
    (и сверху вниз) один и тот же. Если мониторов разное число — ближайший по середине.
    """
    if not monitors or not 0 <= i < len(screens):
        return None
    if len(screens) == len(monitors):
        order = sorted(range(len(screens)), key=lambda k: (screens[k][0], screens[k][1]))
        mons = sorted(monitors, key=lambda m: (m.x, m.y))
        return mons[order.index(i)]
    x, y, w, h = screens[i]
    cx, cy = x + w / 2, y + h / 2
    return min(monitors, key=lambda m: (m.x + m.width / 2 - cx) ** 2 + (m.y + m.height / 2 - cy) ** 2)


class _Shade(QWidget):
    """Затемнение одного монитора, на нём рисуется выделение."""

    def __init__(self, picker: RegionPicker, index: int, screen) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.picker, self.index = picker, index
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self.origin: QPoint | None = None
        self.sel = QRect()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 120))
        if not self.sel.isNull():
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.fillRect(self.sel, Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            p.setPen(QPen(QColor("#E5484D"), 2))
            p.drawRect(self.sel.adjusted(0, 0, -1, -1))
            dpr = self.devicePixelRatioF() or 1.0
            text = f"{round(self.sel.width() * dpr)}×{round(self.sel.height() * dpr)}"
            p.setPen(QColor("white"))
            p.drawText(self.sel.topLeft() + QPoint(6, -8 if self.sel.top() > 24 else 18), text)
        else:
            p.setPen(QColor("white"))
            f = p.font()
            f.setPointSize(16)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "Выделите мышью часть экрана, которую записывать\nEsc — отмена")

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self.origin = e.position().toPoint()
            self.sel = QRect(self.origin, self.origin)
        else:
            self.picker.cancel()

    def mouseMoveEvent(self, e) -> None:
        if self.origin is not None:
            self.sel = QRect(self.origin, e.position().toPoint()).normalized().intersected(self.rect())
            self.update()

    def mouseReleaseEvent(self, e) -> None:
        if self.origin is None or e.button() != Qt.MouseButton.LeftButton:
            return
        self.origin = None
        frac = rect_fraction((self.sel.x(), self.sel.y(), self.sel.width(), self.sel.height()),
                             (0, 0, self.width(), self.height()))
        if frac is None:
            self.sel = QRect()
            self.update()
            if self.picker.on_too_small:
                self.picker.on_too_small()
            return
        self.picker.choose(self.index, frac)

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.picker.cancel()


class RegionPicker(QObject):
    """picked(монитор записи, [x, y, w, h] в долях) или cancelled."""

    picked = Signal(object, list)
    cancelled = Signal()

    def __init__(self, monitors: list[Monitor], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.monitors = list(monitors)
        self.screens = QGuiApplication.screens()
        self.shades: list[_Shade] = []
        self.on_too_small = None

    def start(self) -> None:
        for i, sc in enumerate(self.screens):
            w = _Shade(self, i, sc)
            self.shades.append(w)
            w.showFullScreen()
        if self.shades:
            self.shades[0].activateWindow()

    def _close(self) -> None:
        for w in self.shades:
            w.close()
        self.shades = []

    def choose(self, i: int, frac: list[float]) -> None:
        geo = [sc.geometry() for sc in self.screens]
        dpr = [sc.devicePixelRatio() or 1.0 for sc in self.screens]
        rects = [(round(g.x() * k), round(g.y() * k), round(g.width() * k), round(g.height() * k))
                 for g, k in zip(geo, dpr)]
        mon = match_monitor(rects, self.monitors, i)
        self._close()
        if mon is None:
            self.cancelled.emit()
        else:
            self.picked.emit(mon, frac)

    def cancel(self) -> None:
        self._close()
        self.cancelled.emit()
