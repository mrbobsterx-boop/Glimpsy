"""Главы для YouTube."""

from glimpsy.editor import chapters as ch


def speech(n_sentences=40, words=8, every=6.0):
    out = []
    for k in range(n_sentences):
        t = k * every
        for i in range(words):
            text = f"слово{k}_{i}" + ("." if i == words - 1 else "")
            out.append((t + i * 0.4, t + i * 0.4 + 0.3, text))
    return out


def test_suggest_follows_youtube_rules():
    total = 240.0
    got = ch.suggest(speech(), total)
    assert got[0]["t"] == 0.0 and len(got) >= 3
    assert ch.problems(got, total) == []
    assert got[1]["title"].startswith("Слово") and got[1]["title"].endswith("…")


def test_short_video_has_no_chapters():
    assert ch.suggest(speech(3), 20.0) == []


def test_text_and_problems():
    items = [{"t": 0.4, "title": "Начало"}, {"t": 65, "title": "Камера"}, {"t": 3700, "title": "Итог"}]
    assert ch.as_text(items, 3800).splitlines() == ["0:00:00 Начало", "0:01:05 Камера", "1:01:40 Итог"]
    assert ch.as_text(items[:2], 100) == "00:00 Начало\n01:05 Камера"
    assert any("три" in p for p in ch.problems(items[:2], 100))
    assert any("10 секунд" in p for p in ch.problems([{"t": 0, "title": "a"}, {"t": 5, "title": "b"},
                                                       {"t": 50, "title": "c"}], 100))


def test_dialog_edit_and_copy(qt_app):
    from PySide6.QtWidgets import QApplication

    from glimpsy.editor.chapters_dialog import ChaptersDialog, parse_time

    assert parse_time("1:25") == 85 and parse_time("01:02:03") == 3723 and parse_time("абв") is None
    seeks = []
    dlg = ChaptersDialog([], 240.0, lambda: ch.suggest(speech(), 240.0), lambda: 100.0, seeks.append)
    assert len(dlg.items) >= 3
    dlg._add()
    assert any(c["t"] == 100.0 and c["title"] == "Новая глава" for c in dlg.items)
    row = next(i for i, c in enumerate(dlg.items) if c["t"] == 100.0)
    dlg.table.item(row, 0).setText("1:50")
    assert any(c["t"] == 110.0 for c in dlg.items)
    dlg._copy()
    assert QApplication.clipboard().text().startswith("00:00 ")
    dlg.close()
