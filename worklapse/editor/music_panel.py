"""Панель настроек фоновой музыки."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFormLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from worklapse.editor.music import MusicTrack


class MusicPanel(QWidget):
    edited = Signal(str, object)      # что меняем, значение

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumWidth(320)
        self._loading = False
        self.title = QLabel("Фоновая музыка")
        self.title.setStyleSheet("font-weight: 600;")
        self.title.setWordWrap(True)
        self.volume = QSlider(Qt.Orientation.Horizontal, minimum=0, maximum=100)
        self.volume.valueChanged.connect(lambda v: self._emit("volume", v / 100))
        self.start = QDoubleSpinBox(minimum=0, maximum=36000, singleStep=1, decimals=1, suffix=" с")
        self.start.valueChanged.connect(lambda v: self._emit("in_s", v))
        self.fade_in = QDoubleSpinBox(minimum=0, maximum=10, singleStep=0.5, decimals=1, suffix=" с")
        self.fade_in.valueChanged.connect(lambda v: self._emit("fade_in", v))
        self.fade_out = QDoubleSpinBox(minimum=0, maximum=10, singleStep=0.5, decimals=1, suffix=" с")
        self.fade_out.valueChanged.connect(lambda v: self._emit("fade_out", v))
        self.loop = QCheckBox("Повторять, если трек короче ролика")
        self.loop.toggled.connect(lambda on: self._emit("loop", on))
        self.duck = QCheckBox("Тише, когда в ролике свой звук")
        self.duck.setToolTip("Музыка сама приглушается, пока звучит звук видео (например, голос)")
        self.duck.toggled.connect(lambda on: self._emit("duck", on))
        replace = QPushButton("Заменить трек…")
        replace.clicked.connect(lambda: self.edited.emit("replace", None))
        remove = QPushButton("Убрать музыку")
        remove.clicked.connect(lambda: self.edited.emit("remove", None))

        form = QFormLayout()
        form.addRow("Громкость:", self.volume)
        form.addRow("Начать с места:", self.start)
        form.addRow("Плавное появление:", self.fade_in)
        form.addRow("Плавное затухание:", self.fade_out)
        form.addRow(self.loop)
        form.addRow(self.duck)
        hint = QLabel("Музыка звучит на всю длину ролика. Файл с музыкой можно просто перетащить "
                      "на ленту. В просмотре слышна громкость, а плавное появление, затухание "
                      "и приглушение — в готовом ролике.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8b8d98; font-size: 11px;")
        lay = QVBoxLayout(self)
        lay.addWidget(self.title)
        lay.addLayout(form)
        lay.addWidget(replace)
        lay.addWidget(remove)
        lay.addWidget(hint)
        lay.addStretch(1)

    def set_track(self, m: MusicTrack | None) -> None:
        if m is None:
            return
        self._loading = True
        self.title.setText(f"Фоновая музыка: {m.label}")
        self.volume.setValue(int(round(m.volume * 100)))
        self.start.setMaximum(max(0.0, m.duration - 1))
        self.start.setValue(m.in_s)
        self.fade_in.setValue(m.fade_in)
        self.fade_out.setValue(m.fade_out)
        self.loop.setChecked(m.loop)
        self.duck.setChecked(m.duck)
        self._loading = False

    def _emit(self, what: str, value) -> None:
        if not self._loading:
            self.edited.emit(what, value)
