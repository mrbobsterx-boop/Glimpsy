"""Окошко «Ролик готов» с кнопками и поиск проекта по готовому ролику."""

import json


def test_toast_buttons_and_click(qt_app):
    from glimpsy.ui.toast import Toast

    done = []
    t = Toast("Ролик готов!", "a.mp4", [("Открыть в редакторе", lambda: done.append("edit")),
                                        ("Показать папку", lambda: done.append("folder"))])
    t.popup()
    assert t.isVisible() and [b.text() for b in t.buttons] == ["Открыть в редакторе", "Показать папку"]
    t.buttons[1].click()
    assert done == ["folder"]
    t2 = Toast("x", "y", [("Открыть", lambda: done.append("first"))])
    t2.popup()
    t2._run(0)                                         # щелчок по тексту — первая кнопка
    assert done[-1] == "first"


def test_project_for_video(tmp_path, monkeypatch):
    from glimpsy.editor import sessions

    root = tmp_path / "projects"
    d = root / "project_20261003_124700"
    d.mkdir(parents=True)
    video = tmp_path / "Glimpsy_2026-10-03_12-47.mp4"
    (d / "project.json").write_text(json.dumps({"output": str(video), "clips": []}))
    monkeypatch.setattr(sessions, "projects_root", lambda: root)
    assert sessions.project_for_video(video) == d
    assert sessions.project_for_video(tmp_path / "другой.mp4") is None
