"""Панель свойств выбранного фрагмента (справа, как в CapCut)."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout,
    QWidget,
)

from worklapse.editor.project import MAX_SPEED, MAX_ZOOM, MIN_SPEED, MIN_ZOOM, Clip

SPEED_PRESETS = (0.5, 1, 2, 4, 10)


class Inspector(QWidget):
    # (id фрагмента, что меняем, новое значение)
    edited = Signal(str, str, object)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumWidth(300)
        self.clip: Clip | None = None
        self.aspect = "16:9"
        self.count = 0
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

        # --- кадр (масштаб и положение внутри ролика) ---
        self.frame_title = QLabel()
        self.frame_title.setStyleSheet("font-weight: 600; margin-top: 6px;")
        self.zoom = QDoubleSpinBox(minimum=MIN_ZOOM * 100, maximum=MAX_ZOOM * 100, singleStep=5, decimals=0,
                                   suffix=" %")
        self.pos_x = QDoubleSpinBox(minimum=-150, maximum=150, singleStep=1, decimals=1, suffix=" %")
        self.pos_y = QDoubleSpinBox(minimum=-150, maximum=150, singleStep=1, decimals=1, suffix=" %")
        self.zoom.valueChanged.connect(lambda v: self._emit("frame_zoom", v / 100))
        self.pos_x.valueChanged.connect(lambda v: self._emit("frame_x", v / 100))
        self.pos_y.valueChanged.connect(lambda v: self._emit("frame_y", v / 100))
        self.frame_btns = QWidget()
        fb = QHBoxLayout(self.frame_btns)
        fb.setContentsMargins(0, 0, 0, 0)
        fb.setSpacing(4)
        for text, what, tip in (("Вписать", "frame_fit", "Кадр целиком, по центру"),
                                ("Заполнить", "frame_fill", "Кадр на весь экран, без полей"),
                                ("Ко всем", "frame_all", "Такое же кадрирование для всех фрагментов")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setMinimumWidth(0)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.clicked.connect(lambda _=False, w=what: self._emit(w, None))
            fb.addWidget(b)
        self.frame_hint = QLabel("В просмотре: тащите кадр мышью, размер — уголки или колёсико. "
                                 "Shift+щелчок по фрагментам на ленте — править несколько сразу.")
        self.frame_hint.setWordWrap(True)
        self.frame_hint.setStyleSheet("color: #8b8d98; font-size: 11px;")

        self.form = QFormLayout()
        self.form.addRow("Скорость:", self.speed)
        self.form.addRow(self.presets)
        self.form.addRow(self.sound)
        self.form.addRow("Начало в файле:", self.in_s)
        self.form.addRow("Конец в файле:", self.out_s)
        self.form.addRow("Показывать фото:", self.photo_dur)
        self.form.addRow(self.frame_title)
        self.form.addRow("Масштаб:", self.zoom)
        self.form.addRow("Сдвиг влево/вправо:", self.pos_x)
        self.form.addRow("Сдвиг вверх/вниз:", self.pos_y)
        self.form.addRow(self.frame_btns)

        self.delete_btn = QPushButton("Удалить фрагмент")
        self.delete_btn.clicked.connect(lambda: self._emit("delete", None))

        lay = QVBoxLayout(self)
        lay.addWidget(self.title)
        lay.addWidget(self.info)
        lay.addSpacing(8)
        lay.addLayout(self.form)
        lay.addWidget(self.frame_hint)
        lay.addSpacing(8)
        lay.addWidget(self.delete_btn)
        lay.addStretch(1)
        hint = QLabel("Ctrl+Z — отменить · Ctrl+B — разрезать\nDelete — удалить · Пробел — пуск/пауза\n"
                      "Ctrl+V — вставить файл или картинку")
        hint.setStyleSheet("color: #8b8d98; font-size: 11px;")
        lay.addWidget(hint)
        self.set_clip(None)

    FRAME_ROWS = ("frame_title", "zoom", "pos_x", "pos_y", "frame_btns")

    def set_clip(self, clip: Clip | None, aspect: str = "16:9", count: int = 1) -> None:
        """Показать свойства фрагмента. count > 1 — выбрано несколько: правки идут во все."""
        self.clip, self.aspect, self.count = clip, aspect, count
        self._loading = True
        all_rows = [self.speed, self.presets, self.sound, self.in_s, self.out_s, self.photo_dur] + \
                   [getattr(self, n) for n in self.FRAME_ROWS]
        if clip is None:
            self.title.setText("Выберите фрагмент на ленте")
            self.info.setText("Щёлкните по фрагменту внизу, чтобы изменить скорость, звук, длину и кадр.")
            for w in all_rows:
                self._set_row_visible(w, False)
            self.delete_btn.hide()
            self.frame_hint.hide()
            self._loading = False
            return
        self.delete_btn.show()
        self.frame_hint.show()
        is_video = clip.kind == "video"
        if count > 1:
            self.title.setText(f"Выбрано фрагментов: {count}")
            self.info.setText("Изменения применяются ко всем выбранным. Значения ниже — у последнего выбранного.")
            self.delete_btn.setText(f"Удалить выбранные ({count})")
        else:
            kind = "Фото" if clip.kind == "image" else "Видео"
            self.title.setText(f"{kind}: {clip.label}" if clip.label else kind)
            extra = [f"В ролике: {clip.duration:.2f} с"]
            if clip.width:
                extra.append(f"Размер: {clip.width}×{clip.height}")
            if clip.priority:
                extra.append("⭐ Важный момент")
            self.info.setText("\n".join(extra))
            self.delete_btn.setText("Удалить фрагмент")
        self.speed.setValue(clip.speed)
        self.sound.setChecked(not clip.muted and clip.has_audio)
        self.sound.setEnabled(is_video and (clip.has_audio or count > 1))
        self.sound.setText("Звук фрагмента" if clip.has_audio or count > 1 else "Звук (в записи экрана его нет)")
        for w in (self.in_s, self.out_s):
            w.setMaximum(clip.src_duration)
        self.in_s.setValue(clip.in_s)
        self.out_s.setValue(clip.out_s)
        self.photo_dur.setValue(clip.out_s - clip.in_s)
        z, x, y = clip.frame_for(aspect)
        self.frame_title.setText(f"Кадр в формате {aspect}")
        self.zoom.setValue(z * 100)
        self.pos_x.setValue(x * 100)
        self.pos_y.setValue(y * 100)
        single = count == 1
        self._set_row_visible(self.in_s, is_video and single)
        self._set_row_visible(self.out_s, is_video and single)
        self._set_row_visible(self.photo_dur, not is_video)
        self._set_row_visible(self.speed, is_video)
        self._set_row_visible(self.presets, is_video)
        self._set_row_visible(self.sound, is_video)
        for n in self.FRAME_ROWS:
            self._set_row_visible(getattr(self, n), True)
        self._loading = False

    def _set_row_visible(self, field: QWidget, visible: bool) -> None:
        self.form.setRowVisible(field, visible)

    def _emit(self, what: str, value) -> None:
        if not self._loading and self.clip is not None:
            self.edited.emit(self.clip.id, what, value)

