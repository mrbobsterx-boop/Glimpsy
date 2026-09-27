"""Живое видео наложений в окне просмотра (камера, видео поверх ролика, медиа).

Раньше в просмотре видео-наложение показывалось миниатюрами — кадр раз в секунду, и оно
«дёргалось». Теперь каждое видимое видео играет свой плеер (без звука — звук наложений
слышен в готовом ролике), в такт с лентой; на паузе он стоит на нужном кадре.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame, QVideoSink

log = logging.getLogger(__name__)

RESYNC_S = 0.4           # разошлось с лентой больше — подгоняем (меньше — пусть играет само, без рывков)
STILL_S = 0.04           # на паузе: кадр точнее этого не ищем
KEEP = 4                 # сколько плееров держать открытыми


class _Deck:
    def __init__(self, owner: "OverlayVideos", item_id: str) -> None:
        self.item_id = item_id
        self.mp = QMediaPlayer(owner)
        self.sink = QVideoSink(owner)
        self.mp.setVideoOutput(self.sink)
        self.src = ""
        self.image: QImage | None = None
        self.ready = False
        self.sink.videoFrameChanged.connect(lambda f: owner._on_frame(self, f))
        self.mp.mediaStatusChanged.connect(self._on_status)
        self.mp.errorOccurred.connect(lambda e, s: log.warning("Видео наложения: %s", s))

    def _on_status(self, status) -> None:
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            self.ready = True

    def load(self, path: str) -> None:
        if path != self.src:
            self.src, self.ready, self.image = path, False, None
            self.mp.setSource(QUrl.fromLocalFile(path))

    def close(self) -> None:
        self.mp.stop()
        self.mp.setSource(QUrl())


class OverlayVideos(QObject):
    frame_ready = Signal()               # пришёл новый кадр (на паузе — пора перерисовать просмотр)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._decks: dict[str, _Deck] = {}
        self._order: list[str] = []

    def _deck(self, item_id: str) -> _Deck:
        d = self._decks.get(item_id)
        if d is None:
            d = self._decks[item_id] = _Deck(self, item_id)
        if item_id in self._order:
            self._order.remove(item_id)
        self._order.append(item_id)
        while len(self._order) > KEEP:
            old = self._order.pop(0)
            self._decks.pop(old).close()
        return d

    def sync(self, items: list, t: float, playing: bool, project_dir: Path) -> None:
        """Видимые видео-наложения — на нужный момент ленты; остальные стоят."""
        active = set()
        for it in items:
            if it.kind != "video":
                continue
            active.add(it.id)
            d = self._deck(it.id)
            d.load(str(project_dir / it.src))
            if not d.ready:
                continue
            target = it.in_s + max(0.0, min(it.duration, t - it.start))
            pos = d.mp.position() / 1000.0
            if playing and it.start <= t < it.end:
                if abs(pos - target) > RESYNC_S:
                    d.mp.setPosition(int(target * 1000))
                if d.mp.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                    d.mp.play()
            else:
                if d.mp.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                    d.mp.pause()
                if abs(pos - target) > STILL_S or d.image is None:
                    d.mp.setPosition(int(target * 1000))
        for item_id, d in self._decks.items():
            if item_id not in active and d.mp.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                d.mp.pause()

    def frame(self, item_id: str) -> QImage | None:
        d = self._decks.get(item_id)
        return d.image if d is not None else None

    def pause(self) -> None:
        for d in self._decks.values():
            d.mp.pause()

    def shutdown(self) -> None:
        for d in self._decks.values():
            d.close()
        self._decks.clear()
        self._order.clear()

    def _on_frame(self, d: _Deck, frame: QVideoFrame) -> None:
        img = frame.toImage()
        if not img.isNull():
            d.image = img
            self.frame_ready.emit()
