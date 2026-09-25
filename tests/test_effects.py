"""Эффекты «как в Screen Studio»: клики записываются, кадр наезжает на клик, круг на месте клика."""

import subprocess

import numpy as np
import pytest

from glimpsy import paths
from glimpsy.editor import clicks, motion
from glimpsy.recorder.activity import ActivityTracker

FFMPEG = paths.find_executable("ffmpeg")


def test_click_positions_from_cursor_track():
    act = ActivityTracker(listen_input=False)
    for i in range(30):                                   # курсор 10 раз в секунду на мониторе 1
        act.add_cursor(100 + i / 10, 1, 0.1 + i / 100, 0.5)
    act.click_times.extend([101.02, 150.0])               # второй клик — когда курсора не было
    got = act.clicks_between(100, 103, monitor=1)
    assert len(got) == 1 and got[0][0] == 101.02 and got[0][1] == pytest.approx(0.2, abs=0.011)
    assert act.clicks_between(100, 103, monitor=2) == []


def test_autozoom_goes_to_click():
    # курсор мечется по всему экрану (сам по себе зума бы не было), но в t=2 — клик в углу
    cursor = [[t / 10, (t * 37 % 100) / 100, (t * 53 % 100) / 100] for t in range(60)]
    track = motion.autozoom_track(cursor, 6.0, 2.0, clicks=[[2.0, 0.85, 0.2]])
    z, cx, cy = motion.value_at(track, 2.6)
    assert z > 1.6 and cx > 0.65 and cy < 0.4
    z_far, *_ = motion.value_at(track, 5.8)
    assert z_far < z


def test_active_ripples():
    taps = [[1.0, 0.3, 0.4]]
    assert clicks.active(taps, 0.9, 1.0) == []
    (x, y, prog), = clicks.active(taps, 1.0 + clicks.RIPPLE_S / 2, 1.0)
    assert (x, y) == (0.3, 0.4) and prog == pytest.approx(0.5)


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_ripple_timing_on_cut_ts(tmp_path):
    """Круг появляется ровно в момент клика, даже если фрагмент вырезан из середины .ts."""
    src = tmp_path / "c.ts"
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x404040:size=640x360:rate=30",
                    "-t", "4", "-c:v", "libx264", "-g", "30", "-bf", "0", "-f", "mpegts", str(src)], check=True)
    graph = clicks.ripple_graph("0:v", "v", [[1.5, 0.5, 0.5]], 1.0, 3.5, 1.0, 640)   # клик через 0.5 с после начала
    out = tmp_path / "o.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-ss", "1.0", "-t", "2.5", "-i", str(src), "-filter_complex", graph,
                    "-map", "[v]", "-pix_fmt", "yuv420p", str(out)], check=True)

    def yellow(t):
        raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", str(t), "-i", str(out), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
        img = np.frombuffer(raw, np.uint8).reshape(360, 640, 3).astype(int)
        return int(((img[..., 0] - img[..., 2]) > 80).sum())
    assert yellow(0.3) == 0
    assert yellow(0.65) > 50
    assert yellow(1.3) == 0


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_autozoom_filter_on_ts_does_not_explode(tmp_path):
    """Раньше zoompan на .ts после -ss выдавал кадры с одинаковым временем и съедал память."""
    src = tmp_path / "c.ts"
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                    "-t", "4", "-c:v", "libx264", "-g", "30", "-bf", "0", "-f", "mpegts", str(src)], check=True)
    track = motion.autozoom_track([[t / 10, 0.3, 0.3] for t in range(40)], 4.0, 1.8)
    zp = motion.autozoom_filter(track, 0.5, 3.5, 640, 360, 30)
    r = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", "0.5", "-t", "3", "-i", str(src), "-filter_complex",
                        f"[0:v]{zp},setpts=(PTS-STARTPTS)/1.25,fps=30[v]", "-map", "[v]", "-f", "null", "-"],
                       capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode()[-300:]
