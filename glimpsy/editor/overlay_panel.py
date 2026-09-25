"""Панель настроек выбранного наложения (картинка/видео поверх ролика)."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFormLayout, QGridLayout, QLabel, QPushButton, QSlider, QSpinBox,
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
        self.title.setStyleSheet("font-weight: 600;")
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

        form = QFormLayout()
        form.addRow("Появляется:", self.start)
        form.addRow("Длительность:", self.dur)
        form.addRow("Размер:", self.size)
        form.addRow("Прозрачность:", self.opacity)
        form.addRow("Скругление:", self.radius)
        form.addRow(self.shadow)
        form.addRow(self.sound)
        form.addRow("Положение:", grid)
        form.addRow(full)

        delete = QPushButton("Удалить наложение")
        delete.clicked.connect(lambda: self._emit("delete", None))
        hint = QLabel("В просмотре наложение перетаскивается мышью, размер — уголки или колёсико. "
                      "На ленте его можно двигать по времени и тянуть за края.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8b8d98; font-size: 11px;")

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
        kind = "Картинка" if item.kind == "image" else "Видео"
        self.title.setText(f"{kind} поверх ролика: {item.label}")
        self.start.setValue(item.start)
        self.dur.setMaximum(max(0.2, item.src_duration - item.in_s) if item.kind == "video" and item.src_duration
                            else 36000)
        self.dur.setValue(item.duration)
        self.size.setValue(int(round(item.layout_for(aspect)[2] * 100)))
        self.opacity.setValue(int(round(item.opacity * 100)))
        self.radius.setValue(int(round(item.radius * 100)))
        self.shadow.setChecked(item.shadow)
        self.sound.setVisible(item.kind == "video")
        self.sound.setEnabled(item.has_audio)
        self.sound.setChecked(item.has_audio and not item.muted)
        self.sound.setText("Звук видео" if item.has_audio else "Звук (в файле его нет)")
        self._loading = False

    def _emit(self, what: str, value) -> None:
        if not self._loading and self.item is not None:
            self.edited.emit(self.item.id, what, value)
