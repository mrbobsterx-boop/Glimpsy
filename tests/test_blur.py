"""Размытие части кадра: прямоугольник тянется за края, размыто и в просмотре, и в готовом ролике."""

import subprocess
from types import SimpleNamespace

import numpy as np
import pytest

from glimpsy import paths
from glimpsy.editor.overlay import OverlayItem, blur_box, blur_item, overlay_rect

FFMPEG = paths.find_executable("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")


def test_blur_box_has_free_width_and_height():
    o = blur_item("b", 1.0, 5.0, "16:9", [0.1, 0.2, 0.3, 0.1])
    x, y, w, h = overlay_rect(o, "16:9", 1920, 1080)
    assert (round(x), round(y), round(w), round(h)) == (192, 216, 576, 108)
    o.set_layout("16:9", 0.5, 0.5, 0.2)                    # сдвинули/сменили ширину — высота осталась
    assert overlay_rect(o, "16:9", 1920, 1080)[3] == pytest.approx(108)
    assert blur_box(o, "16:9", 1920, 1080) == (768, 486, 384, 108)


def test_other_overlays_keep_their_proportions():
    o = OverlayItem("i", "image", "a.png", 0, 3, width=200, height=100)
    o.set_layout("16:9", 0.5, 0.5, 0.5)
    assert o.height_for("16:9") is None
    assert overlay_rect(o, "16:9", 1920, 1080)[3] == pytest.approx(480)


def test_blur_outside_frame_is_skipped():
    o = blur_item("b", 0, 2, "16:9", [1.2, 0.2, 0.2, 0.2])
    assert blur_box(o, "16:9", 1920, 1080) is None


def test_drag_edges():
    from glimpsy.editor.preview import _drag_edges, _edge_at
    from PySide6.QtCore import QPointF, QRectF

    assert _drag_edges((0.1, 0.1, 0.5, 0.5), "r", 0.2, 0.3) == pytest.approx((0.1, 0.1, 0.7, 0.5))
    assert _drag_edges((0.1, 0.1, 0.5, 0.5), "tl", 0.1, 0.05) == pytest.approx((0.2, 0.15, 0.5, 0.5))
    x0, _y0, x1, _y1 = _drag_edges((0.1, 0.1, 0.5, 0.5), "l", 0.9, 0)       # дальше правого края — не выворачивает
    assert x1 - x0 == pytest.approx(0.02)
    r = QRectF(100, 100, 200, 100)
    assert _edge_at(r, QPointF(100, 150)) == "l"
    assert _edge_at(r, QPointF(300, 200)) == "br"
    assert _edge_at(r, QPointF(200, 101)) == "t"
    assert _edge_at(r, QPointF(200, 150)) == ""


def test_preview_blurs_what_is_under(qt_app):
    from PySide6.QtGui import QColor, QImage

    from glimpsy.editor.preview import PreviewWidget

    from PySide6.QtGui import QPainter

    img = QImage(320, 180, QImage.Format.Format_RGB32)
    img.fill(QColor("#000000"))
    painter = QPainter(img)                                 # мелкие полоски: после размытия — серое
    for x in range(0, 320, 2):                              # (не setPixelColor: в PySide6 6.12 он
        painter.fillRect(x, 0, 1, 180, QColor("#FFFFFF"))   #  портит счётчик None и роняет Python)
    painter.end()
    w = PreviewWidget()
    w.resize(344, 204)
    w.set_image(img)
    o = blur_item("b", 0, 5, "16:9", [0.0, 0.0, 0.5, 1.0])
    w.set_overlays([(o, None)], None)
    shot = w.grab().toImage()
    c = w.canvas_rect()
    left = [shot.pixelColor(int(c.x() + c.width() * 0.25) + d, int(c.center().y())).red() for d in range(4)]
    right = [shot.pixelColor(int(c.x() + c.width() * 0.75) + d, int(c.center().y())).red() for d in range(4)]
    assert max(left) - min(left) < 60                       # размыто: соседние точки почти одинаковые
    assert max(right) - min(right) > 150                    # справа полоски как были


@needs_ffmpeg
@pytest.mark.parametrize("mosaic", [False, True])
def test_export_blurs_only_the_box(tmp_path, qt_app, mosaic):
    from glimpsy.editor import export as ex
    from glimpsy.editor.project import Project
    from glimpsy.recorder.encoder import software_encoder

    main = tmp_path / "main.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:size=640x360:rate=30:d=2", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-vf", "drawgrid=w=4:h=640:t=2:c=white", "-t", "2",
                    "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", "-c:a", "aac", str(main)], check=True)
    p = Project.for_videos(tmp_path / "projects", [main],
                           [SimpleNamespace(duration=2.0, has_audio=True, width=640, height=360, fps=30)])
    o = blur_item("b", 0.0, 2.0, "16:9", [0.0, 0.0, 0.5, 1.0])
    o.mosaic = mosaic
    o.strength = 1.0
    p.overlays = [o]
    out = ex.export_project(FFMPEG, p, tmp_path / "out.mp4", software_encoder(),
                            overlay_layers=ex.render_overlay_layers(p, tmp_path / "ov"))
    raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", "1", "-i", str(out), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
    H = len(raw) // 1920
    a = np.frombuffer(raw, np.uint8).reshape(H, 1920).astype(int)
    row = a[H // 2]
    assert row[200:260].std() < 25          # слева размыто
    assert row[1500:1560].std() > 60        # справа полоски чёткие


@needs_ffmpeg
def test_blur_in_editor_window(tmp_path, qt_app):
    """Кнопка «Размыть»: обвели область — появилось размытие; края тянутся, правки отменяются."""
    import json

    from glimpsy.editor.window import EditorWindow
    from glimpsy.recorder.encoder import software_encoder

    proj = tmp_path / "project_20260925_101010"
    proj.mkdir()
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:size=320x180:rate=30",
                    "-t", "8", "-pix_fmt", "yuv420p", str(proj / "piece_0000.mp4")], check=True)
    (proj / "project.json").write_text(json.dumps({"output": str(tmp_path / "W.mp4"),
                                                   "clips": [{"file": "piece_0000.mp4", "duration": 8.0}]}))
    w = EditorWindow(proj, FFMPEG, software_encoder, tmp_path)
    try:
        w.add_blur()
        assert w.preview._pick and w.preview._pick_canvas
        w.preview._end_pick([0.1, 0.2, 0.3, 0.1])
        [o] = w.project.overlays
        assert o.kind == "blur" and o.duration == pytest.approx(5.0)
        assert w.timeline.selected_overlay == o.id and w.side.currentWidget() is w.overlay_panel
        assert overlay_rect(o, "16:9", 1920, 1080)[2:] == pytest.approx((576, 108))
        w.preview.overlay_box.emit(o.id, 0.5, 0.5, 0.4, 0.3)          # потянули за угол
        assert overlay_rect(o, "16:9", 1920, 1080)[2:] == pytest.approx((768, 324))
        w.overlay_panel.strength.setValue(90)
        w.overlay_panel.mosaic.setCurrentIndex(1)
        assert o.strength == pytest.approx(0.9) and o.mosaic
        w.preview.grab()                                              # рисуется без ошибок
        w.project.save()
        saved = json.loads((proj / "edit.json").read_text())["overlays"][0]
        assert saved["kind"] == "blur" and saved["mosaic"] is True
        w.undo()
        w.undo()
        assert not w.project.overlay_by_id(o.id).mosaic
    finally:
        w.player.shutdown()
        w.close()
