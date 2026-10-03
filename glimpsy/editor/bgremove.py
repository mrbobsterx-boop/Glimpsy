"""Убрать фон за человеком на видео с камеры (без зелёного экрана).

Нейросеть MODNet (лицензия Apache 2.0, файл ≈25 МБ, скачивается один раз) по каждому кадру
понимает, где человек. Получается «маска» — чёрно-белое видео: белое — человек, чёрное — фон.
Её считаем один раз для всего файла камеры и кладём в папку проекта:
  masks/<имя>.mp4  — для готового ролика (FFmpeg накладывает её как прозрачность);
  masks/<имя>.raw  — маленькая копия для окна просмотра (кадры читаются прямо с диска).

Фон можно убрать совсем (под человеком видно то, что ниже — запись экрана) или размыть.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import threading
from pathlib import Path
from typing import Callable

import numpy as np

from glimpsy.paths import subprocess_flags

log = logging.getLogger(__name__)

MODEL_URL = "https://huggingface.co/Xenova/modnet/resolve/main/onnx/model.onnx"
MODEL_MB = 25
MASK_FPS = 30
MODEL_SHORT = 288          # меньшая сторона кадра для нейросети (кратно 32)
PREVIEW_W = 192            # ширина маски для окна просмотра
SMOOTH = 0.25              # сглаживание между соседними кадрами — маска не мерцает
MODES = {"": "Оставить фон", "remove": "Убрать фон", "blur": "Размыть фон"}


class BgError(RuntimeError):
    pass


class Cancelled(BgError):
    pass


def model_path() -> Path:
    from glimpsy.editor import subtitles as subs

    return subs.models_dir() / "modnet" / "model.onnx"


def model_ready() -> bool:
    p = model_path()
    return p.exists() and p.stat().st_size > 1_000_000


def runtime_ready() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def mask_stem(project_dir: Path, src: str) -> Path:
    """Где лежит маска для файла src (одна на файл, сколько бы раз он ни был в ролике)."""
    h = hashlib.sha1(src.encode("utf-8")).hexdigest()[:12]
    return Path(project_dir) / "masks" / f"{Path(src).stem}_{h}"


def mask_ready(project_dir: Path, src: str) -> bool:
    s = mask_stem(project_dir, src)
    return s.with_suffix(".mp4").exists() and s.with_suffix(".raw").exists() and s.with_suffix(".json").exists()


def model_size(w: int, h: int) -> tuple[int, int]:
    """Размер кадра для нейросети: меньшая сторона MODEL_SHORT, обе кратны 32."""
    if w >= h:
        return max(32, round(MODEL_SHORT * w / h / 32) * 32), MODEL_SHORT
    return MODEL_SHORT, max(32, round(MODEL_SHORT * h / w / 32) * 32)


def make_mask(ffmpeg: str, src: Path, stem: Path, width: int, height: int, duration: float,
              progress: Callable[[float], None], cancel: threading.Event) -> None:
    """Посчитать маску для всего файла src: stem.mp4 (для ролика) и stem.raw + stem.json (для просмотра)."""
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    session = ort.InferenceSession(str(model_path()), opts, providers=["CPUExecutionProvider"])
    W, H = model_size(width or 640, height or 360)
    pw = PREVIEW_W
    ph = max(2, round(PREVIEW_W * H / W))
    stem.parent.mkdir(parents=True, exist_ok=True)
    tmp_mp4, tmp_raw = stem.with_suffix(".part.mp4"), stem.with_suffix(".part.raw")
    dec = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(src), "-an", "-vf",
                            f"fps={MASK_FPS},scale={W}:{H},format=rgb24", "-f", "rawvideo", "-"],
                           stdout=subprocess.PIPE, **subprocess_flags())
    enc = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray",
                            "-s", f"{W}x{H}", "-r", str(MASK_FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
                            "-crf", "16", "-pix_fmt", "yuv420p", str(tmp_mp4)],
                           stdin=subprocess.PIPE, **subprocess_flags())
    assert dec.stdout is not None and enc.stdin is not None
    frame_bytes = W * H * 3
    total = max(1, int(duration * MASK_FPS))
    n = 0
    prev = None
    try:
        with open(tmp_raw, "wb") as small:
            while True:
                if cancel.is_set():
                    raise Cancelled("Отменено")
                buf = dec.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    break
                rgb = np.frombuffer(buf, np.uint8).reshape(H, W, 3).astype(np.float32)
                x = ((rgb / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)[None]
                matte = session.run(None, {"input": x})[0][0, 0]
                if prev is not None:
                    matte = matte * (1 - SMOOTH) + prev * SMOOTH
                prev = matte
                m8 = np.clip(matte * 255 + 0.5, 0, 255).astype(np.uint8)
                enc.stdin.write(m8.tobytes())
                small.write(_shrink(m8, pw, ph).tobytes())
                n += 1
                if n % 10 == 0:
                    progress(min(0.99, n / total))
        enc.stdin.close()
        if enc.wait() != 0 or n == 0:
            raise BgError("Не удалось сохранить маску (FFmpeg)")
    except BaseException:
        for p in (dec, enc):
            p.kill()
        tmp_mp4.unlink(missing_ok=True)
        tmp_raw.unlink(missing_ok=True)
        raise
    finally:
        dec.wait()
    tmp_mp4.replace(stem.with_suffix(".mp4"))
    tmp_raw.replace(stem.with_suffix(".raw"))
    stem.with_suffix(".json").write_text(json.dumps({"w": pw, "h": ph, "fps": MASK_FPS, "frames": n}),
                                         encoding="utf-8")
    progress(1.0)


def _shrink(m: np.ndarray, w: int, h: int) -> np.ndarray:
    """Быстро уменьшить маску (среднее по блокам, без лишних библиотек)."""
    H, W = m.shape
    ys = (np.arange(h) * H // h)
    xs = (np.arange(w) * W // w)
    return m[ys][:, xs]


class PreviewMask:
    """Маска для окна просмотра: кадры читаются с диска по номеру, без загрузки всего файла."""

    def __init__(self, stem: Path) -> None:
        info = json.loads(stem.with_suffix(".json").read_text(encoding="utf-8"))
        self.w, self.h, self.fps, self.frames = int(info["w"]), int(info["h"]), int(info["fps"]), int(info["frames"])
        self.data = np.memmap(stem.with_suffix(".raw"), np.uint8, "r", shape=(self.frames, self.h, self.w))

    def at(self, t: float) -> np.ndarray:
        i = max(0, min(self.frames - 1, int(round(t * self.fps))))
        return np.asarray(self.data[i])


def apply_preview(img, mask: np.ndarray, mode: str):
    """Кадр камеры с маской: фон прозрачный (remove) или размытый (blur)."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter

    if img is None or img.isNull():
        return img
    h, w = mask.shape
    m = QImage(np.ascontiguousarray(mask).data, w, h, w, QImage.Format.Format_Alpha8).copy()
    m = m.scaled(img.width(), img.height(), Qt.AspectRatioMode.IgnoreAspectRatio,
                 Qt.TransformationMode.SmoothTransformation)
    fg = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(fg)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    p.drawImage(0, 0, m)
    p.end()
    if mode != "blur":
        return fg
    small = img.scaled(max(8, img.width() // 16), max(8, img.height() // 16), Qt.AspectRatioMode.IgnoreAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
    out = small.scaled(img.width(), img.height(), Qt.AspectRatioMode.IgnoreAspectRatio,
                       Qt.TransformationMode.SmoothTransformation).convertToFormat(
        QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(out)
    p.drawImage(0, 0, fg)
    p.end()
    return out


def ffmpeg_chain(k: int, m: int, S: float, w: int, h: int, mode: str, label: str) -> str:
    """Фильтры FFmpeg: видео камеры (вход k) + маска (вход m) → [label] с прозрачностью или размытым фоном."""
    head = (f"[{k}:v]setpts=PTS-STARTPTS+{S:.3f}/TB,scale={w}:{h},format=rgba[bk{k}];"
            f"[{m}:v]setpts=PTS-STARTPTS+{S:.3f}/TB,scale={w}:{h},format=gray[bm{k}];")
    if mode == "blur":
        return (head + f"[bk{k}]split[bka{k}][bkb{k}];[bka{k}]boxblur=luma_radius=20:luma_power=2[bbl{k}];"
                f"[bkb{k}][bm{k}]alphamerge[bfg{k}];[bbl{k}][bfg{k}]overlay=format=auto,format=rgba[{label}]")
    return head + f"[bk{k}][bm{k}]alphamerge[{label}]"
