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
    assert "Изменить" in win.b_region.text() and not win.preview.pixmap().isNull()
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
    assert not got and shade.bar.isVisible()                 # рамка нарисована, ждёт подтверждения
    # передвинули рамку за середину на 40 px вправо, потом потянули правый край на 20 px влево
    c = shade.sel.center()
    QTest.mousePress(shade, Qt.MouseButton.LeftButton, pos=c)
    QTest.mouseMove(shade, c + QPoint(40, 0))
    QTest.mouseRelease(shade, Qt.MouseButton.LeftButton, pos=c + QPoint(40, 0))
    right = QPoint(shade.sel.x() + shade.sel.width(), shade.sel.center().y())
    QTest.mousePress(shade, Qt.MouseButton.LeftButton, pos=right)
    QTest.mouseMove(shade, right - QPoint(20, 0))
    QTest.mouseRelease(shade, Qt.MouseButton.LeftButton, pos=right - QPoint(20, 0))
    QTest.keyClick(shade, Qt.Key.Key_Down)                    # стрелка — на 10 px вниз
    assert (shade.sel.x(), shade.sel.y(), shade.sel.width()) == (W // 4 + 40, H // 4 + 10, W // 2 - 20)
    shade.ok.click()
    assert len(got) == 1 and abs(got[0][0] - (W // 4 + 40) / W) < 0.002 and not picker.shades


def test_picker_opens_with_current_region(qt_app):
    from glimpsy.ui.region_picker import RegionPicker

    picker = RegionPicker([Monitor(1, 0, 0, 1920, 1080)], current=(1, [0.25, 0.25, 0.5, 0.5]))
    picker.start()
    shade = picker.shades[0]
    assert abs(shade.sel.width() - shade.width() / 2) <= 1 and shade.bar.isVisible()
    whole = []
    picker.whole.connect(lambda: whole.append(1))
    shade.set_selection(shade.rect())                         # растянули на весь экран
    shade.confirm()
    assert whole and not picker.shades


def test_grip_and_drag_rect():
    from glimpsy.ui.region_picker import MIN_PX, drag_rect, grip_at

    r = (100, 100, 200, 100)
    assert grip_at(r, 100, 100) == "tl" and grip_at(r, 300, 150) == "r" and grip_at(r, 200, 200) == "b"
    assert grip_at(r, 200, 150) == "move" and grip_at(r, 10, 10) == ""
    assert drag_rect(r, "move", 5000, -500, (1000, 800)) == (800, 0, 200, 100)     # не уезжает за экран
    assert drag_rect(r, "l", 500, 0, (1000, 800)) == (300 - MIN_PX, 100, MIN_PX, 100)
    assert drag_rect(r, "br", 50, 20, (1000, 800)) == (100, 100, 250, 120)


def test_region_survives_session_start():
    """Область выбирают до записи — начало записи не должно её сбрасывать."""
    e = _engine()
    e.set_region((1, [0.5, 0.5, 0.5, 0.5]))
    e._reset_session_fields()
    assert e.region == (1, [0.5, 0.5, 0.5, 0.5])


def test_monitor_match_by_corner_with_scaling():
    """Масштаб 150%: Qt уменьшает размер второго экрана, но угол оставляет системным."""
    from glimpsy.ui.region_picker import match_monitor

    mons = [Monitor(1, 0, 0, 1280, 800), Monitor(2, 1280, 0, 3840, 2160)]
    screens = [(1280, 0, 3840, 2160), (0, 0, 1280, 800)]
    assert match_monitor(screens, mons, 0).index == 2 and match_monitor(screens, mons, 1).index == 1


def test_headline_is_honest():
    from glimpsy.ui.status_window import headline

    assert "ИДЁТ ЗАПИСЬ" in headline({"state": State.IDLE, "buffering": True})[0]
    assert "ПАУЗА" in headline({"state": State.IDLE, "buffering": False})[0]
    assert "ПАУЗА" in headline({"state": State.PAUSED, "continuous": True})[0]
    assert "ЗАПУСК" in headline({"state": State.STARTING, "continuous": True})[0]
