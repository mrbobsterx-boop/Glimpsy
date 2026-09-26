"""«Было → стало»: шторка, таймлапс, стоп-кадр."""

import subprocess

import numpy as np
import pytest

from glimpsy import paths
from glimpsy.editor import before_after as ba
from glimpsy.editor.project import Clip, Project

FFMPEG = paths.find_executable("ffmpeg")
pytestmark = pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")


def _project(tmp_path):
    for name, color in (("a.mp4", "red"), ("b.mp4", "blue")):
        subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={color}:size=320x180:rate=30",
                        "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(tmp_path / name)], check=True)
    clips = [Clip("a", "video", "a.mp4", 2, 0, 2, width=320, height=180),
             Clip("b", "video", "b.mp4", 2, 0, 2, width=320, height=180)]
    return Project(tmp_path, "t", clips)


def _rgb(path, t):
    raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(180, 320, 3).astype(int)


@pytest.mark.parametrize("mode", list(ba.MODES))
def test_modes(tmp_path, qt_app, mode):
    p = _project(tmp_path)
    clip = ba.make_clip(FFMPEG, p, mode, 3.0)
    out = p.path_of(clip)
    assert out.exists() and abs(clip.src_duration - 3.0) < 0.05 and "Было" in clip.label
    first, last = _rgb(out, 0.1), _rgb(out, 2.9)
    mid = first[120:170, 100:220].mean(axis=(0, 1))        # середина кадра, под плашками
    end = last[120:170, 100:220].mean(axis=(0, 1))
    assert mid[0] > 150 and mid[2] < 100, mid                # начало — «было» (красный)
    assert end[2] > 150 and end[0] < 100, end                # конец — «стало» (синий)
    if mode == "wipe":
        half = _rgb(out, 1.5)
        assert half[150, 20, 2] > 150 and half[150, 300, 0] > 150   # слева уже «стало», справа ещё «было»
