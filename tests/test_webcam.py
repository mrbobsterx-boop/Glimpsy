"""Веб-камера: поиск устройств, съёмка (вместо камеры — тестовый сигнал FFmpeg), сборка с окошком."""

import json
import subprocess
import time

import pytest

from glimpsy import paths
from glimpsy.recorder import webcam
from glimpsy.recorder.webcam import CamClip, CamStore, Webcam

FFMPEG = paths.find_executable("ffmpeg")
FAKE_CAM = ["-f", "lavfi", "-i", "testsrc2=size=640x480:rate=30"]

DSHOW_NEW = '''[dshow @ 0000] "Integrated Camera" (video)
[dshow @ 0000]   Alternative name "@device_pnp_\\\\?\\usb#vid_04f2"
[dshow @ 0000] "OBS Virtual Camera" (video)
[dshow @ 0000] "Microphone (Realtek Audio)" (audio)
'''
DSHOW_OLD = '''[dshow @ 0000] DirectShow video devices (some may be both video and audio devices)
[dshow @ 0000]  "USB2.0 HD UVC WebCam"
[dshow @ 0000]     Alternative name "@device_pnp_\\\\?\\usb"
[dshow @ 0000] DirectShow audio devices
[dshow @ 0000]  "Microphone"
'''
AVF = '''[AVFoundation indev @ 0x1] AVFoundation video devices:
[AVFoundation indev @ 0x1] [0] FaceTime HD Camera
[AVFoundation indev @ 0x1] [1] iPhone Camera
[AVFoundation indev @ 0x1] [2] Capture screen 0
[AVFoundation indev @ 0x1] AVFoundation audio devices:
[AVFoundation indev @ 0x1] [0] MacBook Pro Microphone
'''


def test_parse_device_lists(tmp_path):
    assert [c.name for c in webcam.parse_dshow(DSHOW_NEW)] == ["Integrated Camera", "OBS Virtual Camera"]
    assert [c.name for c in webcam.parse_dshow(DSHOW_OLD)] == ["USB2.0 HD UVC WebCam"]
    assert [c.name for c in webcam.parse_avfoundation(AVF)] == ["FaceTime HD Camera", "iPhone Camera"]
    # Linux: у камеры два узла /dev/video0 (картинка) и /dev/video1 (служебный) — берём только первый
    for n, name, idx in [(0, "HD Webcam", "0"), (1, "HD Webcam", "1"), (2, "USB Cam", "0")]:
        d = tmp_path / f"video{n}"
        d.mkdir()
        (d / "name").write_text(name + "\n")
        (d / "index").write_text(idx + "\n")
    cams = webcam.linux_cameras(tmp_path, probe=lambda dev: None)          # система не ответила
    assert [(c.name, c.device) for c in cams] == [("HD Webcam", "/dev/video0"), ("USB Cam", "/dev/video2")]
    # система ответила: картинку отдаёт video1, а video0 — служебный (как бывает у некоторых камер)
    caps = {"/dev/video0": False, "/dev/video1": True, "/dev/video2": True}
    cams = webcam.linux_cameras(tmp_path, probe=caps.get)
    assert [(c.name, c.device) for c in cams] == [("HD Webcam", "/dev/video1"), ("USB Cam", "/dev/video2")]


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_record_clip_and_store(tmp_path):
    cam = Webcam(FFMPEG, input_override=FAKE_CAM)
    assert cam.find() is not None
    store = CamStore(tmp_path)
    assert cam.start(store.new_path(), 2.0)
    deadline = time.time() + 30
    while (got := cam.poll()) is None and time.time() < deadline:
        time.sleep(0.1)
    assert isinstance(got, CamClip), cam.last_error
    assert got.duration == pytest.approx(2.0, abs=0.3) and (got.width, got.height) == (640, 480)
    store.add(got, keep=3)
    # больше лимита — остаются разнесённые по времени
    for t in (got.start + 5, got.start + 6, got.start + 100):
        (store.dir / f"x{t}.mp4").write_bytes(b"0")
        store.add(CamClip(f"x{t}.mp4", t, 2.0), keep=3)
    assert [round(c.start - got.start) for c in store.items] == [0, 5, 100]
    assert not (store.dir / f"x{got.start + 6}.mp4").exists()          # лишний файл удалён
    assert [c.file for c in webcam.load_clips(tmp_path)] == [c.file for c in store.items]


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_broken_camera_gives_up(tmp_path):
    cam = Webcam(FFMPEG, input_override=["-f", "lavfi", "-i", "nosuchfilter"])
    cam.find()
    for _ in range(Webcam.MAX_FAILS):
        assert cam.start(tmp_path / "c.mp4", 2.0)
        while (got := cam.poll()) is None:
            time.sleep(0.05)
        assert got is False
    assert cam.gave_up and not cam.start(tmp_path / "c.mp4", 2.0)


