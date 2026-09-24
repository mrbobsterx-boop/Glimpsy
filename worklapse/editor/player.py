"""Проигрывание ленты фрагментов как одного ролика.

Внутри — обычный видеоплеер Qt, который мы переключаем с фрагмента на фрагмент:
выставляем нужный файл, позицию и скорость. Фото показываются заданное время.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QElapsedTimer, QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink

from worklapse.editor.project import Project

log = logging.getLogger(__name__)


class TimelinePlayer(QObject):
    frame = Signal(QImage)          # новый кадр для окна просмотра
    position = Signal(float)        # текущее время на ленте, с
    playing_changed = Signal(bool)

    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project = project
        self.mp = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.mp.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.mp.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._on_frame)
        self.mp.mediaStatusChanged.connect(self._on_status)
        self.mp.errorOccurred.connect(lambda e, s: log.warning("Плеер: %s", s))
        self.timer = QTimer(self, interval=30)
        self.timer.timeout.connect(self._tick)
        self.clock = QElapsedTimer()
        self.t = 0.0
        self.playing = False
        self.idx: int | None = None
        self._src: str | None = None
        self._pending: tuple[float, bool] | None = None
        self._images: dict[str, QImage] = {}

    # ---------- управление ----------

    def toggle(self) -> None:
        self.pause() if self.playing else self.play()

    def play(self) -> None:
        if not self.project.clips:
            return
        if self.t >= self.project.total - 0.05:
            self.t = 0.0
        self.playing = True
        self.clock.start()
        self._show(self.t, True)
        self.timer.start()
        self.playing_changed.emit(True)

    def pause(self) -> None:
        self.playing = False
        self.timer.stop()
        self.mp.pause()
        self.playing_changed.emit(False)

    def seek(self, t: float) -> None:
        self.t = max(0.0, min(t, self.project.total))
        self._show(self.t, self.playing)
        self.position.emit(self.t)

    def refresh(self) -> None:
        """Проект изменился (обрезка, скорость…) — показать актуальный кадр."""
        self.t = min(self.t, self.project.total)
        self._show(self.t, self.playing)
        self.position.emit(self.t)

    def set_volume(self, v: float) -> None:
        self.audio.setVolume(v)

    # ---------- внутреннее ----------

    def _show(self, t: float, play: bool) -> None:
        idx, local = self.project.locate(t)
        self.idx = idx
        if idx is None:
            self.mp.pause()
            self.frame.emit(QImage())
            return
        c = self.project.clips[idx]
        path = str(self.project.path_of(c))
        if c.kind == "image":
            self.mp.pause()
            img = self._images.get(path)
            if img is None:
                img = self._images[path] = QImage(path)
            self.frame.emit(img)
            return
        src_pos = c.in_s + local * c.speed
        self.audio.setMuted(c.muted or not c.has_audio)
        if self._src != path:
            self._src = path
            self._pending = (src_pos, play)
            self.mp.setSource(QUrl.fromLocalFile(path))
            return
        self._apply(src_pos, play, c.speed)

    def _apply(self, src_pos: float, play: bool, speed: float) -> None:
        self.mp.setPlaybackRate(speed)
        if not play:
            self.mp.pause()
        self.mp.setPosition(int(src_pos * 1000))
        if play:
            self.mp.play()

    def _on_status(self, status) -> None:
        S = QMediaPlayer.MediaStatus
        if status in (S.LoadedMedia, S.BufferedMedia) and self._pending is not None:
            pos, play = self._pending
            self._pending = None
            c = self._current()
            self._apply(pos, play and self.playing, c.speed if c else 1.0)
        elif status == S.EndOfMedia and self.playing:
            self._advance()
        elif status == S.InvalidMedia:
            log.warning("Не удалось открыть %s", self._src)

    def _on_frame(self, frame: QVideoFrame) -> None:
        if self.idx is None or self._current() is None or self._current().kind != "video":
            return
        img = frame.toImage()
        if not img.isNull():
            self.frame.emit(img)

    def _current(self):
        if self.idx is None or self.idx >= len(self.project.clips):
            return None
        return self.project.clips[self.idx]

    def _tick(self) -> None:
        c = self._current()
        if c is None:
            self.pause()
            return
        start = self.project.start_of(self.idx)
        dt = self.clock.restart() / 1000.0
        if c.kind == "image" or self._pending is not None:
            self.t += dt if c.kind == "image" else 0.0
        else:
            src = self.mp.position() / 1000.0
            if src >= c.out_s - 0.03:
                self._advance()
                return
            self.t = start + max(0.0, src - c.in_s) / c.speed
        if self.t >= start + c.duration - 1e-3:
            self._advance()
            return
        self.position.emit(self.t)

    def _advance(self) -> None:
        nxt = (self.idx or 0) + 1
        if nxt >= len(self.project.clips):
            self.t = self.project.total
            self.pause()
            self.position.emit(self.t)
            return
        self.t = self.project.start_of(nxt)
        self._show(self.t, True)
        self.position.emit(self.t)

    def shutdown(self) -> None:
        self.timer.stop()
        self.mp.stop()
