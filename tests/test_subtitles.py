"""Автосубтитры: звук ролика, разбор ответа whisper, чистка «придуманных» фраз."""

import json
import subprocess

import numpy as np
import pytest

from worklapse import paths
from worklapse.editor import subtitles as S
from worklapse.editor.project import Clip, Project
from worklapse.editor.text import TextItem

FFMPEG = paths.find_executable("ffmpeg")
WHISPER = paths.find_executable("whisper-cli")


def test_parse_and_clean():
    js = json.dumps({"result": {"language": "ru"}, "transcription": [
        {"offsets": {"from": 0, "to": 1500}, "text": " Привет, это мой"},
        {"offsets": {"from": 1500, "to": 1700}, "text": " дом."},
        {"offsets": {"from": 2000, "to": 3000}, "text": " [музыка]"},
        {"offsets": {"from": 3000, "to": 4000}, "text": " Продолжение следует..."},
        {"offsets": {"from": 5000, "to": 5200}, "text": " Тихо"},
        {"offsets": {"from": 6000, "to": 7000}, "text": " Громко и чётко"},
    ]})
    segs, lang = S.parse_whisper_json(js)
    assert lang == "ru" and len(segs) == 6
    samples = np.zeros(8 * S.SAMPLE_RATE, dtype="float32")
    samples[: 2 * S.SAMPLE_RATE] = 0.1                                      # речь 0–2 с
    samples[6 * S.SAMPLE_RATE: 7 * S.SAMPLE_RATE] = 0.1                     # речь 6–7 с
    clean = S.clean_segments(segs, samples)
    assert [s.text for s in clean] == ["Привет, это мой дом.", "Громко и чётко"]   # «дом.» приклеен
    assert clean[0].start == 0 and clean[0].end == pytest.approx(1.7)
    items = S.make_texts(clean, total=6.5)
    assert all(isinstance(t, TextItem) and t.auto for t in items)
    assert items[1].duration == pytest.approx(0.5)                          # обрезано по концу ролика


def test_audio_command(tmp_path):
    p = Project(tmp_path, "t", [Clip("a", "video", "screen.mp4", 3, 0, 3),                     # без звука
                                Clip("b", "video", "talk.mp4", 10, 2, 8, has_audio=True, speed=2.0),
                                Clip("c", "video", "muted.mp4", 5, 0, 5, has_audio=True, muted=True)])
    assert S.has_speech_audio(p)
    cmd = S.audio_command("ffmpeg", p, tmp_path / "o.wav")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert cmd.count("-i") == 1 and "talk.mp4" in cmd[cmd.index("-i") + 1]
    assert "atempo=2.0" in graph and "adelay=delays=3000" in graph          # после 3 с тишины
    p.clips[1].muted = True
    assert not S.has_speech_audio(p) and S.audio_command("ffmpeg", p, tmp_path / "o.wav") is None


def test_text_auto_roundtrip(tmp_path):
    p = Project(tmp_path, "t", [Clip("a", "video", "v.mp4", 3, 0, 3)])
    p.texts = [TextItem("x", "hi", 0.0, 1.0, auto=True), TextItem("y", "own", 1.0, 1.0)]
    q = Project(tmp_path, "t", [])
    q.restore(p.to_dict())
    assert [t.auto for t in q.texts] == [True, False]


@pytest.mark.skipif(not WHISPER, reason="нет whisper-cli (scripts/build_whisper.py)")
def test_whisper_cli_runs():
    """Собранная программа запускается на этой системе (нет потерянных библиотек)."""
    r = subprocess.run([WHISPER, "--help"], capture_output=True, **paths.subprocess_flags())
    assert r.returncode == 0 and b"--vad" in (r.stdout + r.stderr)

