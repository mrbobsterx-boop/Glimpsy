"""Окно «Записывать только окно…»: выбрать окно за 3 секунды и решить, как его узнавать."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton, QVBoxLayout,
)

from glimpsy.platform.base import ActiveWindowProbe, WindowInfo
from glimpsy.recorder.engine import _app_name
from glimpsy.recorder.streams import StreamSpec
from glimpsy.ui import theme

COUNTDOWN = 3


class StreamPicker(QDialog):
    """Немодальное окно: пока идёт отсчёт, оно сворачивается, чтобы вы щёлкнули по нужному окну."""

    def __init__(self, probe: ActiveWindowProbe, new_id: int, on_done: Callable[[StreamSpec], None],
                 first: bool) -> None:
        super().__init__()
        self.probe = probe
        self.new_id = new_id
        self.on_done = on_done
        self.win: WindowInfo | None = None
        self._left = 0
        self.setWindowTitle("Записывать только окно" if first else "Ещё одно окно")
        self.setMinimumWidth(460)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        title = theme.mark(QLabel("Какое окно записывать?"), "h1")
        hint = theme.mark(QLabel(
            "Нажмите «Выбрать окно» и за 3 секунды щёлкните по нужному окну. "
            "Записываться будет только оно: переключились на другое — запись ждёт, "
            "вернулись — продолжается." + ("" if first else " Это окно получит свой отдельный ролик.")), "muted")
        hint.setWordWrap(True)
        self.b_pick = theme.mark(QPushButton(theme.icon("crosshair", size=16), "  Выбрать окно"), "primary")
        self.b_pick.clicked.connect(self._start_countdown)
        self.status = theme.mark(QLabel(""), "hint")
        self.status.setWordWrap(True)

        # --- после выбора ---
        self.found = QLabel()
        self.found.setWordWrap(True)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Название (так будет называться ролик)")
        self.r_window = QRadioButton("Только это окно")
        self.r_app = QRadioButton("Любое окно этой программы")
        self.r_title = QRadioButton("Окна, в заголовке которых есть:")
        self.title_word = QLineEdit()
        self.title_word.setPlaceholderText("например, название проекта или сайта")
        self.title_word.textEdited.connect(lambda _: self.r_title.setChecked(True))
        group = QButtonGroup(self)
        for r in (self.r_window, self.r_app, self.r_title):
            group.addButton(r)
        self.r_window.setChecked(True)
        self.details = [theme.mark(QLabel("Название"), "section"), self.name,
                        theme.mark(QLabel("Как узнавать окно"), "section"),
                        self.r_window, self.r_app, self.r_title, self.title_word]

        self.b_ok = theme.mark(QPushButton("Записывать"), "primary")
        self.b_ok.clicked.connect(self._accept)
        b_cancel = theme.mark(QPushButton("Отмена"), "ghost")
        b_cancel.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(b_cancel)
        buttons.addWidget(self.b_ok)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(10)
        lay.addWidget(title)
        lay.addWidget(hint)
        row = QHBoxLayout()
        row.addWidget(self.b_pick)
        row.addWidget(self.status, 1)
        lay.addLayout(row)
        lay.addWidget(self.found)
        for w in self.details:
            lay.addWidget(w)
        lay.addStretch(1)
        lay.addLayout(buttons)
        self._show_details(False)

    def _show_details(self, on: bool) -> None:
        for w in self.details:
            w.setVisible(on)
        self.found.setVisible(on)
        self.b_ok.setEnabled(on)
        self.adjustSize()

    # ---------- отсчёт ----------

    def _start_countdown(self) -> None:
        self._left = COUNTDOWN
        self.b_pick.setEnabled(False)
        self.status.setText(f"Щёлкните по нужному окну… {self._left}")
        self.showMinimized()          # чтобы не мешать выбрать окно под ним
        QTimer.singleShot(1000, self._tick)

    def _tick(self) -> None:
        self._left -= 1
        if self._left > 0:
            self.status.setText(f"Щёлкните по нужному окну… {self._left}")
            QTimer.singleShot(1000, self._tick)
            return
        win = self.probe.active() if self.probe.supported else None
        own = {int(w.winId()) for w in QApplication.topLevelWidgets() if w.isVisible() or w is self}
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.b_pick.setEnabled(True)
        self.b_pick.setText("  Выбрать заново")
        if win is None or not (win.app or win.title):
            self.status.setText("Не получилось узнать, какое окно впереди. На Wayland это, к сожалению, "
                                "не поддерживается — можно снимать весь экран.")
            self._show_details(False)
            return
        if win.wid and win.wid in own:
            self.status.setText("Это окно самого Glimpsy — выберите другое.")
            self._show_details(False)
            return
        self.win = win
        self.status.setText("")
        app = _app_name(win.app) or "программа"
        self.found.setText(f"Выбрано: <b>{_esc(win.title) or _esc(app)}</b> · {_esc(app)}")
        self.name.setText((win.title or app)[:40])
        self.r_app.setText(f"Любое окно программы «{app}»")
        self.title_word.setText(win.title[:60])
        self.r_window.setEnabled(bool(win.wid))
        (self.r_window if win.wid else self.r_title).setChecked(True)
        self._show_details(True)

    # ---------- готово ----------

    def _accept(self) -> None:
        win = self.win
        if win is None:
            return
        mode = "window" if self.r_window.isChecked() else "app" if self.r_app.isChecked() else "title"
        if mode == "title" and not self.title_word.text().strip():
            self.status.setText("Впишите слово из заголовка окна.")
            return
        name = self.name.text().strip() or _app_name(win.app) or f"Поток {self.new_id}"
        self.on_done(StreamSpec(id=self.new_id, name=name, mode=mode, wid=win.wid, app=win.app,
                                title=self.title_word.text().strip()))
        self.close()


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def describe(spec: StreamSpec) -> str:
    """Коротко для меню: «Проект А — это окно»."""
    how = {"window": "это окно", "app": "все окна программы", "title": f"заголовок «{spec.title}»"}
    return f"{spec.name} — {how.get(spec.mode, spec.mode)}"
