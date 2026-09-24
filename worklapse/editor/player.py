"""Проигрывание ленты фрагментов как одного ролика.

Каждый фрагмент — отдельный файл, а открытие файла занимает долю секунды. Чтобы на
склейках не было заминок, используются ДВА плеера по очереди (как два магнитофона у
диджея): пока один играет текущий фрагмент, второй заранее открывает следующий и
ждёт на нужном кадре. На склейке они просто меняются местами.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QElapsedTimer, QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink

from worklapse.editor.project import Project

log = logging.getLogger(__name__)
S = QMediaPlayer.MediaStatus


class _Deck:
    """Один плеер: файл, готовность и отложенная команда «перейти и играть/стоять»."""

    def __init__(self, owner: QObject) -> None:
        self.mp = QMediaPlayer(owner)
        self.audio = QAudioOutput(owner)
        self.mp.setAudioOutput(self.audio)
        self.sink = QVideoSink(owner)
        self.mp.setVideoSink(self.sink)
        self.src: str | None = None
        self.ready = False
        self.want: tuple[float, bool] | None = None

    def load(self, path: str, pos: float, play: bool) -> None:
        if self.src != path:
            self.src, self.ready, self.want = path, False, (pos, play)
            self.mp.setSource(QUrl.fromLocalFile(path))
        elif not self.ready:
            self.want = (pos, play)
        else:
            self.go(pos, play)

    def go(self, pos: float, play: bool) -> None:
        if not play:
            self.mp.pause()
        self.mp.setPosition(int(pos * 1000))
        if play:
            self.mp.play()

    def on_status(self, status) -> None:
        if status in (S.LoadedMedia, S.BufferedMedia) and not self.ready:
            self.ready = True
            if self.want is not None:
                pos, play = self.want
                self.want = None
                self.go(pos, play)

    @property
    def busy(self) -> bool:
        return not self.ready or self.want is not None


class TimelinePlayer(QObject):
    frame = Signal(QImage)          # новый кадр для окна просмотра
    position = Signal(float)        # текущее время на ленте, с
    playing_changed = Signal(bool)

    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project = project
        self.decks = [_Deck(self), _Deck(self)]
        self.active = 0
        for d in self.decks:
            d.sink.videoFrameChanged.connect(lambda f, d=d: self._on_frame(d, f))
            d.mp.mediaStatusChanged.connect(lambda st, d=d: self._on_status(d, st))
            d.mp.errorOccurred.connect(lambda e, s: log.warning("Плеер: %s", s))
        self.timer = QTimer(self, interval=30)
        self.timer.timeout.connect(self._tick)
        self.clock = QElapsedTimer()
        self.t = 0.0
        self.playing = False
        self.idx: int | None = None
        self.volume = 0.8
        self._images: dict[str, QImage] = {}

    @property
    def deck(self) -> _Deck:
        return self.decks[self.active]

    @property
    def spare(self) -> _Deck:
        return self.decks[1 - self.active]

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
        for d in self.decks:
            d.mp.pause()
            d.want = (d.want[0], False) if d.want else None
        self.playing_changed.emit(False)

    def seek(self, t: float) -> None:
        self.t = max(0.0, min(t, self.project.total))
        self._show(self.t, self.playing)
        self.position.emit(self.t)

    def refresh(self) -> None:
        """Проект изменился (обрезка, скорость, кадрирование…) — показать актуальный кадр."""
        self.t = min(self.t, self.project.total)
        self._show(self.t, self.playing)
        self.position.emit(self.t)

    def set_volume(self, v: float) -> None:
        self.volume = v
        for d in self.decks:
            d.audio.setVolume(v)

    # ---------- внутреннее ----------

    def _current(self):
        if self.idx is None or not (0 <= self.idx < len(self.project.clips)):
            return None
        return self.project.clips[self.idx]

    def _setup_deck(self, d: _Deck, clip) -> None:
        d.mp.setPlaybackRate(clip.speed)
        d.audio.setMuted(clip.muted or not clip.has_audio)

    def _show(self, t: float, play: bool) -> None:
        idx, local = self.project.locate(t)
        self.idx = idx
        if idx is None:
            for d in self.decks:
                d.mp.pause()
            self.frame.emit(QImage())
            return
        c = self.project.clips[idx]
        path = str(self.project.path_of(c))
        if c.kind == "image":
            self.deck.mp.pause()
            img = self._images.get(path)
            if img is None:
                img = self._images[path] = QImage(path)
            self.frame.emit(img)
        else:
            # если нужный файл уже открыт в запасном плеере — переключаемся на него
            if self.deck.src != path and self.spare.src == path:
                self.deck.mp.pause()
                self.active = 1 - self.active
            self.spare.mp.pause()
            self._setup_deck(self.deck, c)
            self.deck.load(path, c.in_s + local * c.speed, play)
        self._preload(idx + 1)

    def _preload(self, idx: int) -> None:
        """Заранее открыть следующий видеофрагмент в запасном плеере."""
        if not (0 <= idx < len(self.project.clips)):
            return
        c = self.project.clips[idx]
        if c.kind != "video":
            return
        path = str(self.project.path_of(c))
        if path == self.deck.src:
            return   # тот же файл, что играет сейчас — перемотаем его же
        self.spare.load(path, c.in_s, False)

    def _on_status(self, d: _Deck, status) -> None:
        d.on_status(status)
        if status == S.EndOfMedia and d is self.deck and self.playing:
            c = self._current()
            if c is not None and c.kind == "video":
                self._advance()
        elif status == S.InvalidMedia:
            log.warning("Не удалось открыть %s", d.src)

    def _on_frame(self, d: _Deck, frame: QVideoFrame) -> None:
        c = self._current()
        if d is not self.deck or c is None or c.kind != "video":
            return
        img = frame.toImage()
        if not img.isNull():
            self.frame.emit(img)

    def _tick(self) -> None:
        c = self._current()
        if c is None:
            self.pause()
            return
        start = self.project.start_of(self.idx)
        dt = self.clock.restart() / 1000.0
        if c.kind == "image":
            self.t += dt
        elif not self.deck.busy:
            src = self.deck.mp.position() / 1000.0
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
        c = self.project.clips[nxt]
        path = str(self.project.path_of(c))
        if c.kind == "video" and self.spare.src == path and self.spare.ready:
            # мгновенная склейка: запасной плеер уже стоит на нужном кадре
            old = self.deck
            self.active = 1 - self.active
            self.idx = nxt
            self._setup_deck(self.deck, c)
            self.deck.go(c.in_s, True)
            old.mp.pause()
            self._preload(nxt + 1)
        else:
            self._show(self.t, True)
        self.position.emit(self.t)

    def shutdown(self) -> None:
        self.timer.stop()
        for d in self.decks:
            d.mp.stop()
