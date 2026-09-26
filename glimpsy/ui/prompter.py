"""Суфлёр: текст плывёт поверх всех окон, пока вы рассказываете о работе.

Два режима:
  «Настройка» — сверху панель: пуск, скорость, размер букв, прозрачность, текст; окно можно
    двигать за панель и растягивать за правый нижний угол; колесо мыши листает текст.
  «Закреплён» — остаётся только текст, клики проходят насквозь в окно под ним (можно
    спокойно работать). Колесо мыши над суфлёром всё равно листает текст.

В запись суфлёр не попадает: на Windows система сама прячет окно от захвата экрана,
на Linux и Mac Glimpsy закрашивает это место в записи (см. RecorderEngine.set_masks).

Горячие клавиши (работают в любой программе) — Ctrl+Alt+4…0, см. настройки.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QTextDocument, QTextOption
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QSizeGrip, QSlider, QToolButton, QVBoxLayout, QWidget,
)

from glimpsy.ui import theme

log = logging.getLogger(__name__)

MIN_SPEED, MAX_SPEED = 1, 20          # скорость: десятые доли строки в секунду (4 → 0,4 строки/с)
MIN_FONT, MAX_FONT = 14, 72
BAR_H = 44
MARK = 0.3                            # линия чтения — на трети высоты окна
COUNTDOWN_S = 3


def native_exclude_from_capture(widget: QWidget) -> bool:
    """Спрятать окно от записи экрана средствами системы. False — система так не умеет."""
    if sys.platform.startswith("win"):
        try:
            import ctypes
            WDA_EXCLUDEFROMCAPTURE = 0x11          # Windows 10 2004 и новее
            return bool(ctypes.windll.user32.SetWindowDisplayAffinity(int(widget.winId()), WDA_EXCLUDEFROMCAPTURE))
        except Exception:
            log.exception("SetWindowDisplayAffinity не сработал")
            return False
    if sys.platform == "darwin":
        try:
            import objc  # noqa: F401
            from AppKit import NSWindowSharingNone
            from objc import objc_object
            view = objc_object(c_void_p=int(widget.winId()))
            view.window().setSharingType_(NSWindowSharingNone)
        except Exception:
            log.debug("sharingType недоступен", exc_info=True)
        return False           # захват через AVFoundation это не всегда учитывает — закрашиваем всё равно
    return False


class TextDialog(QDialog):
    """Вставить или открыть текст для суфлёра."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Текст суфлёра")
        self.resize(560, 480)
        self.edit = QPlainTextEdit(text)
        self.edit.setPlaceholderText("Вставьте сюда текст (Ctrl+V) или откройте файл .txt\n\n"
                                     "Пустая строка — новый абзац. Слова в [квадратных скобках] "
                                     "показываются приглушённо — удобно для пометок «[пауза]», «[показать экран]».")
        open_btn = QPushButton(theme.icon("folder-open", size=16), "  Открыть файл…")
        open_btn.clicked.connect(self._open)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(open_btn)
        row.addStretch(1)
        row.addWidget(buttons)
        lay = QVBoxLayout(self)
        lay.addWidget(self.edit, 1)
        lay.addLayout(row)

    def _open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Текст для суфлёра", "", "Текст (*.txt *.md);;Все файлы (*)")
        if not path:
            return
        for enc in ("utf-8", "cp1251"):
            try:
                with open(path, encoding=enc) as f:
                    self.edit.setPlainText(f.read())
                return
            except UnicodeDecodeError:
                continue
            except OSError as e:
                self.edit.setPlainText(f"Не удалось открыть файл: {e}")
                return


def to_html(text: str) -> str:
    """Текст → HTML для показа: абзацы, [пометки] приглушённо."""
    import html
    import re

    out = []
    for para in re.split(r"\n\s*\n", text.strip()):
        p = html.escape(para).replace("\n", "<br>")
        p = re.sub(r"\[([^\]]*)\]", r'<span style="color:#7C8494">[\1]</span>', p)
        out.append(f"<p>{p}</p>")
    return "".join(out) or "<p style='color:#7C8494'>Нажмите «Текст», чтобы вставить свой текст.</p>"


