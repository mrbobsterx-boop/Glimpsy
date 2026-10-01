"""Окно «Автомонтаж влога»: формат, длина, отдельным проектом или нет."""

from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QSpinBox, QVBoxLayout,
)

from glimpsy.editor import vlog


class VlogDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Автомонтаж влога")
        self.setMinimumWidth(480)
        st = QSettings("Glimpsy", "editor")
        self.fmt = QComboBox()
        for key, (label, _aspect) in vlog.FORMATS.items():
            self.fmt.addItem(label, key)
        self.fmt.setCurrentIndex(max(0, self.fmt.findData(st.value("vlog/format", "vlog"))))
        self.minutes = QSpinBox(minimum=0, maximum=120, suffix=" мин")
        self.minutes.setSpecialValueText("как получится")
        self.minutes.setValue(int(st.value("vlog/minutes", 0)))
        self.minutes.setToolTip("Не длиннее стольких минут: сначала уберутся лишние кадры, потом — самые "
                                "слабые фразы. «Как получится» — вся речь и лучшие кадры.")
        self.seconds = QComboBox()
        for s in (15, 30, 45, 60, 90):
            self.seconds.addItem(f"{s} секунд", s)
        self.seconds.setCurrentIndex(max(0, self.seconds.findData(int(st.value("vlog/seconds", 45)))))
        self.subs = QCheckBox("Субтитры на видео")
        self.subs.setChecked(True)
        self.separate = QCheckBox("Отдельным проектом (этот монтаж останется как есть)")
        self.separate.setChecked(st.value("vlog/separate", True, type=bool))
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color: #8b8d98; font-size: 11px;")
        form = QFormLayout()
        form.addRow("Формат:", self.fmt)
        self.len_label = QLabel("Длина:")
        form.addRow(self.len_label, self.minutes)
        form.addRow("Длина:", self.seconds)
        form.addRow(self.subs)
        form.addRow(self.separate)
        self.form = form
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Смонтировать")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.info)
        lay.addWidget(buttons)
        self.fmt.currentIndexChanged.connect(self._update)
        self._update()

    @property
    def format(self) -> str:
        return self.fmt.currentData() or "vlog"

    @property
    def target_s(self) -> float:
        return float(self.seconds.currentData()) if self.format == "short" else self.minutes.value() * 60.0

    def _update(self) -> None:
        short = self.format == "short"
        self.form.setRowVisible(self.minutes, not short)
        self.form.setRowVisible(self.seconds, short)
        self.subs.setChecked(short or self.subs.isChecked())
        self.info.setText(
            "Самые живые фразы и короткие красивые кадры, вертикально 9:16 (горизонтальное видео "
            "обрежется по центру — сдвинуть кадр можно мышью в просмотре)." if short else
            "Вся ваша речь (длинные паузы вырезаются), а из мест без речи — лучшие кадры по несколько "
            "секунд: резкие, не тёмные, без тряски.")
        self.info.setText(self.info.text() + " Всё можно поправить в тексте или отменить Ctrl+Z.")

    def _accept(self) -> None:
        st = QSettings("Glimpsy", "editor")
        st.setValue("vlog/format", self.format)
        st.setValue("vlog/minutes", self.minutes.value())
        st.setValue("vlog/seconds", self.seconds.currentData())
        st.setValue("vlog/separate", self.separate.isChecked())
        self.accept()
