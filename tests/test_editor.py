"""Редактор: модель проекта, отмена, разбор файлов и настоящий экспорт через FFmpeg."""

import json
import subprocess
import time
from pathlib import Path

import pytest

from glimpsy import paths
from glimpsy.editor.export import atempo_chain, default_output, export_project
from glimpsy.editor.media import parse_probe
from glimpsy.editor.project import Clip, History, Project, list_projects
from glimpsy.recorder.encoder import software_encoder

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
        "output": str(tmp_path / "Glimpsy_2026-09-24_10-00.mp4"), "fps": 30,
        "clips": [{"file": "piece_0000.mp4", "duration": 3.5, "speed": 1.25, "recorded_at": time.time(),
                   "monitor": 2, "source_size": [1920, 1080], "priority": True, "cursor": []}]}))
    p = Project.load(tmp_path)
    assert p.clips[0].priority and p.clips[0].duration == 3.5 and "монитор 2" in p.clips[0].label
    p.set_speed(0, 2)
    p.save()
    again = Project.load(tmp_path)
    assert again.clips[0].speed == 2 and again.edited
    assert tmp_path in list_projects(tmp_path.parent)
    assert default_output(p, tmp_path).name == "Glimpsy_2026-09-24_10-00_edit.mp4"


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
    from glimpsy.editor.project import cover_zoom, frame_rect
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


