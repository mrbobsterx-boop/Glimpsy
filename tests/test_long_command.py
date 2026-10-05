"""Очень длинный граф фильтров (плавный зум, свой курсор) не должен ломать запуск FFmpeg.

Windows не запускает команду длиннее ~32 тысяч знаков (WinError 206), Linux — аргумент длиннее
128 КБ. Такие графы передаются через файл (paths.short_command).
"""

import subprocess

import pytest

from glimpsy import paths

FFMPEG = paths.find_executable("ffmpeg")


def test_short_commands_unchanged():
    cmd = ["ffmpeg", "-i", "a.mp4", "-vf", "scale=10:10", "b.mp4"]
    with paths.short_command(cmd) as c:
        assert c == cmd


def test_long_graph_goes_to_file():
    graph = "[0:v]" + ",".join(["null"] * 3000) + "[v]"
    cmd = ["ffmpeg", "-i", "a.mp4", "-filter_complex", graph, "-map", "[v]", "b.mp4"]
    with paths.short_command(cmd) as c:
        i = c.index("-/filter_complex")
        f = c[i + 1]
        assert open(f, encoding="utf-8").read() == graph and len(" ".join(c)) < 1000
    import os
    assert not os.path.exists(f)                         # файл убран


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_huge_graph_runs(tmp_path):
    # ≈40 КБ — больше предела Windows (длинное значение, как у выражений плавного зума)
    graph = "[0:v]metadata=mode=add:key=k:value=" + "a" * 40000 + ",scale=32:18[v]"
    out = tmp_path / "o.mp4"
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x36:d=0.2",
           "-filter_complex", graph, "-map", "[v]", "-pix_fmt", "yuv420p", str(out)]
    with paths.short_command(cmd) as c:
        r = subprocess.run(c, capture_output=True)
    assert r.returncode == 0, r.stderr[-500:]
    assert out.stat().st_size > 0
