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


def _mkdir(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    return d


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

    from PySide6.QtWidgets import QApplication

    from glimpsy.editor.project import Project
    from glimpsy.editor.window import EditorWindow
    from glimpsy.recorder.encoder import software_encoder

    said = []

    class FakeSpeaker:
        proc = None

        def say(self, text, lang, ref, out, conds=None):
            said.append((text, lang, Path(ref).exists()))
            subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=500:d=0.9", str(out)],
                           check=True)
            return out

        def stop(self):
            pass

    monkeypatch.setattr(voice, "supported", lambda: True)
    monkeypatch.setattr(voice, "installed", lambda: True)
    monkeypatch.setattr(voice, "speaker", lambda: FakeSpeaker())
    from PySide6.QtWidgets import QDialog

    from glimpsy.editor.voices_dialog import RespeakDialog

    monkeypatch.setattr(RespeakDialog, "exec",
                        lambda self: (self.text.setText("явно должно"), QDialog.DialogCode.Accepted)[1])
    monkeypatch.setattr(voice, "voices_dir", lambda: _mkdir(tmp_path / "voices"))
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


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_voice_library(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "voices_dir", lambda: _mkdir(tmp_path / "voices"))
    wav = tmp_path / "talk.wav"
    # 1 с тишины, 7 с «речи», 1 с тишины
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "aevalsrc='if(between(t,1,8),0.3*sin(2*PI*220*t),0)':s=48000:d=9", str(wav)], check=True)
    v = voice.add_voice(FFMPEG, "  Мой голос ", wav)
    assert v.name == "Мой голос" and v.seconds == pytest.approx(7.0, abs=0.3) and v.sample.exists()
    assert [x.name for x in voice.list_voices()] == ["Мой голос"]
    # из видео — только указанные куски, не длиннее 20 с
    long = tmp_path / "long.wav"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=200:d=60", str(long)], check=True)
    v2 = voice.add_voice(FFMPEG, "Саша", long, spans=[(0.0, 15.0), (20.0, 35.0)])
    assert v2.seconds == pytest.approx(20.0, abs=0.2)
    with pytest.raises(voice.VoiceError):
        voice.add_voice(FFMPEG, "Коротко", long, spans=[(0.0, 2.0)])
    assert len(voice.list_voices()) == 2                       # неудачный не остаётся
    voice.rename_voice(v.id, "Я")
    assert voice.get_voice(v.id).name == "Я"
    voice.delete_voice(v2.id)
    assert [x.name for x in voice.list_voices()] == ["Я"]


def test_record_voice_dialog(tmp_path, qt_app):
    import time as _t

    from PySide6.QtWidgets import QApplication

    from glimpsy.editor.voices_dialog import RecordVoiceDialog
    from tests.test_audio import _FakeRecorder

    dlg = RecordVoiceDialog(opener=lambda: _FakeRecorder(0.3))
    dlg._toggle()                                               # начать
    end = _t.time() + 10
    while (dlg.meter is None or len(dlg.meter.samples()) < 48000 * 6) and _t.time() < end:
        QApplication.processEvents()
        _t.sleep(0.02)
    dlg._toggle()                                               # готово
    assert dlg.result() == dlg.DialogCode.Accepted and dlg.wav is not None and dlg.wav.stat().st_size > 48000 * 2 * 5
    dlg.wav.unlink()


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_respeak_with_saved_voice_and_save_from_text(tmp_path, qt_app, monkeypatch):
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication, QDialog, QInputDialog

    from glimpsy.editor.project import Project
    from glimpsy.editor.voices_dialog import RespeakDialog
    from glimpsy.editor.window import EditorWindow
    from glimpsy.recorder.encoder import software_encoder

    monkeypatch.setattr(voice, "voices_dir", lambda: _mkdir(tmp_path / "voices"))
    said = []

    class FakeSpeaker:
        proc = None

        def say(self, text, lang, ref, out, conds=None):
            said.append((text, Path(ref), conds))
            subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=500:d=0.8", str(out)],
                           check=True)
            return out

        def stop(self):
            pass

    monkeypatch.setattr(voice, "supported", lambda: True)
    monkeypatch.setattr(voice, "installed", lambda: True)
    monkeypatch.setattr(voice, "speaker", lambda: FakeSpeaker())
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Мой голос", True)))
    from PySide6.QtWidgets import QMessageBox
    shown = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: shown.append(a[2])))
    video = tmp_path / "talk.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:size=320x180:rate=30:d=12",
                    "-f", "lavfi", "-i", "sine=f=300:d=12", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(video)], check=True)
    p = Project.for_videos(tmp_path / "projects", [video],
                           [SimpleNamespace(duration=12.0, has_audio=True, width=320, height=180)])
    src = p.clips[0].src
    words = [Word(f"слово{k}", 0.5 + k * 0.8, 0.5 + k * 0.8 + 0.7) for k in range(12)]
    tr.TranscriptStore(p.dir).put(SourceWords(src, 12.0, words, language="ru"), None)
    w = EditorWindow(p.dir, FFMPEG, software_encoder, tmp_path)
    try:
        w.show()
        QApplication.processEvents()
        tr.rebuild_clips(w.project, w.tstore)
        w._changed()
        # слишком мало речи — голос не сохраняется
        w._save_voice([("w", 0, 0), ("w", 0, 1)])
        QApplication.processEvents()
        assert voice.list_voices() == [] and "нужно хотя бы" in shown[0]
        # 8 слов подряд (~6 с) — сохраняется в библиотеку
        w._save_voice([("w", 0, k) for k in range(8)])
        end = time.time() + 10
        while not voice.list_voices() and time.time() < end:
            QApplication.processEvents()
            time.sleep(0.02)
        saved = voice.list_voices()
        assert [v.name for v in saved] == ["Мой голос"] and saved[0].seconds >= 5
        # переозвучка сохранённым голосом: образец не вырезается, используется разобранный голос
        monkeypatch.setattr(RespeakDialog, "exec", lambda self: (
            self.text.setText("новая фраза"), self.voice.setCurrentIndex(self.voice.findData(saved[0].id)),
            QDialog.DialogCode.Accepted)[2])
        w._selection_do("respeak", [("w", 0, 10)])
        end = time.time() + 20
        while w._voice_job and time.time() < end:
            QApplication.processEvents()
            time.sleep(0.02)
        QApplication.processEvents()
        assert said == [("новая фраза", saved[0].sample, saved[0].conds)]
        assert w.project.cuts["respeak"][src][0]["voice"] == "Мой голос"
    finally:
        w.close()