def test_text_style_and_anim(qt_app):
    from glimpsy.editor.text import TextItem, anim_state, effective_style, placement, render_text
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
    from glimpsy.editor.export import render_text_layers
    from glimpsy.editor.text import TextItem
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
    from glimpsy.editor.text import DEFAULT_STYLE, render_text
    style = dict(DEFAULT_STYLE, bg=False, color="#ffffff", size=0.08)
    img = render_text("Скетч логотипа", style, 1920, 1080)
    right = sum(1 for x in range(img.width() * 3 // 4, img.width()) for y in range(img.height())
                if img.pixelColor(x, y).alpha() > 128)
    assert right > 50


def test_custom_font(qt_app, tmp_path, monkeypatch):
    import glob
    from glimpsy import paths as wpaths
    from glimpsy.editor import text as textmod
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
    from glimpsy.editor.export import render_overlay_layers
    from glimpsy.editor.overlay import OverlayItem

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
    from glimpsy.editor import motion
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
    from glimpsy.editor import motion
    expr = motion.piecewise([(0, 1.0), (1, 2.0), (2, 2.0)], 1)
    assert expr.startswith("if(lt(it,1.000)")


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
@pytest.mark.parametrize("mode,aspect", [("autozoom", "16:9"), ("follow", "9:16"), ("follow_hard", "9:16"),
                                         ("follow_zoom", "9:16"), ("region", "16:9"), ("cursor_zoom", "16:9")])
def test_export_with_motion(tmp_path, mode, aspect):
    # слева чёрное, справа белое; курсор всё время справа → кадр должен «уехать» вправо
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:size=640x360:rate=30,drawbox=x=320:y=0:w=320:h=360:color=white:t=fill",
                    "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(tmp_path / "s.mp4")], check=True)
    cursor = [[t / 10, 0.85, 0.5] for t in range(31)]
    c = Clip("a", "video", "s.mp4", 3, 0, 3, width=640, height=360, cursor=cursor, motion=mode, zoom_strength=2.5,
             region=[0.6, 0.4, 0.25, 0.25])
    p = Project(tmp_path, "t", [c], aspect=aspect)
    out = export_project(FFMPEG, p, tmp_path / "o.mp4", software_encoder())
    raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", "2.5", "-i", str(out), "-frames:v", "1",
                          "-vf", "scale=16:16,format=gray", "-f", "rawvideo", "-"], capture_output=True).stdout
    assert sum(raw) / len(raw) > 200, "в кадре должна остаться только белая (правая) часть"


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
@pytest.mark.parametrize("duck", [True, False])
def test_export_with_music(tmp_path, duck):
    from glimpsy.editor.music import MusicTrack, probe_audio
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:size=320x180:rate=30", "-t", "4", "-c:v", "libx264", str(tmp_path / "v.mp4")],
                   check=True)
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=440",
                    "-t", "1.5", str(tmp_path / "song.mp3")], check=True)     # трек короче ролика
    assert 1.3 < probe_audio(FFMPEG, tmp_path / "song.mp3") < 1.7
    p = Project(tmp_path, "t", [Clip("a", "video", "v.mp4", 4, 0, 4, width=320, height=180)])
    p.music = MusicTrack("song.mp3", 1.5, volume=0.8, fade_in=0.2, fade_out=0.5, duck=duck)
    out = export_project(FFMPEG, p, tmp_path / "o.mp4", software_encoder())

    def loud(ts):
        import array
        raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", str(ts), "-t", "0.2", "-i", str(out), "-vn",
                              "-f", "s16le", "-ac", "1", "-"], capture_output=True).stdout
        return max((abs(x) for x in array.array("h", raw[: len(raw) // 2 * 2])), default=0)
    assert loud(0.02) < loud(1.0)          # плавное появление
    assert loud(2.5) > 1000                # трек повторяется (он короче ролика)
    assert loud(3.95) < loud(2.5)          # плавное затухание в конце
    assert parse_probe(subprocess.run([FFMPEG, "-hide_banner", "-i", str(out)], capture_output=True,
                                      text=True).stderr).duration == pytest.approx(4.0, abs=0.2)


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_music_in_editor_window(tmp_path, qt_app):
    """Музыку добавляют, правят, отменяют и убирают через окно редактора."""
    import json
    from glimpsy.editor.window import EditorWindow
    proj = tmp_path / "project_20260925_101010"
    proj.mkdir()
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:size=320x180:rate=30",
                    "-t", "3", "-pix_fmt", "yuv420p", str(proj / "piece_0000.mp4")], check=True)
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440", "-t", "2",
                    str(tmp_path / "song.mp3")], check=True)
    (proj / "project.json").write_text(json.dumps({"output": str(tmp_path / "W.mp4"),
                                                   "clips": [{"file": "piece_0000.mp4", "duration": 3.0}]}))
    w = EditorWindow(proj, FFMPEG, software_encoder, tmp_path)
    try:
        w.insert_files([str(tmp_path / "song.mp3")], 1)        # перетащили mp3 на ленту
        assert w.project.music is not None and len(w.project.clips) == 1
        assert (proj / w.project.music.src).is_file()
        assert w.side.currentWidget() is w.music_panel
        w.music_panel.volume.setValue(25)
        assert w.project.music.volume == pytest.approx(0.25)
        w.undo()
        assert w.project.music.volume == pytest.approx(0.6)
        w.delete_selected()
        assert w.project.music is None and w.side.currentWidget() is w.inspector
        w.undo()
        assert w.project.music is not None
        w.project.save()
        assert json.loads((proj / "edit.json").read_text())["music"]["src"] == w.project.music.src
    finally:
        w.player.shutdown()
        w.close()


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_editor_single_key_shortcuts(tmp_path, qt_app):
    """Z/C/F/G/S/Delete в редакторе — в английской и в русской раскладке."""
    import json

    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    from glimpsy.editor.window import EditorWindow
    proj = tmp_path / "project_20260925_101010"
    proj.mkdir()
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:size=320x180:rate=30",
                    "-t", "3", "-pix_fmt", "yuv420p", str(proj / "piece_0000.mp4")], check=True)
    (proj / "project.json").write_text(json.dumps({"output": str(tmp_path / "W.mp4"), "clips": [
        {"file": "piece_0000.mp4", "duration": 3.0, "cursor": [[0.5, 0.2, 0.2]], "clicks": [[1.0, 0.2, 0.2]]}]}))
    w = EditorWindow(proj, FFMPEG, software_encoder, tmp_path)
    w.isActiveWindow = lambda: True
    w.show()

    def press(key, text):
        w.eventFilter(w, QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier, text))
    try:
        c = w.project.clips[0]
        assert c.motion == "none" and c.click_fx
        press(Qt.Key.Key_Z, "z")
        assert c.motion == "autozoom"
        press(0x44F, "я")                   # «я» — та же клавиша, что Z, в русской раскладке
        assert c.motion == "none"
        press(Qt.Key.Key_C, "c")
        assert not c.click_fx
        press(Qt.Key.Key_G, "g")
        assert c.frame_for(w.project.aspect)[0] > 1.0 or w.project.aspect == "16:9"
        press(Qt.Key.Key_F, "f")
        assert c.frame_for(w.project.aspect) == (1.0, 0.0, 0.0)
        w.player.seek(1.5)
        press(0x44B, "ы")                   # S в русской раскладке — разрезать
        assert len(w.project.clips) == 2
        press(Qt.Key.Key_Backspace, "")
        assert len(w.project.clips) == 1
    finally:
        w.player.shutdown()
        w.close()


