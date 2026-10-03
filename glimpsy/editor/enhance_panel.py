"""Раскрывающаяся слева панель «Улучшить»: звук (чистый голос, громкость) и картинка.

Всё применяется ко всему ролику при сохранении. Звук можно сразу послушать «как было /
как будет» — кусок в месте курсора.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton, QSlider, QToolButton, QVBoxLayout, QWidget,
)

from glimpsy.editor import sound
from glimpsy.ui import theme


def _hint(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setWordWrap(True)
    lb.setProperty("role", "hint")
    return lb


def _section(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setStyleSheet("font-weight: 600; margin-top: 10px;")
    return lb


class EnhancePanel(QFrame):
    close_requested = Signal()
    sound_changed = Signal(str, object)     # что, значение
    listen = Signal(str)                    # "before" / "after" / "stop"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("enhancePanel")
        self.setStyleSheet("QFrame#enhancePanel { background: #14171D; border: 1px solid #262B36;"
                           " border-radius: 12px; }")
        self._loading = False

        title = QLabel("Улучшить")
        title.setProperty("role", "title")
        close = QToolButton()
        close.setIcon(theme.icon("chevron-left", theme.MUTED, 18))
        close.setToolTip("Свернуть")
        close.clicked.connect(self.close_requested)
        head = QHBoxLayout()
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(close)

        # --- чистый голос ---
        self.denoise = QSlider(Qt.Orientation.Horizontal, minimum=0, maximum=100, singleStep=5, pageStep=10)
        self.denoise_value = QLabel()
        self.denoise_value.setMinimumWidth(70)
        self.denoise.valueChanged.connect(self._on_denoise)
        self.denoise.sliderReleased.connect(lambda: self._emit("denoise", self.denoise.value()))
        drow = QHBoxLayout()
        drow.addWidget(self.denoise, 1)
        drow.addWidget(self.denoise_value)
        self.model_status = _hint("")

        # --- громкость ---
        self.level = QCheckBox("Одинаковая громкость всего ролика")
        self.level.toggled.connect(lambda on: (self._emit("level", on), self._sync_enabled()))
        self.lufs = QSlider(Qt.Orientation.Horizontal, minimum=-24, maximum=-8, singleStep=1, pageStep=2)
        self.lufs_value = QLabel()
        self.lufs.valueChanged.connect(lambda v: self.lufs_value.setText(sound.lufs_label(v)))
        self.lufs.sliderReleased.connect(lambda: self._emit("lufs", float(self.lufs.value())))
        self.lufs.valueChanged.connect(lambda v: None if self.lufs.isSliderDown() else self._emit("lufs", float(v)))
        self.even = QCheckBox("Подтягивать тихие места")
        self.even.setToolTip("Если в одном месте вы говорили тише, а в другом громче — тихое станет громче")
        self.even.toggled.connect(lambda on: self._emit("even", on))

        # --- послушать ---
        self.b_before = theme.mark(QPushButton(theme.icon("play", size=16), " Как было"), "ghost")
        self.b_after = theme.mark(QPushButton(theme.icon("play", theme.ACCENT_HOVER, 16), " Как будет"), "ghost")
        self.b_stop = theme.mark(QPushButton(theme.icon("square", size=14), ""), "ghost")
        self.b_stop.setToolTip("Остановить")
        self.b_before.clicked.connect(lambda: self.listen.emit("before"))
        self.b_after.clicked.connect(lambda: self.listen.emit("after"))
        self.b_stop.clicked.connect(lambda: self.listen.emit("stop"))
        lrow = QHBoxLayout()
        lrow.addWidget(self.b_before)
        lrow.addWidget(self.b_after)
        lrow.addWidget(self.b_stop)
        lrow.addStretch(1)
        self.listen_status = _hint("Послушать 8 секунд с места курсора.")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 10, 10)
        lay.setSpacing(6)
        lay.addLayout(head)
        lay.addWidget(_section("Чистый голос"))
        lay.addWidget(_hint("Сколько шума убирать: вентилятор, гул, шипение микрофона. Убранное "
                            "смешивается с исходным звуком — голос не становится «железным»."))
        lay.addLayout(drow)
        lay.addWidget(self.model_status)
        lay.addWidget(_section("Громкость"))
        lay.addWidget(self.level)
        lay.addWidget(self.lufs)
        lay.addWidget(self.lufs_value)
        lay.addWidget(self.even)
        lay.addWidget(_section("Послушать"))
        lay.addLayout(lrow)
        lay.addWidget(self.listen_status)
        lay.addWidget(_hint("В просмотре звук пока как в записи — обработка делается при сохранении ролика."))
        lay.addStretch(1)
        self.setFixedWidth(320)
        self.setVisible(False)

    def set_open(self, on: bool) -> None:
        self.setVisible(on)

    def set_project(self, project) -> None:
        s = sound.settings(project)
        self._loading = True
        self.denoise.setValue(s["denoise"])
        self._on_denoise(s["denoise"])
        self.level.setChecked(s["level"])
        self.lufs.setValue(int(round(s["lufs"])))
        self.lufs_value.setText(sound.lufs_label(s["lufs"]))
        self.even.setChecked(s["even"])
        self._loading = False
        self._sync_enabled()

    def set_model_status(self, text: str) -> None:
        self.model_status.setText(text)
        self.model_status.setVisible(bool(text))

    def set_listen_status(self, text: str) -> None:
        self.listen_status.setText(text)

    def _on_denoise(self, v: int) -> None:
        self.denoise_value.setText("выкл." if v == 0 else f"{v}%")
        if not self.denoise.isSliderDown():
            self._emit("denoise", v)

    def _sync_enabled(self) -> None:
        self.lufs.setEnabled(self.level.isChecked())
        self.lufs_value.setEnabled(self.level.isChecked())

    def _emit(self, what: str, value) -> None:
        if not self._loading:
            self.sound_changed.emit(what, value)