def test_place_camera():
    from glimpsy.assembler import Piece, place_camera
    from glimpsy.recorder.candidates import Candidate

    pieces = []
    for i in range(10):         # 10 кусков по 4 с, сняты с интервалом в минуту
        c = Candidate(id=i, file="", wall_start=1000 + 60 * i, wall_end=1010 + 60 * i, want_start=0, want_end=0,
                      monitor=1, width=1920, height=1080, score=0.5)
        pieces.append(Piece(c, 0.0, 4.0, 1.0))
    cams = [CamClip("a", 1000 + 60 * 3 + 2, 4.0), CamClip("b", 1000 + 60 * 3 + 5, 4.0), CamClip("c", 1000 + 60 * 8, 4.0)]
    placed = place_camera(pieces, cams)
    assert [p.clip.file for p in placed] == ["a", "c"]           # ролик 40 с → не больше 3; «b» слишком близко к «a»
    assert placed[0].start == pytest.approx(12.3)                 # 4-й кусок начинается на 12-й секунде
    assert all(p.duration <= 4.0 for p in placed)
    assert place_camera(pieces, []) == []


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_assemble_with_camera(tmp_path):
    """Сборка ролика: окошко с камеры в правом нижнем углу, в проекте редактора — наложение."""
    from glimpsy.assembler import Assembler
    from glimpsy.config import Settings
    from glimpsy.editor.project import Project
    from glimpsy.recorder.candidates import Candidate
    from glimpsy.recorder.encoder import software_encoder
    from glimpsy.recorder.pacing import make_plan

    sess = tmp_path / "session_20260925_120000"
    sess.mkdir()
    now = time.time()
    cands = []
    for i in range(3):
        f = sess / f"cand_{i:05d}.ts"
        subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:size=640x360:rate=30",
                        "-t", "5", "-c:v", "libx264", "-g", "30", "-bf", "0", "-f", "mpegts", str(f)], check=True)
        cands.append(Candidate(id=i, file=f.name, wall_start=now + 60 * i, wall_end=now + 60 * i + 5,
                               want_start=now + 60 * i, want_end=now + 60 * i + 5, monitor=1,
                               width=640, height=360, score=0.5, activity=[0.5] * 5))
    store = CamStore(sess)
    p = store.new_path()
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=white:size=640x480:rate=30",
                    "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)], check=True)
    store.add(CamClip(p.name, now + 61, 3.0, 640, 480), keep=5)

    s = Settings(output_dir=str(tmp_path / "out"), target_length_s=10, clip_min_s=3, clip_max_s=4,
                 pace="calm", output_width=640, output_height=360).validate()
    out = Assembler(FFMPEG, software_encoder(), s, make_plan(s)).run(sess, cands, now, project_dir=tmp_path / "project_1")

    meta = json.loads((tmp_path / "project_1" / "project.json").read_text())
    assert len(meta["camera"]) == 1
    cam = meta["camera"][0]
    t = cam["start"] + 1.0

    def pixel(x, y):
        raw = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(out), "-frames:v", "1",
                              "-vf", f"crop=4:4:{x}:{y},scale=1:1,format=gray", "-f", "rawvideo", "-"],
                             capture_output=True).stdout
        return raw[0]
    assert pixel(560, 300) > 200       # правый нижний угол — белое окошко камеры
    assert pixel(100, 100) < 40        # остальной кадр — чёрная запись экрана

    project = Project.load(tmp_path / "project_1")
    assert len(project.overlays) == 1 and project.overlays[0].kind == "video"
    assert project.overlays[0].start == pytest.approx(cam["start"])
    assert (tmp_path / "project_1" / project.overlays[0].src).exists()


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_linux_camera_args_are_accepted_by_ffmpeg(monkeypatch):
    """Каждый способ открыть камеру FFmpeg принимает (ошибка может быть только «нет камеры»)."""
    import subprocess

    monkeypatch.setattr(webcam.sys, "platform", "linux")
    for attempt in range(3):
        args = webcam.input_args("/dev/video-glimpsy-missing", attempt)
        r = subprocess.run([FFMPEG, "-hide_banner", *args, "-f", "null", "-"], capture_output=True, text=True)
        assert "Error parsing options" not in r.stderr and "cannot be applied" not in r.stderr, r.stderr
