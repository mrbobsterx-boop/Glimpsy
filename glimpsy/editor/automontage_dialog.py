"""Окно «Автомонтаж»: что сделать с роликом (всё включено, лишнее можно снять)."""

from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from glimpsy.editor.automontage import Options
from glimpsy.ui import theme

ITEMS = (
    ("zoom", "Автозум к курсору — время от времени", "Примерно каждый 2–3-й фрагмент, в первую очередь с кликами"),
    ("clicks", "Подсветка кликов", "Жёлтые круги там, где вы нажимали"),
    ("pace", "Темп: скучное быстрее, активное медленнее", "Фрагменты с речью не трогаются"),
    ("drop_empty", "Убрать пустые фрагменты", "Где ничего не происходило (не больше трети ролика)"),
    ("pushin", "Наезд камеры на важных моментах ⭐", "Медленное приближение к месту работы"),
    ("follow", "9:16 — кадр за курсором", "Для Reels: важное не уходит за край вертикального кадра"),
    ("beats", "Склейки в долю музыки", "Концы фрагментов попадают в удары музыкального трека"),
)


class AutomontageDialog(QDialog):
    def __init__(self, has_music: bool, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Автомонтаж")
        self.setMinimumWidth(460)
        store = QSettings("Glimpsy", "editor")
        title = theme.mark(QLabel("Автомонтаж"), "h1")
        sub = theme.mark(QLabel("Сделает ролик динамичнее. Всё — обычные правки: любую можно поменять "
                                "руками или отменить сразу всё (Ctrl+Z)."), "muted")
        sub.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(6)
        lay.addWidget(title)
        lay.addWidget(sub)
        lay.addSpacing(8)
        self.boxes: dict[str, QCheckBox] = {}
        for key, text, hint in ITEMS:
            box = QCheckBox(text)
            box.setChecked(store.value(f"automontage/{key}", True, type=bool))
            if key == "beats" and not has_music:
                box.setChecked(False)
                box.setEnabled(False)
                hint = "Сначала добавьте музыку (кнопка «Музыка» слева)"
            note = theme.mark(QLabel(hint), "hint")
            note.setContentsMargins(26, 0, 0, 6)
            lay.addWidget(box)
            lay.addWidget(note)
            self.boxes[key] = box
        ok = theme.mark(QPushButton(theme.icon("wand-sparkles", "#FFFFFF", 16), "  Смонтировать"), "primary")
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

    def _accept(self) -> None:
        store = QSettings("Glimpsy", "editor")
        for key, box in self.boxes.items():
            if box.isEnabled():
                store.setValue(f"automontage/{key}", box.isChecked())
        self.accept()

    def options(self) -> Options:
        return Options(**{k: b.isChecked() for k, b in self.boxes.items()})
