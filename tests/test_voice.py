"""Переозвучка своим голосом: образец голоса, подгонка фразы, место в ролике."""

import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from glimpsy.editor import transcript as tr
from glimpsy.editor import voice
from glimpsy.editor.transcript import SourceWords, Word

FFMPEG = __import__("glimpsy.paths", fromlist=["x"]).find_executable("ffmpeg")


def test_reference_spans_skip_phrase_and_deleted():
    ws = [Word(f"w{k}", k * 1.0, k * 1.0 + 0.8) for k in range(20)]
    spans = voice.reference_spans(ws, 5, 6, deleted={4, 7}, want=4.0)
    covered = [(a, b) for a, b in spans]
    assert all(not (a < 6.9 and b > 5.0) for a, b in covered)       # сама фраза не берётся
    assert all(not (a < 4.8 and b > 4.0) for a, b in covered)       # и вырезанное слово
    assert sum(b - a for a, b in covered) >= 4.0


def test_atempo_chain():
    assert voice.atempo(1.2) == "atempo=1.2000"
    assert voice.atempo(3.0) == "atempo=2.0,atempo=1.5000"
    assert voice.atempo(0.3).startswith("atempo=0.5,")


def test_clean_env_drops_our_python(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/x")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/bundle")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib")
    env = voice.clean_env()
    assert "PYTHONPATH" not in env and env["LD_LIBRARY_PATH"] == "/usr/lib" and env["HF_HOME"].endswith("hf")


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_fit_phrase_trims_and_stretches(tmp_path):
    raw = tmp_path / "raw.wav"
    # 0,5 с тишины, 2 с «речи», 0,5 с тишины
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "aevalsrc='if(between(t,0.5,2.5),0.3*sin(2*PI*220*t),0)':s=24000:d=3", str(raw)], check=True)
    src = tmp_path / "src.wav"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:d=6", str(src)], check=True)
    out = tmp_path / "fit.wav"
    d = voice.fit_phrase(FFMPEG, raw, out, 1.8, src, 2.0, 3.8)
    assert d == pytest.approx(1.8, abs=0.12)                 # тишина по краям убрана, 2 с → 1,8 с
    d2 = voice.fit_phrase(FFMPEG, raw, out, 1.0, src, 2.0, 3.0)
    assert d2 == pytest.approx(2.0 / 1.3, abs=0.12)           # сильнее 1,3 раза не ускоряем


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_respeak_in_editor(tmp_path, qt_app, monkeypatch):
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication, QInputDialog

    from glimpsy.editor.project import Project
    from glimpsy.editor.window import EditorWindow
    from glimpsy.recorder.encoder import software_encoder

    said = []

    class FakeSpeaker:
        proc = None

        def say(self, text, lang, ref, out):
            said.append((text, lang, Path(ref).exists()))
            subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=500:d=0.9", str(out)],
                           check=True)
            return out

        def stop(self):
            pass

    monkeypatch.setattr(voice, "supported", lambda: True)
    monkeypatch.setattr(voice, "installed", lambda: True)
    monkeypatch.setattr(voice, "speaker", lambda: FakeSpeaker())
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("явно должно", True)))
    video = tmp_path / "talk.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:size=320x180:rate=30:d=7",
                    "-f", "lavfi", "-i", "sine=f=300:d=7", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(video)], check=True)
    p = Project.for_videos(tmp_path / "projects", [video],
                           [SimpleNamespace(duration=7.0, has_audio=True, width=320, height=180)])
    src = p.clips[0].src
    sw = SourceWords(src, 7.0, [Word("раз", 1.0, 1.4), Word("два.", 1.5, 1.9), Word("по", 3.5, 3.7),
                                Word("идее", 3.8, 4.3), Word("четыре.", 4.6, 5.0)], language="ru")
    tr.TranscriptStore(p.dir).put(sw, None)
    w = EditorWindow(p.dir, FFMPEG, software_encoder, tmp_path)
    try:
        w.show()
        QApplication.processEvents()
        panel = w.text_panel_t
        tr.rebuild_clips(w.project, w.tstore)
        w._changed()
        panel.subs.setChecked(True)
        w._selection_do("respeak", [("w", 0, 2), ("w", 0, 3)])
        end = time.time() + 20
        while w._voice_job and time.time() < end:
            QApplication.processEvents()
            time.sleep(0.02)
        QApplication.processEvents()
        assert said == [("явно должно", "ru", True)]
        entry = w.project.cuts["respeak"][src][0]
        assert (entry["i"], entry["j"], entry["text"]) == (2, 3, "явно должно")
        assert (w.project.dir / entry["file"]).exists()
        # исходный звук фразы выключен, новый — на дорожке «Голос» там же, где фраза
        assert any(c.muted for c in w.project.clips)
        v = [o for o in w.project.overlays if o.auto]
        assert len(v) == 1 and v[0].kind == "audio"
        span = tr.mark_spans(sw, {2, 3}, tr.cuts_of(w.project))[0]
        assert v[0].start == pytest.approx(tr.output_time(w.project, src, span[0]), abs=0.01)
        assert v[0].duration == pytest.approx(entry["dur"])
        # субтитры и текст — с новой фразой
        assert [t.text for t in w.project.texts if t.auto] == ["раз два.", "явно должно четыре."]
        assert panel.view._state[("w", 0, 2)] == "voice"
        # вырезали «раз» — переозвучка сдвинулась вместе с фразой
        before = v[0].start
        w._cut_tokens([("w", 0, 0)])
        v2 = [o for o in w.project.overlays if o.auto][0]
        assert v2.start < before
        # вернуть исходную фразу
        w._unrespeak(src, entry["id"])
        assert not [o for o in w.project.overlays if o.auto] and not any(c.muted for c in w.project.clips)
        assert [t.text for t in w.project.texts if t.auto][-1] == "по идее четыре."
        w.undo()
        assert [o for o in w.project.overlays if o.auto]
    finally:
        w.close()
