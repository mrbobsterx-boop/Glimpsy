"""Монтаж по тексту: слова, вырезы, субтитры."""

import json
from pathlib import Path

import numpy as np
import pytest

from glimpsy.editor import transcript as tr
from glimpsy.editor.transcript import SourceWords, Word


def env_with_speech(duration, spans, loud=0.2, quiet=0.001):
    """Громкость по 10 мс: тишина, а в spans — речь."""
    env = np.full(int(duration / tr.FRAME_S), quiet, dtype=np.float32)
    for a, b in spans:
        env[int(a / tr.FRAME_S):int(b / tr.FRAME_S)] = loud
    return env


def test_parse_whisper_tokens_into_words():
    tok = lambda t, a, b, p=0.9: {"text": t, "offsets": {"from": a, "to": b}, "p": p}  # noqa: E731
    data = {"result": {"language": "ru"}, "transcription": [{"tokens": [
        tok("[_BEG_]", 0, 0), tok(" При", 100, 200), tok("вет", 200, 400, 0.4), tok(",", 400, 420),
        tok(" э", 900, 1000), tok(" мир", 1500, 1800), tok("[_TT_90]", 1800, 1800)]}]}
    words, lang = tr.parse_whisper_words(json.dumps(data))
    assert lang == "ru"
    assert [(w.text, w.start, w.end) for w in words] == [("Привет,", 0.1, 0.42), ("э", 0.9, 1.0), ("мир", 1.5, 1.8)]
    assert words[0].p == pytest.approx(0.4)             # уверенность слова — по самому неуверенному куску


def test_refine_words_by_loudness():
    # речь: 3.0–3.5 и 3.6–4.0; Whisper поставил первые слова в начало тишины (0.0–0.5), а ещё выдумал
    # слово в тишине в самом конце
    env = env_with_speech(8.0, [(3.0, 3.5), (3.6, 4.0)])
    words = [Word("Ну", 0.0, 0.2), Word("итак", 0.2, 0.5), Word("начнём", 3.55, 4.1), Word("спасибо", 6.0, 6.5)]
    out = tr.refine_words(words, env)
    assert [w.text for w in out] == ["Ну", "итак", "начнём"]          # слова не потерялись, выдумка — да
    assert out[0].start == pytest.approx(3.0, abs=0.02)   # «прилипшие» к тишине слова — у начала речи
    assert out[0].start <= out[1].start <= out[2].start and out[2].end == pytest.approx(4.0, abs=0.02)


def sw_simple():
    # слова: 1.0–1.4, 1.5–1.9 | пауза 1.6 с | 3.5–3.9, 4.0–4.3 | пауза 0.3 | 4.6–5.0; видео 7 с
    w = [Word("раз", 1.0, 1.4), Word("два", 1.5, 1.9), Word("три", 3.5, 3.9), Word("э", 4.0, 4.3),
         Word("четыре", 4.6, 5.0)]
    return SourceWords("/v.mp4", 7.0, w)


def test_pauses_are_cut_with_padding():
    sw = sw_simple()
    cuts = dict(tr.DEFAULT_CUTS)
    r = tr.kept_ranges(sw, set(), set(), cuts)
    # тишина в начале, в середине (1,6 с) и в конце вырезана, у речи — запас 0,15 с
    assert r == [(0.85, 2.05), (3.35, 5.15)]
    assert [i for i, _ in tr.pauses(sw.words, 0.7, sw.duration)] == [-1, 1, 4]


def test_restored_pause_and_no_pause_cut():
    sw = sw_simple()
    cuts = dict(tr.DEFAULT_CUTS)
    assert tr.kept_ranges(sw, set(), {1}, cuts) == [(0.85, 5.15)]          # паузу после «два» вернули
    cuts["pause_cut"] = False
    assert tr.kept_ranges(sw, set(), set(), cuts) == [(0.0, 7.0)]          # паузы не режем вовсе


def test_deleted_word_is_cut_even_without_pause_cutting():
    sw = sw_simple()
    cuts = dict(tr.DEFAULT_CUTS)
    r = tr.kept_ranges(sw, {3}, set(), cuts)                               # убрали «э»
    assert r == [(0.85, 2.05), (3.35, 4.0), (4.45, 5.15)]
    cuts["pause_cut"] = False
    r = tr.kept_ranges(sw, {3}, set(), cuts)
    assert r == [(0.0, 4.0), (4.3, 7.0)]                                   # вырезано ровно слово


def test_cut_snaps_to_quiet_point():
    sw = sw_simple()
    env = env_with_speech(7.0, [(1.0, 1.9), (3.5, 5.0)])
    env[int(2.0 / tr.FRAME_S):int(2.1 / tr.FRAME_S)] = 0.05                # шорох сразу после речи
    r = tr.kept_ranges(sw, set(), set(), dict(tr.DEFAULT_CUTS), env)
    end = r[0][1]
    assert 1.9 <= end <= 2.13 and not (2.0 <= end < 2.1)                     # разрез не посреди шороха


