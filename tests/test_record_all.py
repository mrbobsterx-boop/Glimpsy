"""«Записывать всё подряд»: запись без пропусков, после остановки — проект без сборки ролика."""

import json
import subprocess
import time
from types import SimpleNamespace

import numpy as np
import pytest

from glimpsy import paths
from glimpsy.config import RECORD_ALL_CHUNK_S, Settings
from glimpsy.recorder.pacing import make_plan

FFMPEG = paths.find_executable("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")


def test_plan_keeps_everything():
    s = Settings(record_all=True).validate()
    plan = make_plan(s)
    assert plan.record_all and plan.speed == 1.0 and plan.candidate_s == RECORD_ALL_CHUNK_S
    assert plan.pool_size > 10 ** 6
    assert s.buffer_s >= RECORD_ALL_CHUNK_S + 6


def test_engine_saves_back_to_back_chunks():
    from glimpsy.recorder.engine import RecorderEngine

    e = RecorderEngine.__new__(RecorderEngine)
    e.s = Settings(record_all=True).validate()
    e.plan = make_plan(e.s)
    e._reset_session_fields()
    saved = []

    def fake_save_from(run, t0, t1, priority, voice=None):
        saved.append((round(t0, 1), round(t1, 1), priority))
        e._last_save_end = max(e._last_save_end, t1)
        return t0, t1

    e._save_from = fake_save_from
    e.run = SimpleNamespace(available_from=100.0)
    e._marks = [125.0]
    for now in range(100, 152):
        e._save_continuous(float(now))
    assert saved == [(100.0, 120.0, False), (120.0, 140.0, True)]       # без промежутков, звезда — у второго
    e._flush_run(SimpleNamespace(available_from=100.0), 151.5)            # завершили: хвост тоже сохраняется
    assert saved[-1] == (140.0, 151.5, False)
    e._maybe_save(200.0)                                                  # случайных сохранений нет
    assert len(saved) == 3


@needs_ffmpeg
def test_assembly_makes_project_without_video(tmp_path, qt_app):
    from glimpsy.assembler import Assembler
    from glimpsy.editor.project import Project
    from glimpsy.recorder.audio import write_wav
    from glimpsy.recorder.candidates import Candidate
    from glimpsy.recorder.encoder import software_encoder

    sess = tmp_path / "session"
    sess.mkdir()
    now = time.time()
    cands = []
    for i in range(3):
        subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                        "-t", "4", "-c:v", "libx264", "-g", "30", "-bf", "0", "-f", "mpegts", str(sess / f"c{i}.ts")],
                       check=True)
        audio = ""
        if i == 1:                                         # у второго куска есть звук
            t = np.arange(4 * 48000) / 48000
            write_wav(sess / "c1.wav", (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32))
            audio = "c1.wav"
        cands.append(Candidate(id=i + 1, file=f"c{i}.ts", wall_start=now + 4 * i, wall_end=now + 4 * i + 4,
                               want_start=now + 4 * i, want_end=now + 4 * i + 4, monitor=1, width=640, height=360,
                               score=0.5, audio=audio, priority=(i == 2)))
    s = Settings(output_dir=str(tmp_path / "out"), record_all=True).validate()
    proj = tmp_path / "projects" / "project_20261016_101010"
    out = Assembler(FFMPEG, software_encoder(), s, make_plan(s)).run(sess, list(reversed(cands)), now,
                                                                    project_dir=proj)
    assert out == proj
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").glob("*.mp4"))   # ролика нет
    meta = json.loads((proj / "project.json").read_text())
    assert meta["output"] == "" and meta["title"].startswith("Запись ")
    assert [c["duration"] for c in meta["clips"]] == pytest.approx([4, 4, 4], abs=0.2)
    assert meta["clips"][1]["has_audio"] and not meta["clips"][1]["muted"]   # свой голос не выключен
    p = Project.load(proj)
    assert p.name.startswith("Запись ") and len(p.clips) == 3 and p.clips[2].priority
    probe = subprocess.run([FFMPEG, "-i", str(proj / meta["clips"][1]["file"])], capture_output=True, text=True).stderr
    assert "Audio: aac" in probe and "Video: h264" in probe
    assert (proj / "fragments.md").exists()
    assert not list(sess.glob("render/*.mp4"))                               # перенесены, а не скопированы
