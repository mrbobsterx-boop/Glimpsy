"""Окно настроек."""

from __future__ import annotations

import copy

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSlider, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from glimpsy.config import PACE_LABELS, PACES, Settings
from glimpsy.platform import hotkey_format
from glimpsy.platform.base import Monitor, PlatformServices
from glimpsy.recorder.encoder import candidates as encoder_candidates
from glimpsy.recorder.pacing import make_plan
from glimpsy.recorder.webcam import MODE_LABELS as CAMERA_MODES, list_cameras
from glimpsy.ui import theme
from glimpsy.ui.icons import app_logo

PROMPTER_KEYS = [("hotkey_prompter_show", "Показать / спрятать"), ("hotkey_prompter_play", "Пуск / пауза"),
                 ("hotkey_prompter_slower", "Медленнее"), ("hotkey_prompter_faster", "Быстрее"),
                 ("hotkey_prompter_back", "Назад на пару строк"), ("hotkey_prompter_lock", "Закрепить / настроить"),
                 ("hotkey_prompter_top", "В начало текста")]

RESOLUTIONS = [("1920×1080 (Full HD)", 1920, 1080), ("2560×1440 (2K)", 2560, 1440),
               ("3840×2160 (4K)", 3840, 2160), ("1280×720 (HD)", 1280, 720)]


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setProperty("role", "hint")
    return lbl


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, services: PlatformServices, monitors: list[Monitor],
                 status: dict, on_reselect_screen=None, parent=None, ffmpeg: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Glimpsy — настройки")
        self.setMinimumWidth(520)
        self.s = copy.deepcopy(settings)
        self.services = services
        self.ffmpeg = ffmpeg
        pages = [
            ("film", "Ролик", "Длина, темп и эффекты готового ролика", self._video_tab()),
            ("monitor", "Запись", "Что и как записывать", self._record_tab(monitors, on_reselect_screen)),
            ("keyboard", "Горячие клавиши", "Работают в любой программе и раскладке", self._hotkeys_tab()),
            ("eye", "Приватность", "Что никогда не попадает в запись", self._privacy_tab()),
            ("mic", "Звук", "Микрофон, звук компьютера и голосовой режим", self._audio_tab()),
            ("video", "Камера", "Окошко с веб-камеры в углу ролика", self._camera_tab()),
            ("info", "Система", "Сведения о компьютере и записи", self._system_tab(status)),
        ]
        self._camera_page = pages[5][3]
        self.nav = QListWidget()
        self.nav.setObjectName("settingsNav")
        self.nav.setFixedWidth(200)
        self.nav.setIconSize(QSize(18, 18))
        self.stack = QStackedWidget()
        for ic, name, sub, page in pages:
            self.nav.addItem(QListWidgetItem(theme.icon(ic, theme.MUTED, 18), name))
            holder = QWidget()
            hv = QVBoxLayout(holder)
            hv.setContentsMargins(24, 18, 24, 12)
            hv.setSpacing(4)
            hv.addWidget(theme.mark(QLabel(name), "h1"))
            hv.addWidget(theme.mark(QLabel(sub), "muted"))
            hv.addSpacing(10)
            hv.addWidget(page, 1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(holder)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.stack.addWidget(scroll)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.currentRowChanged.connect(lambda i: i == 5 and self._find_cameras(False))
        self.nav.currentRowChanged.connect(lambda i: i == 4 and self._find_mics())
        self.nav.setCurrentRow(0)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        save = buttons.button(QDialogButtonBox.StandardButton.Save)
        save.setText("Сохранить")
        theme.mark(save, "primary")
        theme.mark(buttons.button(QDialogButtonBox.StandardButton.Cancel), "ghost").setText("Отмена")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        self.error = QLabel()
        self.error.setStyleSheet(f"color: {theme.DANGER};")
        self.error.setWordWrap(True)
        side = QFrame()
        side.setObjectName("settingsSide")
        sv = QVBoxLayout(side)
        sv.setContentsMargins(12, 16, 12, 12)
        logo_row = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(app_logo(28))
        logo_row.addWidget(logo)
        logo_row.addWidget(theme.mark(QLabel("Настройки"), "title"))
        logo_row.addStretch(1)
        sv.addLayout(logo_row)
        sv.addSpacing(10)
        sv.addWidget(self.nav, 1)
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 16, 14)
        right.addWidget(self.stack, 1)
        right.addWidget(self.error)
        right.addWidget(buttons)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(side)
        lay.addLayout(right, 1)
        self.resize(820, 600)
        self.setStyleSheet(f"""
            QFrame#settingsSide {{ background: {theme.SURFACE}; border-right: 1px solid {theme.BORDER}; }}
            QListWidget#settingsNav::item {{ padding: 9px 10px; border-radius: 8px; color: {theme.MUTED}; }}
            QListWidget#settingsNav::item:selected {{ background: {theme.ACCENT_SOFT}; color: {theme.TEXT}; }}
            QListWidget#settingsNav::item:hover:!selected {{ background: {theme.HOVER}; }}
        """)
        self._update_plan_hint()

    # ---------- вкладки ----------

    def _video_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.target = QSpinBox(minimum=10, maximum=3600, suffix=" с", value=self.s.target_length_s)
        self.clip_min = QDoubleSpinBox(minimum=0.5, maximum=30, singleStep=0.5, suffix=" с", value=self.s.clip_min_s)
        self.clip_max = QDoubleSpinBox(minimum=0.5, maximum=30, singleStep=0.5, suffix=" с", value=self.s.clip_max_s)
        clip_row = QHBoxLayout()
        clip_row.addWidget(QLabel("от"))
        clip_row.addWidget(self.clip_min)
        clip_row.addWidget(QLabel("до"))
        clip_row.addWidget(self.clip_max)
        self.pace = QComboBox()
        for p in PACES:
            self.pace.addItem(PACE_LABELS[p], p)
        self.pace.setCurrentIndex(PACES.index(self.s.pace))
        self.resolution = QComboBox()
        for label, rw, rh in RESOLUTIONS:
            self.resolution.addItem(label, (rw, rh))
        idx = next((i for i, r in enumerate(RESOLUTIONS) if (r[1], r[2]) == (self.s.output_width, self.s.output_height)), 0)
        self.resolution.setCurrentIndex(idx)
        self.output = QLineEdit(self.s.output_dir)
        browse = QPushButton("Выбрать…")
        browse.clicked.connect(self._browse)
        out_row = QHBoxLayout()
        out_row.addWidget(self.output)
        out_row.addWidget(browse)
        self.continuous = QCheckBox("Непрерывные фрагменты — без ускорения и склеек внутри")
        self.continuous.setChecked(self.s.continuous)
        self.continuous_s = QDoubleSpinBox(minimum=3, maximum=30, singleStep=1, decimals=0, suffix=" с",
                                           value=self.s.continuous_s)
        self.plan_hint = _hint("")
        for widget in (self.target, self.clip_min, self.clip_max, self.continuous_s):
            widget.valueChanged.connect(self._update_plan_hint)
        self.pace.currentIndexChanged.connect(self._update_plan_hint)

        def continuous_changed(on: bool) -> None:
            for wd in (self.clip_min, self.clip_max, self.pace):
                wd.setEnabled(not on)
            self.continuous_s.setEnabled(on)
            self._update_plan_hint()

        self.continuous.toggled.connect(continuous_changed)

        f.addRow("Длина ролика", self.target)
        f.addRow("", self.continuous)
        f.addRow("Длина куска", self.continuous_s)
        f.addRow("", _hint("Каждый кусок — это ровно столько секунд подряд, с обычной скоростью: видео "
                           "не дёргается и не рвётся. Когда вы говорите, речь, как и раньше, идёт целиком."))
        f.addRow("Длина фрагмента", clip_row)
        f.addRow("Темп", self.pace)
        f.addRow("", _hint("Спокойный — фрагменты длиннее, без ускорения. Динамичный — короткие "
                           "фрагменты и ускорение ×1.6."))
        continuous_changed(self.s.continuous)
        f.addRow("", self.plan_hint)
        f.addRow("Разрешение ролика", self.resolution)
        self.fx_zoom = QCheckBox("Плавно приближать к кликам и месту работы")
        self.fx_zoom.setChecked(self.s.fx_zoom)
        self.fx_strength = QDoubleSpinBox(minimum=1.2, maximum=3.0, singleStep=0.1, decimals=1, suffix=" ×")
        self.fx_strength.setValue(self.s.fx_zoom_strength)
        self.fx_strength.setEnabled(self.s.fx_zoom)
        self.fx_zoom.toggled.connect(self.fx_strength.setEnabled)
        self.fx_clicks = QCheckBox("Подсвечивать клики кругом")
        self.fx_clicks.setChecked(self.s.fx_clicks)
        f.addRow("Эффекты", self.fx_zoom)
        f.addRow("   сила приближения", self.fx_strength)
        f.addRow("", self.fx_clicks)
        self.smooth_cursor = QCheckBox("Плавный курсор")
        self.smooth_cursor.setChecked(self.s.smooth_cursor)
        self.smooth_cursor.setToolTip("Экран снимается без курсора, а Glimpsy рисует свой — он плывёт, "
                                      "а не дёргается. Вид и размер меняются в редакторе.")
        f.addRow("", self.smooth_cursor)
        f.addRow("", _hint("Как в Screen Studio: камера сама наезжает туда, где вы кликаете и работаете, "
                           "а клик отмечается расходящимся кругом. В редакторе эффекты можно выключить "
                           "у любого фрагмента."))
        f.addRow("Папка для роликов", out_row)
        f.addRow("", _hint("Можно выбрать папку Google Drive / Яндекс Диска / Dropbox — "
                           "ролики будут сами загружаться в облако. Черновики туда не попадают."))
        return w

    def _record_tab(self, monitors: list[Monitor], on_reselect) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.fps = QSpinBox(minimum=5, maximum=60, suffix=" кадр/с", value=self.s.fps)
        self.buffer = QSpinBox(minimum=10, maximum=300, suffix=" с", value=self.s.buffer_s)
        self.idle = QSpinBox(minimum=10, maximum=3600, suffix=" с", value=self.s.idle_pause_s)
        self.max_h = QComboBox()
        for h in (720, 1080, 1440, 2160, 4320):
            self.max_h.addItem(f"{h}p" if h < 4320 else "Без ограничений", h)
        self.max_h.setCurrentIndex(max(0, self.max_h.findData(self.s.record_max_height)))

        self.monitor_mode = QComboBox()
        self.monitor_mode.addItem("Автоматически — монитор под курсором", "auto")
        for m in monitors:
            self.monitor_mode.addItem(f"Всегда {m.label}", f"manual:{m.index}")
        if self.s.monitor_mode == "manual":
            i = self.monitor_mode.findData(f"manual:{self.s.manual_monitor}")
            self.monitor_mode.setCurrentIndex(max(0, i))
        if not self.services.cursor.supported:
            self.monitor_mode.setEnabled(False)

        self.encoder = QComboBox()
        self.encoder.addItem("Автоматически (лучший доступный)", "auto")
        for e in encoder_candidates():
            self.encoder.addItem(e.label, e.name)
        self.encoder.setCurrentIndex(max(0, self.encoder.findData(self.s.encoder)))
        self.autostart = QCheckBox("Начинать запись сразу при запуске программы")
        self.autostart.setChecked(self.s.autostart_recording)
        from glimpsy import autostart

        self.login = QCheckBox("Запускать Glimpsy вместе с компьютером")
        self.login.setChecked(autostart.is_enabled())   # правда — в системе, а не в файле настроек

        f.addRow("Частота кадров", self.fps)
        f.addRow("Кольцевой буфер", self.buffer)
        f.addRow("", _hint("Сколько последних секунд экрана хранится на диске, чтобы было из чего вырезать момент."))
        f.addRow("Автопауза после", self.idle)
        f.addRow("", _hint("Если столько секунд нет активности, запись встаёт на паузу и сама продолжится."))
        f.addRow("Макс. высота записи", self.max_h)
        f.addRow("Какой монитор писать", self.monitor_mode)
        if self.services.display_server == "wayland" and on_reselect:
            btn = QPushButton("Выбрать экран заново")
            btn.clicked.connect(on_reselect)
            f.addRow("", btn)
            f.addRow("", _hint("На Wayland монитор выбирается в системном окне «Поделиться экраном»."))
        f.addRow("Видеокодек", self.encoder)
        f.addRow("", self.autostart)
        f.addRow("", self.login)
        return w

    def _hotkeys_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.hk_important = QLineEdit(self.s.hotkey_important)
        self.hk_pause = QLineEdit(self.s.hotkey_pause)
        self.hk_finish = QLineEdit(self.s.hotkey_finish)
        self.imp_before = QSpinBox(minimum=1, maximum=60, suffix=" с", value=self.s.important_before_s)
        self.imp_after = QSpinBox(minimum=0, maximum=30, suffix=" с", value=self.s.important_after_s)
        f.addRow("Важный момент", self.hk_important)
        f.addRow("   сохранить до нажатия", self.imp_before)
        f.addRow("   и после нажатия", self.imp_after)
        f.addRow("Пауза / продолжить", self.hk_pause)
        f.addRow("Собрать ролик", self.hk_finish)
        head = QLabel("Суфлёр")
        head.setProperty("role", "section")
        f.addRow(head)
        self.hk_prompter: dict[str, QLineEdit] = {}
        for key, label in PROMPTER_KEYS:
            self.hk_prompter[key] = QLineEdit(getattr(self.s, key))
            f.addRow(label, self.hk_prompter[key])
        f.addRow("", _hint("Формат: Ctrl+Alt+1, Ctrl+Shift+F9, Cmd+Alt+P. Модификаторы: Ctrl, Alt (Option), "
                           "Shift, Cmd (Win)."))
        if not self.services.hotkeys.supported:
            f.addRow("", _hint("⚠ На этой системе глобальные горячие клавиши недоступны — используйте меню в трее."))
        return w

    def _privacy_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel("Когда в фокусе эти приложения, экран не записывается вообще:"))
        self.blacklist = QPlainTextEdit("\n".join(self.s.blacklist))
        v.addWidget(self.blacklist)
        v.addWidget(_hint("По одному на строку. Проверяется название программы и заголовок окна, "
                          "без учёта регистра: например, «bank» сработает и для вкладки браузера "
                          "с сайтом банка. Всё хранится только на этом компьютере — Glimpsy ничего "
                          "не отправляет в интернет."))
        if not self.services.active_window.supported:
            v.addWidget(_hint("⚠ На этой системе нельзя узнать активное окно, поэтому список не работает. "
                              "Ставьте паузу вручную."))
        return w

    def _audio_tab(self) -> QWidget:
        from glimpsy.recorder.audio import system_audio_supported

        w = QWidget()
        f = QFormLayout(w)
        self.a_mic = QCheckBox("Записывать микрофон")
        self.a_mic.setChecked(self.s.audio_mic)
        self.a_mic_dev = QComboBox()
        self.a_mic_dev.addItem("Микрофон по умолчанию", "")
        if self.s.audio_mic_device:
            self.a_mic_dev.addItem(self.s.audio_mic_device, self.s.audio_mic_device)
            self.a_mic_dev.setCurrentIndex(1)
        self._mics_listed = False
        self.a_sys = QCheckBox("Записывать звук компьютера (то, что играет в колонках)")
        self.a_sys.setChecked(self.s.audio_system and system_audio_supported())
        self.a_sys.setEnabled(system_audio_supported())
        self.a_voice = QCheckBox("Голосовой режим: пока я говорю — записывать целиком")
        self.a_voice.setChecked(self.s.voice_mode)
        self.a_sens = QSlider(Qt.Orientation.Horizontal)
        self.a_sens.setRange(40, 300)
        self.a_sens.setValue(int(self.s.voice_sensitivity * 100))
        sens_row = QHBoxLayout()
        sens_row.addWidget(theme.mark(QLabel("громкая речь"), "hint"))
        sens_row.addWidget(self.a_sens, 1)
        sens_row.addWidget(theme.mark(QLabel("тихий голос"), "hint"))
        # проверка микрофона: полоска громкости и «Слышу вас»
        self._meter = None
        self._meter_timer = QTimer(self)
        self._meter_timer.setInterval(50)
        self._meter_timer.timeout.connect(self._meter_tick)
        self.a_test = QPushButton("Проверить")
        self.a_test.setCheckable(True)
        self.a_test.toggled.connect(self._mic_test)
        self.a_level = QProgressBar()
        self.a_level.setRange(0, 100)
        self.a_level.setTextVisible(False)
        self.a_level.setFixedHeight(10)
        self.a_heard = theme.mark(QLabel(""), "hint")
        test_row = QHBoxLayout()
        test_row.addWidget(self.a_test)
        test_row.addWidget(self.a_level, 1)
        test_row.addWidget(self.a_heard)
        self.a_mic_dev.currentIndexChanged.connect(lambda _i: self.a_test.isChecked() and self._restart_meter())
        for widget in (self.a_mic_dev, self.a_voice, self.a_sens, self.a_test):
            self.a_mic.toggled.connect(widget.setEnabled)
            widget.setEnabled(self.s.audio_mic)
        self.a_mic.toggled.connect(lambda on: on or self.a_test.setChecked(False))
        f.addRow("", self.a_mic)
        f.addRow("Микрофон", self.a_mic_dev)
        f.addRow("Проверка", test_row)
        f.addRow("", self.a_sys)
        if not system_audio_supported():
            f.addRow("", _hint("На Mac звук колонок без дополнительных программ записать нельзя — "
                               "пишется только микрофон."))
        f.addRow("", self.a_voice)
        f.addRow("Чувствительность", sens_row)
        f.addRow("", _hint("Программа всё время слушает микрофон. Пока вы говорите, фрагмент не режется и "
                           "не ускоряется — речь попадает в ролик целиком и со звуком, сколько бы вы ни "
                           "говорили. Замолчали — дальше как обычно. У остальных фрагментов звук тоже "
                           "сохраняется, но в ролике выключен — включить можно в редакторе. На паузе и в "
                           "приватных приложениях микрофон не пишется. Звук остаётся только на этом "
                           "компьютере."))
        return w

    def _mic_test(self, on: bool) -> None:
        self._stop_meter()
        if on:
            self.a_test.setText("Стоп")
            self._restart_meter()
        else:
            self.a_test.setText("Проверить")
            self.a_level.setValue(0)
            self.a_heard.setText("")

    def _restart_meter(self) -> None:
        from glimpsy.recorder.audio import MicMeter

        self._stop_meter()
        self._meter = MicMeter(self.a_mic_dev.currentData() or "")
        err = self._meter.start()
        if err:
            self._meter = None
            self.a_heard.setText(f"⚠ {err}")
            return
        self.a_heard.setText("Скажите что-нибудь…")
        self._meter_timer.start()

    def _stop_meter(self) -> None:
        self._meter_timer.stop()
        if self._meter is not None:
            self._meter.stop()
            self._meter = None

    def _meter_tick(self) -> None:
        m = self._meter
        if m is None:
            return
        if m.error:
            self._stop_meter()
            self.a_level.setValue(0)
            self.a_heard.setText(f"⚠ Микрофон не отвечает: {m.error[:80]}")
            return
        self.a_level.setValue(int(m.level * 100))
        if m.heard:
            self.a_heard.setText("✅ Слышу вас")

    def done(self, result: int) -> None:
        self._stop_meter()
        super().done(result)

    def _find_mics(self) -> None:
        if self._mics_listed:
            return
        self._mics_listed = True
        from glimpsy.recorder.audio import list_microphones
        current = self.a_mic_dev.currentData()
        self.a_mic_dev.clear()
        self.a_mic_dev.addItem("Микрофон по умолчанию", "")
        for name in list_microphones():
            self.a_mic_dev.addItem(name, name)
        if current and self.a_mic_dev.findData(current) < 0:
            self.a_mic_dev.addItem(f"{current} (не подключён)", current)
        self.a_mic_dev.setCurrentIndex(max(0, self.a_mic_dev.findData(current)))

    def _camera_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.cam_mode = QComboBox()
        for key, label in CAMERA_MODES.items():
            self.cam_mode.addItem(label, key)
        self.cam_mode.setCurrentIndex(max(0, self.cam_mode.findData(self.s.camera_mode)))
        self.cam_device = QComboBox()
        self.cam_device.addItem("Первая найденная", "")
        if self.s.camera_device:
            self.cam_device.addItem(self.s.camera_device, self.s.camera_device)
            self.cam_device.setCurrentIndex(1)
        self._cams_listed = False
        find = QPushButton("Найти камеры")
        find.clicked.connect(lambda: self._find_cameras(True))
        dev_row = QHBoxLayout()
        dev_row.addWidget(self.cam_device, 1)
        dev_row.addWidget(find)
        self.cam_len = QDoubleSpinBox(minimum=2, maximum=10, singleStep=0.5, decimals=1, suffix=" с")
        self.cam_len.setValue(self.s.camera_clip_s)
        self.cam_status = _hint("")
        f.addRow("Фрагменты с веб-камеры", self.cam_mode)
        f.addRow("Камера", dev_row)
        f.addRow("", self.cam_status)
        f.addRow("Длина фрагмента", self.cam_len)
        f.addRow("", _hint("Пока вы работаете, Glimpsy изредка снимает несколько секунд с камеры "
                           "(в это время горит её лампочка) и ставит их в ролик маленьким окошком в углу. "
                           "В редакторе окошко можно подвинуть, увеличить или удалить. "
                           "На паузе, в приватных приложениях и когда вас нет за компьютером камера "
                           "не включается. Видео остаётся только на этом компьютере."))
        return w

    def _find_cameras(self, force: bool) -> None:
        if (self._cams_listed and not force) or not self.ffmpeg:
            return
        self._cams_listed = True
        self.cam_status.setText("Ищу камеры…")
        self.cam_status.repaint()
        cams = list_cameras(self.ffmpeg)
        current = self.cam_device.currentData()
        self.cam_device.clear()
        self.cam_device.addItem("Первая найденная", "")
        for c in cams:
            self.cam_device.addItem(c.name, c.name)
        if current and self.cam_device.findData(current) < 0:
            self.cam_device.addItem(f"{current} (не подключена)", current)
        self.cam_device.setCurrentIndex(max(0, self.cam_device.findData(current)))
        self.cam_status.setText("Найдено: " + ", ".join(c.name for c in cams) if cams
                                else "Камера не найдена — фрагменты с камеры будут пропускаться.")

    def _system_tab(self, status: dict) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        info = [
            f"Система: {self.services.os_name} ({self.services.display_server})",
            f"Захват экрана: {status.get('capture', self.services.capture.name)}",
            f"Кодек: {status.get('encoder') or 'определяется…'}",
        ]
        for line in info:
            v.addWidget(QLabel(line))
        if self.services.limitations:
            v.addWidget(QLabel("<b>Ограничения этой системы:</b>"))
            for lim in self.services.limitations:
                v.addWidget(_hint("• " + lim))
        v.addStretch(1)
        return w

    # ---------- логика ----------

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Папка для готовых роликов", self.output.text())
        if d:
            self.output.setText(d)

    def _update_plan_hint(self) -> None:
        s = copy.deepcopy(self.s)
        s.target_length_s = self.target.value()
        s.clip_min_s, s.clip_max_s = self.clip_min.value(), max(self.clip_min.value(), self.clip_max.value())
        s.pace = self.pace.currentData()
        s.continuous, s.continuous_s = self.continuous.isChecked(), self.continuous_s.value()
        plan = make_plan(s.validate())
        self.plan_hint.setText(f"≈ {plan.clips_needed} фрагментов в ролике, программа хранит до "
                               f"{plan.pool_size} кандидатов и сама решает, как часто сохранять.")

    def _accept(self) -> None:
        s = self.s
        try:
            s.hotkey_important = hotkey_format.normalize(self.hk_important.text())
            s.hotkey_pause = hotkey_format.normalize(self.hk_pause.text())
            s.hotkey_finish = hotkey_format.normalize(self.hk_finish.text())
            for key, edit in self.hk_prompter.items():
                setattr(s, key, hotkey_format.normalize(edit.text()))
        except hotkey_format.HotkeyError as e:
            self.error.setText(str(e))
            return
        combos = [s.hotkey_important, s.hotkey_pause, s.hotkey_finish] + [getattr(s, k) for k in self.hk_prompter]
        if len(set(combos)) < len(combos):
            self.error.setText("Горячие клавиши не должны повторяться.")
            return
        s.target_length_s = self.target.value()
        s.clip_min_s = self.clip_min.value()
        s.clip_max_s = max(self.clip_min.value(), self.clip_max.value())
        s.pace = self.pace.currentData()
        s.continuous, s.continuous_s = self.continuous.isChecked(), self.continuous_s.value()
        s.output_width, s.output_height = self.resolution.currentData()
        s.fx_zoom = self.fx_zoom.isChecked()
        s.fx_zoom_strength = self.fx_strength.value()
        s.fx_clicks = self.fx_clicks.isChecked()
        s.smooth_cursor = self.smooth_cursor.isChecked()
        s.output_dir = self.output.text().strip() or s.output_dir
        s.fps = self.fps.value()
        s.buffer_s = self.buffer.value()
        s.idle_pause_s = self.idle.value()
        s.record_max_height = self.max_h.currentData()
        mode = self.monitor_mode.currentData()
        if mode == "auto":
            s.monitor_mode = "auto"
        else:
            s.monitor_mode, s.manual_monitor = "manual", int(mode.split(":")[1])
        s.encoder = self.encoder.currentData()
        s.autostart_recording = self.autostart.isChecked()
        s.launch_at_login = self.login.isChecked()
        s.important_before_s = self.imp_before.value()
        s.important_after_s = self.imp_after.value()
        s.blacklist = [x.strip() for x in self.blacklist.toPlainText().splitlines() if x.strip()]
        s.audio_mic = self.a_mic.isChecked()
        s.audio_mic_device = self.a_mic_dev.currentData() or ""
        s.audio_system = self.a_sys.isChecked()
        s.voice_mode = self.a_voice.isChecked()
        s.voice_sensitivity = self.a_sens.value() / 100
        s.camera_mode = self.cam_mode.currentData()
        s.camera_device = self.cam_device.currentData() or ""
        s.camera_clip_s = self.cam_len.value()
        s.validate()
        self.accept()

    def result_settings(self) -> Settings:
        return self.s
