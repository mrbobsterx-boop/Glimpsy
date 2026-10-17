"""Озвучка для VideoStudio: дубли голоса → итоговая дорожка и время слов по ней."""

import json
import subprocess
import wave

import numpy as np
import pytest

from glimpsy import paths
from glimpsy.editor import transcript as tr
from glimpsy.editor import voiceover as vo

FFMPEG = paths.find_executable("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")


def tone_wav(path, beeps, total, rate=48000):
    """Тишина с «словами» — короткими гудками в заданные моменты: [(начало, конец, частота)]."""
    x = np.zeros(int(total * rate), np.float32)
    for a, b, f in beeps:
        t = np.arange(int(a * rate), int(b * rate)) / rate
        x[int(a * rate):int(a * rate) + len(t)] = 0.4 * np.sin(2 * np.pi * f * t)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((x * 32767).astype("<i2").tobytes())


def loud_at(path, t, rate=48000, win=0.03):
    with wave.open(str(path), "rb") as w:
        data = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32767
        assert w.getframerate() == rate and w.getnchannels() == 1 and w.getsampwidth() == 2
    seg = data[int((t - win) * rate):int((t + win) * rate)]
    return float(np.sqrt(np.mean(seg ** 2))) if len(seg) else 0.0


@needs_ffmpeg
def test_package_has_cut_words_removed_and_times_match_sound(tmp_path):
    p = vo.new_project(tmp_path / "projects")
    assert p.text_edit and vo.is_voice(p) and p.clips == []
    store = tr.TranscriptStore(p.dir)
    # дубль 1: «раз два три», «два» — неудачный (вырежем); дубль 2: «четыре пять»
    takes = [([(0.5, 0.9, 440), (1.5, 1.9, 660), (2.5, 2.9, 880)], 3.4, ["Раз,", "два", "три."]),
             ([(0.4, 0.8, 520), (1.2, 1.7, 740)], 2.2, ["Четыре", "пять."])]
    for n, (beeps, total, texts) in enumerate(takes, 1):
        wav = p.dir / "media" / f"voice_{n}.wav"
        wav.parent.mkdir(exist_ok=True)
        tone_wav(wav, beeps, total)
        src = vo.take_source(FFMPEG, wav, p.dir / "media" / f"take_{n}.mp4", f"Дубль {n}")
        vo.insert_take(p, src)
        store.put(tr.SourceWords(src["src"], src["duration"],
                                 [tr.Word(t, a, b, 0.9) for t, (a, b, _f) in zip(texts, beeps)], "ru", "base"), None)
    first = p.cuts["sources"][0]["src"]
    p.cuts["deleted"] = {first: [1]}                    # «два» вырезано
    p.cuts["pause_cut"] = False
    tr.rebuild_clips(p, tr.TranscriptStore(p.dir))
    p.save()
    studio = tmp_path / "studio"
    studio.mkdir()
    (studio / "project.json").write_text("{}")
    folder = vo.export_package(FFMPEG, p, studio)
    data = json.loads((folder / "words.json").read_text(encoding="utf-8"))
    assert data["format"] == "glimpsy-voice" and data["version"] == 1 and data["sample_rate"] == 48000
    assert data["audio"] == "voice.wav" and data["language"] == "ru" and data["glimpsy_project"] == p.name
    words = data["words"]
    assert [w["w"] for w in words] == ["Раз,", "три.", "Четыре", "пять."]       # вырезанного слова нет
    starts = [w["start"] for w in words]
    assert starts == sorted(starts) and all(w["end"] > w["start"] for w in words)
    assert all(a["end"] <= b["start"] for a, b in zip(words, words[1:]))         # не перекрываются
    assert data["duration"] == pytest.approx(3.4 - 0.4 + 2.2, abs=0.05)          # минус вырезанное слово
    wav = folder / "voice.wav"
    for w in words:                                                             # слово звучит там, где записано
        mid = (w["start"] + w["end"]) / 2
        assert loud_at(wav, mid) > 0.05, w
    gap = (words[0]["end"] + words[1]["start"]) / 2
    assert loud_at(wav, gap, win=0.05) < 0.02
    assert data["segments"] and data["segments"][0]["text"].startswith("Раз,")
    assert (folder / "README.txt").read_text(encoding="utf-8").count("\n") == 2
    # повторный экспорт — старый пакет в _old/
    vo.export_package(FFMPEG, p, studio)
    olds = list((folder / "_old").glob("*/voice.wav"))
    assert len(olds) == 1 and (folder / "voice.wav").exists()


def test_insert_take_after_cursor(tmp_path):
    from glimpsy.editor.project import Clip, Project

    p = Project(tmp_path, "v")
    p.cuts = {"sources": [{"src": "a", "duration": 5}, {"src": "b", "duration": 5}], "voice": True}
    p.clips = [Clip("1", "video", "a", 5, 0, 5), Clip("2", "video", "b", 5, 0, 5)]
    vo.insert_take(p, {"src": "c", "duration": 2}, at=2.0)            # курсор в первом дубле
    assert [s["src"] for s in p.cuts["sources"]] == ["a", "c", "b"]
    vo.insert_take(p, {"src": "d", "duration": 2}, at=10.0)           # курсор в конце — в конец
    assert [s["src"] for s in p.cuts["sources"]][-1] == "d"


