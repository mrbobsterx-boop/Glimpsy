"""Выбор видеокодека: сначала аппаратные (видеокарта), в крайнем случае — программный.

Аппаратный кодек сжимает видео на отдельном чипе видеокарты, поэтому процессор почти
не нагружается. Какие кодеки есть — зависит от компьютера, так что мы просто пробуем
закодировать полсекунды тестового видео каждым по очереди. Первый сработавший — наш.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field

from worklapse.paths import subprocess_flags

log = logging.getLogger(__name__)


@dataclass
class Encoder:
    name: str                    # имя кодека в FFmpeg
    label: str                   # понятное название
    hw: bool
    global_args: list[str] = field(default_factory=list)   # ставятся в начало команды
    filter_suffix: str = "format=yuv420p"                  # последний фильтр перед кодеком

    def args(self, mode: str, fps: int) -> list[str]:
        """mode='buffer' — быстро и легко (фоновая запись), 'final' — качественно (итоговый ролик)."""
        fast = mode == "buffer"
        n = self.name
        if n == "h264_nvenc":
            a = ["-c:v", n, "-preset", "p1" if fast else "p5", "-rc", "vbr",
                 "-cq", "27" if fast else "21", "-b:v", "0"]
        elif n == "h264_qsv":
            a = ["-c:v", n, "-preset", "veryfast" if fast else "medium",
                 "-global_quality", "27" if fast else "21"]
        elif n == "h264_amf":
            a = ["-c:v", n, "-quality", "speed" if fast else "balanced", "-rc", "cqp",
                 "-qp_i", "25" if fast else "19", "-qp_p", "27" if fast else "21"]
        elif n == "h264_videotoolbox":
            a = ["-c:v", n, "-b:v", "8M" if fast else "12M", "-allow_sw", "0"]
            if fast:
                a += ["-realtime", "1"]
        elif n == "h264_vaapi":
            a = ["-c:v", n, "-qp", "26" if fast else "20"]
        else:  # libx264 — программный запасной вариант
            a = ["-c:v", "libx264", "-preset", "ultrafast" if fast else "medium",
                 "-crf", "26" if fast else "20"]
            if fast:
                a += ["-tune", "zerolatency"]
        # ключевой кадр каждую секунду — чтобы буфер можно было резать посекундно
        return a + ["-g", str(fps), "-bf", "0" if fast else "2"]


def _vaapi_device() -> str:
    for i in range(128, 136):
        dev = f"/dev/dri/renderD{i}"
        if os.path.exists(dev):
            return dev
    return "/dev/dri/renderD128"


def candidates() -> list[Encoder]:
    sw = Encoder("libx264", "Программный (libx264) — нагружает процессор", False)
    nvenc = Encoder("h264_nvenc", "NVIDIA NVENC", True)
    qsv = Encoder("h264_qsv", "Intel Quick Sync", True, filter_suffix="format=nv12")
    if sys.platform.startswith("win"):
        return [nvenc, qsv, Encoder("h264_amf", "AMD AMF", True, filter_suffix="format=nv12"), sw]
    if sys.platform == "darwin":
        return [Encoder("h264_videotoolbox", "Apple VideoToolbox", True, filter_suffix="format=nv12"), sw]
    vaapi = Encoder("h264_vaapi", "VAAPI (Intel/AMD)", True,
                    global_args=["-vaapi_device", _vaapi_device()], filter_suffix="format=nv12,hwupload")
    return [nvenc, vaapi, qsv, sw]


def test_encoder(ffmpeg: str, enc: Encoder, fps: int = 30) -> bool:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", *enc.global_args,
           "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate={fps}", "-t", "0.5",
           "-vf", enc.filter_suffix, *enc.args("buffer", fps), "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=20, **subprocess_flags())
        if r.returncode != 0:
            log.info("Кодек %s недоступен: %s", enc.name, r.stderr.decode(errors="replace")[-300:])
        return r.returncode == 0
    except Exception:
        log.info("Кодек %s недоступен", enc.name, exc_info=True)
        return False


def pick_encoder(ffmpeg: str, preferred: str = "auto", fps: int = 30) -> Encoder:
    cands = candidates()
    if preferred != "auto":
        chosen = [c for c in cands if c.name == preferred]
        if chosen and test_encoder(ffmpeg, chosen[0], fps):
            return chosen[0]
        log.warning("Выбранный кодек %s не работает, ищем другой", preferred)
    for c in cands:
        if test_encoder(ffmpeg, c, fps):
            log.info("Кодек: %s", c.name)
            return c
    return cands[-1]


def software_encoder() -> Encoder:
    return candidates()[-1]
