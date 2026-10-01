"""Папки программы: все места, где Glimpsy хранит файлы, с размером и кнопкой «Открыть»."""

from pathlib import Path

from glimpsy.ui import folders


def test_places_and_sizes(tmp_path, qt_app, monkeypatch):
    from glimpsy import paths

    opened = []
    monkeypatch.setattr(paths, "open_in_file_manager", lambda p: opened.append(Path(p)))
    items = folders.places(tmp_path / "out", tmp_path / "proj")
    titles = [p.title for p in items]
    for need in ("Готовые ролики", "Этот проект", "Все проекты и сессии", "Модели распознавания речи",
                 "Переводчик субтитров", "Голосовой модуль", "Сохранённые голоса", "Журналы (логи)"):
        assert need in titles
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.mp4").write_bytes(b"x" * 3 * 1024 * 1024)
    assert folders.human_size(folders.folder_size(tmp_path / "out")) == "3,0 МБ"
    assert folders.human_size(0) == "пусто" and folders.folder_size(tmp_path / "nope") == 0
    w = folders.FoldersWidget(items)
    from PySide6.QtWidgets import QPushButton
    btns = w.findChildren(QPushButton)
    assert len(btns) == len(items)
    btns[0].click()
    assert opened == [tmp_path / "out"]