def test_follow_variants_differ():
    from glimpsy.editor import motion

    # курсор перескакивает из левой части в правую на 1-й секунде и там немного «гуляет»
    cursor = [[t / 10, 0.2 if t < 10 else 0.8 + 0.03 * ((t % 4) - 2), 0.5 if t < 10 else 0.8] for t in range(40)]
    hard = motion.follow_track(cursor, 4.0, 1920, 1080, "follow_hard")
    soft = motion.follow_track(cursor, 4.0, 1920, 1080, "follow")
    zoom = motion.follow_track(cursor, 4.0, 1920, 1080, "follow_zoom")
    # жёстко — догоняет быстрее всех
    assert motion.value_at(hard, 1.4)[0] > motion.value_at(soft, 1.4)[0] > 0.3
    # «зона + зум» — крупнее и едет вниз за курсором; остальные по вертикали стоят
    assert zoom[0][3] == 1.5 and motion.value_at(zoom, 3.9)[1] > 0.6
    assert motion.value_at(soft, 3.9)[1] == 0.5
    x, y, w, h = motion.follow_crop(zoom, 3.9, 1920, 1080)
    assert 0 <= x and x + w <= 1.0001 and 0 <= y and y + h <= 1.0001 and abs(h - 1 / 1.5) < 1e-6


def test_scroll_and_cursor_zoom_tracks():
    from glimpsy.editor import motion

    down = motion.scroll_track("scroll_down", 1.0, 5.0)
    z, cx, cy = motion.value_at(down, 1.0)
    z2, cx2, cy2 = motion.value_at(down, 5.0)
    assert z == z2 == motion.SCROLL_ZOOM and cx == cx2 == 0.5 and cy < 0.4 and cy2 > 0.6
    left = motion.scroll_track("scroll_left", 0.0, 2.0)
    assert motion.value_at(left, 0.0)[1] > motion.value_at(left, 2.0)[1]
    track = motion.cursor_zoom_track([[t / 10, 0.9, 0.1] for t in range(30)], 3.0, 2.0)
    z, cx, cy = motion.value_at(track, 2.9)
    assert z == 2.0 and abs(cx - 0.75) < 0.01 and abs(cy - 0.25) < 0.01       # у края — упёрлись в границу
    # зум на область работает и без записи курсора (своё видео)
    c = Clip("a", "video", "x.mp4", 3, 0, 3, width=640, height=360, motion="region", region=[0.1, 0.1, 0.3, 0.3])
    assert c.motion_for("16:9") == "region"
    c.motion = "autozoom"
    assert c.motion_for("16:9") == "none"


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_library_panel_and_back_to_sessions(tmp_path, qt_app, monkeypatch):
    """Файлы с компьютера: папки, фильтр, «на ленту»; «Все записи» закрывает редактор и открывает список."""
    import json
    import time

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from glimpsy.editor.library_panel import list_folder
    from glimpsy.editor.window import EditorWindow

    QSettings("Glimpsy", "editor").remove("library_dir")
    lib = tmp_path / "lib"
    (lib / "Проект").mkdir(parents=True)
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30",
                    "-t", "2", "-pix_fmt", "yuv420p", str(lib / "screen.mp4")], check=True)
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440", "-t", "2",
                    str(lib / "song.mp3")], check=True)
    (lib / "notes.txt").write_text("не медиа")
    (lib / ".hidden.mp4").write_bytes(b"")
    dirs, files = list_folder(lib)
    assert [d.name for d in dirs] == ["Проект"] and sorted(f.name for f in files) == ["screen.mp4", "song.mp3"]
    assert [f.name for f in list_folder(lib, "audio")[1]] == ["song.mp3"]
    assert [f.name for f in list_folder(lib, query="SCR")[1]] == ["screen.mp4"]

    proj = tmp_path / "project_20260925_101010"
    proj.mkdir()
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:size=320x180:rate=30",
                    "-t", "3", "-pix_fmt", "yuv420p", str(proj / "piece_0000.mp4")], check=True)
    (proj / "project.json").write_text(json.dumps({"output": str(tmp_path / "W.mp4"),
                                                   "clips": [{"file": "piece_0000.mp4", "duration": 3.0}]}))
    opened = []
    w = EditorWindow(proj, FFMPEG, software_encoder, tmp_path, on_sessions=lambda: opened.append(1))
    try:
        w.library.set_open(True)
        w.library.open_folder(lib)
        names = [w.library.list.item(i).text().split("\n")[0] for i in range(w.library.list.count())]
        assert names[0] == "Проект" and set(names[1:]) == {"screen.mp4", "song.mp3"}
        mime = w.library.list.mimeData([w.library.list.item(i) for i in range(w.library.list.count())])
        assert sorted(u.fileName() for u in mime.urls()) == ["screen.mp4", "song.mp3"]   # папка не тащится
        video = next(w.library.list.item(i) for i in range(3) if w.library.list.item(i).text().startswith("screen"))
        w.library._on_double(video)                                  # двойной щелчок — на дорожку «Медиа»
        media = w.project.track_for("media")
        assert len(w.project.track_items(media.id)) == 1 and len(w.project.clips) == 1
        w.library._on_double(w.library.list.item(0))                 # папка — заходим внутрь
        assert w.library.folder == lib / "Проект"
        w.resize(1280, 800)
        w.show()
        w.library.open_folder(lib)
        end = time.time() + 3
        while time.time() < end and w.library._pending:
            QApplication.processEvents()
            time.sleep(0.05)
        import os
        if os.environ.get("GLIMPSY_SHOT"):
            w.grab().save(os.environ["GLIMPSY_SHOT"])
        w.back_to_sessions()
        assert opened == [1] and not w.isVisible()
    finally:
        w.library.set_open(False)
        w.close()


