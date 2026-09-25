"""Настоящая запись экрана: буфер → кандидат → готовый ролик.

Запускается только на Linux с X11 (или под Xvfb) при наличии FFmpeg:
    xvfb-run -s "-screen 0 1280x720x24" python -m pytest tests/test_integration_x11.py
"""

import os
import subprocess
import sys
import time

import pytest

from glimpsy import paths
from glimpsy.config import Settings

FFMPEG = paths.find_executable("ffmpeg")
pytestmark = pytest.mark.skipif(
    not (sys.platform.startswith("linux") and os.environ.get("DISPLAY") and FFMPEG),
    reason="нужны Linux, X11 и FFmpeg",
)


def test_buffer_clip_and_assemble(tmp_path):
    from glimpsy.assembler import Assembler
    from glimpsy.platform.capture import X11GrabCapture
    from glimpsy.recorder.activity import ActivityTracker
    from glimpsy.recorder.candidates import Candidate
    from glimpsy.recorder.encoder import pick_encoder
    from glimpsy.recorder.pacing import make_plan
    from glimpsy.recorder.ring_buffer import BufferRun

    cap = X11GrabCapture()
    mon = cap.monitors()[0]
    enc = pick_encoder(FFMPEG, "auto", 30)
    act = ActivityTracker(listen_input=False)
    run = BufferRun(FFMPEG, cap.input_for(mon, 30), mon, enc, 30, tmp_path / "run", 1080,
                    on_frame_diff=act.add_frame_diff)
    run.start()
    try:
        deadline = time.time() + 20
        while time.time() < deadline and len(run.segments) < 6:
            time.sleep(0.3)
            run.poll()
        assert run.alive, run.error_text()
        assert len(run.segments) >= 6, run.error_text()
        run.trim(3)                                    # кольцо: старые кусочки удаляются
        assert run.segments[0].wall_end >= time.time() - 5
        now = time.time()
        got = run.save_clip(now - 3, now, tmp_path / "sess" / "cand_00001.ts")
        assert got and got[1] - got[0] >= 1.5
    finally:
        run.stop()
    run.cleanup()
    assert not (tmp_path / "run").exists()

    s = Settings(output_dir=str(tmp_path / "out"), target_length_s=10, clip_min_s=1, clip_max_s=2,
                 output_width=640, output_height=360).validate()
    c = Candidate(id=1, file="cand_00001.ts", wall_start=got[0], wall_end=got[1], want_start=got[0],
                  want_end=got[1], monitor=1, width=mon.width, height=mon.height, score=0.5,
                  cursor=[[0.5, 0.2, 0.3]])
    out = Assembler(FFMPEG, enc, s, make_plan(s)).run(tmp_path / "sess", [c], time.time(),
                                                      project_dir=tmp_path / "project")
    assert out.exists() and out.stat().st_size > 1000
    probe = subprocess.run([FFMPEG, "-i", str(out)], capture_output=True, text=True).stderr
    assert "640x360" in probe
    assert (tmp_path / "project" / "project.json").exists()
