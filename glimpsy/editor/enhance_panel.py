"""Раскрывающаяся слева панель «Улучшить»: звук (чистый голос, громкость) и картинка.

Всё применяется ко всему ролику при сохранении. Звук можно сразу послушать «как было /
как будет» — кусок в месте курсора.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSlider, QToolButton,
    QVBoxLayout, QWidget,
)

from glimpsy.editor import look, sound
from glimpsy.ui import theme


def _hint(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setWordWrap(True)
    lb.setProperty("role", "hint")
    return lb


def _section(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setStyleSheet("font-weight: 600; margin-top: 10px;")
    return lb


class EnhancePanel(QFrame):
    close_requested = Signal()
    sound_changed = Signal(str, object)     # что, значение
    look_changed = Signal(dict)             # картинка: {ключ: значение} (несколько сразу — для готовых стилей)
    compare = Signal(bool)                  # показать в просмотре «как снято» (пока кнопка нажата)
    listen = Signal(str)                    # "before" / "after" / "stop"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("enhancePanel")
        self.setStyleSheet("QFrame#enhancePanel { background: #14171D; border: 1px solid #262B36;"
                           " border-radius: 12px; }")
        self._loading = False

        title = QLabel("Улучшить")
        title.setProperty("role", "title")
        close = QToolButton()
        close.setIcon(theme.icon("chevron-left", theme.MUTED, 18))
        close.setToolTip("Свернуть")
        close.clicked.connect(self.close_requested)
        head = QHBoxLayout()
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(close)

        # --- чистый голос ---
        self.denoise = QSlider(Qt.Orientation.Horizontal, minimum=0, maximum=100, singleStep=5, pageStep=10)
        self.denoise_value = QLabel()
        self.denoise_value.setMinimumWidth(70)
        self.denoise.valueChanged.connect(self._on_denoise)
        self.denoise.sliderReleased.connect(lambda: self._emit("denoise", self.denoise.value()))
        drow = QHBoxLayout()
        drow.addWidget(self.denoise, 1)
        drow.addWidget(self.denoise_value)
        self.model_status = _hint("")

        # --- громкость ---
        self.level = QCheckBox("Одинаковая громкость всего ролика")
        self.level.toggled.connect(lambda on: (self._emit("level", on), self._sync_enabled()))
        self.lufs = QSlider(Qt.Orientation.Horizontal, minimum=-24, maximum=-8, singleStep=1, pageStep=2)
        self.lufs_value = QLabel()
        self.lufs.valueChanged.connect(lambda v: self.lufs_value.setText(sound.lufs_label(v)))
        self.lufs.sliderReleased.connect(lambda: self._emit("lufs", float(self.lufs.value())))
        self.lufs.valueChanged.connect(lambda v: None if self.lufs.isSliderDown() else self._emit("lufs", float(v)))
        self.even = QCheckBox("Подтягивать тихие места")
        self.even.setToolTip("Если в одном месте вы говорили тише, а в другом громче — тихое станет громче")
        self.even.toggled.connect(lambda on: self._emit("even", on))

        # --- послушать ---
        self.b_before = theme.mark(QPushButton(theme.icon("play", size=16), " Как было"), "ghost")
        self.b_after = theme.mark(QPushButton(theme.icon("play", theme.ACCENT_HOVER, 16), " Как будет"), "ghost")
        self.b_stop = theme.mark(QPushButton(theme.icon("square", size=14), ""), "ghost")
        self.b_stop.setToolTip("Остановить")
        self.b_before.clicked.connect(lambda: self.listen.emit("before"))
        self.b_after.clicked.connect(lambda: self.listen.emit("after"))
        self.b_stop.clicked.connect(lambda: self.listen.emit("stop"))
        lrow = QHBoxLayout()
        lrow.addWidget(self.b_before)
        lrow.addWidget(self.b_after)
        lrow.addWidget(self.b_stop)
        lrow.addStretch(1)
        self.listen_status = _hint("Послушать 8 секунд с места курсора.")

        # --- картинка ---
        self.stab = QSlider(Qt.Orientation.Horizontal, minimum=0, maximum=100, singleStep=5, pageStep=10)
        self.stab_value = QLabel()
        self.stab_value.setMinimumWidth(70)
        self.stab.valueChanged.connect(lambda v: self._look_slider("stabilize", v))
        self.stab.sliderReleased.connect(lambda: self._emit_look({"stabilize": self.stab.value()}))
        srow = QHBoxLayout()
        srow.addWidget(self.stab, 1)
        srow.addWidget(self.stab_value)
        self.preset = QComboBox()
        self.preset.addItem("Свой цвет", "")
        for name in look.PRESETS:
            self.preset.addItem(name, name)
        self.preset.activated.connect(self._on_preset)
        self.color: dict[str, QSlider] = {}
        cform = QFormLayout()
        for key, label in (("brightness", "Яркость"), ("contrast", "Контраст"), ("saturation", "Насыщенность"),
                           ("warmth", "Теплота")):
            sl = QSlider(Qt.Orientation.Horizontal, minimum=-50, maximum=50, singleStep=2, pageStep=10)
            sl.valueChanged.connect(lambda v, k=key: self._look_slider(k, v))
            sl.sliderReleased.connect(lambda k=key: self._emit_look({k: self.color[k].value()}))
            sl.setToolTip("Двойной щелчок по названию — вернуть как было")
            self.color[key] = sl
            cform.addRow(label, sl)
        self.b_compare = theme.mark(QPushButton(theme.icon("eye", size=16), " Сравнить (держите)"), "ghost")
        self.b_compare.setToolTip("Пока кнопка нажата, в просмотре — как снято")
        self.b_compare.pressed.connect(lambda: self.compare.emit(True))
        self.b_compare.released.connect(lambda: self.compare.emit(False))
        b_reset = theme.mark(QPushButton("Сбросить цвет"), "ghost")
        b_reset.clicked.connect(lambda: self._emit_look({k: 0 for k in look.COLOR_KEYS}, update=True))
        crow = QHBoxLayout()
        crow.addWidget(self.b_compare)
        crow.addWidget(b_reset)
        crow.addStretch(1)

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(6)
        lay.addWidget(_section("Чистый голос"))
        lay.addWidget(_hint("Сколько шума убирать: вентилятор, гул, шипение микрофона. Убранное "
                            "смешивается с исходным звуком — голос не становится «железным»."))
        lay.addLayout(drow)
        lay.addWidget(self.model_status)
        lay.addWidget(_section("Громкость"))
        lay.addWidget(self.level)
        lay.addWidget(self.lufs)
        lay.addWidget(self.lufs_value)
        lay.addWidget(self.even)
        lay.addWidget(_section("Послушать"))
        lay.addLayout(lrow)
        lay.addWidget(self.listen_status)
        lay.addWidget(_hint("В просмотре звук пока как в записи — обработка делается при сохранении ролика."))
        lay.addWidget(_section("Стабилизация"))
        lay.addWidget(_hint("Сглаживает дрожание, если снимали с рук. Кадр чуть приближается, чтобы по краям "
                            "не было чёрных полос. Видно в готовом ролике; сохранение дольше."))
        lay.addLayout(srow)
        lay.addWidget(_section("Яркость и цвет"))
        lay.addWidget(self.preset)
        lay.addLayout(cform)
        lay.addLayout(crow)
        lay.addWidget(_hint("Цвет сразу виден в просмотре. Тексты и наложения не меняются."))
        lay.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 10, 10)
        outer.setSpacing(6)
        outer.addLayout(head)
        outer.addWidget(scroll, 1)
        self.setFixedWidth(320)
        self.setVisible(False)

    def set_open(self, on: bool) -> None:
        self.setVisible(on)

    def set_project(self, project) -> None:
        s = sound.settings(project)
        self._loading = True
        self.denoise.setValue(s["denoise"])
        self._on_denoise(s["denoise"])
        self.level.setChecked(s["level"])
        self.lufs.setValue(int(round(s["lufs"])))
        self.lufs_value.setText(sound.lufs_label(s["lufs"]))
        self.even.setChecked(s["even"])
        lk = look.settings(project)
        self.stab.setValue(lk["stabilize"])
        self.stab_value.setText(self._stab_text(lk["stabilize"]))
        for k, sl in self.color.items():
            sl.setValue(lk[k])
        self._sync_preset(lk)
        self._loading = False
        self._sync_enabled()

    def set_model_status(self, text: str) -> None:
        self.model_status.setText(text)
        self.model_status.setVisible(bool(text))

    def set_listen_status(self, text: str) -> None:
        self.listen_status.setText(text)

    def _on_denoise(self, v: int) -> None:
        self.denoise_value.setText("выкл." if v == 0 else f"{v}%")
        if not self.denoise.isSliderDown():
            self._emit("denoise", v)

    def _sync_enabled(self) -> None:
        self.lufs.setEnabled(self.level.isChecked())
        self.lufs_value.setEnabled(self.level.isChecked())

    @staticmethod
    def _stab_text(v: int) -> str:
        return "выкл." if v == 0 else f"{v}%"

    def _look_slider(self, key: str, v: int) -> None:
        if key == "stabilize":
            self.stab_value.setText(self._stab_text(v))
            down = self.stab.isSliderDown()
        else:
            down = self.color[key].isSliderDown()
            if not self._loading:
                self._sync_preset({k: sl.value() for k, sl in self.color.items()})
        if not down:
            self._emit_look({key: v})
        elif key != "stabilize" and not self._loading:
            self.look_changed.emit({key: v, "_live": True})   # просмотр меняется сразу, без записи в историю

    def _on_preset(self, _i: int) -> None:
        name = self.preset.currentData()
        if name:
            self._emit_look(dict(look.PRESETS[name]), update=True)

    def _sync_preset(self, values: dict) -> None:
        cur = {k: int(values.get(k, 0)) for k in look.COLOR_KEYS}
        name = next((n for n, p in look.PRESETS.items() if p == cur), "")
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(max(0, self.preset.findData(name)))
        self.preset.blockSignals(False)

    def _emit_look(self, values: dict, update: bool = False) -> None:
        if update:
            self._loading = True
            for k, v in values.items():
                if k in self.color:
                    self.color[k].setValue(v)
            self._loading = False
            self._sync_preset({k: sl.value() for k, sl in self.color.items()})
        if not self._loading:
            self.look_changed.emit(values)

    def _emit(self, what: str, value) -> None:
        if not self._loading:
            self.sound_changed.emit(what, value)
