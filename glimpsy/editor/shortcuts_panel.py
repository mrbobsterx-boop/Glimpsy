"""Раскрывающаяся слева шпаргалка с горячими клавишами редактора."""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QWidget

# (клавиша, что делает) — None вместо клавиши = заголовок группы
SHORTCUTS = [
    (None, "Просмотр"),
    ("Пробел", "пуск / пауза"),
    ("← →", "на кадр назад / вперёд"),
    ("Shift+← →", "на секунду"),
    ("Home / End", "в начало / в конец"),
    (None, "Фрагменты"),
    ("S", "разрезать по курсору"),
    ("Delete, ⌫", "удалить выбранное"),
    ("Ctrl+A", "выбрать все фрагменты"),
    ("Shift+щелчок", "выбрать несколько"),
    ("M", "добавить видео или фото"),
    ("Ctrl+V", "вставить файл или картинку"),
    (None, "Кадр и эффекты"),
    ("Z", "автозум к курсору — вкл / выкл"),
    ("C", "подсветка кликов — вкл / выкл"),
    ("F", "вписать кадр"),
    ("G", "заполнить кадр"),
    (None, "Текст и проект"),
    ("T", "добавить текст"),
    ("Ctrl+Z", "отменить"),
    ("Ctrl+Y", "повторить"),
    ("Ctrl+E", "экспорт"),
]


class ShortcutsPanel(QFrame):
    """Шпаргалка клавиш, выезжает слева (кнопка «Клавиши» на панели инструментов)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("shortcutsPanel")
        self.setStyleSheet("QFrame#shortcutsPanel { background: #14171D; border: 1px solid #262B36;"
                           " border-radius: 12px; }")
        grid = QGridLayout(self)
        grid.setContentsMargins(14, 12, 12, 12)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(5)
        title = QLabel("Горячие клавиши")
        title.setProperty("role", "title")
        grid.addWidget(title, 0, 0, 1, 2)
        sub = QLabel("работают в любой раскладке")
        sub.setProperty("role", "hint")
        grid.addWidget(sub, 1, 0, 1, 2)
        row = 2
        for key, text in SHORTCUTS:
            if key is None:
                head = QLabel(text)
                head.setProperty("role", "section")
                grid.addWidget(head, row, 0, 1, 2)
            else:
                k = QLabel(key)
                k.setProperty("role", "kbd")
                k.setAlignment(Qt.AlignmentFlag.AlignCenter)
                grid.addWidget(k, row, 0, alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                desc = QLabel(text)
                desc.setWordWrap(True)
                grid.addWidget(desc, row, 1)
            row += 1
        grid.setRowStretch(row, 1)
        grid.setColumnStretch(1, 1)
        self.setFixedWidth(290)
        self.setVisible(self.is_open())

    @staticmethod
    def is_open() -> bool:
        return QSettings("Glimpsy", "editor").value("shortcuts_open", False, type=bool)

    def set_open(self, on: bool) -> None:
        self.setVisible(on)
        QSettings("Glimpsy", "editor").setValue("shortcuts_open", on)
