"""Проект в один файл: упаковать на одном компьютере, открыть на другом (Windows → Linux тоже)."""

import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from glimpsy import paths
from glimpsy.editor import package
from glimpsy.editor import transcript as tr
from glimpsy.editor.project import Project
from glimpsy.editor.transcript import SourceWords, Word

FFMPEG = paths.find_executable("ffmpeg")


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_export_and_import_text_project(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    video = work / "мой ролик.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:size=160x90:rate=30:d=3",
                    "-pix_fmt", "yuv420p", str(video)], check=True)
    p = Project.for_videos(work / "projects", [video],
                           [SimpleNamespace(duration=3.0, has_audio=False, width=160, height=90)])
    src = p.clips[0].src
    tr.TranscriptStore(p.dir).put(SourceWords(src, 3.0, [Word("раз", 0.5, 0.8), Word("два", 1.0, 1.3)]), None)
    p.cuts.setdefault("deleted", {})[src] = [1]
    (p.dir / "media").mkdir()
    (p.dir / "media" / "logo.png").write_bytes(b"png")
    p.save()

    archive = package.export_zip(p.dir, tmp_path / "out" / "проект.glimpsy.zip")
    shutil.rmtree(work)                                        # «другой компьютер»: исходников нет

    home = tmp_path / "home" / "projects"
    d = package.import_zip(archive, home)
    q = Project.load(d)
    new_src = q.clips[0].src
    assert new_src != src and q.path_of(q.clips[0]).is_file()
    assert q.path_of(q.clips[0]).parent == d / "sources"
    assert q.cuts["deleted"][new_src] == [1] and src not in q.cuts["deleted"]
    sw = tr.TranscriptStore(d).get(new_src)
    assert sw is not None and [w.text for w in sw.words] == ["раз", "два"]
    assert (d / "media" / "logo.png").read_bytes() == b"png"
    assert package.import_zip(archive, home) != d               # второй раз — рядом, не поверх


def test_windows_paths_are_relinked(tmp_path):
    win = "C:\\Users\\info\\Videos\\запись.mp4"
    dest = tmp_path / "proj"
    (dest / "transcript").mkdir(parents=True)
    (dest / "sources").mkdir()
    (dest / "sources" / "запись.mp4").write_bytes(b"v")
    (dest / "edit.json").write_text(json.dumps({"name": "x", "source_video": "C:\\Users\\info\\Videos\\out.mp4",
                                                "clips": [{"src": win}], "cuts": {"deleted": {win: [2]},
                                                                                  "sources": [{"src": win}]}},
                                               ensure_ascii=False), encoding="utf-8")
    (dest / "transcript" / f"{tr.source_key(win)}.json").write_text(json.dumps({"src": win, "words": []}),
                                                                    encoding="utf-8")
    package._relink(dest, {"sources/запись.mp4": win})
    new = str(dest / "sources" / "запись.mp4")
    data = json.loads((dest / "edit.json").read_text(encoding="utf-8"))
    assert data["clips"][0]["src"] == new and data["cuts"]["deleted"] == {new: [2]}
    assert data["source_video"] == ""                          # старого готового ролика здесь нет
    moved = dest / "transcript" / f"{tr.source_key(new)}.json"
    assert moved.exists() and json.loads(moved.read_text(encoding="utf-8"))["src"] == new


def test_bad_archive(tmp_path):
    bad = tmp_path / "x.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(package.PackageError):
        package.import_zip(bad, tmp_path / "p")
