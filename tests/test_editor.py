"""Редактор: модель проекта, отмена, разбор файлов и настоящий экспорт через FFmpeg."""

import json
import subprocess
import time

import pytest

from worklapse import paths
from worklapse.editor.export import atempo_chain, default_output, export_project
from worklapse.editor.media import parse_probe
from worklapse.editor.project import Clip, History, Project, list_projects
from worklapse.recorder.encoder import software_encoder

FFMPEG = paths.find_executable("ffmpeg")


def make_project(tmp_path, n=3, dur=4.0):
    return Project(tmp_path, "test", [Clip(f"c{i}", "video", f"p{i}.mp4", dur, 0.0, dur) for i in range(n)])


def test_locate_and_split(tmp_path):
    p = make_project(tmp_path)
    assert p.total == 12
    assert p.locate(5.0) == (1, 1.0)
    right = p.split(5.0)
    assert right == 2 and len(p.clips) == 4
    assert p.clips[1].out_s == 1.0 and p.clips[2].in_s == 1.0
    assert abs(p.total - 12) < 1e-9


def test_speed_changes_duration(tmp_path):
    p = make_project(tmp_path)
    p.set_speed(0, 4)
    assert p.clips[0].duration == 1.0
    p.set_speed(0, 50)
    assert p.clips[0].speed == 10          # максимум ×10


def test_move_and_trim(tmp_path):
    p = make_project(tmp_path)
    p.move(0, 3)
    assert [c.id for c in p.clips] == ["c1", "c2", "c0"]
    p.trim(0, -5, 99)                       # за пределы файла не выйти
    assert (p.clips[0].in_s, p.clips[0].out_s) == (0.0, 4.0)


def test_undo_redo_and_merge(tmp_path):
    p = make_project(tmp_path)
    h = History()
    h.push(p.to_dict())
    p.delete(0)
    h.push(p.to_dict(), key="speed:c1")
    p.set_speed(0, 2)
    h.push(p.to_dict(), key="speed:c1")    # та же правка подряд — одна запись
    p.set_speed(0, 3)
    p.restore(h.undo(p.to_dict()))
    assert len(p.clips) == 2 and p.clips[0].speed == 1
    p.restore(h.undo(p.to_dict()))
    assert len(p.clips) == 3
    p.restore(h.redo(p.to_dict()))
    assert len(p.clips) == 2


def test_load_stage1_project_and_save(tmp_path):
    (tmp_path / "project.json").write_text(json.dumps({
        "output": str(tmp_path / "Worklapse_2026-09-24_10-00.mp4"), "fps": 30,
        "clips": [{"file": "piece_0000.mp4", "duration": 3.5, "speed": 1.25, "recorded_at": time.time(),
                   "monitor": 2, "source_size": [1920, 1080], "priority": True, "cursor": []}]}))
    p = Project.load(tmp_path)
    assert p.clips[0].priority and p.clips[0].duration == 3.5 and "монитор 2" in p.clips[0].label
    p.set_speed(0, 2)
    p.save()
    again = Project.load(tmp_path)
    assert again.clips[0].speed == 2 and again.edited
    assert tmp_path in list_projects(tmp_path.parent)
    assert default_output(p, tmp_path).name == "Worklapse_2026-09-24_10-00_edit.mp4"


def test_atempo():
    assert atempo_chain(1.0) == ["atempo=1.00000"]
    assert atempo_chain(10.0)[:3] == ["atempo=2.0"] * 3


def test_parse_probe_phone_video():
    text = ("Duration: 00:00:12.50, start: 0.0\n Stream #0:0[0x1](und): Video: h264, yuv420p, 1920x1080, 30 fps\n"
            "    Side data:\n      displaymatrix: rotation of -90.00 degrees\n Stream #0:1: Audio: aac, 48000 Hz")
    info = parse_probe(text)
    assert (info.width, info.height, info.has_audio, info.duration) == (1080, 1920, True, 12.5)


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_real_export_both_formats(tmp_path):
    def gen(args, name):
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args, str(tmp_path / name)], check=True)
    gen(["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-f", "lavfi", "-i", "sine=f=440",
         "-t", "3", "-c:v", "libx264", "-c:a", "aac", "-shortest"], "land.mp4")
    gen(["-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30", "-t", "2", "-c:v", "libx264"], "vert.mp4")
    gen(["-f", "lavfi", "-i", "color=c=red:size=400x300", "-frames:v", "1"], "photo.png")
    p = Project(tmp_path, "t", [
        Clip("a", "video", "land.mp4", 3, 0.5, 2.5, speed=2.0, has_audio=True, width=640, height=360),
        Clip("b", "video", "vert.mp4", 2, 0, 2, width=360, height=640),
        Clip("c", "image", "photo.png", 3600, 0, 1.5, width=400, height=300),
    ])
    for aspect, size in (("16:9", "1920x1080"), ("9:16", "1080x1920")):
        p.aspect = aspect
        out = export_project(FFMPEG, p, tmp_path / f"out_{aspect.replace(':', 'x')}.mp4", software_encoder())
        info = subprocess.run([FFMPEG, "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
        assert size in info and "Audio: aac" in info
        dur = parse_probe(info).duration
        assert abs(dur - (1.0 + 2.0 + 1.5)) < 0.3, dur
