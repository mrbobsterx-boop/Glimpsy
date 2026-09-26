"""Звук и голосовой режим: кольцевой буфер, распознавание речи, сборка ролика с речью."""

import json
import subprocess
import time

import numpy as np
import pytest

from glimpsy import paths
from glimpsy.recorder.audio import RATE, Ring, VoiceDetector, write_wav

FFMPEG = paths.find_executable("ffmpeg")


def test_ring_extract_fills_gaps_with_silence():
    r = Ring(seconds=10)
    r.add(100.0, np.full(RATE, 0.5, np.float32))          # 100–101 с
    r.add(102.0, np.full(RATE, -0.5, np.float32))         # 102–103 с (между ними — пропуск)
    a = r.extract(100.5, 102.5)
    assert len(a) == 2 * RATE
    assert a[: RATE // 2].mean() == pytest.approx(0.5)
    assert abs(a[RATE // 2: RATE + RATE // 2]).max() == 0          # пропуск — тишина
    assert a[-RATE // 2:].mean() == pytest.approx(-0.5)


def _feed(det, pattern, t0=1000.0):
    """pattern: [(секунды, громкость)] — подаём кусками по 100 мс."""
    t = t0
    rng = np.random.default_rng(1)
    for sec, amp in pattern:
        for _ in range(int(sec * 10)):
            x = rng.normal(0, 0.003, RATE // 10) + amp * np.sin(np.arange(RATE // 10) * 0.05)
            det.feed(t, x.astype(np.float32))
            t += 0.1
    return t


def test_voice_detector_finds_phrases_and_ignores_noise():
    det = VoiceDetector()
    _feed(det, [(3, 0.0), (4, 0.2), (0.8, 0.0), (2, 0.2), (3, 0.0), (0.2, 0.3), (3, 0.0)])
    phrases = det.pop_finished()
    # короткая пауза (0.8 с) внутри речи не рвёт фразу; щелчок 0.2 с — не речь
    assert len(phrases) == 1
    a, b = phrases[0]
    assert a == pytest.approx(1003.0 - 0.5, abs=0.3) and b == pytest.approx(1009.8 + 0.3, abs=0.3)


def test_voice_detector_adapts_to_loud_room():
    det = VoiceDetector()
    # вентилятор шумит сильнее обычного (но это ровный шум) — речью это быть не должно
    rng = np.random.default_rng(2)
    t = 0.0
    for _ in range(300):
        det.feed(t, rng.normal(0, 0.02, RATE // 10).astype(np.float32))
        t += 0.1
    assert not det.speaking().speaking and det.pop_finished() == []


def test_select_pieces_voice_whole_and_first():
    from glimpsy.assembler import select_pieces
    from glimpsy.config import Settings
    from glimpsy.recorder.candidates import Candidate
    from glimpsy.recorder.pacing import make_plan

    s = Settings(target_length_s=10, clip_min_s=2, clip_max_s=3, pace="dynamic").validate()
    plan = make_plan(s)

    def cand(i, t, dur, **kw):
        return Candidate(id=i, file=f"c{i}.ts", wall_start=t, wall_end=t + dur, want_start=t, want_end=t + dur,
                         monitor=1, width=1920, height=1080, score=0.5, activity=[0.5] * int(dur), **kw)
    cands = [cand(1, 0, 6), cand(2, 100, 15, priority=True, voice_id=1, voice_part=0, audio="c2.wav"),
             cand(3, 115, 7, priority=True, voice_id=1, voice_part=1, audio="c3.wav"),
             cand(4, 110, 6), cand(5, 200, 6), cand(6, 300, 6)]
    pieces = select_pieces(cands, plan, s)
    voice = [p for p in pieces if p.cand.voice_id]
    assert [p.cand.id for p in voice] == [2, 3]                        # речь целиком и по порядку
    assert all(p.speed == 1.0 and p.source_s == p.cand.duration for p in voice)
    assert 4 not in [p.cand.id for p in pieces]                       # пересекается с речью — не берём
    regular = [p for p in pieces if not p.cand.voice_id]
    assert regular and all(p.speed == plan.speed for p in regular)   # речь не «съела» длину ролика


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_assembly_voice_audio(tmp_path):
    """Речь — со звуком; обычный фрагмент в ролике без звука, а в проекте — со звуком (выключенным)."""
    from glimpsy.assembler import Assembler
    from glimpsy.config import Settings
    from glimpsy.recorder.candidates import Candidate
    from glimpsy.recorder.encoder import software_encoder
    from glimpsy.recorder.pacing import make_plan

    sess = tmp_path / "session"
    sess.mkdir()
    now = time.time()
    cands = []
    for i, (dur, voice) in enumerate([(5, 0), (4, 1)]):
        f = sess / f"c{i}.ts"
        subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:size=320x180:rate=30",
                        "-t", str(dur), "-c:v", "libx264", "-g", "30", "-bf", "0", "-f", "mpegts", str(f)], check=True)
        t = np.arange(dur * RATE) / RATE
        write_wav(sess / f"c{i}.wav", (0.3 * np.sin(2 * np.pi * (300 if voice else 900) * t)).astype(np.float32))
        cands.append(Candidate(id=i, file=f.name, wall_start=now + 60 * i, wall_end=now + 60 * i + dur,
                               want_start=now + 60 * i, want_end=now + 60 * i + dur, monitor=1, width=320,
                               height=180, score=0.5, activity=[0.5] * dur, audio=f"c{i}.wav",
                               priority=bool(voice), voice_id=voice))
    s = Settings(output_dir=str(tmp_path / "out"), target_length_s=4, clip_min_s=3, clip_max_s=4, pace="calm",
                 output_width=320, output_height=180, fx_zoom=False, fx_clicks=False).validate()
    out = Assembler(FFMPEG, software_encoder(), s, make_plan(s)).run(sess, cands, now, project_dir=tmp_path / "p")
    raw = subprocess.run([FFMPEG, "-loglevel", "error", "-i", str(out), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le",
                          "-"], capture_output=True).stdout
    a = np.frombuffer(raw, np.int16).astype(float) / 32768
    first, last = a[8000:16000], a[-12000:-4000]           # обычный фрагмент, потом речь
    assert np.sqrt((first ** 2).mean()) < 0.01              # у обычного — тишина
    assert np.sqrt((last ** 2).mean()) > 0.1                # у речи — звук
    meta = json.loads((tmp_path / "p" / "project.json").read_text())
    flags = [(c["has_audio"], c["muted"], c["voice"]) for c in meta["clips"]]
    assert flags == [(True, True, 0), (True, False, 1)]


def test_audio_start_failure_reported_once(monkeypatch):
    """Звук не включился — сообщаем один раз и не пытаемся снова каждые 100 мс."""
    from glimpsy.recorder import audio

    calls = []

    def broken(self, problems):
        calls.append(1)
        problems.append("звук недоступен: нет файла")
        return []

    monkeypatch.setattr(audio.AudioCapture, "_openers", broken)
    a = audio.AudioCapture(30, mic=True, system=False)
    assert a.start() == ["звук недоступен: нет файла"]
    for _ in range(50):
        assert a.start() == []
    assert len(calls) == 1
    a._next_try = 0.0                 # прошла минута — пробуем снова, но молча
    assert a.start() == [] and len(calls) == 2
