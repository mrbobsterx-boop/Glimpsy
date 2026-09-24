"""Панель свойств выбранного фрагмента (справа, как в CapCut)."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout,
    QWidget,
)

from worklapse.editor.project import MAX_SPEED, MIN_SPEED, Clip

SPEED_PRESETS = (0.5, 1, 2, 4, 10)


class Inspector(QWidget):
    # (id фрагмента, что меняем, новое значение)
    edited = Signal(str, str, object)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumWidth(300)
        self.clip: Clip | None = None
        self._loading = False

        self.title = QLabel("Выберите фрагмент на ленте")
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-weight: 600;")
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color: #8b8d98;")

        self.speed = QDoubleSpinBox(minimum=MIN_SPEED, maximum=MAX_SPEED, singleStep=0.25, decimals=2, suffix=" ×")
        self.speed.valueChanged.connect(lambda v: self._emit("speed", v))
        self.presets = QWidget()
        presets = QHBoxLayout(self.presets)
        presets.setContentsMargins(0, 0, 0, 0)
        presets.setSpacing(4)
        for v in SPEED_PRESETS:
            b = QPushButton(f"×{v:g}")
            b.setMinimumWidth(0)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.clicked.connect(lambda _=False, v=v: self.speed.setValue(v))
            presets.addWidget(b)

        self.sound = QCheckBox("Звук фрагмента")
        self.sound.toggled.connect(lambda on: self._emit("muted", not on))

        self.in_s = QDoubleSpinBox(minimum=0, maximum=36000, singleStep=0.1, decimals=2, suffix=" с")
        self.out_s = QDoubleSpinBox(minimum=0, maximum=36000, singleStep=0.1, decimals=2, suffix=" с")
        self.in_s.valueChanged.connect(lambda v: self._emit("in_s", v))
        self.out_s.valueChanged.connect(lambda v: self._emit("out_s", v))
        self.photo_dur = QDoubleSpinBox(minimum=0.2, maximum=600, singleStep=0.5, decimals=1, suffix=" с")
        self.photo_dur.valueChanged.connect(lambda v: self._emit("photo_duration", v))

        self.form = QFormLayout()
        self.form.addRow("Скорость:", self.speed)
        self.form.addRow(self.presets)
        self.form.addRow(self.sound)
        self.form.addRow("Начало в файле:", self.in_s)
        self.form.addRow("Конец в файле:", self.out_s)
        self.form.addRow("Показывать фото:", self.photo_dur)

        self.delete_btn = QPushButton("Удалить фрагмент")
        self.delete_btn.clicked.connect(lambda: self.clip and self.edited.emit(self.clip.id, "delete", None))

        lay = QVBoxLayout(self)
        lay.addWidget(self.title)
        lay.addWidget(self.info)
        lay.addSpacing(8)
        lay.addLayout(self.form)
        lay.addSpacing(8)
        lay.addWidget(self.delete_btn)
        lay.addStretch(1)
        hint = QLabel("Ctrl+Z — отменить · Ctrl+B — разрезать\nDelete — удалить · Пробел — пуск/пауза\n"
                      "Ctrl+V — вставить файл или картинку")
        hint.setStyleSheet("color: #8b8d98; font-size: 11px;")
        lay.addWidget(hint)
        self.set_clip(None)

    def set_clip(self, clip: Clip | None) -> None:
        self.clip = clip
        self._loading = True
        widgets = (self.speed, self.presets, self.sound, self.in_s, self.out_s, self.photo_dur, self.delete_btn)
        for w in widgets:
            w.setEnabled(clip is not None)
        if clip is None:
            self.title.setText("Выберите фрагмент на ленте")
            self.info.setText("Щёлкните по фрагменту внизу, чтобы изменить его скорость, звук и длину.")
            for w in (self.speed, self.presets, self.sound, self.in_s, self.out_s, self.photo_dur):
                self._set_row_visible(w, False)
            self.delete_btn.hide()
        else:
            self.delete_btn.show()
            kind = "Фото" if clip.kind == "image" else "Видео"
            self.title.setText(f"{kind}: {clip.label}" if clip.label else kind)
            extra = [f"В ролике: {clip.duration:.2f} с"]
            if clip.width:
                extra.append(f"Размер: {clip.width}×{clip.height}")
            if clip.priority:
                extra.append("⭐ Важный момент")
            self.info.setText("\n".join(extra))
            self.speed.setValue(clip.speed)
            is_video = clip.kind == "video"
            self.sound.setChecked(not clip.muted and clip.has_audio)
            self.sound.setEnabled(is_video and clip.has_audio)
            self.sound.setText("Звук фрагмента" if clip.has_audio or not is_video else "Звук (в записи экрана его нет)")
            for w in (self.in_s, self.out_s):
                w.setMaximum(clip.src_duration)
            self.in_s.setValue(clip.in_s)
            self.out_s.setValue(clip.out_s)
            self.photo_dur.setValue(clip.out_s - clip.in_s)
            self._set_row_visible(self.in_s, is_video)
            self._set_row_visible(self.out_s, is_video)
            self._set_row_visible(self.photo_dur, not is_video)
            self._set_row_visible(self.speed, is_video)
            self._set_row_visible(self.presets, is_video)
            self._set_row_visible(self.sound, is_video)
        self._loading = False

    def _set_row_visible(self, field: QWidget, visible: bool) -> None:
        self.form.setRowVisible(field, visible)

    def _emit(self, what: str, value) -> None:
        if not self._loading and self.clip is not None:
            self.edited.emit(self.clip.id, what, value)

