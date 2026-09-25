"""Окно настроек."""

from __future__ import annotations

import copy

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QTabWidget,
    QVBoxLayout, QWidget,
)

from glimpsy.config import PACE_LABELS, PACES, Settings
from glimpsy.platform import hotkey_format
from glimpsy.platform.base import Monitor, PlatformServices
from glimpsy.recorder.encoder import candidates as encoder_candidates
from glimpsy.recorder.pacing import make_plan
from glimpsy.recorder.webcam import MODE_LABELS as CAMERA_MODES, list_cameras

RESOLUTIONS = [("1920×1080 (Full HD)", 1920, 1080), ("2560×1440 (2K)", 2560, 1440),
               ("3840×2160 (4K)", 3840, 2160), ("1280×720 (HD)", 1280, 720)]


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet("color: #6b6f7a; font-size: 11px;")
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
        tabs = QTabWidget()
        tabs.addTab(self._video_tab(), "Ролик")
        tabs.addTab(self._record_tab(monitors, on_reselect_screen), "Запись")
        tabs.addTab(self._hotkeys_tab(), "Горячие клавиши")
        tabs.addTab(self._privacy_tab(), "Приватность")
        self._camera_page = self._camera_tab()
        tabs.addTab(self._camera_page, "Камера")
        tabs.currentChanged.connect(lambda _i: tabs.currentWidget() is self._camera_page and self._find_cameras(False))
        tabs.addTab(self._system_tab(status), "Система")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        self.error = QLabel()
        self.error.setStyleSheet("color: #E5484D;")
        self.error.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(self.error)
        lay.addWidget(buttons)
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
        self.plan_hint = _hint("")
        for widget in (self.target, self.clip_min, self.clip_max):
            widget.valueChanged.connect(self._update_plan_hint)
        self.pace.currentIndexChanged.connect(self._update_plan_hint)

        f.addRow("Длина ролика:", self.target)
        f.addRow("Длина фрагмента:", clip_row)
        f.addRow("Темп:", self.pace)
        f.addRow("", _hint("Спокойный — фрагменты длиннее, без ускорения. Динамичный — короткие "
                           "фрагменты и ускорение ×1.6."))
        f.addRow("", self.plan_hint)
        f.addRow("Разрешение ролика:", self.resolution)
        self.fx_zoom = QCheckBox("Плавно приближать к кликам и месту работы")
        self.fx_zoom.setChecked(self.s.fx_zoom)
        self.fx_strength = QDoubleSpinBox(minimum=1.2, maximum=3.0, singleStep=0.1, decimals=1, suffix=" ×")
        self.fx_strength.setValue(self.s.fx_zoom_strength)
        self.fx_strength.setEnabled(self.s.fx_zoom)
        self.fx_zoom.toggled.connect(self.fx_strength.setEnabled)
        self.fx_clicks = QCheckBox("Подсвечивать клики кругом")
        self.fx_clicks.setChecked(self.s.fx_clicks)
        f.addRow("Эффекты:", self.fx_zoom)
        f.addRow("   сила приближения:", self.fx_strength)
        f.addRow("", self.fx_clicks)
        f.addRow("", _hint("Как в Screen Studio: камера сама наезжает туда, где вы кликаете и работаете, "
                           "а клик отмечается расходящимся кругом. В редакторе эффекты можно выключить "
                           "у любого фрагмента."))
        f.addRow("Папка для роликов:", out_row)
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

        f.addRow("Частота кадров:", self.fps)
        f.addRow("Кольцевой буфер:", self.buffer)
        f.addRow("", _hint("Сколько последних секунд экрана хранится на диске, чтобы было из чего вырезать момент."))
        f.addRow("Автопауза после:", self.idle)
        f.addRow("", _hint("Если столько секунд нет активности, запись встаёт на паузу и сама продолжится."))
        f.addRow("Макс. высота записи:", self.max_h)
        f.addRow("Какой монитор писать:", self.monitor_mode)
        if self.services.display_server == "wayland" and on_reselect:
            btn = QPushButton("Выбрать экран заново")
            btn.clicked.connect(on_reselect)
            f.addRow("", btn)
            f.addRow("", _hint("На Wayland монитор выбирается в системном окне «Поделиться экраном»."))
        f.addRow("Видеокодек:", self.encoder)
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
        f.addRow("Важный момент:", self.hk_important)
        f.addRow("   сохранить до нажатия:", self.imp_before)
        f.addRow("   и после нажатия:", self.imp_after)
        f.addRow("Пауза / продолжить:", self.hk_pause)
        f.addRow("Собрать ролик:", self.hk_finish)
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
        f.addRow("Фрагменты с веб-камеры:", self.cam_mode)
        f.addRow("Камера:", dev_row)
        f.addRow("", self.cam_status)
        f.addRow("Длина фрагмента:", self.cam_len)
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
        plan = make_plan(s.validate())
        self.plan_hint.setText(f"≈ {plan.clips_needed} фрагментов в ролике, программа хранит до "
                               f"{plan.pool_size} кандидатов и сама решает, как часто сохранять.")

    def _accept(self) -> None:
        s = self.s
        try:
            s.hotkey_important = hotkey_format.normalize(self.hk_important.text())
            s.hotkey_pause = hotkey_format.normalize(self.hk_pause.text())
            s.hotkey_finish = hotkey_format.normalize(self.hk_finish.text())
        except hotkey_format.HotkeyError as e:
            self.error.setText(str(e))
            return
        if len({s.hotkey_important, s.hotkey_pause, s.hotkey_finish}) < 3:
            self.error.setText("Горячие клавиши не должны повторяться.")
            return
        s.target_length_s = self.target.value()
        s.clip_min_s = self.clip_min.value()
        s.clip_max_s = max(self.clip_min.value(), self.clip_max.value())
        s.pace = self.pace.currentData()
        s.output_width, s.output_height = self.resolution.currentData()
        s.fx_zoom = self.fx_zoom.isChecked()
        s.fx_zoom_strength = self.fx_strength.value()
        s.fx_clicks = self.fx_clicks.isChecked()
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
        s.camera_mode = self.cam_mode.currentData()
        s.camera_device = self.cam_device.currentData() or ""
        s.camera_clip_s = self.cam_len.value()
        s.validate()
        self.accept()

    def result_settings(self) -> Settings:
        return self.s
