"""Быстрое сохранение нарезки: нетронутые куски видео берутся как есть, края пересчитываются."""

import re
import subprocess
from types import SimpleNamespace

import numpy as np
import pytest

from glimpsy import paths
from glimpsy.editor import export as ex
from glimpsy.editor import smartcut
from glimpsy.editor.project import Clip, Project
from glimpsy.recorder.encoder import software_encoder

FFMPEG = paths.find_executable("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")

CUTS = [(0.40, 6.73), (8.10, 8.70), (10.25, 21.90), (21.90, 24.05), (30.5, 39.0)]


def make_source(path, bf=2):
    # картинка меняется каждый кадр (по ней видно сдвиг), в звуке — щелчок в начале каждой секунды
    subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=size=640x360:rate=30:d=40", "-f", "lavfi",
                    "-i", "aevalsrc='if(lt(mod(t,1),0.03),0.8*sin(2*PI*1000*t),0)':s=48000:d=40",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-bf", str(bf), "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(path)], check=True)


def project_for(tmp_path, video):
    p = Project.for_videos(tmp_path / "projects", [video],
                           [SimpleNamespace(duration=40.0, has_audio=True, width=640, height=360, fps=30)])
    src = p.clips[0].src
    p.clips = [Clip(f"c{i}", "video", src, 40.0, a, b, has_audio=True, width=640, height=360)
               for i, (a, b) in enumerate(CUTS)]
    return p


def test_parse_scan_finds_keyframes():
    crc = ("#tb 0: 1/15360\n#dimensions 0: 640x360\n"
           "0,      -1024,          0,      512,    37523, 0x04dd960d\n"
           "0,       -512,       1024,      512,    24696, 0x3f215488, F=0x0\n"
           "0,          0,        512,      512,    24500, 0x5829c30a, F=0x0\n"
           "0,        512,       1536,      512,    24500, 0x5829c30a\n")
    head = "Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), 640x360, 30 fps"
    s = smartcut.parse_scan(crc, head)
    assert s.codec == "h264" and s.pix_fmt == "yuv420p" and s.fps == pytest.approx(30)
    assert s.keys == [0.0, 0.1] and len(s.pts) == 4 and s.timescale == 15360


@needs_ffmpeg
@pytest.mark.parametrize("bf", [0, 2])
def test_smart_export_matches_exact_cut(tmp_path, bf):
    video = tmp_path / "src.mp4"
    make_source(video, bf)
    p = project_for(tmp_path, video)
    plan = smartcut.plan_for(FFMPEG, p, p.clips)
    assert plan is not None and plan.copied > 0.6
    assert any(not x.copy for x in plan.pieces)              # короткий кусок 8.1–8.7 пересчитан целиком
    out = ex.export_project(FFMPEG, p, tmp_path / "out.mp4", software_encoder(), smart=True)

    frames = sum(round((b - a) * 30) for a, b in CUTS)
    r = subprocess.run([FFMPEG, "-v", "error", "-i", str(out), "-f", "null", "-"], capture_output=True, text=True)
    assert r.stderr.strip() == ""                               # декодируется без ошибок
    # каждый кадр — тот же, что в исходнике в этом месте (сдвиг хоть на кадр дал бы большую разницу)
    sel = "+".join(f"gte(t,{a - 0.001})*lt(t,{b - 0.001})" for a, b in CUTS)
    log = tmp_path / "psnr.log"
    subprocess.run([FFMPEG, "-v", "error", "-i", str(out), "-i", str(video), "-filter_complex",
                    f"[1:v]select='{sel}',setpts=N/30/TB[r];[0:v]setpts=N/30/TB[o];"
                    f"[o][r]psnr=stats_file='{log.as_posix()}'", "-f", "null", "-"], check=True)
    values = [float(v) for v in re.findall(r"psnr_avg:([\d.]+|inf)", log.read_text()) if v != "inf"]
    n = len(log.read_text().splitlines())
    assert n == frames
    assert not values or min(values) > 30
    assert len(values) < frames * 0.4                            # большая часть — без пересчёта (точно как было)
    # звук: щелчки там, где им положено быть
    pcm = subprocess.run([FFMPEG, "-loglevel", "error", "-i", str(out), "-ac", "1", "-ar", "8000", "-f", "f32le",
                          "-"], capture_output=True).stdout
    a = np.abs(np.frombuffer(pcm, np.float32))
    onsets = [i / 8000 for i in range(1, len(a)) if a[i] > 0.3 and a[max(0, i - 400):i].max() < 0.3]
    expected, start = [], 0.0
    for s0, s1 in CUTS:
        expected += [start + k - s0 for k in range(int(s0) + 1, int(np.ceil(s1))) if s0 + 0.05 < k < s1 - 0.05]
        start += round((s1 - s0) * 30) / 30
    for t in expected:
        assert min(abs(t - o) for o in onsets) < 0.02, (t, onsets)
    assert len(a) / 8000 == pytest.approx(frames / 30, abs=0.05)


@needs_ffmpeg
def test_smart_not_used_for_effects(tmp_path):
    video = tmp_path / "src.mp4"
    make_source(video)
    p = project_for(tmp_path, video)
    p.clips[1].hidden = True                                     # чёрный кадр — нужен пересчёт
    assert smartcut.plan_for(FFMPEG, p, p.clips) is None
    p.clips[1].hidden = False
    p.aspect = "9:16"                                            # другой формат кадра
    assert smartcut.plan_for(FFMPEG, p, p.clips) is None