def test_tidy_words():
    out = vo.tidy_words([(0.5, 0.9, "б", 0.9), (0.1, 0.6, "а", 2.0), (4.0, 5.0, "за краем", 0.5)], 3.0)
    assert [w["w"] for w in out] == ["а", "б"]
    assert out[0]["p"] == 1.0 and out[1]["start"] >= out[0]["end"]


def test_script_view_paragraphs(qt_app):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    from glimpsy.editor.voice_widgets import ScriptView, paragraph_html, split_script

    text = "# Вступление\nСегодня я покажу игру.\n\n[НА ЭКРАНЕ: бункер]\nЭто бункер.\n\n[ПАУЗА]\n\nКонец."
    assert len(split_script(text)) == 4
    h = paragraph_html("[НА ЭКРАНЕ: бункер]\nЭто **бункер**.")
    assert 'color:#7C8494' in h and "<b>бункер</b>" in h
    v = ScriptView()
    seen = []
    v.position_changed.connect(seen.append)
    v.load_text(text)
    v.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier))
    v.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier))
    assert v.current == 2 and seen == [1, 2]
    assert "бункер" in v.toPlainText()


@needs_ffmpeg
def test_voice_project_record_take_and_export(tmp_path, qt_app, monkeypatch):
    """Новая озвучка → запись с суфлёром → дубль на ленте → «Озвучка для VideoStudio» (распознавание подменено)."""
    import time

    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

    from glimpsy.editor import window as win
    from glimpsy.editor.window import EditorWindow
    from glimpsy.recorder.encoder import software_encoder
    from tests.test_audio import _FakeRecorder

    p = vo.new_project(tmp_path / "projects")
    script = tmp_path / "script.md"
    script.write_text("# 01\nПривет, это тест.\n\n[НА ЭКРАНЕ: игра]\nВторой абзац.", encoding="utf-8")
    w = EditorWindow(p.dir, FFMPEG, software_encoder, tmp_path)
    w.record_mic_opener = lambda: _FakeRecorder(0.4)
    try:
        w.show()
        w.project.cuts["prompter"] = {"path": str(script), "pos": 1}
        w.record()
        dlg = w._rec_dialog
        assert dlg.script_view.isVisible() and dlg.script_view.current == 1       # суфлёр открылся сам
        assert not dlg.camera.isVisible()                                         # озвучка — только голос
        dlg._toggle()
        dlg._t0 = time.monotonic()
        end = time.time() + 1.2
        while time.time() < end:
            dlg._tick()
            QApplication.processEvents()
            time.sleep(0.03)
        monkeypatch.setattr(w, "_transcribe_new", lambda: None)               # без whisper в проверке
        dlg._toggle()
        QApplication.processEvents()
        assert len(w.project.cuts["sources"]) == 1 and len(w.project.clips) == 1
        src = w.project.cuts["sources"][0]
        assert src["wav"].endswith(".wav") and src["duration"] > 0.5 and w.project.voices() == []
        # «распознали»: два слова
        w.tstore.put(tr.SourceWords(src["src"], src["duration"], [tr.Word("Привет,", 0.1, 0.4, 0.9),
                                                                  tr.Word("тест.", 0.5, 0.8, 0.8)], "ru", "base"),
                     None)
        studio = tmp_path / "studio"
        studio.mkdir()
        (studio / "project.json").write_text("{}")
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(studio))
        monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
        w.export_voice()
        end = time.time() + 30
        while w.exporting and time.time() < end:
            QApplication.processEvents()
            time.sleep(0.05)
        data = json.loads((studio / "voice" / "words.json").read_text(encoding="utf-8"))
        assert [x["w"] for x in data["words"]] == ["Привет,", "тест."]
        assert (studio / "voice" / "voice.wav").exists() and win.QSettings("Glimpsy", "editor").value(
            "voice/studio_dir") == str(studio)
    finally:
        w.player.shutdown()
        w.close()


@needs_ffmpeg
def test_videostudio_video_opens_as_project(tmp_path):
    from types import SimpleNamespace

    from glimpsy.editor import subtitles as subs
    from glimpsy.editor.project import Project

    mp4 = tmp_path / "studio_final.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=navy:size=1920x1080:rate=30",
                    "-f", "lavfi", "-i", "sine=f=300:sample_rate=48000", "-t", "3", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(mp4)], check=True)
    p = Project.for_videos(tmp_path / "projects", [mp4],
                           [SimpleNamespace(duration=3.0, has_audio=True, width=1920, height=1080, fps=30)])
    assert p.aspect == "16:9" and p.fps == 30 and len(p.clips) == 1
    assert subs.has_speech_audio(p) and subs.audio_command(FFMPEG, p, tmp_path / "a.wav") is not None