def test_history_skips_empty_steps_and_keeps_redo():
    """Снимки «на всякий случай» (щелчок без правки) не мешают Ctrl+Z и Ctrl+Y."""
    from glimpsy.editor.project import History

    h = History()
    h.push({"v": 1})                      # правка 1 → 2
    cur = {"v": 2}
    h.push(cur)                           # щелчок по тексту: снимок, но ничего не поменялось
    h.push(cur)
    assert h.undo(cur) == {"v": 1}        # сразу настоящая правка
    assert h.redo({"v": 1}) == {"v": 2}
    assert h.undo({"v": 2}) == {"v": 1}
    h.push({"v": 1})                      # снова щелчок без правки — повтор не пропадает
    assert h.can_redo and h.redo({"v": 1}) == {"v": 2}
    h.undo({"v": 2})
    h.settle({"v": 5})                    # а настоящая новая правка — отменяет «повтор»
    assert not h.can_redo


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_editor_keys_cut_jump_and_leaving_text(tmp_path, qt_app):
    """Ctrl+→/← — по склейкам; щелчок мимо поля текста или Esc — ввод закончен, клавиши снова работают."""
    import json

    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QPlainTextEdit

    from glimpsy.editor.window import EditorWindow
    proj = tmp_path / "project_20260925_101010"
    proj.mkdir()
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:size=320x180:rate=30",
                    "-t", "3", "-pix_fmt", "yuv420p", str(proj / "p.mp4")], check=True)
    (proj / "project.json").write_text(json.dumps({"output": str(tmp_path / "W.mp4"),
                                                   "clips": [{"file": "p.mp4", "duration": 3.0}] * 3}))
    w = EditorWindow(proj, FFMPEG, software_encoder, tmp_path)
    try:
        w.show()
        QApplication.processEvents()
        w.player.seek(1.0)
        QTest.keyClick(w, Qt.Key.Key_Right, Qt.KeyboardModifier.ControlModifier)
        assert w.player.t == pytest.approx(3.0) and w.timeline.selected == w.project.clips[1].id
        QTest.keyClick(w, Qt.Key.Key_Right, Qt.KeyboardModifier.ControlModifier)
        assert w.player.t == pytest.approx(6.0)
        QTest.keyClick(w, Qt.Key.Key_Left, Qt.KeyboardModifier.ControlModifier)
        assert w.player.t == pytest.approx(3.0)

        QTest.keyClick(w, Qt.Key.Key_T)
        box = QApplication.focusWidget()
        assert isinstance(box, QPlainTextEdit)
        QTest.keyClicks(box, "Hello")
        QTest.mouseClick(w.timeline, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))   # мимо поля
        assert not isinstance(QApplication.focusWidget(), QPlainTextEdit)
        n = len(w.project.texts)
        QTest.keyClick(QApplication.focusWidget() or w, Qt.Key.Key_T)            # T — снова «добавить текст»
        assert len(w.project.texts) == n + 1
        QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Escape)
        assert not isinstance(QApplication.focusWidget(), QPlainTextEdit)
        QTest.keyClick(QApplication.focusWidget() or w, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert len(w.project.texts) == n
    finally:
        w.close()


def test_tracks_model_and_old_projects(tmp_path):
    """У каждого рода — своя дорожка; старые проекты раскладываются по дорожкам сами."""
    from glimpsy.editor.overlay import OverlayItem, camera_item
    from glimpsy.editor.project import Project
    from glimpsy.editor.text import TextItem

    p = make_project(tmp_path)
    assert [t.kind for t in p.tracks] == ["subtitles", "text", "overlay"]
    old = p.to_dict()
    old.pop("tracks")
    old["texts"] = [vars(TextItem("a", "привет", 0, 1, auto=True)), vars(TextItem("b", "заголовок", 0, 1))]
    old["overlays"] = [vars(OverlayItem("o", "image", "x.png", 0, 1)),
                       vars(camera_item("c", "cam.mp4", 0, 1, 640, 360))]
    for d in old["texts"] + old["overlays"]:
        d.pop("track", None)
    old["overlays"][1]["track"] = "camera"
    q = Project(tmp_path, "t")
    q.restore(old)
    kinds = {x.id: q.track_by_id(x.track).kind for x in q.texts + q.overlays}
    assert kinds == {"a": "subtitles", "b": "text", "o": "overlay", "c": "camera"}
    assert [t.kind for t in q.tracks] == ["subtitles", "text", "overlay", "camera"]
    # новая дорожка текста встаёт после текстовых, медиа — между наложением и камерой
    t2 = q.add_track("text")
    m = q.add_track("media")
    assert [t.id for t in q.tracks] == ["subtitles", "text", t2.id, "overlay", m.id, "camera"]
    assert t2.name == "Текст 2" and q.track_for("text").id == "text"
    # рисуются снизу вверх: камера (ниже всех) — первой, наложение (выше) — поверх
    assert [o.id for o in q.overlays_by_depth()] == ["c", "o"]
    q.remove_track("camera")
    assert q.overlay_by_id("c") is None


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_timeline_tracks_add_move_drop(tmp_path, qt_app):
    """Лента: своя дорожка у каждого рода, перенос текста на другую дорожку, файлы на «Медиа», отмена."""
    import json
    import os

    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from glimpsy.editor.window import EditorWindow
    proj = tmp_path / "project_20260925_101010"
    proj.mkdir()
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:size=320x180:rate=30",
                    "-t", "6", "-pix_fmt", "yuv420p", str(proj / "p.mp4")], check=True)
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:size=320x180",
                    "-frames:v", "1", str(tmp_path / "pic.png")], check=True)
    (proj / "project.json").write_text(json.dumps({"output": str(tmp_path / "W.mp4"),
                                                   "clips": [{"file": "p.mp4", "duration": 6.0}]}))
    w = EditorWindow(proj, FFMPEG, software_encoder, tmp_path)
    try:
        w.resize(1280, 820)
        w.show()
        QApplication.processEvents()
        tl = w.timeline
        h0 = tl.needed_height()
        w.player.seek(1.0)
        w.add_text()
        text = w.project.texts[-1]
        assert w.project.track_by_id(text.track).kind == "text"
        t2 = tl.add_track("text")
        assert tl.needed_height() > h0
        # перетащили текст вниз, на «Текст 2»
        r = tl.lane_rects(text.track)[0]
        y2 = tl.row_y([t.id for t in w.project.tracks].index(t2.id)) + 10
        QTest.mousePress(tl, Qt.MouseButton.LeftButton, pos=r.center().toPoint())
        QTest.mouseMove(tl, QPoint(int(r.center().x()), int(y2)))
        QTest.mouseRelease(tl, Qt.MouseButton.LeftButton, pos=QPoint(int(r.center().x()), int(y2)))
        assert text.track == t2.id
        w.add_overlays([str(tmp_path / "pic.png")], 2.0, "media")
        media = w.project.track_for("media")
        ov = w.project.track_items(media.id)[0]
        assert ov.layout_for("16:9")[2] == pytest.approx(1.0, abs=0.01)     # медиа — на весь кадр
        w.add_overlays([str(tmp_path / "pic.png")], 2.5, media.id)           # в ту же дорожку
        assert len(w.project.track_items(media.id)) == 2 and len(w.project.tracks) == 5
        if os.environ.get("GLIMPSY_SHOT"):
            tl.fit()
            QApplication.processEvents()
            w.grab().save(os.environ["GLIMPSY_SHOT"])
        n = len(w.project.tracks)
        w.undo()
        w.undo()
        assert len(w.project.tracks) == n - 1                             # дорожка «Медиа» ушла вместе с файлом
    finally:
        w.close()


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_left_panels_and_subtitles_list(tmp_path, qt_app):
    """Панели слева открываются по одной; субтитры — списком, правка и удаление прямо в нём."""
    import json
    import os

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from glimpsy.editor.window import EditorWindow
    QSettings("Glimpsy", "editor").setValue("left_panel", "")
    proj = tmp_path / "project_20260925_101010"
    proj.mkdir()
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:size=320x180:rate=30",
                    "-t", "8", "-pix_fmt", "yuv420p", str(proj / "p.mp4")], check=True)
    (proj / "project.json").write_text(json.dumps({"output": str(tmp_path / "W.mp4"),
                                                   "clips": [{"file": "p.mp4", "duration": 8.0}]}))
    w = EditorWindow(proj, FFMPEG, software_encoder, tmp_path)
    try:
        w.resize(1280, 820)
        w.show()
        QApplication.processEvents()
        w.toggle_left("media")
        assert w.media_lib.isVisible() and not w.subs_panel.isVisible()
        w.toggle_left("subtitles")                                   # открылась другая — прошлая свернулась
        assert w.subs_panel.isVisible() and not w.media_lib.isVisible()
        assert QSettings("Glimpsy", "editor").value("left_panel") == "subtitles"
        from glimpsy.editor import subtitles as subs
        w._apply_subtitles([subs.Segment(0.5, 2.0, "Привет всем"), subs.Segment(3.0, 5.0, "Сегодня про звук")],
                           replace=True)
        table = w.subs_panel.table
        assert table.rowCount() == 2 and table.item(1, 1).text() == "Сегодня про звук"
        table.item(1, 1).setText("Сегодня про запись")               # исправили прямо в списке
        assert sorted(t.text for t in w.project.texts) == ["Привет всем", "Сегодня про запись"]
        w.player.seek(6.0)
        w.add_subtitle()
        assert table.rowCount() == 3
        tr = w.project.track_by_id(w.project.texts[-1].track)
        assert tr.kind == "subtitles"
        if os.environ.get("GLIMPSY_SHOT"):
            w.grab().save(os.environ["GLIMPSY_SHOT"])
        w.subs_panel.selected.emit(table.item(0, 0).data(0x0100))     # щелчок по строке — к месту
        assert w.timeline.selected_text == table.item(0, 0).data(0x0100)
        w._delete_texts([table.item(0, 0).data(0x0100)])
        assert table.rowCount() == 2
        w.undo()
        assert table.rowCount() == 3
        w.toggle_left("subtitles")
        assert not w.subs_panel.isVisible() and QSettings("Glimpsy", "editor").value("left_panel") == ""
    finally:
        w.close()
