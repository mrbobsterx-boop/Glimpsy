"""Проигрывание ленты фрагментов как одного ролика.

Каждый фрагмент — отдельный файл, а открытие файла занимает долю секунды. Чтобы на
склейках не было заминок, используются ДВА плеера по очереди (как два магнитофона у
диджея): пока один играет текущий фрагмент, второй заранее открывает следующий и
ждёт на нужном кадре. На склейке они просто меняются местами.

Фоновая музыка играет третьим плеером и подстраивается под время ленты.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QElapsedTimer, QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink

from glimpsy.editor.project import Project

log = logging.getLogger(__name__)
S = QMediaPlayer.MediaStatus
# Музыка и голос играют сами; перематываем их, только если разошлись с видео заметно.
# Время видео на слабом компьютере идёт неровно — частые мелкие перемотки звук и «рвали».
RESYNC_S = 1.0
# На стыках вырезанных кусков звук плавно затихает и нарастает — без резкого обрыва.
EDGE_FADE_S = 0.08


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
        # фоновая музыка
        self.music = QMediaPlayer(self)
        self.music_audio = QAudioOutput(self)
        self.music.setAudioOutput(self.music_audio)
        self.music.errorOccurred.connect(lambda e, s: log.warning("Музыка: %s", s))
        self.music.mediaStatusChanged.connect(self._on_music_status)
        self._music_src: str | None = None
        self._music_ready = False
        self._music_ticks = 0
        # записанный в редакторе голос (дорожки «Голос»)
        self.voice = QMediaPlayer(self)
        self.voice_audio = QAudioOutput(self)
        self.voice.setAudioOutput(self.voice_audio)
        self.voice.errorOccurred.connect(lambda e, s: log.warning("Голос: %s", s))
        self.voice.mediaStatusChanged.connect(self._on_voice_status)
        self._voice_src: str | None = None
        self._voice_ready = False

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
        self._music_sync(force=True)
        self.timer.start()
        self.playing_changed.emit(True)

    def pause(self) -> None:
        self.playing = False
        self.timer.stop()
        for d in self.decks:
            d.mp.pause()
            d.want = (d.want[0], False) if d.want else None
        self.music.pause()
        self.voice.pause()
        self.playing_changed.emit(False)

    def seek(self, t: float) -> None:
        self.t = max(0.0, min(t, self.project.total))
        self._show(self.t, self.playing)
        self._music_sync(force=True)
        self.position.emit(self.t)

    def refresh(self) -> None:
        """Проект изменился (обрезка, скорость, кадрирование…) — показать актуальный кадр."""
        self.t = min(self.t, self.project.total)
        self._show(self.t, self.playing)
        self._music_sync(force=True)
        self.position.emit(self.t)

    def set_volume(self, v: float) -> None:
        self.volume = v
        for d in self.decks:
            d.audio.setVolume(v)
        self._music_volume()
        self.voice_audio.setVolume(v)

    # ---------- музыка ----------

    def _music_target(self) -> float | None:
        """Где сейчас должна быть музыка (с от начала файла) или None — тишина."""
        m = self.project.music
        if m is None or m.duration <= 0:
            return None
        pos = m.in_s + self.t
        if pos >= m.duration:
            if not m.loop:
                return None
            span = max(0.5, m.duration - m.in_s)       # при повторе трек снова идёт с «Начать с места»
            pos = m.in_s + (self.t % span)
        return pos

    def _music_volume(self) -> None:
        m = self.project.music
        self.music_audio.setVolume(self.volume * (m.volume if m is not None else 0.0))

    def _music_sync(self, force: bool = False) -> None:
        self._voice_sync(force)                         # голос сверяется с лентой вместе с музыкой
        m = self.project.music
        path = str(self.project.dir / m.src) if m is not None else None
        if path != self._music_src:
            self._music_src, self._music_ready = path, False
            self.music.stop()
            self.music.setSource(QUrl.fromLocalFile(path) if path else QUrl())
        if path is None:
            return
        self._music_volume()
        target = self._music_target()
        if not self.playing or target is None:
            self.music.pause()
            return
        if not self._music_ready:
            return                                      # догоним, когда файл откроется
        drift = abs(self.music.position() / 1000.0 - target)
        if force or drift > RESYNC_S or self.music.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            if force or drift > RESYNC_S:
                self.music.setPosition(int(target * 1000))
            self.music.play()

    def _on_music_status(self, status) -> None:
        if status in (S.LoadedMedia, S.BufferedMedia) and not self._music_ready:
            self._music_ready = True
            self._music_sync(force=True)
        elif status == S.EndOfMedia and self.playing:
            self._music_sync(force=True)                # повтор трека

    # ---------- голос ----------

    def _voice_sync(self, force: bool = False) -> None:
        voices = self.project.voices() if hasattr(self.project, "voices") else []
        item = next((v for v in voices if v.start <= self.t < v.end and not v.muted), None)
        path = str(self.project.dir / item.src) if item is not None else self._voice_src
        if path != self._voice_src:
            self._voice_src, self._voice_ready = path, False
            self.voice.stop()
            self.voice.setSource(QUrl.fromLocalFile(path) if path else QUrl())
        if item is None or not self.playing:
            self.voice.pause()
            return
        if not self._voice_ready:
            return                                      # догоним, когда файл откроется
        target = self.t - item.start + item.in_s
        drift = abs(self.voice.position() / 1000.0 - target)
        if force or drift > RESYNC_S or self.voice.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            if force or drift > RESYNC_S:
                self.voice.setPosition(int(target * 1000))
            self.voice.play()

    def _on_voice_status(self, status) -> None:
        if status in (S.LoadedMedia, S.BufferedMedia) and not self._voice_ready:
            self._voice_ready = True
            self._voice_sync(force=True)

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
        # и для того же файла (монтаж по тексту: сотни вырезов из одного видео) — запасной плеер
        # заранее встаёт на начало следующего куска, и склейка проходит без заминки на перемотку
        self.spare.load(path, c.in_s, False)

    def _on_status(self, d: _Deck, status) -> None:
        d.on_status(status)
        if status == S.EndOfMedia and d is self.deck and self.playing:
            c = self._current()
            if c is not None and c.kind == "video":
                self._advance()
        elif status == S.InvalidMedia:
            log.warning("Не удалось открыть %s", d.src)

    def _black(self) -> QImage:
        if getattr(self, "_black_img", None) is None:
            self._black_img = QImage(16, 9, QImage.Format.Format_RGB32)
            self._black_img.fill(0)
        return self._black_img

    def _on_frame(self, d: _Deck, frame: QVideoFrame) -> None:
        c = self._current()
        if d is not self.deck or c is None or c.kind != "video":
            return
        if c.hidden:                                   # картинка убрана — чёрный кадр, звук идёт
            self.frame.emit(self._black())
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
            self._edge_fade(c, src)
        if self.t >= start + c.duration - 1e-3:
            self._advance()
            return
        self._music_ticks += 1
        if self._music_ticks % 15 == 0:                 # раз в ~0,5 с сверяем музыку и голос с лентой
            self._music_sync()
        self.position.emit(self.t)

    def _edge_fade(self, c, src: float) -> None:
        """Громкость у краёв куска: там, где кусок склеен с другим местом записи, — плавно."""
        idx = self.idx or 0
        clips = self.project.clips
        g = 1.0
        prev = clips[idx - 1] if idx > 0 else None
        nxt = clips[idx + 1] if idx + 1 < len(clips) else None
        if prev is not None and not (prev.src == c.src and abs(prev.out_s - c.in_s) < 1e-3 and not prev.muted):
            g = min(g, (src - c.in_s) / EDGE_FADE_S)
        if nxt is not None and not (nxt.src == c.src and abs(nxt.in_s - c.out_s) < 1e-3 and not nxt.muted):
            g = min(g, (c.out_s - src) / EDGE_FADE_S)
        g = max(0.0, min(1.0, g))
        vol = self.volume * g
        if abs(self.deck.audio.volume() - vol) > 0.01:
            self.deck.audio.setVolume(vol)

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
            prev = self.project.clips[nxt - 1]
            if not (prev.src == c.src and abs(prev.out_s - c.in_s) < 1e-3 and not prev.muted):
                self.deck.audio.setVolume(self.volume * 0.25)   # начало куска после склейки — тихо, дальше нарастает
            else:
                self.deck.audio.setVolume(self.volume)
            self.deck.go(c.in_s, True)
            old.mp.pause()
            self._preload(nxt + 1)
        else:
            self._show(self.t, True)
        self.position.emit(self.t)

    def shutdown(self) -> None:
        # pause, а не stop: у Qt (движок FFmpeg) stop() звукового плеера изредка зависает навсегда,
        # если звук ещё подгружается, — окно редактора тогда не закрывалось. Плееры всё равно
        # удаляются вместе с окном.
        self.timer.stop()
        for d in self.decks:
            d.mp.pause()
        self.music.pause()
        self.voice.pause()
