"""Субтитры «караоке»: подсвечивается слово, которое звучит."""

import subprocess

import numpy as np
import pytest

from glimpsy import paths
from glimpsy.editor import text as tx
from glimpsy.editor import transcript as tr
from glimpsy.editor.text import TextItem

FFMPEG = paths.find_executable("ffmpeg")


def item(**kw):
    return TextItem("t", "раз два три четыре", 10.0, 4.0, **kw)


def test_word_times_exact_and_estimated():
    exact = item(words=[[0.1, 0.4], [0.6, 0.9], [1.5, 1.9], [2.5, 3.6]])
    assert tx.active_word(exact, 10.0) == 0 and tx.active_word(exact, 11.6) == 2 and tx.active_word(exact, 13.9) == 3
    segs = tx.karaoke_segments(exact)
    assert [k for *_x, k in segs] == [0, 1, 2, 3]
    assert segs[0][0] == 10.0 and segs[-1][1] == 14.0 and segs[1][0] == pytest.approx(10.6)
    est = item(words=[[0, 1]])                                 # не совпадает с числом слов — оцениваем
    times = tx.word_times(est)
    assert len(times) == 4 and times[0][0] < times[1][0] < times[3][0] and times[-1][1] <= 4.0


def test_cue_words_follow_cues():
    words = [(0.5 + k * 0.5, 0.8 + k * 0.5, w) for k, w in enumerate("раз два три. четыре пять".split())]
    lines = tr.cues(words, 42)
    timing = tr.cue_words(words, 42)
    assert [len(t) for t in timing] == [len(t.split()) for _a, _b, t in lines]
    assert timing[1][0] == [0.0, 0.3]


@pytest.mark.parametrize("mode", ["color", "box", "fill", "word"])
def test_render_highlights_active_word(qt_app, mode):
    style = dict(tx.DEFAULT_STYLE, karaoke=mode, hl_color="#FF0000", bg=False)
    a = tx.render_text("раз два три", style, 1280, 720, 0)
    b = tx.render_text("раз два три", style, 1280, 720, 2)

    def red(img, part):
        w = img.width()
        xs = range(0, w // 3) if part == 0 else range(2 * w // 3, w)
        return sum(1 for x in xs for y in range(img.height()) if img.pixelColor(x, y).red() > 200
                   and img.pixelColor(x, y).green() < 80 and img.pixelColor(x, y).alpha() > 200)

    if mode == "word":
        assert a.width() < tx.render_text("раз два три", dict(style, karaoke="none"), 1280, 720).width()
        return
    assert (a.width(), a.height()) == (b.width(), b.height())      # текст не прыгает
    assert red(a, 0) > 20 and red(b, 2) > 20
    if mode != "fill":
        assert red(a, 2) == 0
    assert tx.effective_style(TextItem("x", "a", 0, 1, style={"karaoke": mode}), {})["anim"] == "none"


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_export_draws_word_by_word(tmp_path, qt_app):
    from types import SimpleNamespace

    from glimpsy.editor import export as ex
    from glimpsy.editor.project import Project
    from glimpsy.editor.text import TextItem
    from glimpsy.recorder.encoder import software_encoder

    video = tmp_path / "v.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=black:size=640x360:rate=30:d=3",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "3", "-c:v", "libx264", "-pix_fmt",
                    "yuv420p", "-c:a", "aac", str(video)], check=True)
    p = Project.for_videos(tmp_path / "projects", [video],
                           [SimpleNamespace(duration=3.0, has_audio=True, width=640, height=360, fps=30)])
    p.text_style = {"karaoke": "color", "hl_color": "#FF0000", "bg": False}
    p.texts = [TextItem("s", "раз два три", 0.0, 3.0, words=[[0, 0.9], [1, 1.9], [2, 2.9]], pos={"16:9": [0.5, 0.5]})]
    layers = ex.render_text_layers(p, tmp_path / "layers")
    assert len(layers) == 3
    out = ex.export_project(FFMPEG, p, tmp_path / "out.mp4", software_encoder(), text_layers=layers)

    def red_at(t):
        raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", str(t), "-i", str(out), "-frames:v", "1",
                              "-vf", "scale=192:108", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True).stdout
        a = np.frombuffer(raw, np.uint8).reshape(108, 192, 3).astype(int)
        redness = (a[..., 0] > 150) & (a[..., 1] < 90)
        cols = np.nonzero(redness.any(axis=0))[0]
        return cols.mean() if cols.size else None

    x0, x2 = red_at(0.5), red_at(2.5)
    assert x0 is not None and x2 is not None and x2 > x0 + 20          # подсветка переехала вправо


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_telegram_file_is_smaller(tmp_path):
    from glimpsy.editor import export as ex

    src = tmp_path / "big.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:d=3",
                    "-f", "lavfi", "-i", "sine=d=3", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", str(src)], check=True)
    pf = ex.PLATFORMS["telegram"]
    out = tmp_path / "small.mp4"
    ex.shrink(FFMPEG, src, out, pf["height"], pf["crf"], pf["audio"])
    info = subprocess.run([FFMPEG, "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
    assert "1280x720" in info and out.stat().st_size < src.stat().st_size / 2
    assert ex.PLATFORMS["shorts"]["aspect"] == "9:16"
