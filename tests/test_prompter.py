"""Суфлёр: движение текста, закрепление, закраска в записи."""

import time

from glimpsy.platform.base import CaptureInput, Monitor


def test_prompter_scrolls_locks_and_reports_mask(qt_app, tmp_path, monkeypatch):
    import os

    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtWidgets import QApplication

    from glimpsy.ui.prompter import Prompter, to_html

    QSettings("Glimpsy", "prompter").clear()
    assert "color:#7C8494" in to_html("Привет\n\n[пауза] дальше")
    speaking = {"on": True}
    p = Prompter(is_speaking=lambda: speaking["on"])
    masks = []
    p.masks_changed.connect(masks.append)
    p.text = "\n\n".join(f"Абзац {i}: " + "слово " * 30 for i in range(20))
    p._apply_text()
    p.resize(600, 300)
    p.move(100, 120)
    p.show_prompter()
    QApplication.processEvents()
    if p._native_hidden:
        assert masks[-1] == []                                  # Windows прячет окно от записи сама
    else:
        assert masks and masks[-1][0][2] >= 600                 # место суфлёра закрашивается в записи
    p.play()
    p._countdown_until = 0                                      # без отсчёта 3-2-1
    t0 = time.monotonic()
    while time.monotonic() - t0 < 0.5:
        p._step()
        time.sleep(0.02)
    moved = p.offset
    assert moved > 0
    p.voice_follow = True
    speaking["on"] = False                                     # молчите — текст стоит
    for _ in range(10):
        p._step()
        time.sleep(0.02)
    assert p.offset == moved
    p.back()
    assert p.offset < moved
    p.faster()
    assert p.speed == 5
    p.set_locked(True)
    assert p.windowFlags() & Qt.WindowType.WindowTransparentForInput and not p.bar.isVisible()
    if os.environ.get("GLIMPSY_SHOT"):
        p.set_locked(False)
        p.offset = 90
        p.grab().save(os.environ["GLIMPSY_SHOT"])
    p.hide()
    assert masks[-1] == []                                     # спрятали — закрашивать нечего
    QSettings("Glimpsy", "prompter").clear()


def test_engine_masks_region_in_capture(tmp_path):
    from glimpsy.config import Settings
    from glimpsy.recorder.encoder import software_encoder
    from glimpsy.recorder.engine import RecorderEngine
    from glimpsy.recorder.ring_buffer import BufferRun

    eng = RecorderEngine(Settings(), object(), "ffmpeg")
    eng.set_masks([(1800, 100, 400, 200)])                     # суфлёр на стыке двух мониторов
    left, right = Monitor(1, 0, 0, 1920, 1080), Monitor(2, 1920, 0, 1280, 720)
    cap = CaptureInput([], 1920, 1080)
    assert eng._masks_for(left, cap) == [(1800, 100, 120, 200)]
    assert eng._masks_for(right, CaptureInput([], 2560, 1440)) == [(0, 200, 560, 400)]   # 2× пикселей
    run = BufferRun("ffmpeg", cap, left, software_encoder(), 30, tmp_path, 1080, masks=eng._masks_for(left, cap))
    graph = run.build_command()[run.build_command().index("-filter_complex") + 1]
    assert "drawbox=x=1800:y=100:w=120:h=200" in graph
