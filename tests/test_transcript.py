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


def test_pause_marks_cut_keep_and_shorten():
    sw = sw_simple()
    cuts = dict(tr.DEFAULT_CUTS)
    # паузу 1,6 с укоротили до 0,6 с — по 0,3 с тишины с каждой стороны
    assert tr.kept_ranges(sw, set(), set(), cuts, marks={1: 0.6}) == [(0.85, 2.2), (3.2, 5.15)]
    assert tr.pause_state(1, 1.6, cuts, {1: 0.6}) == "short"
    assert tr.pause_state(1, 1.6, cuts, {1: -1}) == "keep"
    assert tr.pause_state(1, 1.6, cuts, {}) == "cut"
    # «паузы вручную»: сами не режутся, вырезана только отмеченная
    cuts["pause_cut"] = False
    assert tr.kept_ranges(sw, set(), set(), cuts, marks={1: 0.0}) == [(0.0, 2.05), (3.35, 7.0)]
    assert tr.pause_state(1, 1.6, cuts, {}) == "keep"
    # короткая пауза (0,3 с) в тексте не видна, пока её не пометили
    assert 3 not in dict(tr.shown_pauses(sw, cuts, {}))
    assert dict(tr.shown_pauses(sw, cuts, {3: 0.0}))[3] == pytest.approx(0.3)
    # старые проекты: «оставленные паузы» понимаются как пометка «целиком»
    assert tr.pause_marks({"kept_pauses": {"/v.mp4": [1]}, "pause_marks": {"/v.mp4": {"4": 0.5}}}, "/v.mp4") == \
        {1: -1.0, 4: 0.5}


def test_find_fillers():
    words = [Word(t, n, n + 0.5) for n, t in enumerate(
        ["Ну,", "э-э-э", "короче", "как", "бы", "Ээ.", "сообщение", "типа", "ну-ну"])]
    hits = tr.find_fillers(words, ["э", "ну", "как бы", "короче", "типа", "вот"])
    assert hits == {"э": [1, 5], "ну": [0], "как бы": [3, 4], "короче": [2], "типа": [7], "вот": []}
    assert tr.norm_word("Ё-моё!") == tr.norm_word("емое")


def test_mute_and_hide_split_pieces():
    sw = sw_simple()
    cuts = dict(tr.DEFAULT_CUTS)
    spans = tr.mark_spans(sw, {3}, cuts)                    # «э»: край — посередине паузы, не дальше запаса
    assert spans == [(3.95, 4.45)]
    rngs = tr.kept_ranges(sw, set(), set(), cuts)
    pieces = tr.split_ranges(rngs, spans, [])
    assert pieces == [(0.85, 2.05, False, False), (3.35, 3.95, False, False), (3.95, 4.45, True, False),
                      (4.45, 5.15, False, False)]
    # картинку убрали у «два три» (через вырезанную паузу), звук — у «три»
    both = tr.split_ranges(rngs, tr.mark_spans(sw, {2}, cuts), tr.mark_spans(sw, {1, 2}, cuts))
    assert [(m, h) for _a, _b, m, h in both] == [(False, False), (False, True), (True, True), (False, False)]


