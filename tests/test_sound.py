"""Чистый голос и одинаковая громкость при сохранении ролика."""

import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from glimpsy import paths
from glimpsy.editor import export as ex
from glimpsy.editor import sound
from glimpsy.editor.project import Clip, Project
from glimpsy.recorder.encoder import software_encoder

FFMPEG = paths.find_executable("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
# веса RNNoise в тестах не скачиваем; если файл лежит рядом (GLIMPSY_TEST_RNNN) — проверим и нейросеть
RNNN = os.environ.get("GLIMPSY_TEST_RNNN", "")


def make_source(path, voice=0.3, noise=0.03, d=12):
    # «голос» — тон, который звучит 1 с через 1 с; под ним всё время шипение
    subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"testsrc2=size=640x360:rate=30:d={d}", "-f", "lavfi",
                    "-i", f"aevalsrc='{voice}*lt(mod(t,2),1)*sin(2*PI*220*t)*(0.6+0.4*sin(2*PI*3*t))':s=48000:d={d}",
                    "-f", "lavfi", "-i", f"anoisesrc=a={noise}:c=white:r=48000:d={d}",
                    "-filter_complex", "[1:a][2:a]amix=inputs=2:normalize=0[a]", "-map", "0:v", "-map", "[a]",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-b:a", "256k", str(path)], check=True)


def project_for(tmp_path, video, d=12):
    p = Project.for_videos(tmp_path / "projects", [video],
                           [SimpleNamespace(duration=float(d), has_audio=True, width=640, height=360, fps=30)])
    src = p.clips[0].src
    p.clips = [Clip("a", "video", src, float(d), 0.0, 5.0, has_audio=True, width=640, height=360),
               Clip("b", "video", src, float(d), 6.0, float(d), has_audio=True, width=640, height=360)]
    return p


def pcm(path):
    raw = subprocess.run([FFMPEG, "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", "8000", "-f", "f32le",
                          "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.float32)


def gaps_and_voice(a):
    """Громкость в паузах (шум) и в «голосе» — по секундам (чётные — голос, нечётные — пауза)."""
    sec = [a[i * 8000 + 1500:(i + 1) * 8000 - 1500] for i in range(len(a) // 8000)]
    rms = [float(np.sqrt(np.mean(s ** 2))) for s in sec]
    return rms


def lufs(path):
    r = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "ebur128", "-f", "null",
                        "-"], capture_output=True, text=True)
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)[-1])


def test_settings_are_clamped():
    p = SimpleNamespace(sound={"denoise": 140, "lufs": -40})
    s = sound.settings(p)
    assert s["denoise"] == 100 and s["lufs"] == -24 and not s["level"]
    assert not sound.active(SimpleNamespace(sound={}))
    assert "afftdn" in sound.denoise_chain(50, None) and "highpass" in sound.denoise_chain(50, None)
    assert sound.denoise_chain(0, None) == ""
    assert "arnndn=m=sh.rnnn:mix=0.50" in sound.denoise_chain(50, Path("/x/sh.rnnn"))


@needs_ffmpeg
@pytest.mark.parametrize("model", ["afftdn"] + (["rnnoise"] if RNNN else []))
@pytest.mark.parametrize("smart", [True, False])
def test_denoise_lowers_noise_keeps_voice(tmp_path, monkeypatch, model, smart):
    video = tmp_path / "src.mp4"
    make_source(video)
    p = project_for(tmp_path, video)
    if model == "rnnoise":
        target = tmp_path / "models" / "sh.rnnn"
        target.parent.mkdir()
        target.write_bytes(Path(RNNN).read_bytes())
        monkeypatch.setattr(sound, "model_path", lambda: target)
    else:
        monkeypatch.setattr(sound, "model_path", lambda: tmp_path / "нет" / "sh.rnnn")
    plain = ex.export_project(FFMPEG, p, tmp_path / "plain.mp4", software_encoder(), smart=smart)
    p.sound = {"denoise": 100}
    clean = ex.export_project(FFMPEG, p, tmp_path / "clean.mp4", software_encoder(), smart=smart)
    before, after = gaps_and_voice(pcm(plain)), gaps_and_voice(pcm(clean))
    # итог: 0–5 с исходника, потом 6–12 (исх. 6 → итог 5) → паузы в итоге на секундах 1, 3, 6, 8
    gaps = [1, 3, 6, 8]
    voice = [0, 2, 4, 5, 7, 9]
    noise_drop = np.mean([after[i] for i in gaps]) / np.mean([before[i] for i in gaps])
    voice_kept = np.mean([after[i] for i in voice]) / np.mean([before[i] for i in voice])
    assert noise_drop < 0.6, (before, after)
    if model == "afftdn":              # RNNoise ждёт настоящую речь — тон для неё тоже «шум»
        assert voice_kept > 0.6, (before, after)


@needs_ffmpeg
@pytest.mark.parametrize("target", [-14.0, -20.0])
def test_level_reaches_target(tmp_path, target):
    video = tmp_path / "src.mp4"
    make_source(video, voice=0.03, noise=0.002)             # тихая запись
    p = project_for(tmp_path, video)
    p.sound = {"level": True, "lufs": target}
    out = ex.export_project(FFMPEG, p, tmp_path / "out.mp4", software_encoder(), smart=True)
    assert lufs(out) == pytest.approx(target, abs=1.5)
    dur = subprocess.run([FFMPEG, "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
    assert "Video: h264" in dur and "Audio: aac" in dur


@needs_ffmpeg
def test_preview_before_after(tmp_path, monkeypatch):
    video = tmp_path / "src.mp4"
    make_source(video)
    monkeypatch.setattr(sound, "model_path", lambda: tmp_path / "нет" / "sh.rnnn")
    p = SimpleNamespace(sound={"denoise": 100, "level": True, "lufs": -14})
    b, a = tmp_path / "b.wav", tmp_path / "a.wav"
    sound.preview(FFMPEG, p, video, 2.0, b, a, length=4.0)
    assert len(pcm(b)) == pytest.approx(4 * 8000, abs=400) and len(pcm(a)) == pytest.approx(4 * 8000, abs=400)


@needs_ffmpeg
def test_enhance_panel_in_editor(tmp_path, qt_app, monkeypatch):
    """Ползунки «Улучшить» меняют проект, отмена возвращает; «как было / как будет» готовит звук."""
    import json

    from PySide6.QtCore import QCoreApplication, QTimer

    from glimpsy.editor.window import EditorWindow
    from glimpsy.recorder.encoder import software_encoder as sw

    monkeypatch.setattr(sound, "model_path", lambda: tmp_path / "нет" / "sh.rnnn")
    monkeypatch.setattr(EditorWindow, "_ensure_denoise_model", lambda self: None)   # без интернета
    proj = tmp_path / "project_20260925_101010"
    proj.mkdir()
    make_source(proj / "piece_0000.mp4", d=4)
    (proj / "project.json").write_text(json.dumps({"output": str(tmp_path / "W.mp4"),
                                                   "clips": [{"file": "piece_0000.mp4", "duration": 4.0,
                                                              "has_audio": True}]}))
    w = EditorWindow(proj, FFMPEG, sw, tmp_path)
    try:
        w.toggle_left("enhance")
        assert not w.enhance.isHidden()
        w.enhance.denoise.setValue(60)
        w.enhance.level.setChecked(True)
        w.enhance.lufs.setValue(-11)
        assert w.project.sound == {"denoise": 60, "level": True, "lufs": -11.0}
        w.undo()
        assert w.project.sound.get("lufs") is None and w.enhance.lufs.value() == -14
        played = []
        monkeypatch.setattr(w, "_play_listen", lambda path: played.append(path))
        w.player.seek(0.5)
        w._listen("after")
        t = QTimer()
        t.setSingleShot(True)
        t.start(15000)
        while not played and t.isActive():
            QCoreApplication.processEvents()
        assert played, w.enhance.listen_status.text()
        assert played[0].name.startswith("after") and played[0].stat().st_size > 10_000
    finally:
        w.player.shutdown()
        w.close()
