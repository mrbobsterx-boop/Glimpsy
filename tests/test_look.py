"""Картинка: цвет (в просмотре и в готовом ролике одинаковый) и стабилизация."""

import subprocess
from types import SimpleNamespace

import numpy as np
import pytest

from glimpsy import paths
from glimpsy.editor import export as ex
from glimpsy.editor import look
from glimpsy.editor.project import Clip, Project
from glimpsy.recorder.encoder import software_encoder

FFMPEG = paths.find_executable("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")


def frames(path, n=None, size="320x180"):
    w, h = map(int, size.split("x"))
    raw = subprocess.run([FFMPEG, "-loglevel", "error", "-i", str(path), "-vf", f"scale={w}:{h}", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    a = np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3)
    return a[:n] if n else a


def test_filters_and_clamp():
    p = SimpleNamespace(look={"brightness": 99, "saturation": -50})
    s = look.settings(p)
    assert s["brightness"] == 50 and s["stabilize"] == 0
    assert look.color_filter(p).startswith("eq=brightness=0.200:contrast=1.000:saturation=0.000")
    assert look.color_filter(SimpleNamespace(look={})) == ""
    assert "colorchannelmixer" in look.color_filter(SimpleNamespace(look={"warmth": 20}))
    assert look.stabilize_filter(SimpleNamespace(look={"stabilize": 50}), "a.trf", True).startswith(
        "vidstabtransform=input=a.trf:smoothing=22")
    assert look.stabilize_filter(SimpleNamespace(look={"stabilize": 50}), None, False) == "deshake"


@needs_ffmpeg
@pytest.mark.parametrize("preset", ["Ярче", "Кино", "Тёплый", "Чёрно-белый"])
def test_preview_matches_export(tmp_path, qt_app, preset):
    from PySide6.QtGui import QImage

    src = tmp_path / "src.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:d=2",
                    "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(src)], check=True)
    p = SimpleNamespace(look=dict(look.PRESETS[preset]))
    out = tmp_path / "out.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-i", str(src), "-vf", look.color_filter(p) + ",format=yuv420p",
                    "-c:v", "libx264", "-crf", "12", str(out)], check=True)
    a, b = frames(src, 1)[0], frames(out, 1)[0]
    img = QImage(a.tobytes(), 320, 180, 320 * 3, QImage.Format.Format_RGB888).copy()
    shown = look.apply_qimage(img, p).convertToFormat(QImage.Format.Format_RGB888)
    s = np.frombuffer(shown.constBits(), np.uint8, count=shown.bytesPerLine() * 180).reshape(180, -1)[:, :960]
    s = s.reshape(180, 320, 3).astype(int)
    assert np.abs(s.mean(axis=(0, 1)) - b.astype(int).mean(axis=(0, 1))).max() < 8      # средний цвет — тот же
    assert np.abs(s - b.astype(int)).mean() < 14


@needs_ffmpeg
def test_stabilize_reduces_shake(tmp_path):
    if not look.has_vidstab(FFMPEG):
        pytest.skip("в этом FFmpeg нет vid.stab")
    src = tmp_path / "shaky.mp4"
    # неподвижная картинка, кадр дрожит: окно 640x360 прыгает по большому кадру
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "smptehdbars=size=800x450:rate=30:d=4", "-f", "lavfi", "-i", "nullsrc=s=800x450:d=4",
                    "-filter_complex", "[0:v]drawgrid=w=40:h=40:t=2:c=white@0.8,"
                    "crop=w=640:h=360:x='80+40*sin(n*1.3)':y='45+25*cos(n*1.9)'[v]", "-map", "[v]",
                    "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", str(src)], check=True)
    proj = Project.for_videos(tmp_path / "projects", [src],
                              [SimpleNamespace(duration=4.0, has_audio=False, width=640, height=360, fps=30)])
    proj.clips = [Clip("a", "video", proj.clips[0].src, 4.0, 0.0, 4.0, width=640, height=360)]
    plain = ex.export_project(FFMPEG, proj, tmp_path / "plain.mp4", software_encoder())
    proj.look = {"stabilize": 100}
    stable = ex.export_project(FFMPEG, proj, tmp_path / "stable.mp4", software_encoder())

    def shake(path):
        f = frames(path).astype(np.float32)[15:-15]          # края: стабилизатор разгоняется
        return float(np.abs(np.diff(f, axis=0)).mean())

    assert shake(stable) < shake(plain) * 0.6, (shake(plain), shake(stable))


@needs_ffmpeg
def test_picture_controls_in_editor(tmp_path, qt_app):
    import json

    from PySide6.QtGui import QImage

    from glimpsy.editor.window import EditorWindow

    proj = tmp_path / "project_20260925_101010"
    proj.mkdir()
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:size=320x180:rate=30",
                    "-t", "2", "-pix_fmt", "yuv420p", str(proj / "piece_0000.mp4")], check=True)
    (proj / "project.json").write_text(json.dumps({"output": str(tmp_path / "W.mp4"),
                                                   "clips": [{"file": "piece_0000.mp4", "duration": 2.0}]}))
    w = EditorWindow(proj, FFMPEG, software_encoder, tmp_path)
    try:
        p = w.enhance
        p.preset.setCurrentIndex(p.preset.findData("Тёплый"))
        p._on_preset(0)
        assert w.project.look["warmth"] == 30 and p.color["warmth"].value() == 30
        p.stab.setValue(40)
        assert w.project.look["stabilize"] == 40
        gray = QImage(64, 36, QImage.Format.Format_RGB32)
        gray.fill(0xFF808080)
        w._show_frame(gray)
        shown = w.preview.image
        assert shown.pixelColor(5, 5).red() > shown.pixelColor(5, 5).blue()         # теплее
        w._on_compare(True)
        assert w.preview.image.pixelColor(5, 5).red() == 128                       # «как снято»
        w._on_compare(False)
        w.undo()
        assert w.project.look.get("stabilize", 0) == 0 and p.stab.value() == 0
        w.undo()
        assert w.project.look.get("warmth", 0) == 0 and p.preset.currentData() == "Как снято"
    finally:
        w.player.shutdown()
        w.close()
