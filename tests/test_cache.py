from pathlib import Path

from worklapse import cache, paths


def test_clear_keeps_videos_and_current_session(tmp_path, monkeypatch):
    data, temp, videos = tmp_path / "data", tmp_path / "temp", tmp_path / "Videos"
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    monkeypatch.setattr(paths, "temp_root", lambda: temp)
    for d in (data / "projects" / "project_1", data / "fonts", temp / "thumbs", temp / "session_old",
              temp / "session_now", videos):
        d.mkdir(parents=True)
    (data / "projects" / "project_1" / "piece_0000.mp4").write_bytes(b"x" * 1000)
    (data / "fonts" / "my.ttf").write_bytes(b"f")
    (temp / "thumbs" / "a.jpg").write_bytes(b"t" * 10)
    (temp / "session_old" / "cand.ts").write_bytes(b"c" * 100)
    (temp / "session_now" / "cand.ts").write_bytes(b"n")
    (videos / "Worklapse_edit.mp4").write_bytes(b"v")

    report = cache.scan(keep=temp / "session_now", output_dir=videos)
    assert report.projects == 1 and report.project_bytes == 1000 and report.other_bytes == 110
    freed = cache.clear(report)
    assert freed == 1110
    assert not (data / "projects" / "project_1").exists()
    assert (data / "fonts" / "my.ttf").exists()                 # шрифты остаются
    assert (temp / "session_now" / "cand.ts").exists()          # идущая запись не тронута
    assert (videos / "Worklapse_edit.mp4").exists()             # готовые ролики на месте


def test_output_inside_cache_is_protected(tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(paths, "temp_root", lambda: temp)
    (temp / "videos").mkdir(parents=True)
    (temp / "videos" / "a.mp4").write_bytes(b"v")
    report = cache.scan(output_dir=temp / "videos")
    assert Path(temp / "videos") not in report.paths


def test_human():
    assert cache.human(500) == "500 байт" and cache.human(1536) == "1.5 КБ"
    assert cache.human(3 * 1024 ** 3) == "3.0 ГБ"
