"""Субтитры на другом языке: предложения, деление перевода на строки, перевод в фоне."""

import time
from pathlib import Path

import pytest

from glimpsy.editor import translate as mt
from glimpsy.editor import transcript as tr


def words(*items):
    return [(a, b, t) for a, b, t in items]


def test_sentences_and_split_like():
    ws = words((0.0, 0.4, "Привет."), (0.5, 0.9, "Сегодня"), (1.0, 1.3, "идём"), (3.0, 3.4, "в"), (3.5, 3.9, "поход"))
    assert [[w[2] for w in s] for s in mt.sentences(ws)] == [["Привет."], ["Сегодня", "идём"], ["в", "поход"]]
    parts = mt.split_like("Ich habe vergessen, Brot zu kaufen, also fahren wir zum Laden.",
                          ["Я забыл купить хлеб,", "поэтому заедем в магазин."])
    assert len(parts) == 2 and all(parts) and " ".join(parts).startswith("Ich habe")
    assert mt.split_like("Ja.", ["a b c", "d e f"]) == ["Ja.", ""]
    assert mt.split_like("eins zwei drei", ["x"]) == ["eins zwei drei"]


def test_translated_cues_use_cache_and_report_missing(tmp_path):
    cache = mt.Cache(tmp_path)
    ws = words((0.0, 0.4, "Привет."), (1.0, 1.3, "Идём"), (1.4, 1.8, "гулять."))
    lines, missing = mt.translated_cues(ws, cache, "m2m", "de")
    assert missing == ["Привет.", "Идём гулять."] and [t for _a, _b, t in lines] == ["Привет.", "Идём гулять."]
    cache.put("m2m", "de", {"Привет.": "Hallo."})
    lines, missing = mt.translated_cues(ws, mt.Cache(tmp_path), "m2m", "de")      # и с диска
    assert missing == ["Идём гулять."] and lines[0] == (0.0, 0.5, "Hallo.")
    srt = tr.srt_text(lines)
    assert "00:00:00,000 --> 00:00:00,500\nHallo." in srt


@pytest.mark.skipif(not mt.available(), reason="нет CTranslate2")
def test_real_translation_if_model_present():
    """Настоящий перевод — если переводчик уже скачан (на сборочных машинах его нет)."""
    if not mt.model_ready("m2m"):
        pytest.skip("переводчик не скачан")
    out = mt.translate(["Сегодня мы идём в поход."], "ru", "de", "m2m")
    assert out and "heute" in out[0].lower()


FFMPEG = __import__("glimpsy.paths", fromlist=["x"]).find_executable("ffmpeg")


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_german_subtitles_in_editor(tmp_path, qt_app, monkeypatch):
    import subprocess
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication

    from glimpsy.editor.project import Project
    from glimpsy.editor.transcript import SourceWords, Word
    from glimpsy.editor.window import EditorWindow
    from glimpsy.recorder.encoder import software_encoder

    calls = []

    def fake_translate(texts, src, dst, key="m2m", **kw):
        calls.append((tuple(texts), src, dst, key))
        return [f"DE[{t}]" for t in texts]

    monkeypatch.setattr(mt, "translate", fake_translate)
    monkeypatch.setattr(mt, "available", lambda: True)
    monkeypatch.setattr(mt, "model_ready", lambda key: True)
    video = tmp_path / "talk.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:size=320x180:rate=30:d=7",
                    "-f", "lavfi", "-i", "sine=f=300:d=7", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(video)], check=True)
    p = Project.for_videos(tmp_path / "projects", [video],
                           [SimpleNamespace(duration=7.0, has_audio=True, width=320, height=180)])
    src = p.clips[0].src
    sw = SourceWords(src, 7.0, [Word("раз", 1.0, 1.4), Word("два.", 1.5, 1.9), Word("три", 3.5, 3.9),
                                Word("э", 4.0, 4.3), Word("четыре.", 4.6, 5.0)], language="ru")
    tr.TranscriptStore(p.dir).put(sw, None)
    w = EditorWindow(p.dir, FFMPEG, software_encoder, tmp_path)

    def wait_translated():
        end = time.time() + 5
        while (w._mt_busy or w._mt_todo) and time.time() < end:
            QApplication.processEvents()
            time.sleep(0.01)
        QApplication.processEvents()

    try:
        w.show()
        QApplication.processEvents()
        panel = w.text_panel_t
        tr.rebuild_clips(w.project, w.tstore)
        w._changed()
        panel.subs_lang.setCurrentIndex(panel.subs_lang.findData("de"))
        assert w.project.cuts["subs_lang"] == "de" and w.project.cuts["subtitles"] and panel.subs.isChecked()
        wait_translated()
        assert [t.text for t in w.project.texts if t.auto] == ["DE[раз два.]", "DE[три э четыре.]"]
        assert calls[0][1:] == ("ru", "de", "m2m")
        # вырезали «э» — заново переводится только изменившееся предложение
        n = len(calls)
        w._cut_tokens([("w", 0, 3)])
        wait_translated()
        assert [c[0] for c in calls[n:]] == [("три четыре.",)]
        assert [t.text for t in w.project.texts if t.auto] == ["DE[раз два.]", "DE[три четыре.]"]
        # обратно на язык речи
        panel.subs_lang.setCurrentIndex(panel.subs_lang.findData(""))
        assert [t.text for t in w.project.texts if t.auto] == ["раз два.", "три четыре."]
        w.undo()
        assert w.project.cuts["subs_lang"] == "de" and panel.subs_lang.currentData() == "de"
        assert Path(w.project.dir / "transcript" / "translations.json").exists()
    finally:
        w.close()
