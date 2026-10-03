"""Своё всплывающее окошко в углу экрана — с кнопками, которые точно работают.

Системные уведомления на Linux (например, на Steam Deck) часто не сообщают программе, что по
ним щёлкнули, — поэтому «Нажмите, чтобы открыть…» ничего не делало. Здесь окошко рисует сама
программа: кнопки, щелчок по тексту, крестик; само прячется через несколько секунд (пока мышь
над ним — ждёт).
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton, QVBoxLayout, QWidget

from glimpsy.ui import theme

SHOW_MS = 12000


class Toast(QWidget):
    def __init__(self, title: str, text: str, actions: list[tuple[str, Callable[[], None]]]) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.actions = actions
        box = QFrame(self)
        box.setObjectName("toast")
        box.setStyleSheet("QFrame#toast { background: #1C2029; border: 1px solid #3A4152; border-radius: 12px; }")
        head = QLabel(f"<b>{title}</b>")
        head.setStyleSheet("font-size: 14px;")
        close = QToolButton()
        close.setIcon(theme.icon("x", theme.MUTED, 16))
        close.setToolTip("Закрыть")
        close.clicked.connect(self.close)
        top = QHBoxLayout()
        top.addWidget(head, 1)
        top.addWidget(close)
        body = QLabel(text)
        body.setWordWrap(True)
        body.setStyleSheet("color: #C9CCD6;")
        body.setCursor(Qt.CursorShape.PointingHandCursor if actions else Qt.CursorShape.ArrowCursor)
        body.mousePressEvent = lambda _e: self._run(0)          # щелчок по тексту — первая кнопка
        row = QHBoxLayout()
        row.addStretch(1)
        self.buttons = []
        for i, (label, _fn) in enumerate(actions):
            b = theme.mark(QPushButton(label), "primary" if i == 0 else "ghost")
            b.clicked.connect(lambda _=False, i=i: self._run(i))
            row.addWidget(b)
            self.buttons.append(b)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(14, 10, 10, 12)
        lay.setSpacing(6)
        lay.addLayout(top)
        lay.addWidget(body)
        if actions:
            lay.addLayout(row)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(box)
        self.setFixedWidth(380)
        self._timer = QTimer(self, singleShot=True, interval=SHOW_MS)
        self._timer.timeout.connect(self.close)

    def _run(self, i: int) -> None:
        if 0 <= i < len(self.actions):
            fn = self.actions[i][1]
            self.close()
            fn()

    def popup(self) -> None:
        self.adjustSize()
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.right() - self.width() - 16, area.bottom() - self.height() - 16)
        self.show()
        self.raise_()
        self._timer.start()

    def enterEvent(self, e) -> None:
        self._timer.stop()                                   # читают — не прячемся
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self._timer.start(4000)
        super().leaveEvent(e)
