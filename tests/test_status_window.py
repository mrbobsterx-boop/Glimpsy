"""Окно «Состояние записи»: крупно — пишется ли, что и как это выглядит."""

from types import SimpleNamespace

from glimpsy.recorder.engine import State
from glimpsy.recorder.ring_buffer import PREVIEW_SIZE


def test_status_window_shows_state_and_preview(qt_app, monkeypatch, tmp_path):
    from PySide6.QtCore import QSettings

    from glimpsy.ui import status_window as sw

    QSettings("Glimpsy", "status").clear()
    w, h = PREVIEW_SIZE
    frame = bytes([200, 30, 30]) * (w * h)
    calls = []
    engine = SimpleNamespace(session_start=0.0, preview=lambda: None)
    acts = {k: (lambda k=k: calls.append(k)) for k in ("start", "pause", "continuous", "important", "finish",
                                                         "editor")}
    win = sw.StatusWindow(engine, acts)
    assert "НЕ ЗАПИСЫВАЕТ" in win.big.text() and win.b_start.isVisibleTo(win)
    assert not win.b_pause.isVisibleTo(win)
    engine.preview = lambda: frame
    import time

    engine.session_start = time.time() - 65
    win.update_status({"state": State.RECORDING, "monitor": "2 (DP-1)", "candidates": 7, "continuous": True})
    assert "НЕПРЕРЫВНАЯ" in win.big.text() and "DP-1" in win.what.text()
    assert "01:0" in win.info.text() and "фрагментов: 7" in win.info.text()
    assert not win.preview.pixmap().isNull() and win.b_cont.isChecked()
    win.b_cont.click()
    win.b_star.click()
    assert calls == ["continuous", "important"]
    win.update_status({"state": State.PAUSED})
    assert "ПАУЗА" in win.big.text() and win.b_pause.text().strip() == "Продолжить"
    win.update_status({"state": State.ASSEMBLING})
    assert "СОХРАНЯЮ" in win.big.text() and not win.b_finish.isVisibleTo(win)
    win.open()
    assert QSettings("Glimpsy", "status").value("open", False, type=bool)
    win.close()
    assert not QSettings("Glimpsy", "status").value("open", True, type=bool)
    assert sw.elapsed_text(3725) == "1:02:05" and sw.elapsed_text(65) == "01:05"


def test_preview_frame_from_buffer_run():
    """Запись отдаёт цветной кадр и по-прежнему считает активность по серому 64×36."""
    import io

    import numpy as np

    from glimpsy.recorder import ring_buffer as rb

    w, h = PREVIEW_SIZE
    frames = [np.full((h, w, 3), v, np.uint8).tobytes() for v in (10, 10, 200)]
    run = rb.BufferRun.__new__(rb.BufferRun)
    diffs = []
    run.on_frame_diff = lambda d, t: diffs.append(d)
    run.latest_preview = None
    run.diff_crop = None
    run._proc = SimpleNamespace(stdout=io.BytesIO(b"".join(frames)))
    run._read_thumbs()
    assert run.latest_preview == frames[-1]
    assert diffs[0] == 0 and abs(diffs[1] - 190) < 1
