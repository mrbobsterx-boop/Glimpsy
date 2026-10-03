"""Панель настроек выбранного наложения (картинка/видео поверх ролика)."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QLabel, QPushButton, QSlider, QSpinBox,
    QVBoxLayout, QWidget,
)

from glimpsy.editor.overlay import OverlayItem

# центр X, центр Y для кнопок положения (с отступом от краёв)
CORNERS = (("↖", 0.2, 0.2), ("↑", 0.5, 0.2), ("↗", 0.8, 0.2),
           ("←", 0.2, 0.5), ("●", 0.5, 0.5), ("→", 0.8, 0.5),
           ("↙", 0.2, 0.8), ("↓", 0.5, 0.8), ("↘", 0.8, 0.8))


class OverlayPanel(QWidget):
    edited = Signal(str, str, object)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumWidth(320)
        self.item: OverlayItem | None = None
        self._loading = False

        self.title = QLabel("Наложение")
        self.title.setProperty("role", "title")
        self.title.setWordWrap(True)
        self.start = QDoubleSpinBox(minimum=0, maximum=36000, singleStep=0.1, decimals=2, suffix=" с")
        self.dur = QDoubleSpinBox(minimum=0.2, maximum=36000, singleStep=0.1, decimals=2, suffix=" с")
        self.start.valueChanged.connect(lambda v: self._emit("start", v))
        self.dur.valueChanged.connect(lambda v: self._emit("duration", v))
        self.size = QSpinBox(minimum=3, maximum=300, suffix=" % ширины")
        self.size.valueChanged.connect(lambda v: self._emit("scale", v / 100))
        self.opacity = QSlider(Qt.Orientation.Horizontal, minimum=5, maximum=100)
        self.opacity.valueChanged.connect(lambda v: self._emit("opacity", v / 100))
        self.radius = QSlider(Qt.Orientation.Horizontal, minimum=0, maximum=50)
        self.radius.valueChanged.connect(lambda v: self._emit("radius", v / 100))
        self.shadow = QCheckBox("Тень")
        self.shadow.toggled.connect(lambda on: self._emit("shadow", on))
        from glimpsy.editor.bgremove import MODES

        self.bg = QComboBox()
        for key, label in MODES.items():
            self.bg.addItem(label, key)
        self.bg.setToolTip("Убрать или размыть фон за человеком — без зелёного экрана.\n"
                           "Первый раз программа скачает нейросеть (≈25 МБ) и обработает видео.")
        self.bg.currentIndexChanged.connect(lambda _i: self._emit("bg", self.bg.currentData()))
        self.bg_status = QLabel()
        self.bg_status.setWordWrap(True)
        self.bg_status.setProperty("role", "hint")
        self.bg_status.setVisible(False)
        self.sound = QCheckBox("Звук видео")
        self.sound.toggled.connect(lambda on: self._emit("muted", not on))

        grid = QGridLayout()
        grid.setSpacing(4)
        for i, (label, x, y) in enumerate(CORNERS):
            b = QPushButton(label)
            b.setToolTip("Поставить сюда")
            b.clicked.connect(lambda _=False, x=x, y=y: self._emit("place", (x, y)))
            grid.addWidget(b, i // 3, i % 3)
        full = QPushButton("На весь кадр")
        full.clicked.connect(lambda: self._emit("fill", None))

        form = self.form = QFormLayout()
        form.addRow("Появляется", self.start)
        form.addRow("Длительность", self.dur)
        form.addRow("Размер", self.size)
        form.addRow("Прозрачность", self.opacity)
        form.addRow("Скругление", self.radius)
        form.addRow(self.shadow)
        form.addRow("Фон", self.bg)
        form.addRow(self.bg_status)
        form.addRow(self.sound)
        form.addRow("Положение", grid)
        form.addRow(full)
        self._visual_rows = (self.size, self.opacity, self.radius, self.shadow, grid, full)

        delete = self.delete_btn = QPushButton("Удалить наложение")
        delete.clicked.connect(lambda: self._emit("delete", None))
        hint = self.hint = QLabel()
        hint.setWordWrap(True)
        hint.setProperty("role", "hint")

        lay = QVBoxLayout(self)
        lay.addWidget(self.title)
        lay.addLayout(form)
        lay.addWidget(delete)
        lay.addWidget(hint)
        lay.addStretch(1)

    def set_item(self, item: OverlayItem | None, aspect: str) -> None:
        self.item = item
        if item is None:
            return
        self._loading = True
        audio = item.kind == "audio"
        kind = {"image": "Картинка", "audio": "Голос"}.get(item.kind, "Видео")
        self.title.setText(f"Голос: {item.label}" if audio else f"{kind} поверх ролика: {item.label}")
        for row in self._visual_rows:              # у голоса нет картинки — только время и звук
            self.form.setRowVisible(row, not audio)
        self.delete_btn.setText("Удалить запись голоса" if audio else "Удалить наложение")
        self.hint.setText("На ленте голос можно двигать по времени и обрезать за края." if audio else
                          "В просмотре наложение перетаскивается мышью, размер — уголки или колёсико. "
                          "На ленте его можно двигать по времени и тянуть за края.")
        self.start.setValue(item.start)
        self.dur.setMaximum(max(0.2, item.src_duration - item.in_s) if item.kind != "image" and item.src_duration
                            else 36000)
        self.dur.setValue(item.duration)
        self.size.setValue(int(round(item.layout_for(aspect)[2] * 100)))
        self.opacity.setValue(int(round(item.opacity * 100)))
        self.radius.setValue(int(round(item.radius * 100)))
        self.shadow.setChecked(item.shadow)
        self.form.setRowVisible(self.bg, item.kind == "video")
        self.bg.setCurrentIndex(max(0, self.bg.findData(getattr(item, "bg", ""))))
        self.sound.setVisible(item.kind != "image")
        self.sound.setEnabled(item.has_audio)
        self.sound.setChecked(item.has_audio and not item.muted)
        self.sound.setText(("Звук включён" if audio else "Звук видео") if item.has_audio else "Звук (в файле его нет)")
        self._loading = False

    def set_bg_status(self, text: str) -> None:
        self.bg_status.setText(text)
        self.bg_status.setVisible(bool(text))

    def _emit(self, what: str, value) -> None:
        if not self._loading and self.item is not None:
            self.edited.emit(self.item.id, what, value)