def test_rebuild_clips_and_srt(tmp_path):
    from glimpsy.editor.project import Project

    store = tr.TranscriptStore(tmp_path)
    sw = sw_simple()
    store.put(sw, env_with_speech(7.0, [(1.0, 1.9), (3.5, 5.0)]))
    p = Project(tmp_path, "t")
    p.cuts = {"sources": [{"src": "/v.mp4", "duration": 7.0, "has_audio": True, "width": 1920, "height": 1080}],
              "deleted": {"/v.mp4": [3]}}
    tr.rebuild_clips(p, store)
    # (разрезы — в серединах 10-мс отрезков громкости: точность ±5 мс)
    got = [x for c in p.clips for x in (c.in_s, c.out_s)]
    assert got == pytest.approx([0.85, 2.05, 3.35, 4.0, 4.45, 5.15], abs=0.006)
    # время в готовом ролике: слова идут без удалённого «э»
    words = tr.output_words(p, tr.TranscriptStore(tmp_path))
    assert [w[2] for w in words] == ["раз", "два", "три", "четыре"]
    assert words[2][0] == pytest.approx(1.2 + 0.15, abs=0.012)            # «три» — сразу после первого куска
    srt = tr.make_srt(words)
    assert srt.startswith("1\n00:00:00,15") and "раз два три четыре" in srt
    # пометки — часть проекта: отмена возвращает всё, как было
    snap = p.to_dict()
    p.cuts["deleted"] = {}
    p.restore(snap)
    assert p.cuts["deleted"] == {"/v.mp4": [3]}


def test_project_for_videos_references_files(tmp_path):
    from types import SimpleNamespace

    from glimpsy.editor.project import Project

    video = tmp_path / "my video.mp4"
    video.write_bytes(b"x")
    info = SimpleNamespace(duration=1800.0, has_audio=True, width=1920, height=1080)
    p = Project.for_videos(tmp_path / "projects", [video], [info])
    assert p.text_edit and p.clips[0].src == str(video.resolve()) and p.clips[0].out_s == 1800.0
    assert not (p.dir / "media").exists()                                  # исходник не копировался
    again = Project.load(p.dir)
    assert again.text_edit and again.name == "my video" and again.path_of(again.clips[0]) == video.resolve()


