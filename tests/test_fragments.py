"""Фрагменты по отдельности: имена файлов, описания из записи, список fragments.md в папке."""

import json
import subprocess

import pytest

from glimpsy import paths
from glimpsy.editor import fragments as fr
from glimpsy.editor.project import Clip
from glimpsy.recorder.candidates import Candidate, CandidatePool, dominant_window

FFMPEG = paths.find_executable("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")


def clip(**kw):
    base = dict(id="c", kind="video", src="a.mp4", src_duration=20.0, in_s=0.0, out_s=12.0)
    base.update(kw)
    return Clip(**base)


def test_dominant_window():
    log = [(0.0, "Chrome", "Чат"), (4.0, "Code", "main.py"), (5.0, "Chrome", "Чат")]
    assert dominant_window(log, 0, 10) == ("Chrome", "Чат")
    assert dominant_window(log, 4, 5) == ("Code", "main.py")
    assert dominant_window([], 0, 10) == ("", "")


def test_candidate_pool_reads_old_and_new_fields(tmp_path):
    (tmp_path / "1.ts").write_bytes(b"x")
    (tmp_path / "candidates.json").write_text(json.dumps({"next_id": 2, "items": [
        {"id": 1, "file": "1.ts", "wall_start": 0, "wall_end": 5, "want_start": 0, "want_end": 5, "monitor": 0,
         "width": 10, "height": 10, "score": 0.5, "app": "Chrome", "title": "Чат", "future_field": 1}]}))
    [c] = CandidatePool(tmp_path).items
    assert isinstance(c, Candidate) and c.app == "Chrome" and c.title == "Чат"


def test_titles_and_names():
    assert fr.clean_title("Claude Code — чат — Google Chrome", "Chrome") == "Claude Code — чат"
    assert fr.clean_title("main.py - Glimpsy - Visual Studio Code") == "main.py - Glimpsy"
    assert fr.slug("Чат с Claude: большая задача!") == "chat-s-claude-bolshaya-zadacha"
    assert fr.safe_file_name(' V02-chat-big-task.mp4 ') == "V02-chat-big-task"
    assert fr.safe_file_name('a/b:c?') == "a-b-c"
    clips = [clip(id="1", name="V02-chat-big-task"), clip(id="2", app="Chrome", title="Story Map — Google Chrome"),
             clip(id="3", name="V02-chat-big-task"), clip(id="4")]
    assert fr.file_names(clips) == ["V02-chat-big-task.mp4", "02-story-map.mp4", "V02-chat-big-task-2.mp4",
                                    "04-fragment.mp4"]


def test_suggested_note():
    c = clip(app="Chrome", title="Claude Code — Google Chrome", clicks=[[1, 0.5, 0.5]] * 3, priority=True)
    note = fr.suggest_note(c, "привет, это тест")
    assert note == "Chrome: «Claude Code». 12 с, кликов: 3, важный момент. Говорю: «привет, это тест»"
    assert fr.suggest_note(clip()) == "Запись экрана. 12 с"


def test_manifest_merges(tmp_path):
    for n in ("a.mp4", "b.mp4", "old.mp4"):
        (tmp_path / n).write_bytes(b"x")
    fr.write_manifest(tmp_path, [fr.Entry("b.mp4", "B", "старое", 3), fr.Entry("old.mp4", "O", "o", 1),
                                 fr.Entry("gone.mp4", "G", "g", 1)])
    (tmp_path / "gone.mp4").unlink(missing_ok=True)
    items = fr.write_manifest(tmp_path, [fr.Entry("a.mp4", "A", "что | на нём", 5, speech="раз два"),
                                         fr.Entry("b.mp4", "B", "новое", 4)])
    assert [e.file for e in items] == ["a.mp4", "b.mp4", "old.mp4"]
    md = (tmp_path / fr.MANIFEST_MD).read_text(encoding="utf-8")
    assert "| `b.mp4` | 4 с | B | новое |" in md and "старое" not in md and "что \\| на нём" in md
    assert "`a.mp4`: «раз два»" in md
    assert len(json.loads((tmp_path / fr.MANIFEST_JSON).read_text(encoding="utf-8"))["fragments"]) == 3