def test_ssl_context_finds_certificates_without_defaults(monkeypatch):
    """Собранная программа на SteamOS не знает, где системные сертификаты (CERTIFICATE_VERIFY_FAILED)."""
    import ssl

    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent")
    monkeypatch.setenv("SSL_CERT_DIR", "/nonexistent")
    ctx = voice.ssl_context()
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.cert_store_stats()["x509_ca"] > 50                         # список сертификатов загружен


@pytest.mark.skipif(__import__("sys").platform.startswith("win"), reason="сигналы — только Linux/macOS")
def test_install_step_killed_by_system_says_memory(tmp_path, monkeypatch):
    import sys
    import threading

    monkeypatch.setattr(voice, "home", lambda: _mkdir(tmp_path / "voice"))
    with pytest.raises(voice.VoiceError) as e:
        voice._run([sys.executable, "-c", "import os, signal; print('loading'); os.kill(os.getpid(), signal.SIGKILL)"],
                   threading.Event())
    assert "памяти" in str(e.value) and "install.log" in str(e.value)
    assert "loading" in (tmp_path / "voice" / "install.log").read_text()
    with pytest.raises(voice.VoiceError) as e:
        voice._run([sys.executable, "-c", "import sys; print('нет такого пакета'); sys.exit(1)"], threading.Event())
    assert "нет такого пакета" in str(e.value)


def test_worker_loads_weights_without_a_second_copy():
    assert "low_memory_loading(torch)" in voice.WORKER and '"mmap", True' in voice.WORKER


def test_download_resumes_after_broken_connection(tmp_path, monkeypatch):
    """Слабый интернет: связь рвётся посреди файла — докачиваем с того же места, а не с начала."""
    import http.server
    import threading

    data = bytes(range(256)) * 4000                                     # ~1 МБ
    calls = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            rng = self.headers.get("Range")
            calls.append(rng)
            if rng is None:                                             # первый раз — обрыв на середине
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data[: len(data) // 2])
                self.wfile.flush()
                self.connection.shutdown(2)
                return
            start = int(rng.split("=")[1].rstrip("-"))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            self.send_header("Content-Length", str(len(data) - start))
            self.end_headers()
            self.wfile.write(data[start:])

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(voice.time if hasattr(voice, "time") else __import__("time"), "sleep", lambda s: None)
    try:
        dest = tmp_path / "model.bin"
        seen = []
        voice._download(f"http://127.0.0.1:{srv.server_address[1]}/m", dest, seen.append, threading.Event(),
                        size=len(data))
        assert dest.read_bytes() == data
        assert calls[0] is None and calls[1] == f"bytes={len(data) // 2}-"   # докачка, а не заново
        assert seen[-1] == 1.0
    finally:
        srv.shutdown()


def test_model_files_ready_check(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "home", lambda: _mkdir(tmp_path / "voice"))
    assert not voice.model_ready()
    d = _mkdir(voice.model_dir())
    for f, n in voice.MODEL_FILES.items():
        with open(d / f, "wb") as fh:
            fh.truncate(n)                                              # «пустой» файл нужного размера
    assert voice.model_ready()
