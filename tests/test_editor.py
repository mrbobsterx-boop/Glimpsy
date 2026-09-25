"""Редактор: модель проекта, отмена, разбор файлов и настоящий экспорт через FFmpeg."""

import json
import subprocess
import time
from pathlib import Path

import pytest

from worklapse import paths
from worklapse.editor.export import atempo_chain, default_output, export_project
from worklapse.editor.media import parse_probe
from worklapse.editor.project import Clip, History, Project, list_projects
from worklapse.recorder.encoder import software_encoder

FFMPEG = paths.find_executable("ffmpeg")


def make_project(tmp_path, n=3, dur=4.0):
    return Project(tmp_path, "test", [Clip(f"c{i}", "video", f"p{i}.mp4", dur, 0.0, dur) for i in range(n)])


def test_locate_and_split(tmp_path):
    p = make_project(tmp_path)
    assert p.total == 12
    assert p.locate(5.0) == (1, 1.0)
    right = p.split(5.0)
    assert right == 2 and len(p.clips) == 4
    assert p.clips[1].out_s == 1.0 and p.clips[2].in_s == 1.0
    assert abs(p.total - 12) < 1e-9


def test_speed_changes_duration(tmp_path):
    p = make_project(tmp_path)
    p.set_speed(0, 4)
    assert p.clips[0].duration == 1.0
    p.set_speed(0, 50)
    assert p.clips[0].speed == 10          # максимум ×10


def test_move_and_trim(tmp_path):
    p = make_project(tmp_path)
    p.move(0, 3)
    assert [c.id for c in p.clips] == ["c1", "c2", "c0"]
    p.trim(0, -5, 99)                       # за пределы файла не выйти
    assert (p.clips[0].in_s, p.clips[0].out_s) == (0.0, 4.0)


def test_undo_redo_and_merge(tmp_path):
    p = make_project(tmp_path)
    h = History()
    h.push(p.to_dict())
    p.delete(0)
    h.push(p.to_dict(), key="speed:c1")
    p.set_speed(0, 2)
    h.push(p.to_dict(), key="speed:c1")    # та же правка подряд — одна запись
    p.set_speed(0, 3)
    p.restore(h.undo(p.to_dict()))
    assert len(p.clips) == 2 and p.clips[0].speed == 1
    p.restore(h.undo(p.to_dict()))
    assert len(p.clips) == 3
    p.restore(h.redo(p.to_dict()))
    assert len(p.clips) == 2


def test_load_stage1_project_and_save(tmp_path):
    (tmp_path / "project.json").write_text(json.dumps({
        "output": str(tmp_path / "Worklapse_2026-09-24_10-00.mp4"), "fps": 30,
        "clips": [{"file": "piece_0000.mp4", "duration": 3.5, "speed": 1.25, "recorded_at": time.time(),
                   "monitor": 2, "source_size": [1920, 1080], "priority": True, "cursor": []}]}))
    p = Project.load(tmp_path)
    assert p.clips[0].priority and p.clips[0].duration == 3.5 and "монитор 2" in p.clips[0].label
    p.set_speed(0, 2)
    p.save()
    again = Project.load(tmp_path)
    assert again.clips[0].speed == 2 and again.edited
    assert tmp_path in list_projects(tmp_path.parent)
    assert default_output(p, tmp_path).name == "Worklapse_2026-09-24_10-00_edit.mp4"


def test_atempo():
    assert atempo_chain(1.0) == ["atempo=1.00000"]
    assert atempo_chain(10.0)[:3] == ["atempo=2.0"] * 3