@needs_ffmpeg
def test_fragments_exported_with_blur(tmp_path, qt_app):
    from types import SimpleNamespace

    import numpy as np

    from glimpsy.editor import export as ex
    from glimpsy.editor.overlay import blur_item
    from glimpsy.editor.project import Project
    from glimpsy.recorder.encoder import software_encoder

    srcs = []
    for i, color in enumerate(("red", "blue")):
        f = tmp_path / f"v{i}.mp4"
        subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c={color}:size=320x180:rate=30:d=2", "-f", "lavfi", "-i",
                        "anullsrc=r=48000:cl=stereo", "-vf", "drawgrid=w=4:h=400:t=2:c=white", "-t", "2", "-c:v",
                        "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(f)], check=True)
        srcs.append(f)
    p = Project.for_videos(tmp_path / "projects", srcs,
                           [SimpleNamespace(duration=2.0, has_audio=True, width=320, height=180, fps=30)] * 2)
    p.clips[0].name, p.clips[0].note = "V02-chat", "Чат с задачей"
    p.overlays = [blur_item("b", 2.5, 1.0, "16:9", [0.0, 0.0, 1.0, 1.0])]      # только на втором фрагменте
    out = tmp_path / "drive"
    rows = fr.entries(p, p.clips)
    assert [r.file for r in rows] == ["V02-chat.mp4", "02-fragment.mp4"]
    for c, r in zip(p.clips, rows):
        snap = fr.clip_project(p, c)
        assert len(snap.clips) == 1 and snap.total == pytest.approx(2.0)
        ex.export_project(FFMPEG, snap, out / r.file, software_encoder(),
                          overlay_layers=ex.render_overlay_layers(snap, tmp_path / "ov" / c.id))
    assert fr.clip_project(p, p.clips[1]).overlays[0].start == pytest.approx(0.5)
    assert fr.clip_project(p, p.clips[0]).overlays == []
    fr.write_manifest(out, rows)
    assert "| `V02-chat.mp4` | 2 с | V02-chat | Чат с задачей |" in (out / "fragments.md").read_text(encoding="utf-8")

    def row_std(path, t):
        raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
        a = np.frombuffer(raw, np.uint8).reshape(-1, 1920).astype(int)
        return a[a.shape[0] // 2, 400:600].std()

    assert row_std(out / "V02-chat.mp4", 1.0) > 40               # без размытия — полоски
    assert row_std(out / "02-fragment.mp4", 1.0) < 20            # размытие перешло на второй фрагмент
    assert row_std(out / "02-fragment.mp4", 0.2) > 40            # но начинается с 0,5 с


@needs_ffmpeg
def test_name_and_note_in_inspector(tmp_path, qt_app):
    from glimpsy.editor.window import EditorWindow
    from glimpsy.recorder.encoder import software_encoder

    proj = tmp_path / "project_20260925_101010"
    proj.mkdir()
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:size=320x180:rate=30",
                    "-t", "3", "-pix_fmt", "yuv420p", str(proj / "piece_0000.mp4")], check=True)
    (proj / "project.json").write_text(json.dumps({"output": str(tmp_path / "W.mp4"), "clips": [
        {"file": "piece_0000.mp4", "duration": 3.0, "app": "Chrome", "title": "Story Map — Google Chrome"}]}))
    w = EditorWindow(proj, FFMPEG, software_encoder, tmp_path)
    try:
        c = w.project.clips[0]
        assert c.app == "Chrome" and c.title == "Story Map — Google Chrome"
        w.timeline.select(c.id)
        w._refresh_inspector()
        insp = w.inspector
        assert insp.name.placeholderText() == "Story Map"
        assert insp.note.placeholderText().startswith("Chrome: «Story Map»")
        insp.name.textEdited.emit("V05-story-map")
        insp.note.setPlainText("Карта событий")
        assert c.name == "V05-story-map" and c.note == "Карта событий"
        w.project.save()
        saved = json.loads((proj / "edit.json").read_text())["clips"][0]
        assert saved["name"] == "V05-story-map" and saved["note"] == "Карта событий"
        w.undo()
        assert w.project.clips[0].note == ""
    finally:
        w.player.shutdown()
        w.close()
