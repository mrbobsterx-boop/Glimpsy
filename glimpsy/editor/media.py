"""Сведения о медиафайлах и миниатюры для ленты — всё через встроенный FFmpeg."""

from __future__ import annotations

import hashlib
import logging
import queue
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage

from glimpsy import paths
from glimpsy.paths import subprocess_flags

log = logging.getLogger(__name__)

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".ts", ".mts", ".wmv", ".flv", ".3gp"}


@dataclass
class MediaInfo:
    duration: float
    width: int
    height: int
    has_audio: bool
    is_image: bool
    fps: float = 0.0           # кадров в секунду (0 — неизвестно)


class MediaError(RuntimeError):
    pass


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXT | VIDEO_EXT


def probe(ffmpeg: str, path: Path) -> MediaInfo:
    """Читает длительность, размер и наличие звука (из вывода `ffmpeg -i`)."""
    if path.suffix.lower() in IMAGE_EXT:
        img = QImage(str(path))
        if not img.isNull():
            return MediaInfo(0.0, img.width(), img.height(), False, True)
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, **subprocess_flags())
    return parse_probe(r.stderr.decode("utf-8", "replace"), path.suffix.lower() in IMAGE_EXT)


def parse_probe(text: str, image_hint: bool = False) -> MediaInfo:
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    v = re.search(r"Stream #\S+.*?Video:.*?(\d{2,5})x(\d{2,5})", text)
    if not v:
        raise MediaError("В файле нет видео или картинки, которую можно прочитать")
    w, h = int(v.group(1)), int(v.group(2))
    rot = re.search(r"rotation of (-?\d+(?:\.\d+)?) degrees|rotate\s*:\s*(-?\d+)", text)
    if rot:
        angle = abs(int(float(rot.group(1) or rot.group(2)))) % 180
        if angle == 90:          # видео с телефона, снятое вертикально
            w, h = h, w
    has_audio = bool(re.search(r"Stream #\S+.*?Audio:", text))
    is_image = image_hint or duration == 0.0
    f = re.search(r"Stream #\S+.*?Video:.*?(\d+(?:\.\d+)?) fps", text)
    return MediaInfo(duration, w, h, has_audio, is_image, float(f.group(1)) if f else 0.0)


class Thumbnailer(QObject):
    """Делает миниатюры кадров в фоне и сообщает, когда готово (сигнал ready)."""

    ready = Signal()

    def __init__(self, ffmpeg: str) -> None:
        super().__init__()
        self.ffmpeg = ffmpeg
        self._mem: dict[str, QImage] = {}
        self._queued: set[str] = set()
        self._q: queue.Queue = queue.Queue()
        self._cache = paths.temp_root() / "thumbs"
        self._cache.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._worker, daemon=True, name="thumbs").start()

    def get(self, path: Path, t: float, height: int, is_image: bool = False) -> QImage | None:
        key = f"{path}|{round(t, 1)}|{height}"
        img = self._mem.get(key)
        if img is None and key not in self._queued:
            self._queued.add(key)
            self._q.put((key, path, t, height, is_image))
        return img

    def _worker(self) -> None:
        while True:
            key, path, t, height, is_image = self._q.get()
            img = QImage()
            try:
                if is_image:
                    img = QImage(str(path))
                    if not img.isNull():
                        img = img.scaledToHeight(height, Qt.TransformationMode.SmoothTransformation)
                else:
                    disk = self._cache / (hashlib.sha1(key.encode()).hexdigest() + ".jpg")
                    if not disk.exists():
                        subprocess.run(
                            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.2f}",
                             "-i", str(path), "-frames:v", "1", "-vf", f"scale=-2:{height}", "-y", str(disk)],
                            capture_output=True, timeout=20, **subprocess_flags())
                    img = QImage(str(disk))
            except Exception:
                log.debug("Миниатюра не получилась: %s", key, exc_info=True)
            if not img.isNull():
                self._mem[key] = img
                try:
                    self.ready.emit()
                except RuntimeError:          # окно уже закрыли — миниатюра больше не нужна
                    return
