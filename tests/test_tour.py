"""Обучение по редактору (кнопка «?»)."""

import json
import subprocess

import pytest

from glimpsy import paths
from glimpsy.editor import tour

FFMPEG = paths.find_executable("ffmpeg")


@pytest.mark.skipif(not FFMPEG, reason="нужен FFmpeg")
def test_tour_walks_all_parts(tmp_path, qt_app):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    from glimpsy.editor.window import EditorWindow
    from glimpsy.recorder.encoder import software_encoder

    proj = tmp_path / "project_20260925_101010"
    proj.mkdir()
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:size=320x180:rate=30",
                    "-t", "2", "-pix_fmt", "yuv420p", str(proj / "piece_0000.mp4")], check=True)
    (proj / "project.json").write_text(json.dumps({"output": str(tmp_path / "W.mp4"),
                                                   "clips": [{"file": "piece_0000.mp4", "duration": 2.0}]}))
    w = EditorWindow(proj, FFMPEG, software_encoder, tmp_path, on_sessions=lambda: None)
    try:
        w.resize(1400, 900)
        w.show()
        QApplication.processEvents()
        # у каждого шага (кроме вступления и «Текст» — его нет в обычном проекте) есть что подсветить
        keys = {k for k, _t, _x in tour.STEPS if k}
        missing = {k for k in keys if w._tour_targets.get(k) is None and k != "timeline"} - {"tool:Текст"}
        assert missing == set(), missing
        w.start_tour()
        t = w._tour
        assert t is not None and t.isVisible() and t.target_rect() is None          # вступление — по центру
        t.go(1)
        r = t.target_rect()
        assert r is not None and t.rect().contains(r.center())
        assert not t.card.geometry().intersects(r.adjusted(8, 8, -8, -8))         # карточка не закрывает
        seen = 1
        while w._tour is not None and seen < 60:
            QApplication.sendEvent(t, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier))
            seen += 1
        QApplication.processEvents()
        assert w._tour is None and seen == len(t.steps)
        w.start_tour()
        QApplication.sendEvent(w._tour, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                                  Qt.KeyboardModifier.NoModifier))
        assert w._tour is None
    finally:
        w.player.shutdown()
        w.close()
