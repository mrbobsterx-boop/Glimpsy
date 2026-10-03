"""Панель настроек выбранного текста (справа, вместо свойств фрагмента)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFontComboBox, QFormLayout,
    QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QSlider, QSpinBox,
    QVBoxLayout, QWidget,
)

from glimpsy.editor.text import ANIMATIONS, KARAOKE, TextItem, add_font

POSITIONS = (("Верх", 0.12), ("Центр", 0.5), ("Низ", 0.85))
REF_PX = 1080   # размер показываем в пикселях для кадра 1080 по меньшей стороне


class ColorButton(QPushButton):
    picked = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.color = "#ffffff"
        self.setFixedWidth(64)
        self.clicked.connect(self._pick)

    def set_color(self, c: str) -> None:
        self.color = c
        self.setStyleSheet(f"QPushButton {{ background: {c}; border: 1px solid #888; border-radius: 4px;"
                           f" min-height: 20px; }}")

    def _pick(self) -> None:
        c = QColorDialog.getColor(QColor(self.color), self, "Цвет")
        if c.isValid():
            self.set_color(c.name())
            self.picked.emit(c.name())


class TextPanel(QWidget):
    # (id текста, что меняем, значение)
    edited = Signal(str, str, object)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumWidth(320)
        self.item: TextItem | None = None
        self._loading = False

        title = QLabel("Текст")
        title.setProperty("role", "title")
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText("Введите текст…")
        self.edit.setFixedHeight(70)
        self.edit.textChanged.connect(lambda: self._emit("text", self.edit.toPlainText()))

        self.start = QDoubleSpinBox(minimum=0, maximum=36000, singleStep=0.1, decimals=2, suffix=" с")
        self.dur = QDoubleSpinBox(minimum=0.2, maximum=3600, singleStep=0.1, decimals=2, suffix=" с")
        self.start.valueChanged.connect(lambda v: self._emit("start", v))
        self.dur.valueChanged.connect(lambda v: self._emit("duration", v))

        self.own = QCheckBox("Свой стиль для этого текста")
        self.own.toggled.connect(lambda on: self._emit("own_style", on))
        self.scope = QLabel()
        self.scope.setWordWrap(True)
        self.scope.setProperty("role", "hint")

        self.font_box = QFontComboBox()
        self.font_box.currentFontChanged.connect(lambda f: self._emit("font", f.family()))
        add_font_btn = QPushButton("＋ Добавить свой шрифт…")
        add_font_btn.setToolTip("Добавить свой шрифт (.ttf, .otf) — он будет доступен во всех проектах")
        add_font_btn.clicked.connect(self._add_font)
        self.font_box.setMinimumWidth(0)
        self.font_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.size = QSpinBox(minimum=10, maximum=400, suffix=" пикс.")
        self.size.valueChanged.connect(lambda v: self._emit("size", v / REF_PX))
        self.bold = QCheckBox("Жирный")
        self.italic = QCheckBox("Курсив")
        self.shadow = QCheckBox("Тень")
        self.bold.toggled.connect(lambda on: self._emit("bold", on))
        self.italic.toggled.connect(lambda on: self._emit("italic", on))
        self.shadow.toggled.connect(lambda on: self._emit("shadow", on))
        look = QHBoxLayout()
        for w in (self.bold, self.italic, self.shadow):
            look.addWidget(w)
        self.color = ColorButton()
        self.color.picked.connect(lambda c: self._emit("color", c))

        self.bg = QCheckBox("Подложка")
        self.bg.toggled.connect(lambda on: self._emit("bg", on))
        self.bg_color = ColorButton()
        self.bg_color.picked.connect(lambda c: self._emit("bg_color", c))
        bg_row = QHBoxLayout()
        bg_row.addWidget(self.bg)
        bg_row.addWidget(self.bg_color)
        bg_row.addStretch(1)
        self.bg_opacity = QSlider(Qt.Orientation.Horizontal, minimum=0, maximum=100)
        self.bg_opacity.valueChanged.connect(lambda v: self._emit("bg_opacity", v / 100))
        self.bg_radius = QSpinBox(minimum=0, maximum=100, suffix=" %")
        self.bg_radius.valueChanged.connect(lambda v: self._emit("bg_radius", v / 100))

        self.anim = QComboBox()
        for key, label in ANIMATIONS.items():
            self.anim.addItem(label, key)
        self.anim.currentIndexChanged.connect(lambda _: self._emit("anim", self.anim.currentData()))

        self.karaoke = QComboBox()
        for key, label in KARAOKE.items():
            self.karaoke.addItem(label, key)
        self.karaoke.setToolTip("Караоке: всё предложение на экране, а слово, которое звучит, выделяется.\n"
                                "Точнее всего — у субтитров из расшифровки речи.")
        self.karaoke.currentIndexChanged.connect(lambda _: self._emit("karaoke", self.karaoke.currentData()))
        self.hl_color = ColorButton()
        self.hl_color.picked.connect(lambda c: self._emit("hl_color", c))
        kara_row = QHBoxLayout()
        kara_row.addWidget(self.karaoke, 1)
        kara_row.addWidget(self.hl_color)

        pos_row = QHBoxLayout()
        for label, y in POSITIONS:
            b = QPushButton(label)
            b.setMinimumWidth(0)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.clicked.connect(lambda _=False, y=y: self._emit("pos_preset", y))
            pos_row.addWidget(b)

        form = QFormLayout()
        form.addRow("Появляется", self.start)
        form.addRow("Длительность", self.dur)
        form.addRow(self.own)
        form.addRow(self.scope)
        form.addRow("Шрифт", self.font_box)
        form.addRow(add_font_btn)
        form.addRow("Размер", self.size)
        form.addRow("Цвет", self.color)
        form.addRow(look)
        form.addRow(bg_row)
        form.addRow("Прозрачность", self.bg_opacity)
        form.addRow("Скругление", self.bg_radius)
        form.addRow("Анимация", self.anim)
        form.addRow("Слова", kara_row)
        form.addRow("Положение", pos_row)

        delete = QPushButton("Удалить текст")
        delete.clicked.connect(lambda: self._emit("delete", None))
        self.delete_auto = QPushButton("Удалить все автосубтитры")
        self.delete_auto.clicked.connect(lambda: self._emit("delete_auto", None))
        hint = QLabel("Текст можно перетаскивать мышью в окне просмотра, а на ленте — двигать по времени "
                      "и тянуть за края.")
        hint.setWordWrap(True)
        hint.setProperty("role", "hint")

        lay = QVBoxLayout(self)
        lay.addWidget(title)
        lay.addWidget(self.edit)
        lay.addLayout(form)
        lay.addWidget(delete)
        lay.addWidget(self.delete_auto)
        lay.addWidget(hint)
        lay.addStretch(1)

    def set_item(self, item: TextItem | None, style: dict | None = None) -> None:
        self.item = item
        if item is None or style is None:
            return
        self._loading = True
        self.delete_auto.setVisible(item.auto)
        if self.edit.toPlainText() != item.text:
            self.edit.setPlainText(item.text)
        self.start.setValue(item.start)
        self.dur.setValue(item.duration)
        self.own.setChecked(item.style is not None)
        self.scope.setText("Меняется только этот текст." if item.style is not None
                           else "Меняется общий стиль — все тексты без «своего стиля».")
        self.font_box.setCurrentFont(QFont(style["font"]) if style.get("font") else QFont())
        self.size.setValue(int(round(style["size"] * REF_PX)))
        self.bold.setChecked(bool(style["bold"]))
        self.italic.setChecked(bool(style["italic"]))
        self.shadow.setChecked(bool(style["shadow"]))
        self.color.set_color(style["color"])
        self.bg.setChecked(bool(style["bg"]))
        self.bg_color.set_color(style["bg_color"])
        self.bg_opacity.setValue(int(round(style["bg_opacity"] * 100)))
        self.bg_radius.setValue(int(round(style["bg_radius"] * 100)))
        self.anim.setCurrentIndex(max(0, self.anim.findData(style["anim"])))
        self.karaoke.setCurrentIndex(max(0, self.karaoke.findData(style.get("karaoke", "none"))))
        self.hl_color.set_color(style.get("hl_color", "#FFD400"))
        self.hl_color.setEnabled(style.get("karaoke", "none") not in ("none", "word"))
        self.anim.setEnabled(style.get("karaoke", "none") == "none")
        self._loading = False

    def focus_text(self) -> None:
        self.edit.setFocus()
        self.edit.selectAll()

    def _add_font(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "Добавить шрифт", str(Path.home()),
                                           "Шрифты (*.ttf *.otf *.ttc)")
        if not f:
            return
        family = add_font(Path(f))
        if not family:
            QMessageBox.warning(self, "Glimpsy", "Не получилось подключить этот файл шрифта.")
            return
        self.font_box.setCurrentFont(QFont(family))   # это же и применит шрифт

    def _emit(self, what: str, value) -> None:
        if not self._loading and self.item is not None:
            self.edited.emit(self.item.id, what, value)