class Prompter(QWidget):
    """Окно суфлёра."""

    masks_changed = Signal(list)          # [(x, y, w, h)] — что закрасить в записи (пусто — ничего)
    _wheel = Signal(float)                # колесо мыши над закреплённым окном (из потока pynput)

    def __init__(self, is_speaking: Callable[[], bool] | None = None) -> None:
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        super().__init__(None, flags)
        self.setWindowTitle("Суфлёр Glimpsy")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.is_speaking = is_speaking
        st = QSettings("Glimpsy", "prompter")
        self.text = st.value("text", "", type=str)
        self.offset = st.value("offset", 0.0, type=float)
        self.speed = st.value("speed", 4, type=int)
        self.font_px = st.value("font", 30, type=int)
        self.bg_alpha = st.value("alpha", 0.8, type=float)
        self.voice_follow = st.value("voice_follow", False, type=bool)
        self.locked = False
        self.playing = False
        self._countdown_until = 0.0
        self._last = time.monotonic()
        self._drag: QPointF | None = None
        self._native_hidden = False
        self._listener = None
        self.doc = QTextDocument()
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WordWrap)
        self.doc.setDefaultTextOption(opt)
        self._build_bar()
        self._apply_text()
        geo = st.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(640, 300)
        self._timer = QTimer(self, interval=16)
        self._timer.timeout.connect(self._step)
        self._mask_timer = QTimer(self, singleShot=True, interval=600)
        self._mask_timer.timeout.connect(self._report_masks)
        self._wheel.connect(self.scroll_by)
        self.setMinimumSize(320, 160)

    # ---------- панель ----------

    def _build_bar(self) -> None:
        self.bar = QWidget(self)
        self.bar.setObjectName("prompterBar")
        self.bar.setStyleSheet("QWidget#prompterBar { background: transparent; }"
                               "QLabel { color: #C9CED8; } QToolButton { padding: 3px; }")

        def btn(icon: str, tip: str, slot) -> QToolButton:
            b = QToolButton()
            b.setIcon(theme.icon(icon, "#E6E8EE", 18))
            b.setToolTip(tip)
            b.clicked.connect(slot)
            return b

        self.play_btn = btn("play", "Пуск / пауза (Ctrl+Alt+5)", self.toggle_play)
        top = btn("rotate-ccw", "В начало (Ctrl+Alt+0)", self.to_top)
        self.speed_sl = QSlider(Qt.Orientation.Horizontal)
        self.speed_sl.setRange(MIN_SPEED, MAX_SPEED)
        self.speed_sl.setValue(self.speed)
        self.speed_sl.setFixedWidth(90)
        self.speed_sl.setToolTip("Скорость (Ctrl+Alt+6 / Ctrl+Alt+7)")
        self.speed_sl.valueChanged.connect(self._set_speed)
        self.speed_lbl = QLabel()
        smaller = btn("minus", "Мельче", lambda: self._set_font(self.font_px - 2))
        bigger = btn("plus", "Крупнее", lambda: self._set_font(self.font_px + 2))
        self.alpha_sl = QSlider(Qt.Orientation.Horizontal)
        self.alpha_sl.setRange(10, 100)
        self.alpha_sl.setValue(int(self.bg_alpha * 100))
        self.alpha_sl.setFixedWidth(70)
        self.alpha_sl.setToolTip("Прозрачность фона")
        self.alpha_sl.valueChanged.connect(self._set_alpha)
        self.voice_box = QCheckBox("по голосу")
        self.voice_box.setToolTip("Текст идёт, только пока вы говорите (нужен микрофон в настройках звука)")
        self.voice_box.setChecked(self.voice_follow)
        self.voice_box.setStyleSheet("color: #C9CED8;")
        self.voice_box.toggled.connect(self._set_voice_follow)
        text_btn = btn("type", "Текст: вставить или открыть файл", self.edit_text)
        lock = btn("focus", "Закрепить: клики насквозь, остаётся только текст (Ctrl+Alt+9)", lambda: self.set_locked(True))
        close = btn("x", "Спрятать (Ctrl+Alt+4)", self.hide)
        self.time_lbl = QLabel()
        lay = QHBoxLayout(self.bar)
        lay.setContentsMargins(10, 6, 8, 2)
        lay.setSpacing(4)
        for w in (self.play_btn, top, self.speed_sl, self.speed_lbl, smaller, bigger, self.alpha_sl,
                  self.voice_box, text_btn):
            lay.addWidget(w)
        lay.addStretch(1)
        lay.addWidget(self.time_lbl)
        lay.addWidget(lock)
        lay.addWidget(close)
        self.grip = QSizeGrip(self)
        self._set_speed(self.speed)

    def resizeEvent(self, e) -> None:
        self.bar.setGeometry(0, 0, self.width(), BAR_H)
        self.grip.move(self.width() - self.grip.sizeHint().width(), self.height() - self.grip.sizeHint().height())
        self._apply_text()
        self._geometry_changed()
        super().resizeEvent(e)

    def moveEvent(self, e) -> None:
        self._geometry_changed()
        super().moveEvent(e)

    # ---------- текст и вид ----------

    def _apply_text(self) -> None:
        f = QFont(theme.FONT)
        f.setPixelSize(self.font_px)
        f.setWeight(QFont.Weight.DemiBold)
        self.doc.setDefaultFont(f)
        self.doc.setDefaultStyleSheet(f"p {{ color: #F4F5F7; margin-bottom: {self.font_px // 2}px; "
                                      f"line-height: 140%; }}")
        self.doc.setHtml(to_html(self.text))
        self.doc.setTextWidth(max(100, self.width() - 56))
        self.update()

    @property
    def line_h(self) -> float:
        return self.font_px * 1.4

    def _set_speed(self, v: int) -> None:
        self.speed = max(MIN_SPEED, min(MAX_SPEED, int(v)))
        if self.speed_sl.value() != self.speed:
            self.speed_sl.setValue(self.speed)
        self.speed_lbl.setText(f"{self.speed}")
        self._save()

    def _set_font(self, px: int) -> None:
        # при смене размера остаёмся на том же месте текста
        frac = self.offset / max(1.0, self.doc.size().height())
        self.font_px = max(MIN_FONT, min(MAX_FONT, px))
        self._apply_text()
        self.offset = frac * self.doc.size().height()
        self._save()

    def _set_alpha(self, v: int) -> None:
        self.bg_alpha = v / 100
        self.update()
        self._save()

    def _set_voice_follow(self, on: bool) -> None:
        self.voice_follow = on
        self._save()

    def edit_text(self) -> None:
        was = self.playing
        self.pause()
        dlg = TextDialog(self.text, self)
        if dlg.exec():
            self.text = dlg.edit.toPlainText()
            self.offset = 0.0
            self._apply_text()
            self._save()
        elif was:
            self.play()

    def _save(self) -> None:
        st = QSettings("Glimpsy", "prompter")
        st.setValue("text", self.text)
        st.setValue("offset", self.offset)
        st.setValue("speed", self.speed)
        st.setValue("font", self.font_px)
        st.setValue("alpha", self.bg_alpha)
        st.setValue("voice_follow", self.voice_follow)
        st.setValue("geometry", self.saveGeometry())

    # ---------- движение ----------

    def play(self) -> None:
        if self.offset >= self.doc.size().height():
            self.offset = 0.0
        self.playing = True
        self._countdown_until = time.monotonic() + COUNTDOWN_S
        self._last = time.monotonic()
        self.play_btn.setIcon(theme.icon("pause", "#E6E8EE", 18))
        self.update()

    def pause(self) -> None:
        self.playing = False
        self._countdown_until = 0.0
        self.play_btn.setIcon(theme.icon("play", "#E6E8EE", 18))
        self._save()
        self.update()

    def toggle_play(self) -> None:
        self.pause() if self.playing else self.play()

    def slower(self) -> None:
        self._set_speed(self.speed - 1)

    def faster(self) -> None:
        self._set_speed(self.speed + 1)

    def back(self) -> None:
        self.scroll_by(-2 * self.line_h)

    def to_top(self) -> None:
        self.offset = 0.0
        self._save()
        self.update()

    def scroll_by(self, dy: float) -> None:
        self.offset = min(max(0.0, self.offset + dy), self.doc.size().height())
        self._countdown_until = 0.0          # пролистали — дальше сразу с этого места
        self.update()

    def lines_per_s(self) -> float:
        return self.speed / 10

    def _step(self) -> None:
        now = time.monotonic()
        dt, self._last = min(0.1, now - self._last), now
        if not self.playing:
            return
        if now < self._countdown_until:
            self.update()
            return
        if self.voice_follow and self.is_speaking is not None and not self.is_speaking():
            return
        self.offset += self.lines_per_s() * self.line_h * dt
        if self.offset >= self.doc.size().height():
            self.offset = self.doc.size().height()
            self.pause()
        self.update()

    def wheelEvent(self, e) -> None:
        self.scroll_by(-e.angleDelta().y() / 120 * self.line_h)

    # ---------- рисование ----------

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        bg = QColor(20, 23, 29)
        bg.setAlphaF(self.bg_alpha)
        p.setPen(QPen(QColor(255, 255, 255, 40 if self.locked else 70), 1))
        p.setBrush(bg)
        p.drawRoundedRect(r, 14, 14)
        clip = QPainterPath()
        top = 6 if self.locked else BAR_H
        text_r = QRectF(0, top, self.width(), self.height() - top - 6)
        clip.addRoundedRect(text_r, 10, 10)
        p.setClipPath(clip)
        mark_y = top + text_r.height() * MARK
        # полоса чтения
        band = QColor(34, 174, 187, 34)
        p.fillRect(QRectF(0, mark_y - self.line_h * 0.15, self.width(), self.line_h * 1.15), band)
        p.save()
        p.translate(28, mark_y - self.offset)
        self.doc.drawContents(p, QRectF(0, self.offset - mark_y, self.doc.textWidth(), self.height() + mark_y))
        p.restore()
        # мягкое затухание сверху и снизу
        for y0, y1, a0, a1 in ((top, top + 40, 1.0, 0.0), (self.height() - 46, self.height() - 6, 0.0, 1.0)):
            g = QLinearGradient(0, y0, 0, y1)
            c0, c1 = QColor(bg), QColor(bg)
            c0.setAlphaF(self.bg_alpha * a0)
            c1.setAlphaF(self.bg_alpha * a1)
            g.setColorAt(0, c0)
            g.setColorAt(1, c1)
            p.fillRect(QRectF(0, y0, self.width(), y1 - y0), g)
        # стрелка у линии чтения
        p.setClipping(False)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(34, 174, 187))
        tri = QPainterPath()
        tri.moveTo(8, mark_y + self.line_h * 0.1)
        tri.lineTo(18, mark_y + self.line_h * 0.4)
        tri.lineTo(8, mark_y + self.line_h * 0.7)
        tri.closeSubpath()
        p.drawPath(tri)
        # отсчёт перед стартом
        now = time.monotonic()
        if self.playing and now < self._countdown_until:
            n = int(self._countdown_until - now) + 1
            f = QFont(self.doc.defaultFont())
            f.setPixelSize(int(min(self.height() * 0.4, 120)))
            p.setFont(f)
            p.setPen(QColor(34, 174, 187, 230))
            p.drawText(QRectF(self.rect()), Qt.AlignmentFlag.AlignCenter, str(n))
        # сколько осталось
        left = max(0.0, self.doc.size().height() - self.offset) / self.line_h / max(0.05, self.lines_per_s())
        self.time_lbl.setText(f"осталось ~{int(left // 60)}:{int(left % 60):02d}")
        if self.locked:
            p.setPen(QColor(255, 255, 255, 90))
            f = QFont(self.doc.defaultFont())
            f.setPixelSize(11)
            p.setFont(f)
            p.drawText(QRectF(0, 0, self.width() - 12, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       "Ctrl+Alt+9 — настроить")
        p.end()

    # ---------- перетаскивание за панель ----------

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and e.position().y() <= BAR_H:
            self._drag = e.globalPosition() - QPointF(self.pos())

    def mouseMoveEvent(self, e) -> None:
        if self._drag is not None:
            self.move((e.globalPosition() - self._drag).toPoint())

    def mouseReleaseEvent(self, _e) -> None:
        if self._drag is not None:
            self._drag = None
            self._save()

    # ---------- закрепление: клики насквозь ----------

    def set_locked(self, on: bool) -> None:
        if on == self.locked and self.isVisible():
            return
        self.locked = on
        vis = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, on)
        self.bar.setVisible(not on)
        self.grip.setVisible(not on)
        if vis:
            self.show()
        self._listen_wheel(on and vis)
        self.update()

    def toggle_locked(self) -> None:
        self.set_locked(not self.locked)

    def _listen_wheel(self, on: bool) -> None:
        """Закреплённое окно не получает событий мыши — колесо слушаем глобально (pynput)."""
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                log.debug("pynput stop", exc_info=True)
            self._listener = None
        if not on:
            return
        try:
            from pynput import mouse
        except Exception:
            log.info("Колесо над закреплённым суфлёром недоступно (нет pynput)")
            return

        def on_scroll(x, y, _dx, dy) -> None:
            ratio = self.devicePixelRatioF() or 1.0
            g = self.frameGeometry()
            if g.contains(int(x / ratio), int(y / ratio)):
                self._wheel.emit(-dy * self.line_h)

        try:
            self._listener = mouse.Listener(on_scroll=on_scroll)
            self._listener.start()
        except Exception:
            log.exception("Не удалось слушать колесо мыши")
            self._listener = None

    # ---------- показ / скрытие и «не попадать в запись» ----------

    def toggle_visible(self) -> None:
        self.hide() if self.isVisible() else self.show_prompter()

    def show_prompter(self) -> None:
        self.show()
        self.raise_()
        self._native_hidden = native_exclude_from_capture(self)
        self._timer.start()
        self._listen_wheel(self.locked)
        self._report_masks()

    def hideEvent(self, e) -> None:
        self.pause()
        self._timer.stop()
        self._listen_wheel(False)
        self.masks_changed.emit([])
        super().hideEvent(e)

    def _geometry_changed(self) -> None:
        if self.isVisible():
            self._mask_timer.start()          # сообщим, когда окно перестанет двигаться

    def _report_masks(self) -> None:
        if not self.isVisible() or self._native_hidden:
            self.masks_changed.emit([])
            return
        g = self.frameGeometry()
        ratio = self.devicePixelRatioF() or 1.0
        # запас в пару пикселей — чтобы не осталось каёмки
        self.masks_changed.emit([(int((g.x() - 2) * ratio), int((g.y() - 2) * ratio),
                                  int((g.width() + 4) * ratio), int((g.height() + 4) * ratio))])

    def sizeHint(self) -> QSize:
        return QSize(640, 300)
