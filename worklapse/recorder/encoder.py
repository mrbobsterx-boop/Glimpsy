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
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

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
        # Ключевой кадр каждую секунду — чтобы буфер можно было резать посекундно.
        # Важно: аппаратные кодеки NVIDIA/Intel/AMD по умолчанию делают «принудительные»
        # ключевые кадры неполноценными (I, а не IDR) — тогда FFmpeg не может резать
        # видео на кусочки и буфер пишется одним сплошным файлом. forced_idr это чинит.
        if n == "h264_nvenc":
            a += ["-forced-idr", "1"]
        elif n in ("h264_qsv", "h264_amf"):
            a += ["-forced_idr", "1"]
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


def error_summary(stderr: str, limit: int = 6) -> str:
    """Самые полезные строки ошибки FFmpeg целиком (а не обрезок с середины слова)."""
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    keys = ("error", "cannot", "failed", "not found", "no ", "unsupported", "невозмож")
    useful = [ln for ln in lines if any(k in ln.lower() for k in keys)]
    return " | ".join((useful or lines)[:limit])[:600]


def test_encoder(ffmpeg: str, enc: Encoder, fps: int = 30) -> bool:
    """Кодек подходит, только если он работает И умеет резать видео на секундные кусочки.

    Кодируем 3 секунды тестового видео тем же способом, что и настоящий буфер,
    и проверяем, что получилось несколько кусочков, а не один сплошной файл.
    """
    with tempfile.TemporaryDirectory(prefix="worklapse_enc_") as tmp:
        seg_list = Path(tmp) / "list.csv"
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", *enc.global_args,
               "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate={fps}", "-t", "3",
               "-vf", enc.filter_suffix, *enc.args("buffer", fps),
               "-force_key_frames", "expr:gte(t,n_forced*1)",
               "-f", "segment", "-segment_time", "1", "-segment_format", "mpegts",
               "-segment_list", str(seg_list), "-segment_list_type", "csv",
               str(Path(tmp) / "s_%03d.ts")]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30, **subprocess_flags())
        except Exception:
            log.info("Кодек %s недоступен", enc.name, exc_info=True)
            return False
        if r.returncode != 0:
            log.info("Кодек %s недоступен: %s", enc.name, error_summary(r.stderr.decode(errors="replace")))
            return False
        try:
            segments = [x for x in seg_list.read_text().splitlines() if x.strip()]
        except OSError:
            segments = []
        if len(segments) < 2:
            log.warning("Кодек %s работает, но не делает ключевые кадры по запросу "
                        "(кусочков: %s) — не подходит для буфера", enc.name, len(segments))
            return False
        return True


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
