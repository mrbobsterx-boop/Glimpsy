"""Окно «Состояние записи» — обычное окно, которое можно держать на втором мониторе.

Крупно видно, пишется ли сейчас что-нибудь, что именно (монитор или окно) и как это выглядит
(маленький живой кадр записи), сколько идёт запись и сколько фрагментов. Кнопки — те же, что в
меню значка: начать, пауза, непрерывная запись, важный момент, завершить. Место и размер окна
запоминаются; если окно было открыто — оно откроется и при следующем запуске.
"""

from __future__ import annotations

import time
from typing import Callable

from PySide6.QtCore import QByteArray, QSettings, Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from glimpsy.recorder.engine import State
from glimpsy.recorder.ring_buffer import PREVIEW_SIZE
from glimpsy.ui import theme

# (текст, цвет) крупной строки для каждого состояния
HEADLINES = {
    State.RECORDING: ("● ИДЁТ ЗАПИСЬ", "#E5484D"),
    State.IDLE: ("⏸ ПАУЗА — нет активности", "#F5A524"),
    State.PAUSED: ("⏸ ПАУЗА", "#F5A524"),
    State.PRIVATE: ("⏸ НЕ ПИШЕТСЯ — приватное окно", "#8E4EC6"),
    State.WAITING: ("⏸ ЖДЁТ ОКНА ПОТОКА", "#22AEBB"),
    State.STARTING: ("… ЗАПУСК", "#8B8D98"),
    State.ASSEMBLING: ("💾 СОХРАНЯЮ ФРАГМЕНТЫ", "#3E63DD"),
    State.ERROR: ("⚠ ОШИБКА ЗАПИСИ", "#E5484D"),
    State.STOPPED: ("○ НЕ ЗАПИСЫВАЕТ", "#8B8D98"),
}


def headline(st: dict) -> tuple[str, str]:
    state = st.get("state", State.STOPPED)
    if st.get("continuous") and state == State.RECORDING:
        return "● НЕПРЕРЫВНАЯ ЗАПИСЬ", "#E5484D"
    return HEADLINES.get(state, ("", "#8B8D98"))


