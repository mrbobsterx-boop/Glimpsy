"""Автомонтаж: темп, пустые фрагменты, зум время от времени, наезды, 9:16 и доли музыки."""

import subprocess

import pytest

from glimpsy import paths
from glimpsy.editor import automontage, motion
from glimpsy.editor.automontage import Options
from glimpsy.editor.project import Clip, Project

FFMPEG = paths.find_executable("ffmpeg")


def _clip(i, score, clicks=0, moving=True, priority=False, voice=False):
    cursor = [[t / 10, 0.3 + (0.02 * t if moving else 0), 0.5] for t in range(40)]
    return Clip(f"c{i}", "video", f"p{i}.mp4", 4, 0, 4, width=1920, height=1080, cursor=cursor,
                clicks=[[0.5 + k * 0.4, 0.5, 0.5] for k in range(clicks)], score=score, priority=priority,
                has_audio=voice, muted=not voice)


def test_apply_all(tmp_path):
    clips = [_clip(0, 0.5, 2), _clip(1, 0.0, 0, moving=False), _clip(2, 0.5, 1), _clip(3, 0.05, 0),
             _clip(4, 0.9, 6), _clip(5, 0.5, 0, priority=True), _clip(6, 0.02, 0, voice=True), _clip(7, 0.5, 1)]
    p = Project(tmp_path, "t", clips)
    rep = automontage.apply(p, Options())
    ids = [c.id for c in p.clips]
    assert "c1" not in ids and rep.dropped == 1                       # пустой убран
    assert "c6" in ids                                                # речь не трогаем, даже «тихую»
    by = {c.id: c for c in p.clips}
    assert by["c5"].motion == "pushin" and by["c5"].motion_raw("9:16") == "follow_zoom"
    assert by["c0"].motion_raw("9:16") == "follow"
    zoomed = [c.id for c in p.clips if c.motion == "autozoom"]
    assert 0 < len(zoomed) < len(p.clips) - 1                         # время от времени, а не везде
    assert by["c3"].speed > 1.3 and by["c4"].speed < 1.0 and by["c6"].speed == 1.0
    assert by["c5"].speed <= 1.0                                      # важное не ускоряется
    assert all(c.click_fx for c in p.clips if c.clicks)
    # повторный запуск не ускоряет ещё раз
    speeds = [c.speed for c in p.clips]
    automontage.apply(p, Options(drop_empty=False))
    assert [c.speed for c in p.clips] == speeds
    # в 16:9 «за курсором» не действует, в 9:16 — да
    assert by["c0"].motion_for("16:9") in ("autozoom", "none") and by["c0"].motion_for("9:16") == "follow"


def test_pushin_track_moves_in():
    track = motion.pushin_track([[t / 10, 0.8, 0.3] for t in range(30)], [], 0.0, 3.0)
    z0, *_ = motion.value_at(track, 0.0)
    z1, cx, cy = motion.value_at(track, 3.0)
    assert z0 == 1.0 and abs(z1 - motion.PUSHIN_ZOOM) < 1e-6 and cx > 0.6 and cy < 0.45


def test_align_to_beats():
    clips = [Clip(f"c{i}", "video", "x.mp4", 10, 0, d, width=1, height=1) for i, d in enumerate((2.1, 1.8, 2.3, 2.0))]
    beats = [k * 0.5 for k in range(20)]
    moved = automontage.align_to_beats(clips, beats, 0.5)
    ends, t = [], 0.0
    for c in clips[:-1]:
        t += c.duration
        ends.append(round(t, 3))
    assert moved == 3 and all(abs(e * 2 - round(e * 2)) < 1e-6 for e in ends), ends


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_detect_beats(tmp_path):
    f = tmp_path / "beat.wav"
    # 100 ударов в минуту: короткий щелчок каждые 0.6 с, первый — на 0.25 с
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i",
                    "aevalsrc='0.8*sin(2*PI*880*t)*lt(mod(t-0.25+6,0.6),0.04)':s=44100:d=12",
                    str(f)], check=True)
    beats, period = automontage.detect_beats(FFMPEG, f)
    assert abs(period - 0.6) < 0.03, period
    assert min(abs(beats[3] - (0.25 + 0.6 * k)) for k in range(20)) < 0.05, beats[:5]
