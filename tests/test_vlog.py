"""Автомонтаж влога: оценка картинки, план (речь + красивые кадры), форматы."""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from glimpsy.editor import transcript as tr
from glimpsy.editor import vlog
from glimpsy.editor.transcript import SourceWords, Word

FFMPEG = __import__("glimpsy.paths", fromlist=["x"]).find_executable("ffmpeg")


def test_frame_stats_sharp_bright_change():
    rng = np.random.default_rng(1)
    noisy = rng.integers(0, 255, (vlog.SIDE, vlog.SIDE)).astype(np.uint8)          # резкий, «живой»
    flat = np.full((vlog.SIDE, vlog.SIDE), 128, np.uint8)                          # ровный
    dark = np.full((vlog.SIDE, vlog.SIDE), 5, np.uint8)
    v = vlog.frame_stats(np.stack([flat, flat, noisy, dark]), np.array([0, 1, 2, 3], np.float32))
    assert v.sharp[2] > v.sharp[0] * 100
    assert v.bright[3] < 0.05 and v.change[1] == 0 and v.change[2] > 0.1


def test_shot_scores_prefer_sharp_lit_steady():
    t = np.arange(6, dtype=np.float32)
    vis = vlog.Visual(t, sharp=np.array([0.001, 0.001, 0.05, 0.05, 0.05, 0.05], np.float32),
                      bright=np.array([0.5, 0.5, 0.5, 0.5, 0.02, 0.5], np.float32),
                      change=np.array([0.0, 0.0, 0.05, 0.05, 0.05, 0.9], np.float32))
    sc = vlog.shot_scores(vis)
    assert sc[2] > sc[0] and sc[2] > sc[4]                # резкий и светлый лучше смазанного и тёмного
    w = vlog.best_window(vis, sc, 0.0, 6.0, 2.0)
    assert 1.0 <= w[0] <= 3.0 and w[1] - w[0] == 2.0


def make_words():
    # речь 0–8 с (две фразы), долгая прогулка без речи 8–40 с, фраза 40–44 с
    return [Word("Привет,", 0.5, 1.0), Word("сегодня", 1.1, 1.6), Word("поход.", 1.7, 2.3),
            Word("Идём", 4.0, 4.4), Word("в", 4.5, 4.6), Word("горы!", 4.7, 5.5),
            Word("Вот", 40.2, 40.6), Word("вершина.", 40.7, 41.6)]


def make_visual():
    t = np.arange(0, 45, 1.0, dtype=np.float32)
    sharp = np.full(len(t), 0.002, np.float32)
    sharp[(t >= 22) & (t <= 27)] = 0.08                  # красивое место — 22–27 с
    return vlog.Visual(t, sharp, np.full(len(t), 0.5, np.float32), np.full(len(t), 0.05, np.float32))


def test_plan_vlog_keeps_speech_and_adds_best_shots():
    src = "/v.mp4"
    sources = [{"src": src, "duration": 45.0}]
    p = vlog.plan("vlog", sources, lambda s: make_words(), lambda s: None, lambda s: make_visual(),
                  lambda s: set())
    assert not p.deleted                                    # вся речь осталась
    shots = p.broll[src]
    assert shots and any(20 <= a <= 27 for a, _b in shots)   # лучший кадр — из красивого места
    assert all(b - a == pytest.approx(4.0) for a, b in shots)
    assert all(not (a < 41.6 and b > 40.2) for a, b in shots)  # не поверх речи


def test_plan_short_fits_target():
    src = "/v.mp4"
    sources = [{"src": src, "duration": 45.0}]
    p = vlog.plan("short", sources, lambda s: make_words(), lambda s: None, lambda s: make_visual(),
                  lambda s: set(), target_s=6.0)
    assert p.length <= 8.0 and p.talk > 0 and p.deleted          # лишние фразы удалены
    assert all(b - a == pytest.approx(2.0) for a, b in p.broll.get(src, []))


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_analyze_visual_real_video(tmp_path):
    video = tmp_path / "walk.mp4"
    # 0–4 с серый экран, 4–8 с «пейзаж» (тестовая картинка движется), 8–12 с почти чёрный
    subprocess.run([FFMPEG, "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=gray:size=320x180:rate=30:d=4",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:d=4",
                    "-f", "lavfi", "-i", "color=c=0x050505:size=320x180:rate=30:d=4",
                    "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0", "-g", "15", "-pix_fmt", "yuv420p",
                    str(video)], check=True)
    vis = vlog.analyze_visual(FFMPEG, video, 12.0)
    assert len(vis.t) >= 6
    sc = vlog.shot_scores(vis)
    w = vlog.best_window(vis, sc, 0.0, 12.0, 3.0)
    assert 3.0 <= w[0] <= 6.0


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_vlog_short_variant_in_editor(tmp_path, qt_app):
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication

    from glimpsy.editor import export as ex
    from glimpsy.editor.project import Project
    from glimpsy.editor.window import EditorWindow
    from glimpsy.recorder.encoder import software_encoder

    video = tmp_path / "walk.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=white:size=320x180:rate=30:d=45",
                    "-f", "lavfi", "-i", "sine=f=300:d=45", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(video)], check=True)
    p = Project.for_videos(tmp_path / "projects", [video],
                           [SimpleNamespace(duration=45.0, has_audio=True, width=320, height=180)])
    src = p.clips[0].src
    tr.TranscriptStore(p.dir).put(SourceWords(src, 45.0, make_words(), language="ru"), None)
    opened = []
    w = EditorWindow(p.dir, FFMPEG, software_encoder, tmp_path, on_open=opened.append)
    try:
        w.show()
        QApplication.processEvents()
        tr.rebuild_clips(w.project, w.tstore)
        w._changed()
        before = w.project.to_dict()
        plan = vlog.plan("short", w._sources(), lambda s: make_words(), lambda s: None, lambda s: make_visual(),
                         lambda s: set(), target_s=6.0)
        w._apply_vlog(plan, "short", True, True)
        assert opened and opened[0] != w.project.dir
        assert w.project.to_dict() == before                     # этот монтаж не тронут
        v = Project.load(opened[0])
        assert v.aspect == "9:16" and v.cuts["subtitles"] and v.cuts["broll"]
        assert v.clips and all(c.frames.get("9:16") for c in v.clips)   # кадр — по вертикали, без полей
        assert (opened[0] / "transcript").exists()
        assert sum(c.duration for c in v.clips) < 15
        # готовый вертикальный ролик — без полей сверху и снизу
        out = ex.export_project(FFMPEG, v, tmp_path / "short.mp4", software_encoder())
        raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", "0.5", "-i", str(out), "-frames:v", "1",
                              "-vf", "scale=108:192", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                             capture_output=True).stdout
        img = np.frombuffer(raw, np.uint8).reshape(192, 108)
        assert img[:10].mean() > 200 and img[-10:].mean() > 200
        # и на месте — тоже можно (одним шагом отмены)
        w._apply_vlog(plan, "vlog", False, False)
        assert w.project.aspect == "16:9" and w.project.cuts["broll"]
        w.undo()
        assert w.project.to_dict()["cuts"] == before["cuts"]
    finally:
        w.close()