def test_cut_snaps_to_quiet_point():
    sw = sw_simple()
    env = env_with_speech(7.0, [(1.0, 1.9), (3.5, 5.0)])
    env[int(2.0 / tr.FRAME_S):int(2.1 / tr.FRAME_S)] = 0.05                # шорох сразу после речи
    r = tr.kept_ranges(sw, set(), set(), dict(tr.DEFAULT_CUTS), env)
    end = r[0][1]
    assert 1.9 <= end <= 2.16 and not (2.0 <= end < 2.1)                     # разрез не посреди шороха


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
    # после удалённого «э» звук идёт без перерыва — разрез у самого конца «э», а не посреди звука
    assert got == pytest.approx([0.85, 2.05, 3.35, 4.0, 4.305, 5.15], abs=0.006)
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
def test_text_modes_pauses_and_fillers(tmp_path, qt_app, monkeypatch):
    """Каждое предложение — с новой строки; паузы вручную и своя длина; слова-паразиты."""
    import subprocess
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication

    from glimpsy.editor.project import Project
    from glimpsy.editor.window import EditorWindow
    from glimpsy.recorder.encoder import software_encoder

    monkeypatch.setattr(EditorWindow, "_fillers", lambda self: getattr(self, "_test_fillers", ["э", "ну"]))
    monkeypatch.setattr(EditorWindow, "_set_fillers",
                        lambda self, f: (setattr(self, "_test_fillers", f), self._tr_states()))
    video = tmp_path / "talk.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:size=320x180:rate=30:d=7",
                    "-f", "lavfi", "-i", "sine=f=300:d=7", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(video)], check=True)
    p = Project.for_videos(tmp_path / "projects", [video],
                           [SimpleNamespace(duration=7.0, has_audio=True, width=320, height=180)])
    src = p.clips[0].src
    sw = sw_simple()
    sw.words[1].text = "два."                                 # конец предложения
    sw.src = src
    tr.TranscriptStore(p.dir).put(sw, None)
    w = EditorWindow(p.dir, FFMPEG, software_encoder, tmp_path)
    try:
        w.show()
        QApplication.processEvents()
        panel = w.text_panel_t
        lines = [x for x in panel.view.toPlainText().split("\n") if x.strip()]
        assert lines[0].startswith("[пауза") and lines[0].rstrip().endswith("два. [пауза 1,6 с]")
        assert lines[1].startswith("три э четыре")
        tr.rebuild_clips(w.project, w.tstore)
        w._changed()
        # режим «Паузы вручную»: ничего само не режется
        panel.mode.setCurrentIndex(panel.mode.findData("manual"))
        assert w.project.cuts["mode"] == "manual" and not w.project.cuts["pause_cut"]
        assert [(c.in_s, c.out_s) for c in w.project.clips] == [(0.0, 7.0)]
        # щелчок по паузе — вырезать только её; ещё раз — вернуть
        w._on_text_token(("p", 0, 1))
        assert [(c.in_s, c.out_s) for c in w.project.clips] == [(0.0, 2.05), (3.35, 7.0)]
        assert panel.view._state[("p", 0, 1)] == "cut" and panel.sel_box.isVisibleTo(panel)
        w._on_text_token(("p", 0, 1))
        assert [(c.in_s, c.out_s) for c in w.project.clips] == [(0.0, 7.0)]
        # своя длина паузы: оставить 0,6 с из 1,6
        panel.sel_len.setValue(0.6)
        w._on_pause_length(0.6)
        assert [(c.in_s, c.out_s) for c in w.project.clips] == [(0.0, 2.2), (3.2, 7.0)]
        assert "[пауза 1,6 → 0,6 с]" in panel.view.toPlainText()
        assert panel.view._state[("p", 0, 1)] == "short"
        w.undo()
        assert "[пауза 1,6 с]" in panel.view.toPlainText()
        # слова-паразиты: найдены, вырезаны разом, одно вернули щелчком
        panel.mode.setCurrentIndex(panel.mode.findData("fillers"))
        assert not panel.pause_box.isVisibleTo(panel) and panel.filler_box.isVisibleTo(panel)
        assert panel.view._state[("w", 0, 3)] == "filler"
        assert panel.filler_list.item(0).text().startswith("э   — 1")
        w._set_fillers(["э", "ну", "четыре"])
        assert panel.filler_list.count() == 3
        panel.fillers_cut.emit(["э", "четыре"])
        assert w.project.cuts["deleted"][src] == [3, 4]
        assert panel.view._state[("w", 0, 3)] == "cut"
        w._on_text_token(("w", 0, 4))
        assert w.project.cuts["deleted"][src] == [3]
        panel.fillers_restore.emit(["э"])
        assert w.project.cuts["deleted"][src] == []
        # субтитры из текста: появились на видео и следуют за вырезами и исправлениями
        panel.subs.setChecked(True)
        texts = [t.text for t in w.project.texts if t.auto]
        assert texts == ["раз два.", "три э четыре"]
        assert all(t.track == w.project.track_for("subtitles").id for t in w.project.texts)
        w._cut_tokens([("w", 0, 3)])
        assert [t.text for t in w.project.texts if t.auto] == ["раз два.", "три четыре"]
        from PySide6.QtWidgets import QInputDialog
        monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("пять", True)))
        w._edit_word(("w", 0, 4))
        assert [t.text for t in w.project.texts if t.auto] == ["раз два.", "три пять"]
        assert "три э пять" in panel.view.toPlainText()
        w.undo()
        assert [t.text for t in w.project.texts if t.auto] == ["раз два.", "три четыре"]
        panel.subs.setChecked(False)
        assert not any(t.auto for t in w.project.texts)
        # звук и картинка отдельно: выделили «три», убрали звук; «четыре» — картинку
        w._selection_do("mute", [("w", 0, 2)])
        assert panel.view._state[("w", 0, 2)] == "mute"
        assert [c.muted for c in w.project.clips if c.in_s >= 3.0][:1] == [True]
        w._selection_do("hide", [("w", 0, 4)])
        assert any(c.hidden and not c.muted for c in w.project.clips)
        assert panel.view._state[("w", 0, 4)] == "hide"
        total = w.project.total
        w._selection_do("mute", [("w", 0, 2)])                 # ещё раз — звук вернулся
        assert not any(c.muted for c in w.project.clips) and w.project.total == pytest.approx(total)
        w._selection_do("restore", [("w", 0, 4)])
        assert not any(c.hidden for c in w.project.clips)
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
def test_export_without_picture_or_sound(tmp_path):
    """Кусок «без картинки» — чёрный, звук идёт без провала; кусок «без звука» — тихий, картинка есть."""
    import subprocess
    from types import SimpleNamespace

    from glimpsy.editor import export as ex
    from glimpsy.editor.project import Clip, Project
    from glimpsy.recorder.encoder import software_encoder

    video = tmp_path / "src.mp4"
    subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "color=c=white:size=320x240:rate=30:d=4", "-f", "lavfi", "-i", "sine=f=440:r=48000:d=4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video)], check=True)
    p = Project.for_videos(tmp_path / "projects", [video],
                           [SimpleNamespace(duration=4.0, has_audio=True, width=320, height=240, fps=30)])
    src = p.clips[0].src
    p.clips = [Clip("a", "video", src, 4.0, 0.0, 1.0, has_audio=True, width=320, height=240),
               Clip("b", "video", src, 4.0, 1.0, 2.0, has_audio=True, width=320, height=240, hidden=True),
               Clip("c", "video", src, 4.0, 2.0, 3.0, has_audio=True, width=320, height=240, muted=True)]
    assert ex.simple_cuts(p, p.clips)
    out = ex.export_project(FFMPEG, p, tmp_path / "out.mp4", software_encoder())

    def luma(t):
        raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", str(t), "-i", str(out), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
        return np.frombuffer(raw, np.uint8).mean()

    assert luma(0.5) > 150 and luma(1.5) < 20 and luma(2.5) > 150      # (по бокам — поля кадра)
    pcm = subprocess.run([FFMPEG, "-loglevel", "error", "-i", str(out), "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
                         capture_output=True).stdout
    a = np.frombuffer(pcm, np.float32)

    def rms(t0, t1):
        x = a[int(t0 * 8000):int(t1 * 8000)]
        return float(np.sqrt((x * x).mean()))

    assert rms(1.3, 1.7) > 0.06 and rms(2.3, 2.7) < 0.005
    assert rms(0.99, 1.01) > 0.06                         # на стыке «картинка → без картинки» звук не проседает


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


def test_cut_does_not_clip_a_word_with_late_timing():
    """Распознавание поставило начало слова позже, чем оно звучит: разрез уходит к тишине перед словом."""
    sw = SourceWords("/v.mp4", 6.0, [Word("раз", 1.0, 1.4), Word("два", 3.6, 4.0)])     # «два» звучит с 3,2 с
    env = env_with_speech(6.0, [(1.0, 1.4), (3.2, 4.0)])
    r = tr.kept_ranges(sw, set(), set(), dict(tr.DEFAULT_CUTS), env)
    assert r[1][0] == pytest.approx(3.15, abs=0.02)                         # а не 3,45 — посреди слова


def test_sound_annotations_are_not_speech():
    W = Word
    words = [W(t, k, k + 0.5) for k, t in enumerate(
        "*поет* Лианты уходят. *звук отзыва* *звук отзыва* Blackstar. [музыка] Ну (смеется) да ♪ ок".split())]
    kept, mapping = tr.drop_noise(words)
    assert [w.text for w in kept] == ["Лианты", "уходят.", "Blackstar.", "Ну", "да", "ок"]
    assert mapping[1] == 0 and mapping[0] == -1 and mapping[7] == 2
    # незакрытая звёздочка не съедает текст дальше
    words2 = [W(t, k, k + 0.5) for k, t in enumerate("*звук а б в г д е ж з".split())]
    assert [w.text for w in tr.drop_noise(words2)[0]] == list("абвгдежз")
    # новые расшифровки чистятся сразу
    assert [w.text for w in tr.refine_words(words, None)] == ["Лианты", "уходят.", "Blackstar.", "Ну", "да", "ок"]


def test_remap_cuts_after_cleaning():
    #        0       1        2        3        4         5
    words = ["*звук", "отзыва*", "раз", "два.", "*поет*", "три"]
    _kept, mapping = tr.drop_noise([Word(t, k, k + 0.5) for k, t in enumerate(words)])
    cuts = {"deleted": {"/v": [1, 3, 5]}, "muted": {"/v": [2]}, "pause_marks": {"/v": {"-1": 0.0, "4": 0.5}},
            "word_text": {"/v": {"2": "Раз", "4": "x"}},
            "kept_pauses": {"/v": [4]}}
    tr.remap_cuts(cuts, "/v", mapping)
    assert cuts["deleted"]["/v"] == [1, 2] and cuts["muted"]["/v"] == [0]
    assert cuts["pause_marks"]["/v"] == {"-1": 0.0, "1": 0.5}          # пауза после «*поет*» — после «два.»
    assert cuts["word_text"]["/v"] == {"0": "Раз"}
    assert cuts["kept_pauses"]["/v"] == [1]


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_old_project_is_cleaned_of_sound_annotations(tmp_path, qt_app):
    import subprocess
    from types import SimpleNamespace

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
    words = [Word("*звук", 0.2, 0.5), Word("отзыва*", 0.5, 0.8), Word("раз", 1.0, 1.4), Word("два.", 1.5, 1.9),
             Word("три", 3.5, 3.9), Word("э", 4.0, 4.3), Word("четыре.", 4.6, 5.0)]
    tr.TranscriptStore(p.dir).put(SourceWords(src, 7.0, words), None)      # как было в старой версии
    p.cuts["deleted"] = {src: [5]}                                         # вырезано «э»
    p.cuts["subtitles"] = True
    p.save()
    w = EditorWindow(p.dir, FFMPEG, software_encoder, tmp_path)
    try:
        w.show()
        for _ in range(5):
            QApplication.processEvents()
        assert [x.text for x in w.tstore.get(src).words] == ["раз", "два.", "три", "э", "четыре."]
        assert w.project.cuts["deleted"][src] == [3]                       # всё ещё «э»
        assert "*" not in w.text_panel_t.view.toPlainText()
        assert [t.text for t in w.project.texts if t.auto] == ["раз два.", "три четыре."]
        assert not any("*" in x["text"] for x in __import__("json").loads(
            (p.dir / "transcript" / f"{tr.source_key(src)}.json").read_text())["words"] for x in [{"text": x[0]}])
    finally:
        w.close()


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_stretched_piece_is_remembered(tmp_path, qt_app):
    """Кусок растянули на ленте — после правки текста он остаётся растянутым; отмена возвращает."""
    import subprocess
    from types import SimpleNamespace

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
        assert [(c.in_s, c.out_s) for c in w.project.clips] == [(0.85, 2.05), (3.35, 5.15)]
        # тянем начало второго куска на 0,3 с раньше (как мышью на ленте)
        w.history.push(w.project.to_dict(), "trim")
        w.project.trim(1, 3.05, 5.15)
        w._changed()
        w._trim_timer.stop()
        w._trims_done()
        assert [(c.in_s, c.out_s) for c in w.project.clips] == [(0.85, 2.05), (3.05, 5.15)]
        # правка текста в другом месте — растяжка остаётся
        w._cut_tokens([("w", 0, 0)])
        assert w.project.clips[-1].in_s == pytest.approx(3.05)
        assert w.project.cuts["edges"][src] == [[3.35, 3.05]]
        # и её можно растянуть дальше
        w.history.push(w.project.to_dict(), "trim2")
        w.project.trim(len(w.project.clips) - 1, 2.9, 5.15)
        w._changed()
        w._trims_done()
        assert w.project.clips[-1].in_s == pytest.approx(2.9) and w.project.cuts["edges"][src] == [[3.35, 2.9]]
        w.undo()
        w.undo()
        w.undo()
        assert not w.project.cuts.get("edges", {}).get(src)
    finally:
        w.close()


def test_dtw_word_times_are_used():
    """Выравнивание по звуку (t_dtw): обычное время Whisper сдвинуто на секунды — берём точное."""
    tok = lambda t, a, b, d: {"text": t, "offsets": {"from": a, "to": b}, "p": 0.9, "t_dtw": d}  # noqa: E731
    data = {"result": {"language": "ru"}, "transcription": [{"tokens": [
        tok(" И", 5000, 5100, 826), tok(" ещё", 5480, 5600, 852), tok(" Сегод", 11100, 11200, 1290),
        tok("ня", 11200, 11290, 1296)]}]}
    words, _ = tr.parse_whisper_words(json.dumps(data))
    assert [w.text for w in words] == ["И", "ещё", "Сегодня"]
    assert words[0].start == pytest.approx(8.26 - tr.DTW_LEAD_S) and words[2].start == pytest.approx(12.65)
    assert words[0].end <= words[1].start
    cmd = tr.word_command("whisper", Path("m.bin"), Path("a.wav"), Path("/tmp/w"), "ru", "turbo")
    assert cmd[cmd.index("-dtw") + 1] == "large.v3.turbo" and "-nfa" in cmd


def test_retime_keeps_words_and_marks():
    old = [Word("Привет.", 0, 1), Word("это", 2, 3), Word("мой", 3, 4), Word("эээ", 4, 4.5), Word("голос.", 5, 6)]
    new = [Word("Привет.", 1.1, 1.5), Word("Это", 1.8, 2.0), Word("мой", 2.1, 2.3), Word("голос.", 2.9, 3.3)]
    out = tr.retime_words(old, new, 10.0)
    assert [w.text for w in out] == [w.text for w in old]                 # те же слова, те же номера
    assert [w.start for w in out[:3]] == [1.1, 1.8, 2.1] and out[4].start == 2.9
    assert 2.3 <= out[3].start < 2.9                                       # «эээ» — между соседями
