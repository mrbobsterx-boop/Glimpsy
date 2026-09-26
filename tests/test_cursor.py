"""Свой плавный курсор: путь, картинка, ролик из редактора и автосборка."""

import subprocess
import time

import numpy as np
import pytest

from glimpsy import paths
from glimpsy.editor import cursor as cur

FFMPEG = paths.find_executable("ffmpeg")


def test_smooth_track_is_smooth_and_hides_gaps():
    # точки 10 раз в секунду, потом курсор «ушёл» на другой монитор на 2 с
    pts = [[t / 10, 0.1 + 0.02 * t, 0.5] for t in range(20)] + [[4.0 + t / 10, 0.8, 0.2] for t in range(10)]
    track = cur.smooth_track(pts, 5.0)
    assert cur.position_at(track, 3.0) is None                      # пропуск — курсора нет
    x1, _ = cur.position_at(track, 1.0)
    assert abs(x1 - 0.3) < 0.03
    xs = [p[1] for p in track if 0.2 <= p[0] <= 1.8]
    steps = [b - a for a, b in zip(xs, xs[1:])]
    assert max(steps) - min(steps) < 0.004                           # равномерно, без рывков
    runs = cur.runs(track, 0.0, 5.0)
    assert len(runs) >= 2 and all(r[-1][0] - r[0][0] <= cur.CHUNK + 1e-6 for r in runs)


def test_cursor_images(qt_app):
    for style in cur.STYLES:
        img = cur.image(style, 40)
        assert img.height() == 40 and not img.isNull()
    assert cur.png("arrow", 32).exists()


def _probe_frame(path, t, w, h):
    raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                          "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(h, w)


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_editor_export_draws_cursor(tmp_path, qt_app):
    from glimpsy.editor.export import export_project
    from glimpsy.editor.project import Clip, Project
    from glimpsy.recorder.encoder import software_encoder

    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x303030:size=640x360:rate=30",
                    "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(tmp_path / "s.mp4")], check=True)
    cursor = [[t / 10, 0.25 + 0.5 * t / 30, 0.5] for t in range(31)]      # едет слева направо
    c = Clip("a", "video", "s.mp4", 3, 0, 3, width=640, height=360, cursor=cursor, own_cursor=True)
    p = Project(tmp_path, "t", [c])
    p.cursor = {"style": "arrow", "size": 3.0}
    out = export_project(FFMPEG, p, tmp_path / "o.mp4", software_encoder())
    early, late = _probe_frame(out, 0.3, 160, 90), _probe_frame(out, 2.7, 160, 90)
    # белая стрелка: сначала в левой половине, в конце — в правой
    assert early[:, :80].max() > 200 and early[:, 90:].max() < 120
    assert late[:, 100:].max() > 200 and late[:, :60].max() < 120
    p.cursor["show"] = False
    out2 = export_project(FFMPEG, p, tmp_path / "o2.mp4", software_encoder())
    assert _probe_frame(out2, 1.5, 160, 90).max() < 120                # выключили — курсора нет


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_assembly_draws_cursor_but_project_is_clean(tmp_path, qt_app):
    import json

    from glimpsy.assembler import Assembler
    from glimpsy.config import Settings
    from glimpsy.recorder.candidates import Candidate
    from glimpsy.recorder.encoder import software_encoder
    from glimpsy.recorder.pacing import make_plan

    sess = tmp_path / "s"
    sess.mkdir()
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x303030:size=640x360:rate=30",
                    "-t", "4", "-c:v", "libx264", "-g", "30", "-bf", "0", "-f", "mpegts", str(sess / "c.ts")],
                   check=True)
    now = time.time()
    c = Candidate(id=1, file="c.ts", wall_start=now, wall_end=now + 4, want_start=now, want_end=now + 4, monitor=1,
                  width=640, height=360, score=0.5, activity=[0.5] * 4, own_cursor=True,
                  cursor=[[t / 10, 0.5, 0.5] for t in range(41)])
    s = Settings(output_dir=str(tmp_path / "out"), target_length_s=3, clip_min_s=3, clip_max_s=3, pace="calm",
                 output_width=640, output_height=360, fx_zoom=False, fx_clicks=False).validate()
    out = Assembler(FFMPEG, software_encoder(), s, make_plan(s)).run(sess, [c], now, project_dir=tmp_path / "p")
    assert _probe_frame(out, 1.0, 640, 360).max() > 200                 # в ролике курсор есть
    meta = json.loads((tmp_path / "p" / "project.json").read_text())
    assert meta["clips"][0]["own_cursor"] is True
    clean = tmp_path / "p" / meta["clips"][0]["file"]
    assert _probe_frame(clean, 1.0, 640, 360).max() < 120              # в проекте — без него (рисует редактор)
