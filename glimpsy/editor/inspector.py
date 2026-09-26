"""Панель свойств выбранного фрагмента (справа, как в CapCut)."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDoubleSpinBox, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

from glimpsy.editor.motion import is_follow, needs_cursor
from glimpsy.editor.project import MAX_SPEED, MAX_ZOOM, MIN_SPEED, MIN_ZOOM, Clip

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
        self.title.setProperty("role", "title")
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setProperty("role", "muted")

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
        # --- движение кадра по курсору (этап 3) ---
        self.motion_title = QLabel("Движение кадра")
        self.motion_title.setProperty("role", "section")
        # все режимы — отдельными кнопками, чтобы переключать одним щелчком
        self.motion = QWidget()
        grid = QGridLayout(self.motion)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(4)
        self.motion_group = QButtonGroup(self)
        self.motion_btns: dict[str, QPushButton] = {}
        layout = (("none", "Без движения", 0, 0, 2, "Кадр стоит на месте"),
                  ("autozoom", "Автозум", 0, 2, 2, "Приближение к кликам и туда, где работает курсор (Z)"),
                  ("cursor_zoom", "К стрелке", 1, 0, 2, "Кадр всё время приближен и плавно едет за стрелкой"),
                  ("pushin", "Наезд", 1, 2, 2, "Камера медленно приближается за время фрагмента — "
                                               "для важных моментов"),
                  ("region", "Зум на область", 2, 0, 2, "Камера наезжает на выбранную область, держит её "
                                                        "и в конце отъезжает"),
                  ("scroll_down", "↓", 4, 0, 1, "Прокрутка вниз: камера плавно едет сверху вниз"),
                  ("scroll_up", "↑", 4, 1, 1, "Прокрутка вверх: камера плавно едет снизу вверх"),
                  ("scroll_left", "←", 4, 2, 1, "Прокрутка влево: камера плавно едет справа налево"),
                  ("scroll_right", "→", 4, 3, 1, "Прокрутка вправо: камера плавно едет слева направо"),
                  ("follow_hard", "Жёстко", 6, 0, 1, "Курсор всегда в центре кадра — кадр едет сразу за ним"),
                  ("follow", "Плавно", 6, 1, 1, "Кадр мягко догоняет курсор и стоит, пока курсор в середине"),
                  ("follow_zoom", "Зона + зум", 6, 2, 2, "Крупнее (×1.5), едет и вверх-вниз, большая «мёртвая зона»"))
        for key, text, row, col, span, tip in layout:
            b = QPushButton(text)
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setMinimumWidth(0)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.setProperty("motion", key)
            self.motion_group.addButton(b)
            self.motion_btns[key] = b
            grid.addWidget(b, row, col, 1, span)
        self.region_pick = QPushButton("Выбрать область…")
        self.region_pick.setToolTip("Обведите мышью в окне просмотра, куда приблизить камеру")
        self.region_pick.setMinimumWidth(0)
        self.region_pick.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.region_pick.clicked.connect(lambda: self._emit("region_pick", None))
        grid.addWidget(self.region_pick, 2, 2, 1, 2)
        scroll_label = QLabel("Прокрутка камеры:")
        scroll_label.setProperty("role", "hint")
        grid.addWidget(scroll_label, 3, 0, 1, 4)
        self.follow_label = QLabel("За курсором — для Reels (9:16):")
        self.follow_label.setProperty("role", "hint")
        grid.addWidget(self.follow_label, 5, 0, 1, 4)
        self.motion_group.buttonClicked.connect(lambda b: self._emit("motion", b.property("motion")))
        self.strength = QDoubleSpinBox(minimum=1.2, maximum=4.0, singleStep=0.1, decimals=1, suffix=" ×")
        self.strength.valueChanged.connect(lambda v: self._emit("zoom_strength", v))
        self.click_fx = QCheckBox("Подсвечивать клики")
        self.click_fx.setToolTip("В месте клика расходится круг — зрителю видно, куда вы нажали")
        self.click_fx.toggled.connect(lambda on: self._emit("click_fx", on))
        # --- свой курсор (для записей без системного курсора) — общий для всего ролика ---
        from glimpsy.editor.cursor import MAX_SIZE, MIN_SIZE, STYLES

        self.cursor_title = QLabel("Курсор (во всём ролике)")
        self.cursor_title.setProperty("role", "section")
        self.cursor_show = QCheckBox("Показывать курсор")
        self.cursor_show.toggled.connect(lambda on: self._emit("cursor_show", on))
        self.cursor_styles = QWidget()
        cs = QGridLayout(self.cursor_styles)
        cs.setContentsMargins(0, 0, 0, 0)
        cs.setSpacing(4)
        self.cursor_group = QButtonGroup(self)
        self.cursor_btns: dict[str, QPushButton] = {}
        for i, (key, text) in enumerate(STYLES.items()):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setMinimumWidth(0)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.setProperty("cursor", key)
            b.setIcon(_cursor_icon(key))
            self.cursor_group.addButton(b)
            self.cursor_btns[key] = b
            cs.addWidget(b, i // 2, i % 2)
        self.cursor_group.buttonClicked.connect(lambda b: self._emit("cursor_style", b.property("cursor")))
        self.cursor_size = QDoubleSpinBox(minimum=MIN_SIZE, maximum=MAX_SIZE, singleStep=0.25, decimals=2,
                                          suffix=" ×")
        self.cursor_size.valueChanged.connect(lambda v: self._emit("cursor_size", v))

        self.motion_hint = QLabel()
        self.motion_hint.setWordWrap(True)
        self.motion_hint.setProperty("role", "hint")
        # у переносимых подписей в QFormLayout Qt иногда занижает высоту — задаём её явно
        self.motion_hint.setMinimumHeight(self.motion_hint.fontMetrics().lineSpacing() * 4 + 4)

        self.frame_title = QLabel()
        self.frame_title.setProperty("role", "section")
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
        self.frame_hint.setProperty("role", "hint")

        self.form = QFormLayout()
        self.form.addRow("Скорость", self.speed)
        self.form.addRow(self.presets)
        self.form.addRow(self.sound)
        self.form.addRow("Начало в файле", self.in_s)
        self.form.addRow("Конец в файле", self.out_s)
        self.form.addRow("Показывать фото", self.photo_dur)
        self.form.addRow(self.motion_title)
        self.form.addRow(self.motion)
        self.form.addRow("Сила зума", self.strength)
        self.form.addRow(self.click_fx)
        self.form.addRow(self.motion_hint)
        self.form.addRow(self.cursor_title)
        self.form.addRow(self.cursor_show)
        self.form.addRow(self.cursor_styles)
        self.form.addRow("Размер", self.cursor_size)
        self.form.addRow(self.frame_title)
        self.form.addRow("Масштаб", self.zoom)
        self.form.addRow("Сдвиг влево/вправо", self.pos_x)
        self.form.addRow("Сдвиг вверх/вниз", self.pos_y)
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
        hint = QLabel("Все горячие клавиши — кнопка ⌨ слева от просмотра")
        hint.setProperty("role", "hint")
        lay.addWidget(hint)
        self.set_clip(None)

    FRAME_ROWS = ("frame_title", "zoom", "pos_x", "pos_y", "frame_btns")
    MOTION_ROWS = ("motion_title", "motion", "strength", "click_fx", "motion_hint")
    CURSOR_ROWS = ("cursor_title", "cursor_show", "cursor_styles", "cursor_size")

    def set_clip(self, clip: Clip | None, aspect: str = "16:9", count: int = 1,
                 cursor: tuple[str, float, bool] = ("arrow", 1.0, True)) -> None:
        """Показать свойства фрагмента. count > 1 — выбрано несколько: правки идут во все."""
        self.clip, self.aspect, self.count = clip, aspect, count
        self._loading = True
        all_rows = [self.speed, self.presets, self.sound, self.in_s, self.out_s, self.photo_dur] + \
                   [getattr(self, n) for n in self.FRAME_ROWS + self.MOTION_ROWS + self.CURSOR_ROWS]
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
        has_cursor = is_video and bool(clip.cursor)
        mode = clip.motion_raw(aspect)
        (self.motion_btns.get(mode) or self.motion_btns["none"]).setChecked(True)
        self.motion.setEnabled(is_video)
        for key, b in self.motion_btns.items():
            b.setEnabled(is_video and (has_cursor or not needs_cursor(key)) and (aspect == "9:16" or not is_follow(key)))
        self.strength.setValue(clip.zoom_strength)
        self.click_fx.setChecked(clip.click_fx and bool(clip.clicks))
        self.click_fx.setEnabled(bool(clip.clicks) or count > 1)
        self.click_fx.setText(f"Подсвечивать клики ({len(clip.clicks)})" if clip.clicks or count > 1
                              else "Подсвечивать клики (в этом фрагменте кликов нет)")
        self.strength.setEnabled(has_cursor and mode in ("autozoom", "cursor_zoom"))
        if mode == "region":
            self.motion_hint.setText("Камера наезжает на область и в конце отъезжает. «Выбрать область…» — "
                                     "обвести мышью в просмотре другую.")
        elif mode.startswith("scroll_"):
            self.motion_hint.setText("Камера приближена и плавно проезжает по кадру за время фрагмента — "
                                     "хорошо для длинных страниц и макетов.")
        elif not has_cursor:
            self.motion_hint.setText("Зум на область, наезд и прокрутка работают с любым видео. Автозум, «к стрелке» "
                                     "и «за курсором» — только для записей Glimpsy (там сохранён курсор).")
        elif aspect != "9:16":
            self.motion_hint.setText("Кадр плавно приближается к кликам и туда, где работает курсор. "
                                     "«За курсором» — в формате 9:16 (переключатель вверху).")
        elif is_follow(mode):
            self.motion_hint.setText("Узкий кадр 9:16 едет за курсором — важное не уходит за край. "
                                     "Ctrl+A — выбрать все фрагменты и включить сразу для всех.")
        else:
            self.motion_hint.setText("Кадр плавно приближается к кликам и туда, где работает курсор. "
                                     "Ctrl+A — включить сразу для всех фрагментов.")
        for n in self.MOTION_ROWS:
            self._set_row_visible(getattr(self, n), is_video)
        style, size, show = cursor
        self.cursor_show.setChecked(show)
        (self.cursor_btns.get(style) or self.cursor_btns["arrow"]).setChecked(True)
        self.cursor_size.setValue(size)
        for w in (self.cursor_styles, self.cursor_size):
            w.setEnabled(show)
        for n in self.CURSOR_ROWS:
            self._set_row_visible(getattr(self, n), is_video and clip.own_cursor)
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


def _cursor_icon(style: str):
    """Маленькая картинка курсора для кнопки выбора вида."""
    from PySide6.QtGui import QIcon, QPixmap

    from glimpsy.editor.cursor import image

    return QIcon(QPixmap.fromImage(image(style, 18)))
