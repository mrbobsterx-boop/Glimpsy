"""«Продолжить запись»: новые фрагменты дописываются в тот же проект, в конец смонтированной ленты."""

import json
import subprocess
import time

import pytest

from glimpsy import paths

FFMPEG = paths.find_executable("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")


def test_new_clips_go_after_my_order(tmp_path):
    from glimpsy.editor.project import Project

    proj = tmp_path / "project_20260925_101010"
    proj.mkdir()
    meta = {"output": str(tmp_path / "A.mp4"), "clips": [{"file": f"piece_000{i}.mp4", "duration": 2.0}
                                                         for i in range(3)], "camera": []}
    (proj / "project.json").write_text(json.dumps(meta))
    p = Project.load(proj)
    p.clips.reverse()                                   # свой порядок
    p.clips[0].note = "моё"
    p.save()
    # продолжили запись: автосборка дописала 2 фрагмента и окошко камеры (время — от начала всех записей)
    meta["clips"] += [{"file": "piece_0003.mp4", "duration": 1.5}, {"file": "piece_0004.mp4", "duration": 1.0}]
    meta["camera"] = [{"file": "camera_000.mp4", "start": 6.5, "duration": 1.0, "width": 320, "height": 180}]
    meta["parts"] = [str(tmp_path / "B.mp4")]
    (proj / "project.json").write_text(json.dumps(meta))
    p2 = Project.load(proj)
    assert [c.src for c in p2.clips] == ["piece_0002.mp4", "piece_0001.mp4", "piece_0000.mp4",
                                         "piece_0003.mp4", "piece_0004.mp4"]
    assert p2.clips[0].note == "моё"
    [cam] = p2.overlays
    assert cam.start == pytest.approx(6.5)              # 6 с старой ленты + 0,5 с от начала новой записи
    assert len(Project.load(proj).clips) == 5          # при следующем открытии не дублируются

    from glimpsy.editor.sessions import project_for_video
    import glimpsy.editor.sessions as sessions

    orig = sessions.projects_root
    sessions.projects_root = lambda: tmp_path
    try:
        assert project_for_video(tmp_path / "B.mp4") == proj
    finally:
        sessions.projects_root = orig


def test_old_projects_are_not_changed(tmp_path):
    from glimpsy.editor.project import Project

    proj = tmp_path / "project_20260925_101010"
    proj.mkdir()
    (proj / "project.json").write_text(json.dumps({"clips": [{"file": "a.mp4", "duration": 2.0},
                                                             {"file": "b.mp4", "duration": 2.0}]}))
    p = Project.load(proj)
    p.clips.pop()                                       # фрагмент удалили в редакторе
    data = p.to_dict()
    data["cuts"] = {}                                   # edit.json старой версии — без счётчиков
    (proj / "edit.json").write_text(json.dumps(data))
    assert [c.src for c in Project.load(proj).clips] == ["a.mp4"]   # удалённое не возвращается


@needs_ffmpeg
def test_assembler_appends_to_existing_project(tmp_path, qt_app):
    from glimpsy.assembler import Assembler
    from glimpsy.config import Settings
    from glimpsy.editor.project import Project
    from glimpsy.recorder.candidates import Candidate
    from glimpsy.recorder.encoder import software_encoder
    from glimpsy.recorder.pacing import make_plan

    s = Settings(auto_video=True, output_dir=str(tmp_path / "out"), target_length_s=3, clip_min_s=3, clip_max_s=3, pace="calm",
                 output_width=640, output_height=360, fx_zoom=False, fx_clicks=False).validate()
    proj = tmp_path / "project_20260925_101010"

    def record(color: str, sub: str) -> None:
        sess = tmp_path / sub
        sess.mkdir()
        subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={color}:size=640x360:rate=30",
                        "-t", "4", "-c:v", "libx264", "-g", "30", "-bf", "0", "-f", "mpegts", str(sess / "c.ts")],
                       check=True)
        now = time.time()
        c = Candidate(id=1, file="c.ts", wall_start=now, wall_end=now + 4, want_start=now, want_end=now + 4,
                      monitor=1, width=640, height=360, score=0.5, activity=[0.5] * 4, app="Chrome")
        Assembler(FFMPEG, software_encoder(), s, make_plan(s)).run(sess, [c], now, project_dir=proj)

    record("red", "s1")
    p = Project.load(proj)
    p.clips[0].name = "первый"
    p.save()
    first = p.clips[0].src
    record("blue", "s2")
    meta = json.loads((proj / "project.json").read_text())
    assert len(meta["clips"]) == 2 and len(meta["parts"]) == 1
    assert meta["clips"][1]["file"] != first and (proj / meta["clips"][1]["file"]).exists()
    p2 = Project.load(proj)
    assert [c.src for c in p2.clips] == [first, meta["clips"][1]["file"]]
    assert p2.clips[0].name == "первый" and p2.clips[1].app == "Chrome"
    assert "первый" in (proj / "fragments.md").read_text(encoding="utf-8")

