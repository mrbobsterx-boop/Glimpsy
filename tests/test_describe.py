"""«Описать фрагменты»: кадр, речь и описание от Claude — и для старых записей тоже."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from glimpsy import paths
from glimpsy.editor import describe as ds
from glimpsy.editor import fragments as fr

FFMPEG = paths.find_executable("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")


def make_project(tmp_path):
    proj = tmp_path / "project_20260925_101010"
    proj.mkdir()
    for i, color in enumerate(("red", "blue")):
        subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={color}:size=320x180:rate=30",
                        "-t", "3", "-pix_fmt", "yuv420p", str(proj / f"piece_000{i}.mp4")], check=True)
    # старая запись: ни окна, ни описаний
    (proj / "project.json").write_text(json.dumps({"output": str(tmp_path / "Glimpsy_old.mp4"), "clips": [
        {"file": "piece_0000.mp4", "duration": 3.0}, {"file": "piece_0001.mp4", "duration": 3.0}]}))
    return proj


@needs_ffmpeg
def test_old_recording_gets_frames_and_claude_notes(tmp_path, monkeypatch):
    from glimpsy.editor.project import Project

    proj = make_project(tmp_path)
    p = Project.load(proj)
    p.clips[1].note = "моё описание"
    p.save()
    seen = []

    def fake_ai(key, image, title="", speech=""):
        seen.append((key, image.name))
        assert image.read_bytes()[:2] == b"\xff\xd8"          # это JPEG
        return "Красный экран без окон"

    monkeypatch.setattr(ds, "ai_describe", fake_ai)
    count, warnings = ds.describe_project(FFMPEG, proj, ds.Options(speech=False, ai_key="sk-test"))
    assert count == 2 and warnings == []
    assert len(seen) == 1 and seen[0][0] == "sk-test"        # своё описание Claude не трогает
    p2 = Project.load(proj)
    assert p2.clips[0].ai_note == "Красный экран без окон" and p2.clips[1].note == "моё описание"
    assert (proj / ds.frame_rel(p2.clips[0])).exists() and (proj / ds.frame_rel(p2.clips[1])).exists()
    md = (proj / fr.MANIFEST_MD).read_text(encoding="utf-8")
    assert "| Красный экран без окон. 3 с |" in md and "моё описание" in md
    assert f"![кадр]({ds.frame_rel(p2.clips[0])})" in md
    # второй раз — ничего не пересчитывается и Claude не зовётся
    ds.describe_project(FFMPEG, proj, ds.Options(speech=False, ai_key="sk-test"))
    assert len(seen) == 1


@needs_ffmpeg
def test_bad_key_stops_asking_claude(tmp_path, monkeypatch):
    proj = make_project(tmp_path)
    calls = []

    def broken(*a, **k):
        calls.append(1)
        raise RuntimeError("401")

    monkeypatch.setattr(ds, "ai_describe", broken)
    count, warnings = ds.describe_project(FFMPEG, proj, ds.Options(speech=False, ai_key="bad"))
    assert count == 2 and len(calls) == 1 and warnings and warnings[0].startswith("Claude:")


@needs_ffmpeg
def test_speech_goes_into_description(tmp_path, monkeypatch):
    from glimpsy.editor import transcript as tr
    from glimpsy.editor.project import Project

    proj = make_project(tmp_path)
    meta = json.loads((proj / "project.json").read_text())
    meta["clips"][0]["has_audio"] = True
    (proj / "project.json").write_text(json.dumps(meta))

    def fake_transcribe(ffmpeg, src, key, duration, model, lang, store, progress=None, cancel=None, retime=False):
        sw = tr.SourceWords(key, duration, [tr.Word("привет", 0.2, 0.6), tr.Word("мир", 0.7, 1.0)], "ru", model)
        store.put(sw, None)
        return sw

    monkeypatch.setattr(ds, "speech_model", lambda: "base")
    monkeypatch.setattr(ds, "speech_available", lambda: True)
    monkeypatch.setattr(tr, "transcribe_source", fake_transcribe)
    ds.describe_project(FFMPEG, proj, ds.Options(frames=False, speech=True))
    c = Project.load(proj).clips[0]
    assert fr.speech_text(Project.load(proj), c) == "привет мир"
    assert "Говорю: «привет мир»" in (proj / fr.MANIFEST_MD).read_text(encoding="utf-8")


def test_ai_request_shape(tmp_path, monkeypatch):
    """Запрос к Claude: картинка + сведения, модель по умолчанию, отказ — пустое описание."""
    import anthropic

    img = tmp_path / "f.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0fake")
    sent = {}

    class FakeMessages:
        def create(self, **kw):
            sent.update(kw)
            return SimpleNamespace(stop_reason="end_turn",
                                   content=[SimpleNamespace(type="thinking"),
                                            SimpleNamespace(type="text", text="«Чат с Claude в браузере.»")])

    class FakeClient:
        def __init__(self, **kw):
            self.beta = SimpleNamespace(messages=FakeMessages())

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    assert ds.ai_describe("k", img, "Claude — чат", "сделай игру") == "Чат с Claude в браузере"
    assert sent["model"] == ds.AI_MODEL and sent["fallbacks"] == "default"
    content = sent["messages"][0]["content"]
    assert content[0]["type"] == "image" and "Claude — чат" in content[1]["text"]

    class Refuse(FakeMessages):
        def create(self, **kw):
            return SimpleNamespace(stop_reason="refusal", content=[])

    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(beta=SimpleNamespace(messages=Refuse())))
    assert ds.ai_describe("k", img) == ""


@needs_ffmpeg
def test_dialog_runs_for_all_projects(tmp_path, qt_app, monkeypatch):
    from PySide6.QtCore import QEventLoop, QTimer

    from glimpsy.editor.describe_dialog import DescribeDialog

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    pa, pb = make_project(a), make_project(b)
    dlg = DescribeDialog(FFMPEG, [pa, pb])
    dlg.speech.setChecked(False)
    dlg.ai.setChecked(False)
    loop = QEventLoop()
    dlg.finished_ok.connect(loop.quit)
    QTimer.singleShot(30000, loop.quit)
    dlg.start()
    loop.exec()
    assert not dlg.running and "Описано фрагментов: 4" in dlg.status.text()
    assert (pa / "fragments.md").exists() and (pb / "frames").is_dir()
