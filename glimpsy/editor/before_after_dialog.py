"""Окно «Было → стало»: выбрать вариант, длину и куда вставить."""

from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QPushButton, QRadioButton,
    QVBoxLayout,
)

from glimpsy.editor.before_after import DEFAULT_S, MODES
from glimpsy.ui import theme

HINTS = {
    "wipe": "Первый кадр работы, по нему проезжает шторка и открывает последний. Классика для дизайна.",
    "timelapse": "Вся работа, сжатая в несколько секунд, с полоской прогресса внизу.",
    "freeze": "Замерший первый кадр с плашкой «Было», резкая склейка — «Стало».",
}
PLACES = {"end": "В конец ролика", "start": "В начало ролика", "here": "На место курсора"}


class BeforeAfterDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Было → стало")
        self.setMinimumWidth(480)
        store = QSettings("Glimpsy", "editor")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(6)
        lay.addWidget(theme.mark(QLabel("Было → стало"), "h1"))
        sub = theme.mark(QLabel("Короткая вставка, которая показывает результат. Появится на ленте как "
                                "обычный фрагмент — её можно двигать, обрезать или удалить."), "muted")
        sub.setWordWrap(True)
        lay.addWidget(sub)
        lay.addSpacing(8)
        self.group = QButtonGroup(self)
        self.radios: dict[str, QRadioButton] = {}
        last = store.value("before_after/mode", "wipe")
        for key, text in MODES.items():
            r = QRadioButton(text)
            r.setChecked(key == last)
            r.toggled.connect(lambda on, k=key: on and self._pick(k))
            self.group.addButton(r)
            self.radios[key] = r
            note = theme.mark(QLabel(HINTS[key]), "hint")
            note.setWordWrap(True)
            note.setContentsMargins(26, 0, 0, 6)
            lay.addWidget(r)
            lay.addWidget(note)
        if not any(r.isChecked() for r in self.radios.values()):
            self.radios["wipe"].setChecked(True)
        self.seconds = QDoubleSpinBox(minimum=1.5, maximum=20, singleStep=0.5, decimals=1, suffix=" с")
        self.place = QComboBox()
        for k, v in PLACES.items():
            self.place.addItem(v, k)
        self.place.setCurrentIndex(max(0, self.place.findData(store.value("before_after/place", "end"))))
        form = QFormLayout()
        form.addRow("Длина", self.seconds)
        form.addRow("Куда вставить", self.place)
        lay.addSpacing(6)
        lay.addLayout(form)
        ok = theme.mark(QPushButton(theme.icon("square-split-horizontal", "#FFFFFF", 16), "  Создать"), "primary")
        ok.setDefault(True)
        ok.clicked.connect(self._accept)
        cancel = theme.mark(QPushButton("Отмена"), "ghost")
        cancel.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addSpacing(8)
        lay.addLayout(row)
        self._pick(self.mode)

    @property
    def mode(self) -> str:
        return next((k for k, r in self.radios.items() if r.isChecked()), "wipe")

    def _pick(self, key: str) -> None:
        self.seconds.setValue(DEFAULT_S[key])

    def _accept(self) -> None:
        store = QSettings("Glimpsy", "editor")
        store.setValue("before_after/mode", self.mode)
        store.setValue("before_after/place", self.place.currentData())
        self.accept()
