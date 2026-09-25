"""Потоки: только выбранные окна, каждое — в свой ролик."""

import json
import subprocess
import time

import numpy as np
import pytest

from glimpsy import paths
from glimpsy.platform.base import Monitor, WindowInfo
from glimpsy.recorder.streams import StreamSpec, crop_fraction, match, monitor_for, to_crop

FFMPEG = paths.find_executable("ffmpeg")


def test_stream_matching_modes():
    a = StreamSpec(1, "Проект А", "window", wid=42)
    b = StreamSpec(2, "Браузер", "app", app="chrome.exe")
    c = StreamSpec(3, "Проект Б", "title", title="проект б")
    assert match([a, b, c], WindowInfo("krita", "x", wid=42)) is a
    assert match([a, b, c], WindowInfo("chrome.exe", "Почта", wid=7)) is b
    assert match([a, b, c], WindowInfo("figma", "Макет — Проект Б", wid=9)) is c
    assert match([a, b, c], WindowInfo("figma", "Другое", wid=9)) is None
    assert match([a], None) is None
    assert StreamSpec.from_dict(c.to_dict()) == c


def test_crop_and_points():
    mon = Monitor(2, 1920, 0, 1920, 1080)
    # окно 960×540 в правой половине второго монитора; одно положение «выпало» (окно двигали)
    rects = [(1920 + 960, 0, 960, 540)] * 4 + [(1920 + 900, 10, 960, 540)]
    assert crop_fraction(rects, mon) == [0.5, 0.0, 0.5, 0.5]
    assert crop_fraction([(1920, 0, 1920, 1080)], mon) is None          # на весь экран — не режем
    assert crop_fraction([(-5000, 0, 100, 100)], mon) is None            # за краем
    pts = [[0.1, 0.75, 0.25], [0.2, 0.1, 0.9]]
    assert to_crop(pts, [0.5, 0.0, 0.5, 0.5]) == [[0.1, 0.5, 0.5]]      # точка вне окна отброшена
    mons = [Monitor(1, 0, 0, 1920, 1080), mon]
    assert monitor_for(mons, (2500, 100, 400, 300)) is mon
    assert monitor_for(mons, (1800, 100, 400, 300)) is mons[0] or monitor_for(mons, (1800, 100, 400, 300)) is mon
    assert monitor_for(mons, None) is None


def test_engine_switches_pools(tmp_path):
    from glimpsy.config import Settings
    from glimpsy.recorder.engine import RecorderEngine

    class Services:
        pass

    eng = RecorderEngine(Settings(), Services(), "ffmpeg")
    eng.session_dir = tmp_path
    eng._load_pools(tmp_path)
    eng.set_streams([StreamSpec(1, "А", "window", wid=5), StreamSpec(2, "Б", "title", title="проект б")])
    eng._set_streams(eng.streams)
    eng._check_stream(WindowInfo("x", "y", wid=5, rect=(0, 0, 100, 100)), time.time())
    assert eng._stream == 1 and not eng._waiting and eng.pool.dir == tmp_path / "stream_1"
    eng._check_stream(WindowInfo("x", "Проект Б — браузер", wid=6), time.time())
    assert eng._stream == 2 and eng.pool.dir == tmp_path / "stream_2"
    eng._check_stream(WindowInfo("x", "почта", wid=8), time.time())
    assert eng._waiting                                              # чужое окно — запись ждёт
    assert json.loads((tmp_path / "streams.json").read_text()) == {"1": "А", "2": "Б"}
    eng.set_streams([])
    eng._set_streams([])
    assert eng._stream == 0 and not eng._waiting and eng.pool.dir == tmp_path


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_assembly_crops_window(tmp_path):
    """В ролик попадает только окно: левая половина кадра (красная) отрезана, остаётся синяя."""
    from glimpsy.assembler import Assembler
    from glimpsy.config import Settings
    from glimpsy.recorder.candidates import Candidate
    from glimpsy.recorder.encoder import software_encoder
    from glimpsy.recorder.pacing import make_plan

    sess = tmp_path / "session" / "stream_1"
    sess.mkdir(parents=True)
    f = sess / "c.ts"
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:size=320x180:rate=30",
                    "-f", "lavfi", "-i", "color=c=blue:size=320x180:rate=30", "-filter_complex", "[0][1]hstack",
                    "-t", "4", "-c:v", "libx264", "-g", "30", "-bf", "0", "-f", "mpegts", str(f)], check=True)
    now = time.time()
    c = Candidate(id=1, file="c.ts", wall_start=now, wall_end=now + 4, want_start=now, want_end=now + 4, monitor=1,
                  width=640, height=180, score=0.5, activity=[0.5] * 4, crop=[0.5, 0.0, 0.5, 1.0],
                  cursor=[[1.0, 0.5, 0.5]], clicks=[[1.5, 0.5, 0.5]])
    s = Settings(output_dir=str(tmp_path / "out"), target_length_s=3, clip_min_s=3, clip_max_s=3, pace="calm",
                 output_width=320, output_height=180).validate()
    out = Assembler(FFMPEG, software_encoder(), s, make_plan(s)).run(
        sess, [c], now, project_dir=tmp_path / "p", name="Проект: А", shared_dir=tmp_path / "session")
    assert out.name.endswith("_Проект_ А.mp4")
    raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", "0.5", "-i", str(out), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    img = np.frombuffer(raw, np.uint8).reshape(180, 320, 3).astype(int)
    # кадр 320×180 без полей, почти весь синий (круг клика немного жёлтый — края берём вне его)
    corner = img[:30, :30].reshape(-1, 3).mean(axis=0)
    assert corner[2] > 150 and corner[0] < 80, corner
    meta = json.loads((tmp_path / "p" / "project.json").read_text())
    assert meta["stream"] == "Проект: А" and meta["clips"][0]["source_size"] == [320, 180]