def test_parse_probe_phone_video():
    text = ("Duration: 00:00:12.50, start: 0.0\n Stream #0:0[0x1](und): Video: h264, yuv420p, 1920x1080, 30 fps\n"
            "    Side data:\n      displaymatrix: rotation of -90.00 degrees\n Stream #0:1: Audio: aac, 48000 Hz")
    info = parse_probe(text)
    assert (info.width, info.height, info.has_audio, info.duration) == (1080, 1920, True, 12.5)


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_real_export_both_formats(tmp_path):
    def gen(args, name):
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args, str(tmp_path / name)], check=True)
    gen(["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-f", "lavfi", "-i", "sine=f=440",
         "-t", "3", "-c:v", "libx264", "-c:a", "aac", "-shortest"], "land.mp4")
    gen(["-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30", "-t", "2", "-c:v", "libx264"], "vert.mp4")
    gen(["-f", "lavfi", "-i", "color=c=red:size=400x300", "-frames:v", "1"], "photo.png")
    p = Project(tmp_path, "t", [
        Clip("a", "video", "land.mp4", 3, 0.5, 2.5, speed=2.0, has_audio=True, width=640, height=360),
        Clip("b", "video", "vert.mp4", 2, 0, 2, width=360, height=640),
        Clip("c", "image", "photo.png", 3600, 0, 1.5, width=400, height=300),
    ])
    for aspect, size in (("16:9", "1920x1080"), ("9:16", "1080x1920")):
        p.aspect = aspect
        out = export_project(FFMPEG, p, tmp_path / f"out_{aspect.replace(':', 'x')}.mp4", software_encoder())
        info = subprocess.run([FFMPEG, "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
        assert size in info and "Audio: aac" in info
        dur = parse_probe(info).duration
        assert abs(dur - (1.0 + 2.0 + 1.5)) < 0.3, dur


def test_frame_geometry():
    from worklapse.editor.project import cover_zoom, frame_rect
    # горизонтальное видео в вертикальном кадре: вписано по центру
    x, y, w, h = frame_rect(1920, 1080, 1080, 1920, (1.0, 0.0, 0.0))
    assert (round(w), round(h), round(x)) == (1080, 608, 0) and abs(y - (1920 - 607.5) / 2) < 1
    z = cover_zoom(1920, 1080, 1080, 1920)
    _, _, w, h = frame_rect(1920, 1080, 1080, 1920, (z, 0.0, 0.0))
    assert round(h) == 1920 and w > 1080          # «заполнить» — без полей
    c = Clip("a", "video", "a.mp4", 1, 0, 1)
    c.set_frame("9:16", 2.0, 0.1, -0.2)
    assert c.frame_for("9:16") == (2.0, 0.1, -0.2) and c.frame_for("16:9") == (1.0, 0.0, 0.0)
    c.set_frame("9:16", 1.0, 0.0, 0.0)
    assert c.frames == {}


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_export_with_framing(tmp_path):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=white:size=640x360:rate=30", "-t", "1", "-c:v", "libx264", str(tmp_path / "w.mp4")],
                   check=True)
    c = Clip("a", "video", "w.mp4", 1, 0, 1, width=640, height=360)
    c.set_frame("9:16", 1.0, 0.0, -0.3)          # белый кадр поднят вверх
    p = Project(tmp_path, "t", [c], aspect="9:16")
    out = export_project(FFMPEG, p, tmp_path / "o.mp4", software_encoder())
    raw = subprocess.run([FFMPEG, "-loglevel", "error", "-i", str(out), "-frames:v", "1", "-vf", "scale=9:16,format=gray",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    rows = [sum(raw[r * 9:(r + 1) * 9]) / 9 for r in range(16)]
    brightest = rows.index(max(rows))
    assert brightest < 6, rows                    # белая полоса — в верхней части, а не по центру


@pytest.fixture(scope="module")
def qt_app():
    import os
    import sys
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_text_style_and_anim(qt_app):
    from worklapse.editor.text import TextItem, anim_state, effective_style, placement, render_text
    t = TextItem("t1", "Скетч", 1.0, 2.0)
    st = effective_style(t, {"color": "#ff0000"})
    assert st["color"] == "#ff0000" and st["bold"]           # общий стиль + стандартные значения
    t.style = {"color": "#00ff00"}
    assert effective_style(t, {"color": "#ff0000"})["color"] == "#00ff00"   # свой стиль важнее
    img = render_text("Скетч\nвторая строка", st, 1080, 1920)
    assert img.width() > 100 and img.height() > 100
    x, y = placement(img.width(), img.height(), (0.5, 0.99), 1080, 1920)
    assert y + img.height() <= 1920                            # не вылезает за край
    assert anim_state("fade", 0.0, 2.0)[0] == 0 and anim_state("fade", 1.0, 2.0)[0] == 1
    assert anim_state("slide", 0.0, 2.0)[1] > 0 and anim_state("pop", 0.0, 2.0)[2] < 1


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
@pytest.mark.parametrize("anim", ["fade", "slide", "pop", "none"])
def test_export_with_text(tmp_path, qt_app, anim):
    from worklapse.editor.export import render_text_layers
    from worklapse.editor.text import TextItem
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:size=640x360:rate=30", "-t", "2", "-c:v", "libx264", str(tmp_path / "b.mp4")],
                   check=True)
    p = Project(tmp_path, "t", [Clip("a", "video", "b.mp4", 2, 0, 2, width=640, height=360)])
    p.text_style = {"bg": False, "color": "#ffffff", "size": 0.2, "anim": anim}
    p.texts = [TextItem("x", "ТЕКСТ", 0.5, 1.2)]
    layers = render_text_layers(p, tmp_path / "layers")
    from PySide6.QtGui import QImage
    png = QImage(str(layers[0].png))
    opaque = [png.pixelColor(x, y) for x in range(png.width()) for y in range(png.height())
              if png.pixelColor(x, y).alpha() > 200]
    assert len(opaque) > 100, "текст не нарисовался в PNG"
    assert sum(c.lightness() for c in opaque) / len(opaque) > 200, "текст в PNG не белый"
    out = export_project(FFMPEG, p, tmp_path / "o.mp4", software_encoder(), text_layers=layers)

    def brightness(ts):
        raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", str(ts), "-i", str(out), "-frames:v", "1",
                              "-vf", "scale=32:18,format=gray", "-f", "rawvideo", "-"], capture_output=True).stdout
        return sum(raw) / max(1, len(raw))
    assert brightness(0.2) < 2           # до появления — чёрный кадр
    info = subprocess.run([FFMPEG, "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
    assert brightness(1.1) > 3, (layers[0], info[-600:])   # текст виден
    assert brightness(1.9) < 2           # после — снова пусто


def test_text_not_cut_off(qt_app):
    """Регрессия: второе слово не должно пропадать (раньше Qt переносил его на невидимую строку)."""
    from worklapse.editor.text import DEFAULT_STYLE, render_text
    style = dict(DEFAULT_STYLE, bg=False, color="#ffffff", size=0.08)
    img = render_text("Скетч логотипа", style, 1920, 1080)
    right = sum(1 for x in range(img.width() * 3 // 4, img.width()) for y in range(img.height())
                if img.pixelColor(x, y).alpha() > 128)
    assert right > 50


def test_custom_font(qt_app, tmp_path, monkeypatch):
    import glob
    from worklapse import paths as wpaths
    from worklapse.editor import text as textmod
    fonts = glob.glob("/usr/share/fonts/**/*.ttf", recursive=True) + glob.glob("C:/Windows/Fonts/*.ttf") \
        + glob.glob("/System/Library/Fonts/*.ttf")
    if not fonts:
        pytest.skip("в системе нет .ttf")
    monkeypatch.setattr(wpaths, "data_dir", lambda: tmp_path)
    family = textmod.add_font(Path(fonts[0]))
    assert family and (tmp_path / "fonts" / Path(fonts[0]).name).exists()
    assert family in textmod.load_custom_fonts()


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_export_with_overlays(tmp_path, qt_app):
    from worklapse.editor.export import render_overlay_layers
    from worklapse.editor.overlay import OverlayItem

    def gen(args, name):
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args, str(tmp_path / name)], check=True)
    gen(["-f", "lavfi", "-i", "color=c=black:size=640x360:rate=30", "-t", "3", "-c:v", "libx264"], "bg.mp4")
    gen(["-f", "lavfi", "-i", "color=c=white:size=400x300", "-frames:v", "1"], "logo.png")
    gen(["-f", "lavfi", "-i", "color=c=white:size=320x240:rate=30", "-f", "lavfi", "-i", "sine=f=880",
         "-t", "2", "-c:v", "libx264", "-c:a", "aac", "-shortest"], "phone.mp4")
    p = Project(tmp_path, "t", [Clip("a", "video", "bg.mp4", 3, 0, 3, width=640, height=360)])
    img = OverlayItem("i", "image", "logo.png", 0.5, 0.8, width=400, height=300, radius=0.2, shadow=True)
    img.set_layout("16:9", 0.25, 0.5, 0.3)                 # левая половина кадра
    vid = OverlayItem("v", "video", "phone.mp4", 1.6, 1.0, width=320, height=240, src_duration=2,
                      has_audio=True, radius=0.15, opacity=0.9)
    vid.set_layout("16:9", 0.75, 0.5, 0.3)                 # правая половина
    p.overlays = [img, vid]
    layers = render_overlay_layers(p, tmp_path / "layers")
    out = export_project(FFMPEG, p, tmp_path / "o.mp4", software_encoder(), overlay_layers=layers)

    def halves(ts):
        raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", str(ts), "-i", str(out), "-frames:v", "1",
                              "-vf", "scale=16:9,format=gray", "-f", "rawvideo", "-"], capture_output=True).stdout
        left = sum(raw[r * 16 + c] for r in range(9) for c in range(8)) / 72
        right = sum(raw[r * 16 + c] for r in range(9) for c in range(8, 16)) / 72
        return left, right
    l0, r0 = halves(0.2)
    l1, r1 = halves(0.9)
    l2, r2 = halves(2.1)
    assert l0 < 5 and r0 < 5                 # до наложений — чёрный кадр
    assert l1 > 20 and r1 < 5                # картинка слева
    assert l2 < 5 and r2 > 20                # видео справа, картинка уже исчезла
    info = subprocess.run([FFMPEG, "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
    assert "Audio: aac" in info
    # звук видео-наложения подмешан: в 2.1 с есть сигнал, в 0.5 с — тишина
    def loud(ts):
        raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", str(ts), "-t", "0.2", "-i", str(out), "-vn",
                              "-f", "s16le", "-ac", "1", "-"], capture_output=True).stdout
        import array
        a = array.array("h", raw[: len(raw) // 2 * 2])
        return max((abs(x) for x in a), default=0)
    assert loud(0.5) < 100 and loud(2.1) > 1000


def test_autozoom_track_zooms_on_still_cursor():
    from worklapse.editor import motion
    # первые 2 с курсор «рисует» в маленькой области справа вверху, потом мечется по экрану
    cursor = [[t / 10, 0.8 + 0.01 * (t % 3), 0.2] for t in range(0, 20)]
    cursor += [[2 + t / 10, (t * 0.37) % 1, (t * 0.61) % 1] for t in range(0, 20)]
    track = motion.autozoom_track(cursor, 4.0, 2.0)
    z_mid, cx_mid, cy_mid = motion.value_at(track, 1.3)
    z_end = motion.value_at(track, 4.0)[0]
    assert z_mid > 1.6 and cx_mid > 0.65 and cy_mid < 0.35      # приблизились к месту работы
    assert z_end < 1.3                                          # потом отдалились
    assert all(abs(b[1] - a[1]) < 0.3 for a, b in zip(track, track[1:]))   # плавно, без скачков


def test_piecewise_expression():
    from worklapse.editor import motion
    expr = motion.piecewise([(0, 1.0), (1, 2.0), (2, 2.0)], 1)
    assert expr.startswith("if(lt(it,1.000)")


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
@pytest.mark.parametrize("mode,aspect", [("autozoom", "16:9"), ("follow", "9:16")])
def test_export_with_motion(tmp_path, mode, aspect):
    # слева чёрное, справа белое; курсор всё время справа → кадр должен «уехать» вправо
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:size=640x360:rate=30,drawbox=x=320:y=0:w=320:h=360:color=white:t=fill",
                    "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(tmp_path / "s.mp4")], check=True)
    cursor = [[t / 10, 0.85, 0.5] for t in range(31)]
    c = Clip("a", "video", "s.mp4", 3, 0, 3, width=640, height=360, cursor=cursor, motion=mode, zoom_strength=2.5)
    p = Project(tmp_path, "t", [c], aspect=aspect)
    out = export_project(FFMPEG, p, tmp_path / "o.mp4", software_encoder())
    raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", "2.5", "-i", str(out), "-frames:v", "1",
                          "-vf", "scale=16:16,format=gray", "-f", "rawvideo", "-"], capture_output=True).stdout
    assert sum(raw) / len(raw) > 200, "в кадре должна остаться только белая (правая) часть"
