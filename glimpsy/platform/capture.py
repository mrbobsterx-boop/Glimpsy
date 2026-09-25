"""Источники видео для FFmpeg на каждой платформе.

Ключевое решение: экран снимает сам FFmpeg (а не Python). Python только говорит,
какой монитор снимать. Так картинка не гоняется через Python и нагрузка на процессор
минимальна.

  Windows  — ddagrab (Desktop Duplication, через видеокарту), запасной — gdigrab
  macOS    — avfoundation
  Linux/X11 — x11grab
  Linux/Wayland — портал xdg-desktop-portal + PipeWire (через GStreamer), см. wayland_portal.py
"""

from __future__ import annotations

import logging
import os
import struct
import subprocess

from glimpsy.paths import subprocess_flags
from glimpsy.platform.base import CaptureBackend, CaptureInput, Monitor
from glimpsy.platform.common import list_monitors_mss, open_mss

log = logging.getLogger(__name__)


class X11GrabCapture(CaptureBackend):
    name = "x11grab"

    def __init__(self) -> None:
        self.display = os.environ.get("DISPLAY", ":0")

    def monitors(self) -> list[Monitor]:
        return list_monitors_mss()

    def input_for(self, monitor: Monitor, fps: int) -> CaptureInput:
        return CaptureInput(
            args=[
                "-f", "x11grab", "-draw_mouse", "1", "-framerate", str(fps),
                "-video_size", f"{monitor.width}x{monitor.height}",
                "-i", f"{self.display}+{monitor.x},{monitor.y}",
            ],
            width=monitor.width, height=monitor.height,
        )


class GdiGrabCapture(CaptureBackend):
    """Windows, классический способ. Надёжный, но нагружает процессор сильнее ddagrab."""

    name = "gdigrab"

    def monitors(self) -> list[Monitor]:
        return list_monitors_mss()

    def input_for(self, monitor: Monitor, fps: int) -> CaptureInput:
        return CaptureInput(
            args=[
                "-f", "gdigrab", "-draw_mouse", "1", "-framerate", str(fps),
                "-offset_x", str(monitor.x), "-offset_y", str(monitor.y),
                "-video_size", f"{monitor.width}x{monitor.height}", "-i", "desktop",
            ],
            width=monitor.width, height=monitor.height,
        )


class DdaGrabCapture(CaptureBackend):
    """Windows 8+: захват через видеокарту (Desktop Duplication API). Почти не грузит процессор.

    ddagrab нумерует мониторы по-своему (output_idx), и этот порядок может не совпадать
    с порядком Windows. Поэтому при старте мы «калибруемся»: снимаем по кадру с каждого
    output_idx и сравниваем с картинкой каждого монитора.
    """

    name = "ddagrab"

    def __init__(self, ffmpeg: str) -> None:
        self.ffmpeg = ffmpeg
        self._mapping: dict[tuple, int] = {}
        self._layout: tuple = ()

    def monitors(self) -> list[Monitor]:
        mons = list_monitors_mss()
        layout = tuple(mons)
        if layout != self._layout:
            self._layout = layout
            self._mapping = self._calibrate(mons)
        return mons

    def input_for(self, monitor: Monitor, fps: int) -> CaptureInput:
        idx = self._mapping.get((monitor.x, monitor.y, monitor.width, monitor.height), monitor.index - 1)
        return CaptureInput(
            args=["-f", "lavfi", "-i", f"ddagrab=output_idx={idx}:framerate={fps}:draw_mouse=1"],
            width=monitor.width, height=monitor.height,
            # кадр живёт в видеопамяти — скачиваем его, чтобы дальше работали обычные фильтры
            pre_filter="hwdownload,format=bgra,",
        )

    def _calibrate(self, mons: list[Monitor]) -> dict[tuple, int]:
        import numpy as np

        def thumb(arr: "np.ndarray") -> "np.ndarray":
            h, w = arr.shape[:2]
            ys = (np.linspace(0, h - 1, 36)).astype(int)
            xs = (np.linspace(0, w - 1, 64)).astype(int)
            return arr[ys][:, xs, :3].astype(np.float32).mean(axis=2)

        shots: dict[int, tuple[int, int, "np.ndarray"]] = {}
        for idx in range(len(mons)):
            try:
                out = subprocess.run(
                    [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                     "-i", f"ddagrab=output_idx={idx}", "-frames:v", "1",
                     "-vf", "hwdownload,format=bgra", "-f", "image2pipe", "-c:v", "bmp", "-"],
                    capture_output=True, timeout=10, **subprocess_flags(),
                )
                bmp = out.stdout
                if len(bmp) < 54:
                    continue
                w, h = struct.unpack_from("<ii", bmp, 18)
                off = struct.unpack_from("<I", bmp, 10)[0]
                bpp = struct.unpack_from("<H", bmp, 28)[0] // 8
                data = np.frombuffer(bmp, dtype=np.uint8, offset=off, count=abs(w * h) * bpp)
                img = data.reshape(abs(h), abs(w), bpp)
                if h > 0:  # BMP хранит строки снизу вверх
                    img = img[::-1]
                shots[idx] = (abs(w), abs(h), thumb(img))
            except Exception:
                log.debug("ddagrab output %s недоступен", idx, exc_info=True)
        mapping: dict[tuple, int] = {}
        used: set[int] = set()
        with open_mss() as sct:
            for m in mons:
                real = np.asarray(sct.grab({"left": m.x, "top": m.y, "width": m.width, "height": m.height}))
                t = thumb(real)
                best, best_d = None, float("inf")
                for idx, (w, h, th) in shots.items():
                    if idx in used:
                        continue
                    d = float(np.abs(th - t).mean()) + (0 if (w, h) == (m.width, m.height) else 1000)
                    if d < best_d:
                        best, best_d = idx, d
                if best is not None:
                    used.add(best)
                    mapping[(m.x, m.y, m.width, m.height)] = best
        log.info("Калибровка ddagrab: %s", mapping)
        return mapping


class AVFoundationCapture(CaptureBackend):
    """macOS. Нужно разрешение «Запись экрана» в Системных настройках → Конфиденциальность.

    FFmpeg и библиотека mss перечисляют экраны в одном и том же системном порядке,
    поэтому «Монитор N» из mss соответствует «Capture screen N-1» в FFmpeg.
    """

    name = "avfoundation"

    def monitors(self) -> list[Monitor]:
        return list_monitors_mss()

    def input_for(self, monitor: Monitor, fps: int) -> CaptureInput:
        return CaptureInput(
            args=[
                "-f", "avfoundation", "-capture_cursor", "1", "-capture_mouse_clicks", "0",
                "-framerate", str(fps), "-pixel_format", "nv12",
                "-i", f"Capture screen {monitor.index - 1}:none",
            ],
            # На Retina реальный размер кадра больше, но он нам и не нужен заранее
            width=monitor.width, height=monitor.height,
        )
