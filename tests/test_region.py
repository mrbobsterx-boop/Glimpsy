"""Запись части экрана: выделили мышью прямоугольник — пишется только он."""

import io
from types import SimpleNamespace

import numpy as np

from glimpsy.config import Settings
from glimpsy.platform.base import Monitor
from glimpsy.recorder.engine import State
from glimpsy.recorder.pacing import make_plan
from glimpsy.recorder.ring_buffer import PREVIEW_SIZE


def test_rect_fraction_and_monitor_match():
    from glimpsy.ui.region_picker import match_monitor, rect_fraction

    assert rect_fraction((480, 270, 960, 540), (0, 0, 1920, 1080)) == [0.25, 0.25, 0.5, 0.5]
    assert rect_fraction((1930, 0, 960, 540), (1920, 0, 1920, 1080)) == [0.0052, 0.0, 0.5, 0.5]
    assert rect_fraction((0, 0, 10, 300), (0, 0, 1920, 1080)) is None          # случайный щелчок
    assert rect_fraction((0, 0, 1920, 1080), (0, 0, 1920, 1080)) is None       # весь экран — не область
    mons = [Monitor(2, 2560, 0, 1920, 1080), Monitor(1, 0, 0, 2560, 1440)]
    # у Qt координаты в другом масштабе, но порядок слева направо тот же
    screens = [(0, 0, 1707, 960), (1707, 0, 1280, 720)]
    assert match_monitor(screens, mons, 0).index == 1 and match_monitor(screens, mons, 1).index == 2
    assert match_monitor([(0, 0, 1280, 800)], [Monitor(1, 0, 0, 1280, 800), Monitor(2, 1280, 0, 1920, 1080)],
                         0).index == 1


def _engine():
    from glimpsy.recorder.engine import RecorderEngine

    e = RecorderEngine.__new__(RecorderEngine)
    e.s = Settings().validate()
    e.plan = make_plan(e.s)
    e._reset_session_fields()
    e.region = None
    e.streams = []
    e._monitors = [Monitor(1, 0, 0, 1920, 1080), Monitor(2, 1920, 0, 2560, 1440)]
    e.run = None
    e._stream = 0
    e._bg = {}
    sent = []
    e._emit_status = lambda now, force=False: sent.append(force)
    return e


def test_engine_region_crops_only_its_monitor():
    e = _engine()
    e.run = SimpleNamespace(monitor=e._monitors[1], diff_crop=None)
    e.set_region((2, [0.25, 0.5, 0.5, 0.25]))
    assert e.run.diff_crop == [0.25, 0.5, 0.5, 0.25] and e.preview_crop() == [0.25, 0.5, 0.5, 0.25]
    assert e.region_label() == "1280×360, монитор 2"
    assert e._region_for(e._monitors[0]) is None and e._region_for(e._monitors[1]) == [0.25, 0.5, 0.5, 0.25]
    e._stream = 5                              # пишется поток-окно — у него своя вырезка
    e._screen_ids = lambda: set()
    assert e._region_for(e._monitors[1]) is None
    e._stream = 0
    e.set_region(None)
    assert e.run.diff_crop is None and e.region_label() == ""


def test_activity_counts_only_inside_region():
    """Меняется только угол вне области — для записи это «ничего не происходит»."""
    from glimpsy.recorder import ring_buffer as rb

    w, h = PREVIEW_SIZE
    a = np.zeros((h, w, 3), np.uint8)
    b = a.copy()
    b[:20, :20] = 255                          # левый верхний угол
    run = rb.BufferRun.__new__(rb.BufferRun)
    diffs = []
    run.on_frame_diff = lambda d, t: diffs.append(d)
    run.latest_preview = None
    run.diff_crop = [0.5, 0.5, 0.5, 0.5]
    run._proc = SimpleNamespace(stdout=io.BytesIO(a.tobytes() + b.tobytes()))
    run._read_thumbs()
    assert diffs == [0.0]
    run.diff_crop = None
    run._proc = SimpleNamespace(stdout=io.BytesIO(a.tobytes() + b.tobytes()))
    diffs.clear()
    run._read_thumbs()
    assert diffs[0] > 0


def test_status_window_region_buttons_and_cropped_preview(qt_app):
    from PySide6.QtCore import QSettings

    from glimpsy.ui import status_window as sw

    QSettings("Glimpsy", "status").clear()
    w, h = PREVIEW_SIZE
    calls = []
    engine = SimpleNamespace(session_start=0.0, preview=lambda: bytes(w * h * 3),
                             preview_crop=lambda: [0.0, 0.0, 0.5, 0.5])
    acts = {k: (lambda k=k: calls.append(k)) for k in ("start", "pause", "continuous", "important", "finish",
                                                         "editor", "region", "whole")}
    win = sw.StatusWindow(engine, acts)
    assert not win.b_whole.isVisibleTo(win)
    win.update_status({"state": State.RECORDING, "monitor": "1", "region": "960×540"})
    assert "область 960×540" in win.what.text() and win.b_whole.isVisibleTo(win)
    assert "Другая" in win.b_region.text() and not win.preview.pixmap().isNull()
    win.b_region.click()
    win.b_whole.click()
    assert calls == ["region", "whole"]
    win.close()


def test_picker_reports_monitor_and_fraction(qt_app):
    from glimpsy.ui.region_picker import RegionPicker

    picker = RegionPicker([Monitor(1, 0, 0, 1920, 1080)])
    got = []
    picker.picked.connect(lambda m, f: got.append((m.index, f)))
    picker.choose(0, [0.1, 0.2, 0.3, 0.4])
    assert got == [(1, [0.1, 0.2, 0.3, 0.4])]


def test_picker_drag_on_screen(qt_app):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from glimpsy.ui.region_picker import RegionPicker

    picker = RegionPicker([Monitor(1, 0, 0, 1920, 1080)])
    got, small = [], []
    picker.picked.connect(lambda m, f: got.append(f))
    picker.on_too_small = lambda: small.append(1)
    picker.start()
    shade = picker.shades[0]
    QTest.mouseClick(shade, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))      # просто щелчок — мало
    assert small and not got and picker.shades
    W, H = shade.width(), shade.height()
    QTest.mousePress(shade, Qt.MouseButton.LeftButton, pos=QPoint(W // 4, H // 4))
    QTest.mouseMove(shade, QPoint(W * 3 // 4, H * 3 // 4))
    QTest.mouseRelease(shade, Qt.MouseButton.LeftButton, pos=QPoint(W * 3 // 4, H * 3 // 4))
    assert len(got) == 1 and abs(got[0][2] - 0.5) < 0.01 and not picker.shades
