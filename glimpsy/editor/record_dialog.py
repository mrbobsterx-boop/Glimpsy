"""Кнопка «Запись» в редакторе: записать голос, камеру или то и другое прямо в проект.

Выбираете, что писать (и чем: какой микрофон, какая камера), нажимаете «Начать» —
отсчёт 3-2-1, запись, «Стоп». Голос ложится на дорожку «Голос», видео с камеры — на
дорожку «Камера», с места курсора на ленте. Дальше это обычные элементы: их можно
двигать, обрезать, выключить звук, удалить или отменить (Ctrl+Z).

Окно записи не мешает редактору: пока идёт запись, видео можно запускать, листать,
смотреть — и рассказывать, что происходит. По умолчанию видео в редакторе запускается
само вместе с записью (и без звука — чтобы звук ролика не попал в микрофон).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout,
)

from glimpsy.recorder.audio import RATE, MicMeter, list_microphones, write_wav
from glimpsy.recorder.webcam import CamClip, CameraRecorder, list_cameras
from glimpsy.ui import theme

log = logging.getLogger(__name__)

COUNTDOWN_S = 3


@dataclass
class Recording:
    voice: Path | None = None       # записанный голос (wav в папке проекта)
    voice_s: float = 0.0
    camera: CamClip | None = None   # видео с камеры (файл — в папке media проекта)
    camera_path: Path | None = None
    camera_offset: float = 0.0      # на сколько позже голоса началось видео


class RecordDialog(QDialog):
    started = Signal()                 # запись пошла (после отсчёта)
    recorded = Signal(object)          # Recording — запись закончена
    cancelled = Signal()               # закрыли, ничего не сохранив

    def __init__(self, ffmpeg: str, media_dir: Path, parent=None, mic_opener=None,
                 camera_input: list[str] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Запись")
        self.setMinimumWidth(460)
        # не блокирует редактор: плавает поверх, а редактором можно пользоваться
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.ffmpeg = ffmpeg
        self.media_dir = media_dir
        self._mic_opener = mic_opener            # для проверок: «микрофон» без устройства
        self._camera_input = camera_input        # для проверок: вход FFmpeg вместо камеры
        self.result_rec: Recording | None = None
        self._meter: MicMeter | None = None
        self._cam: CameraRecorder | None = None
        self._state = "idle"                     # idle / countdown / recording
        self._t0 = 0.0

        title = theme.mark(QLabel("Запись"), "h1")
        sub = theme.mark(QLabel("Голос ляжет на дорожку «Голос», камера — на «Камеру», с места курсора на ленте."),
                         "muted")
        sub.setWordWrap(True)
        self.voice = QCheckBox("Голос (микрофон)")
        self.voice.setChecked(True)
        self.mic = QComboBox()
        self.mic.addItem("Микрофон по умолчанию", "")
        for name in ([] if mic_opener else list_microphones()):
            self.mic.addItem(name, name)
        self.level = QProgressBar()
        self.level.setRange(0, 100)
        self.level.setTextVisible(False)
        self.level.setFixedHeight(8)
        self.camera = QCheckBox("Камера")
        self.cam = QComboBox()
        cams = [] if camera_input else list_cameras(ffmpeg)
        if camera_input:
            self.cam.addItem("Тестовая камера", "")
        for c in cams:
            self.cam.addItem(c.name, c.device)
        self._has_camera = self.cam.count() > 0
        if not self._has_camera:
            self.cam.addItem("Камера не найдена", "")
            self.camera.setEnabled(False)
        for box, combo in ((self.voice, self.mic), (self.camera, self.cam)):
            combo.setEnabled(box.isChecked())
            box.toggled.connect(combo.setEnabled)
            box.toggled.connect(self._update_start)
        self.mic.currentIndexChanged.connect(lambda _i: self._preview_mic())
        self.voice.toggled.connect(lambda _on: self._preview_mic())

        store = QSettings("Glimpsy", "editor")
        self.play_along = QCheckBox("Запустить видео в редакторе вместе с записью")
        self.play_along.setChecked(store.value("record/play_along", True, type=bool))
        self.play_along.setToolTip("Голос сразу совпадёт с тем, что вы видите. Видео можно и остановить, "
                                   "и перемотать — редактор во время записи работает как обычно.")
        self.mute = QCheckBox("Без звука ролика, пока идёт запись")
        self.mute.setChecked(store.value("record/mute", True, type=bool))
        self.mute.setToolTip("Чтобы звук ролика из колонок не попал в микрофон")

        form = QFormLayout()
        form.addRow(self.voice)
        form.addRow("Микрофон", self.mic)
        form.addRow("Громкость", self.level)
        form.addRow(self.camera)
        form.addRow("Камера", self.cam)
        form.addRow(self.play_along)
        form.addRow(self.mute)

        self.status = theme.mark(QLabel(""), "title")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.start_btn = theme.mark(QPushButton(theme.icon("circle", "#FFFFFF", 14), "  Начать запись"), "primary")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._toggle)
        cancel = theme.mark(QPushButton("Отмена"), "ghost")
        cancel.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(self.start_btn)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.addWidget(title)
        lay.addWidget(sub)
        lay.addSpacing(6)
        lay.addLayout(form)
        lay.addWidget(self.status)
        lay.addLayout(row)

        self._timer = QTimer(self, interval=50)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._update_start()
        self._preview_mic()

    # ---------- микрофон до записи: полоска громкости ----------

    def _new_meter(self, keep: bool) -> MicMeter:
        return MicMeter(self.mic.currentData() or "", opener=self._mic_opener, keep=keep)

    def _preview_mic(self) -> None:
        if self._state != "idle":
            return
        self._stop_meter()
        if not self.voice.isChecked():
            self.level.setValue(0)
            return
        self._meter = self._new_meter(False)
        err = self._meter.start()
        if err:
            self._meter = None
            self.status.setText(f"⚠ {err}")

    def _stop_meter(self) -> None:
        if self._meter is not None:
            self._meter.stop()
            self._meter = None

    # ---------- запись ----------

    def _update_start(self) -> None:
        self.start_btn.setEnabled(self._state != "idle" or self.voice.isChecked() or self.camera.isChecked())

    def _toggle(self) -> None:
        if self._state == "idle":
            store = QSettings("Glimpsy", "editor")
            store.setValue("record/play_along", self.play_along.isChecked())
            store.setValue("record/mute", self.mute.isChecked())
            self._state = "countdown"
            self._t0 = time.monotonic() + COUNTDOWN_S
            for w in (self.voice, self.mic, self.camera, self.cam, self.play_along, self.mute):
                w.setEnabled(False)
            self.start_btn.setText("  Стоп")
            self.start_btn.setIcon(theme.icon("square", "#FFFFFF", 14))
        elif self._state == "countdown":
            self._reset()
        else:
            self._finish()

    def _begin(self) -> None:
        self._stop_meter()
        stamp = time.strftime("%Y%m%d_%H%M%S")
        if self.voice.isChecked():
            self._meter = self._new_meter(True)
            err = self._meter.start()
            if err:
                self._meter = None
                self.status.setText(f"⚠ Микрофон: {err}")
                self._reset()
                return
        if self.camera.isChecked():
            self._cam = CameraRecorder(self.ffmpeg, self.cam.currentData() or "", self._camera_input)
            err = self._cam.start(self.media_dir / f"camera_{stamp}.mp4")
            if err:
                self._cam = None
                self._stop_meter()
                self.status.setText(f"⚠ {err}")
                self._reset()
                return
        self._state = "recording"
        self._t0 = time.monotonic()
        self._stamp = stamp
        self.started.emit()

    def _finish(self) -> None:
        rec = Recording()
        meter, cam = self._meter, self._cam
        self._meter = self._cam = None
        if meter is not None:
            meter.stop()
            samples = meter.samples()
            if len(samples) > RATE * 0.3:
                path = self.media_dir / f"voice_{self._stamp}.wav"
                self.media_dir.mkdir(parents=True, exist_ok=True)
                write_wav(path, samples)
                rec.voice, rec.voice_s = path, len(samples) / RATE
        if cam is not None:
            clip = cam.stop()
            if clip is not None:
                rec.camera, rec.camera_path = clip, cam.out
                # остановили одновременно — значит, камера (она включается дольше) начала позже на разницу длин
                if rec.voice is not None:
                    rec.camera_offset = max(0.0, rec.voice_s - clip.duration)
        self._state = "idle"
        if rec.voice is None and rec.camera is None:
            self.status.setText("Ничего не записалось — попробуйте ещё раз")
            self._reset()
            return
        self.result_rec = rec
        self._timer.stop()
        self.hide()
        self.recorded.emit(rec)
        self.deleteLater()

    def _reset(self) -> None:
        self._state = "idle"
        self.start_btn.setText("  Начать запись")
        self.start_btn.setIcon(theme.icon("circle", "#FFFFFF", 14))
        self.voice.setEnabled(True)
        self.camera.setEnabled(self._has_camera)
        self.play_along.setEnabled(True)
        self.mute.setEnabled(True)
        self.mic.setEnabled(self.voice.isChecked())
        self.cam.setEnabled(self.camera.isChecked())
        self._preview_mic()

    def _tick(self) -> None:
        now = time.monotonic()
        if self._state == "countdown":
            left = self._t0 - now
            if left <= 0:
                self._begin()
            else:
                self.status.setText(f"Запись через {int(left) + 1}…")
        elif self._state == "recording":
            s = now - self._t0
            self.status.setText(f"● Идёт запись  {int(s // 60)}:{int(s % 60):02d}")
            if self._cam is not None and not self._cam.alive:
                self.status.setText("⚠ Камера отключилась — запись остановлена")
                self._finish()
                return
        elif self._state == "idle" and not self.status.text().startswith("⚠"):
            self.status.setText("")
        if self._meter is not None:
            self.level.setValue(int(self._meter.level * 100))

    def reject(self) -> None:
        # закрыли или «Отмена» (в том числе во время записи) — ничего не сохраняем
        self._stop_meter()
        if self._cam is not None:
            clip_path = self._cam.out
            self._cam.stop()
            self._cam = None
            if clip_path is not None:
                clip_path.unlink(missing_ok=True)
        self._timer.stop()
        self._state = "idle"
        super().reject()
        self.cancelled.emit()
        self.deleteLater()

    def done(self, result: int) -> None:
        self._stop_meter()
        self._timer.stop()
        super().done(result)
