"""Обложка: удачные кадры, заголовок, своя картинка, сохранение."""

import subprocess
from types import SimpleNamespace

import pytest

from glimpsy import paths
from glimpsy.editor import cover
from glimpsy.editor.project import Clip, Project

FFMPEG = paths.find_executable("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")


def project(tmp_path):
    video = tmp_path / "v.mp4"
    # первые 4 с — почти чёрные, потом — яркая резкая картинка
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=0x050505:size=640x360:rate=30:d=4",
                    "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:d=8", "-filter_complex",
                    "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
                   check=True)
    p = Project.for_videos(tmp_path / "projects", [video],
                           [SimpleNamespace(duration=12.0, has_audio=False, width=640, height=360, fps=30)])
    p.clips = [Clip("a", "video", p.clips[0].src, 12.0, 0.0, 12.0, width=640, height=360)]
    return p


@needs_ffmpeg
def test_candidates_prefer_good_frames(tmp_path, qt_app):
    p = project(tmp_path)
    frames = cover.candidates(FFMPEG, p, samples=12, pick=4)
    assert 1 <= len(frames) <= 4
    assert all(t > 4.0 for t, _img in frames[:3])                   # тёмное начало не выбрано


def test_compose_sizes_and_text(qt_app):
    from PySide6.QtGui import QColor, QImage

    bg = QImage(320, 180, QImage.Format.Format_RGB32)
    bg.fill(QColor("#3060A0"))
    plain = cover.compose(bg, dict(cover.DEFAULTS), "16:9")
    assert (plain.width(), plain.height()) == (1280, 720)
    titled = cover.compose(bg, dict(cover.DEFAULTS, title="Обзор камеры", color="#FF0000"), "9:16")
    assert (titled.width(), titled.height()) == (1080, 1920)
    reds = sum(1 for x in range(0, 1080, 6) for y in range(960, 1920, 6)
               if titled.pixelColor(x, y).red() > 200 and titled.pixelColor(x, y).green() < 60)
    assert reds > 50                                                 # заголовок внизу


@needs_ffmpeg
def test_cover_dialog_save(tmp_path, qt_app, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QTimer
    from PySide6.QtWidgets import QFileDialog

    from glimpsy.editor.cover_dialog import CoverDialog

    p = project(tmp_path)
    dlg = CoverDialog(FFMPEG, p, tmp_path)
    t = QTimer()
    t.setSingleShot(True)
    t.start(20000)
    while not dlg.frames and t.isActive():
        QCoreApplication.processEvents()
    assert dlg.frames and not dlg.background.isNull()
    dlg.title.setText("Заголовок")
    target = tmp_path / "обложка.jpg"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), "")))
    dlg._save()
    assert target.exists() and dlg.saved == target
    opts = dlg.result_opts()
    assert opts["title"] == "Заголовок" and opts["t"] is not None
    dlg.close()
