"""Убрать фон за человеком на камере: маска → прозрачность в просмотре и в готовом ролике."""

import json
import os
import subprocess
import threading
from pathlib import Path

import numpy as np
import pytest

from glimpsy import paths
from glimpsy.editor import bgremove as bg

FFMPEG = paths.find_executable("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
MODEL = os.environ.get("GLIMPSY_TEST_MODNET", "")      # веса нейросети в тестах не скачиваем


def fake_mask(project_dir: Path, src: str, w=320, h=180, seconds=3):
    """Маска «человек слева»: левая половина белая, правая чёрная."""
    stem = bg.mask_stem(project_dir, src)
    stem.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c=black:size={w}x{h}:rate=30:d={seconds}", "-vf",
                    f"drawbox=x=0:y=0:w={w // 2}:h={h}:color=white:t=fill", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(stem.with_suffix(".mp4"))], check=True)
    m = np.zeros((seconds * 30, 27, 48), np.uint8)
    m[:, :, :24] = 255
    stem.with_suffix(".raw").write_bytes(m.tobytes())
    stem.with_suffix(".json").write_text(json.dumps({"w": 48, "h": 27, "fps": 30, "frames": seconds * 30}))
    return stem


def test_model_size_multiple_of_32():
    assert bg.model_size(1920, 1080) == (512, 288)
    assert bg.model_size(720, 1280) == (288, 512)


def test_preview_cut_and_blur(tmp_path, qt_app):
    from PySide6.QtGui import QColor, QImage

    img = QImage(96, 54, QImage.Format.Format_RGB32)
    img.fill(QColor("#FF0000"))
    mask = np.zeros((27, 48), np.uint8)
    mask[:, :24] = 255
    cut = bg.apply_preview(img, mask, "remove")
    assert cut.pixelColor(10, 20).alpha() == 255 and cut.pixelColor(85, 20).alpha() == 0
    blur = bg.apply_preview(img, mask, "blur")
    assert blur.pixelColor(85, 20).alpha() == 255


@needs_ffmpeg
def test_export_with_removed_background(tmp_path, qt_app):
    from types import SimpleNamespace

    from glimpsy.editor import export as ex
    from glimpsy.editor.overlay import OverlayItem
    from glimpsy.editor.project import Project
    from glimpsy.recorder.encoder import software_encoder

    main = tmp_path / "main.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:size=640x360:rate=30:d=3",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "3", "-c:v", "libx264", "-pix_fmt",
                    "yuv420p", "-c:a", "aac", str(main)], check=True)
    p = Project.for_videos(tmp_path / "projects", [main],
                           [SimpleNamespace(duration=3.0, has_audio=True, width=640, height=360, fps=30)])
    (p.dir / "media").mkdir()
    cam = p.dir / "media" / "cam.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:size=320x180:rate=30:d=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cam)], check=True)
    o = OverlayItem("cam", "video", "media/cam.mp4", 0.0, 3.0, width=320, height=180, src_duration=3.0, bg="remove")
    o.set_layout("16:9", 0.5, 0.5, 1.0)                                # на весь кадр
    p.overlays = [o]
    fake_mask(p.dir, o.src)
    layers = ex.render_overlay_layers(p, tmp_path / "ov")
    out = ex.export_project(FFMPEG, p, tmp_path / "out.mp4", software_encoder(), overlay_layers=layers)
    raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", "1", "-i", str(out), "-frames:v", "1", "-vf",
                          "scale=64:36", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    a = np.frombuffer(raw, np.uint8).reshape(36, 64, 3).astype(int)
    left, right = a[18, 8], a[18, 56]
    assert left[0] > 150 and left[2] < 100          # слева «человек» (красный)
    assert right[2] > 150 and right[0] < 100        # справа фон убран — видно синее видео под ним


@needs_ffmpeg
@pytest.mark.skipif(not MODEL, reason="нужны веса MODNet (GLIMPSY_TEST_MODNET)")
def test_make_mask_with_model(tmp_path, monkeypatch):
    monkeypatch.setattr(bg, "model_path", lambda: Path(MODEL))
    src = tmp_path / "cam.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:d=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    stem = tmp_path / "masks" / "cam"
    seen = []
    bg.make_mask(FFMPEG, src, stem, 640, 360, 2.0, seen.append, threading.Event())
    pm = bg.PreviewMask(stem)
    assert pm.frames == 60 and pm.at(1.0).shape == (pm.h, pm.w) and seen[-1] == 1.0
    assert stem.with_suffix(".mp4").exists()