FFMPEG = __import__("glimpsy.paths", fromlist=["x"]).find_executable("ffmpeg")


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_text_editing_in_editor(tmp_path, qt_app):
    """Панель «Текст»: щелчок — к слову, Delete — вырезать, щелчок по зачёркнутому — вернуть, паузы, отмена."""
    import subprocess
    from types import SimpleNamespace

    from PySide6.QtGui import QTextCursor
    from PySide6.QtWidgets import QApplication

    from glimpsy.editor.project import Project
    from glimpsy.editor.window import EditorWindow
    from glimpsy.recorder.encoder import software_encoder

    video = tmp_path / "talk.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:size=320x180:rate=30:d=7",
                    "-f", "lavfi", "-i", "sine=f=300:d=7", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(video)], check=True)
    p = Project.for_videos(tmp_path / "projects", [video],
                           [SimpleNamespace(duration=7.0, has_audio=True, width=320, height=180)])
    src = p.clips[0].src
    sw = sw_simple()
    sw.src = src
    tr.TranscriptStore(p.dir).put(sw, None)
    w = EditorWindow(p.dir, FFMPEG, software_encoder, tmp_path)
    try:
        w.show()
        QApplication.processEvents()
        panel = w.text_panel_t
        assert panel.isVisible() and not panel.transcribe_btn.isVisible()
        text = panel.view.toPlainText()
        assert "раз два" in text and "[пауза 1,6 с]" in text
        tr.rebuild_clips(w.project, w.tstore)                 # как после расшифровки
        w._changed()
        assert [(c.in_s, c.out_s) for c in w.project.clips] == [(0.85, 2.05), (3.35, 5.15)]
        # щелчок по «три» — видео перематывается к нему (на ленте это 1,2 + 0,15 с)
        w._on_text_token(("w", 0, 2))
        assert w.player.t == pytest.approx(1.35, abs=0.01)
        # выделили «э» и нажали Delete
        a, b = panel.view._spans[("w", 0, 3)]
        c = panel.view.textCursor()
        c.setPosition(a)
        c.setPosition(b, QTextCursor.MoveMode.KeepAnchor)
        panel.view.setTextCursor(c)
        panel.view.delete_keys.emit(panel.view.keys_in_selection())
        assert w.project.cuts["deleted"][src] == [3]
        assert len(w.project.clips) == 3 and panel.view._state[("w", 0, 3)] == "cut"
        # щелчок по зачёркнутому — вернуть
        w._on_text_token(("w", 0, 3))
        assert w.project.cuts["deleted"][src] == [] and len(w.project.clips) == 2
        # пауза в середине: щелчок — оставить её
        w._on_text_token(("p", 0, 1))
        assert [(c.in_s, c.out_s) for c in w.project.clips] == [(0.85, 5.15)]
        assert panel.view._state[("p", 0, 1)] == "keep"
        # порог пауз больше самой длинной паузы — ничего не режется, кроме удалённого
        panel.pause_min.setValue(3.0)
        assert [(c.in_s, c.out_s) for c in w.project.clips] == [(0.0, 7.0)]
        w.undo()
        assert [(c.in_s, c.out_s) for c in w.project.clips] == [(0.85, 5.15)]
        w.undo()
        w.undo()
        assert w.project.cuts["deleted"][src] == [3] and panel.view._state[("w", 0, 3)] == "cut"
        assert "стало" in panel.stats.text()
    finally:
        w.close()


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_fast_cut_export_keeps_sync(tmp_path):
    """Нарезка из десятков кусков: длина точная, звук не уезжает от видео, пачки склеены без пропусков."""
    import re
    import subprocess
    from types import SimpleNamespace

    from glimpsy.editor import export as ex
    from glimpsy.editor.project import Clip, Project
    from glimpsy.recorder.encoder import software_encoder

    video = tmp_path / "src.mp4"
    # 60 с: картинка и звук, у звука каждую секунду — «щелчок» в начале секунды
    subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc=size=320x240:rate=30:d=60", "-f", "lavfi",
                    "-i", "aevalsrc='if(lt(mod(t,1),0.05),0.8*sin(2*PI*1000*t),0)':s=48000:d=60",
                    "-c:v", "libx264", "-g", "60", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video)], check=True)
    p = Project.for_videos(tmp_path / "projects", [video],
                           [SimpleNamespace(duration=60.0, has_audio=True, width=320, height=240, fps=30)])
    # 60 кусков: из каждой секунды берём [k+0.0, k+0.5)
    p.clips = [Clip(f"c{k}", "video", p.clips[0].src, 60.0, float(k), k + 0.5, has_audio=True, width=320,
                    height=240) for k in range(60)]
    assert ex.simple_cuts(p, p.clips)
    out = ex.export_project(FFMPEG, p, tmp_path / "out.mp4", software_encoder())
    info = subprocess.run([FFMPEG, "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    assert dur == pytest.approx(30.0, abs=0.15)
    # щелчки: каждый кусок начинается со щелчка → в итоге щелчок каждые 0,5 с, без сдвига к концу
    pcm = subprocess.run([FFMPEG, "-loglevel", "error", "-i", str(out), "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
                         capture_output=True).stdout
    a = np.abs(np.frombuffer(pcm, np.float32))
    onsets = [i / 8000 for i in range(1, len(a)) if a[i] > 0.3 and a[max(0, i - 400):i].max() < 0.3]
    assert len(onsets) >= 55
    drift = [t - round(t * 2) / 2 for t in onsets]
    assert max(abs(d) for d in drift) < 0.05, drift[-5:]


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_sessions_new_project_from_videos(tmp_path, qt_app, monkeypatch):
    """«Смонтировать видео…»: выбрали файлы — появился проект, открылся редактор; видео не скопировано."""
    import subprocess

    from PySide6.QtWidgets import QFileDialog

    from glimpsy.editor import sessions

    v = tmp_path / "talk.mp4"
    subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=gray:size=320x180:rate=25:d=3", "-f", "lavfi", "-i", "sine=d=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(v)], check=True)
    monkeypatch.setattr(sessions, "projects_root", lambda: tmp_path / "projects")
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", staticmethod(lambda *a, **k: ([str(v)], "")))
    opened = []
    dlg = sessions.SessionsDialog(FFMPEG, opened.append)
    dlg._new_from_videos()
    assert len(opened) == 1
    from glimpsy.editor.project import Project
    p = Project.load(opened[0])
    assert p.text_edit and p.fps == 25 and Path(p.clips[0].src) == v.resolve()
    assert not any(f.suffix == ".mp4" for f in opened[0].rglob("*"))
    dlg.close()



@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_preview_plays_through_cuts_of_one_file(tmp_path, qt_app):
    """Просмотр идёт по вырезам одного видео подряд: запасной плеер заранее стоит на следующем куске."""
    import subprocess
    import time
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication

    from glimpsy.editor.player import TimelinePlayer
    from glimpsy.editor.project import Clip, Project

    v = tmp_path / "src.mp4"
    subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:d=12", "-c:v", "libx264", "-g", "60", "-pix_fmt", "yuv420p",
                    str(v)], check=True)
    p = Project.for_videos(tmp_path / "projects", [v],
                           [SimpleNamespace(duration=12.0, has_audio=False, width=320, height=240, fps=30)])
    p.clips = [Clip(f"c{k}", "video", p.clips[0].src, 12.0, k * 2.0, k * 2.0 + 0.8) for k in range(6)]
    pl = TimelinePlayer(p)
    frames = []
    pl.frame.connect(lambda img: frames.append(pl.t))
    try:
        pl.seek(0.0)
        end = time.time() + 1.0
        while time.time() < end:
            QApplication.processEvents()
            time.sleep(0.01)
        pl.play()
        end = time.time() + 4.0
        while time.time() < end and pl.playing:
            QApplication.processEvents()
            time.sleep(0.01)
        assert pl.t > 2.4 and (pl.idx or 0) >= 3            # прошли несколько склеек
        assert pl.spare.src == pl.deck.src                   # следующий кусок того же файла — уже в запасном
        assert len(frames) > 30
    finally:
        pl.shutdown()
