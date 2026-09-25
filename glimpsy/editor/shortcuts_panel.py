"""Раскрывающаяся слева шпаргалка с горячими клавишами редактора."""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QToolButton, QVBoxLayout, QWidget

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
    """Узкая полоска с кнопкой ⌨; по нажатию раскрывается список клавиш."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("shortcutsPanel")
        self.toggle = QToolButton()
        self.toggle.setText("⌨")
        self.toggle.setToolTip("Горячие клавиши")
        self.toggle.setCheckable(True)
        self.toggle.setAutoRaise(True)
        self.toggle.toggled.connect(self._set_open)

        self.body = QWidget()
        grid = QGridLayout(self.body)
        grid.setContentsMargins(4, 0, 8, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)
        title = QLabel("Горячие клавиши")
        title.setStyleSheet("font-weight: 600;")
        grid.addWidget(title, 0, 0, 1, 2)
        grid.addWidget(QLabel("работают в любой раскладке"), 1, 0, 1, 2)
        grid.itemAtPosition(1, 0).widget().setStyleSheet("color: #8b8d98; font-size: 11px;")
        row = 2
        for key, text in SHORTCUTS:
            if key is None:
                head = QLabel(text)
                head.setStyleSheet("color: #8b8d98; font-size: 11px; margin-top: 8px;")
                grid.addWidget(head, row, 0, 1, 2)
            else:
                k = QLabel(key)
                k.setObjectName("kbd")
                k.setStyleSheet("QLabel#kbd { border: 1px solid rgba(127,127,127,0.45); border-radius: 4px;"
                                " padding: 1px 5px; font-size: 11px; }")
                k.setAlignment(Qt.AlignmentFlag.AlignCenter)
                grid.addWidget(k, row, 0, alignment=Qt.AlignmentFlag.AlignLeft)
                desc = QLabel(text)
                desc.setWordWrap(True)
                grid.addWidget(desc, row, 1)
            row += 1
        grid.setRowStretch(row, 1)
        grid.setColumnStretch(1, 1)
        self.body.setFixedWidth(300)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(self.toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.body, 1)
        opened = QSettings("Glimpsy", "editor").value("shortcuts_open", False, type=bool)
        self.toggle.setChecked(opened)
        self._set_open(opened)

    def _set_open(self, on: bool) -> None:
        self.body.setVisible(on)
        self.setFixedWidth(306 if on else self.toggle.sizeHint().width() + 4)
        self.toggle.setText("⌨  ‹" if on else "⌨")
        QSettings("Glimpsy", "editor").setValue("shortcuts_open", on)