def elapsed_text(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


class StatusWindow(QWidget):
    """actions — что делают кнопки: start, pause, continuous, important, finish, editor."""

    def __init__(self, engine, actions: dict[str, Callable[[], None]]) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.engine = engine
        self.setWindowTitle("Glimpsy — состояние записи")
        self.setMinimumWidth(300)
        self.status: dict = {"state": State.STOPPED}
        self.big = QLabel()
        self.big.setStyleSheet("font-size: 22px; font-weight: 800;")
        self.what = theme.mark(QLabel(), "title")
        self.what.setWordWrap(True)
        self.preview = QLabel()
        w, h = PREVIEW_SIZE
        self.preview.setMinimumSize(w, h)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("background: #0B0E13; border-radius: 8px; color: #7C8494;")
        self.preview_hint = theme.mark(QLabel("Так сейчас видит запись"), "muted")
        self.info = theme.mark(QLabel(), "muted")
        self.info.setWordWrap(True)

        self.b_start = theme.mark(QPushButton(theme.icon("circle", theme.DANGER, 16), "  Начать запись"), "primary")
        self.b_start.clicked.connect(lambda _=False: actions["start"]())
        self.b_pause = QPushButton(theme.icon("pause", size=16), "  Пауза")
        self.b_pause.clicked.connect(lambda _=False: actions["pause"]())
        self.b_cont = QPushButton(theme.icon("circle", theme.DANGER, 16), "  Непрерывная")
        self.b_cont.setCheckable(True)
        self.b_cont.setToolTip("Вкл — пишется всё подряд, без пропусков. Выкл — снова кусками.")
        self.b_cont.clicked.connect(lambda _=False: actions["continuous"]())
        self.b_star = QPushButton(theme.icon("star", "#F5C518", 16), "  Важный момент")
        self.b_star.clicked.connect(lambda _=False: actions["important"]())
        self.b_finish = QPushButton(theme.icon("square", size=16), "  Завершить запись")
        self.b_finish.clicked.connect(lambda _=False: actions["finish"]())
        self.b_editor = theme.mark(QPushButton(theme.icon("film", size=16), "  Редактор…"), "ghost")
        self.b_editor.clicked.connect(lambda _=False: actions["editor"]())
        self.on_top = QCheckBox("Поверх всех окон")
        st = QSettings("Glimpsy", "status")
        self.on_top.setChecked(st.value("on_top", False, type=bool))
        self.on_top.toggled.connect(self._set_on_top)

        row1 = QHBoxLayout()
        for b in (self.b_start, self.b_pause, self.b_cont):
            row1.addWidget(b)
        row2 = QHBoxLayout()
        for b in (self.b_star, self.b_finish):
            row2.addWidget(b)
        bottom = QHBoxLayout()
        bottom.addWidget(self.on_top)
        bottom.addStretch(1)
        bottom.addWidget(self.b_editor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        lay.addWidget(self.big)
        lay.addWidget(self.what)
        lay.addWidget(self.preview, 1)
        lay.addWidget(self.preview_hint)
        lay.addWidget(self.info)
        lay.addLayout(row1)
        lay.addLayout(row2)
        lay.addLayout(bottom)

        geo = st.value("geometry", None)
        if isinstance(geo, QByteArray):
            self.restoreGeometry(geo)
        self._set_on_top(self.on_top.isChecked(), save=False)
        self._timer = QTimer(self, interval=500)
        self._timer.timeout.connect(self._tick)
        self.update_status(self.status)

    # ---------- показ ----------

    def open(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        QSettings("Glimpsy", "status").setValue("open", True)
        # на Windows окно не попадает в запись, даже если стоит на записываемом мониторе
        from glimpsy.ui.prompter import native_exclude_from_capture

        native_exclude_from_capture(self)

    def _set_on_top(self, on: bool, save: bool = True) -> None:
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        if visible:
            self.show()
        if save:
            QSettings("Glimpsy", "status").setValue("on_top", on)

    def showEvent(self, e) -> None:
        self._timer.start()
        self._tick()
        super().showEvent(e)

    def hideEvent(self, e) -> None:
        self._timer.stop()
        super().hideEvent(e)

    def closeEvent(self, e) -> None:
        st = QSettings("Glimpsy", "status")
        st.setValue("geometry", self.saveGeometry())
        st.setValue("open", False)
        super().closeEvent(e)

    def moveEvent(self, e) -> None:
        QSettings("Glimpsy", "status").setValue("geometry", self.saveGeometry())
        super().moveEvent(e)

    # ---------- данные ----------

    def update_status(self, st: dict) -> None:
        self.status = st
        state = st.get("state", State.STOPPED)
        text, color = headline(st)
        self.big.setText(text)
        self.big.setStyleSheet(f"font-size: 22px; font-weight: 800; color: {color};")
        if st.get("stream"):
            what = f"Пишется окно «{st['stream']}»"
        elif st.get("monitor") and state in (State.RECORDING, State.IDLE):
            what = f"Пишется монитор: {st['monitor']}"
        elif state == State.STOPPED:
            what = "Нажмите «Начать запись» или горячую клавишу"
        else:
            what = st.get("label", "")
        self.what.setText(what)
        stopped = state in (State.STOPPED, State.ERROR)
        busy = state == State.ASSEMBLING
        self.b_start.setVisible(stopped)
        for b in (self.b_pause, self.b_cont, self.b_star, self.b_finish):
            b.setVisible(not stopped and not busy)
        self.b_pause.setText("  Продолжить" if state == State.PAUSED else "  Пауза")
        self.b_pause.setIcon(theme.icon("play" if state == State.PAUSED else "pause", size=16))
        self.b_cont.setChecked(bool(st.get("continuous")))
        self._tick()

    def _tick(self) -> None:
        st = self.status
        state = st.get("state", State.STOPPED)
        parts = []
        start = getattr(self.engine, "session_start", 0.0)
        if state not in (State.STOPPED, State.ASSEMBLING) and start:
            parts.append(f"Запись идёт {elapsed_text(time.time() - start)}")
        n = st.get("candidates", 0)
        if n:
            parts.append(f"фрагментов: {n}")
        if st.get("important"):
            parts.append(f"важных: {st['important']}")
        if st.get("voice"):
            parts.append("🔴 пишется голос")
        if st.get("parallel"):
            parts.append("параллельно: " + ", ".join(st["parallel"]))
        if st.get("error"):
            parts.append(st["error"][:160])
        self.info.setText(" · ".join(parts))
        data = self.engine.preview() if hasattr(self.engine, "preview") else None
        if data:
            w, h = PREVIEW_SIZE
            img = QImage(data, w, h, w * 3, QImage.Format.Format_RGB888).copy()
            self.preview.setPixmap(QPixmap.fromImage(img).scaled(
                self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            self.preview_hint.setVisible(True)
        else:
            self.preview.setPixmap(QPixmap())
            self.preview.setText("Сейчас ничего не пишется" if state != State.STARTING else "Запускаю…")
            self.preview_hint.setVisible(False)
