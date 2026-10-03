"""Обработка картинки ролика: стабилизация и цвет (яркость, контраст, насыщенность, теплота).

Настройки — в project.look:
  stabilize   0–100  насколько сглаживать дрожание (0 — выключено). Для видео с телефона или
                     камеры с рук. Края кадра чуть приближаются, чтобы не было чёрных полос;
  brightness  −50…50, contrast −50…50, saturation −50…50, warmth −50…50 — ползунки цвета.

Стабилизация — два прохода (vid.stab в FFmpeg): сначала каждый кусок ролика анализируется,
потом при сохранении кадры сдвигаются. Если в FFmpeg нет vid.stab — простой фильтр deshake.
Цвет виден сразу в просмотре (look.apply_qimage) и делается тем же при сохранении (eq).
"""

from __future__ import annotations

import functools
import logging
import subprocess
from pathlib import Path
from typing import Callable

import numpy as np

from glimpsy.paths import subprocess_flags

log = logging.getLogger(__name__)

COLOR_KEYS = ("brightness", "contrast", "saturation", "warmth")
DEFAULTS = {"stabilize": 0, "brightness": 0, "contrast": 0, "saturation": 0, "warmth": 0}
PRESETS = {
    "Как снято": {"brightness": 0, "contrast": 0, "saturation": 0, "warmth": 0},
    "Ярче": {"brightness": 12, "contrast": 8, "saturation": 10, "warmth": 0},
    "Сочные цвета": {"brightness": 4, "contrast": 14, "saturation": 30, "warmth": 0},
    "Тёплый": {"brightness": 4, "contrast": 6, "saturation": 8, "warmth": 30},
    "Холодный": {"brightness": 0, "contrast": 8, "saturation": -5, "warmth": -30},
    "Кино": {"brightness": -6, "contrast": 22, "saturation": -15, "warmth": 12},
    "Чёрно-белый": {"brightness": 0, "contrast": 15, "saturation": -50, "warmth": 0},
}


def settings(project) -> dict:
    d = dict(DEFAULTS)
    d.update(getattr(project, "look", None) or {})
    for k in DEFAULTS:
        lo, hi = (0, 100) if k == "stabilize" else (-50, 50)
        d[k] = max(lo, min(hi, int(d[k])))
    return d


def color_active(project) -> bool:
    s = settings(project)
    return any(s[k] for k in COLOR_KEYS)


def active(project) -> bool:
    return color_active(project) or bool(settings(project)["stabilize"])


# ---------------- цвет ----------------

def _factors(s: dict) -> tuple[float, float, float, float]:
    """Сдвиг яркости, контраст, насыщенность (множители), теплота (−1…1)."""
    return s["brightness"] / 250, 1 + s["contrast"] / 100, 1 + s["saturation"] / 50, s["warmth"] / 50


def color_filter(project) -> str:
    """Фильтры FFmpeg для цвета ('' — без изменений)."""
    s = settings(project)
    if not any(s[k] for k in COLOR_KEYS):
        return ""
    b, c, sat, warm = _factors(s)
    parts = [f"eq=brightness={b:.3f}:contrast={c:.3f}:saturation={max(0.0, sat):.3f}"]
    if warm:
        # теплее — больше красного, меньше синего (и наоборот)
        parts.append(f"colorchannelmixer=rr={1 + 0.12 * warm:.3f}:bb={1 - 0.12 * warm:.3f}")
    return ",".join(parts)


def apply_qimage(img, project):
    """Тот же цвет — для кадра в просмотре (QImage → новый QImage)."""
    from PySide6.QtGui import QImage

    s = settings(project)
    if img is None or img.isNull() or not any(s[k] for k in COLOR_KEYS):
        return img
    b, c, sat, warm = _factors(s)
    src = img.convertToFormat(QImage.Format.Format_RGB32)
    w, h = src.width(), src.height()
    arr = np.frombuffer(src.constBits(), np.uint8, count=src.bytesPerLine() * h).reshape(h, src.bytesPerLine())
    px = arr[:, : w * 4].reshape(h, w, 4).astype(np.float32) / 255.0      # B, G, R, A
    r, g, bl = px[..., 2], px[..., 1], px[..., 0]
    # как eq в FFmpeg: яркость и контраст — только по яркости (Y), насыщенность — по цвету (U, V)
    y = 0.299 * r + 0.587 * g + 0.114 * bl
    u, v = (bl - y) * 0.564, (r - y) * 0.713
    y = (y - 0.5) * c + 0.5 + b
    u, v = u * max(0.0, sat), v * max(0.0, sat)
    r2 = y + 1.403 * v
    b2 = y + 1.773 * u
    g2 = y - 0.344 * u - 0.714 * v
    r2, g2, b2 = (np.clip(x, 0.0, 1.0) for x in (r2, g2, b2))        # как FFmpeg: обрезка до смешивания
    if warm:
        r2 = r2 * (1 + 0.12 * warm)
        b2 = b2 * (1 - 0.12 * warm)
    rgb = np.stack([b2, g2, r2], axis=-1)
    out = np.empty((h, w, 4), np.uint8)
    out[..., :3] = np.clip(rgb * 255.0 + 0.5, 0, 255).astype(np.uint8)
    out[..., 3] = 255
    res = QImage(out.data, w, h, w * 4, QImage.Format.Format_RGB32)
    return res.copy()                                   # свой буфер — массив out исчезнет


# ---------------- стабилизация ----------------

@functools.lru_cache(maxsize=4)
def has_vidstab(ffmpeg: str) -> bool:
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=20,
                           **subprocess_flags())
    except (OSError, subprocess.SubprocessError):
        return False
    return "vidstabdetect" in r.stdout


def detect_command(ffmpeg: str, src: Path, in_s: float, dur: float, trf: str) -> list[str]:
    """Первый проход: как дрожит кадр (результат — файл trf в рабочей папке)."""
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{in_s:.3f}", "-t", f"{dur:.3f}",
            "-i", str(src), "-vf", f"vidstabdetect=shakiness=6:accuracy=12:result={trf}", "-f", "null", "-"]


def stabilize_filter(project, trf: str | None, vidstab: bool) -> str:
    """Второй проход: сдвиг кадров. Имя trf — относительно папки, где запущен FFmpeg."""
    strength = settings(project)["stabilize"]
    if not strength:
        return ""
    if not vidstab or not trf:
        return "deshake"
    smoothing = int(4 + strength * 0.36)              # 4…40 кадров: чем больше, тем плавнее
    return (f"vidstabtransform=input={trf}:smoothing={smoothing}:optzoom=1:zoomspeed=0.2:interpol=bicubic,"
            "unsharp=5:5:0.5:3:3:0")


def prepare(ffmpeg: str, project, clips: list, work: Path, run: Callable[[list[str]], None],
            progress: Callable[[float, str], None]) -> dict[str, str]:
    """Анализ дрожания для каждого видеокуска: {id куска: имя файла в work}."""
    if not settings(project)["stabilize"] or not has_vidstab(ffmpeg):
        return {}
    out = {}
    videos = [c for c in clips if c.kind == "video"]
    for n, c in enumerate(videos):
        progress(n / max(1, len(videos)), f"Стабилизация: анализ {n + 1} из {len(videos)}")
        name = f"stab_{n:04d}.trf"
        run(detect_command(ffmpeg, project.path_of(c), c.in_s, c.out_s - c.in_s, name))
        out[c.id] = name
    return out
