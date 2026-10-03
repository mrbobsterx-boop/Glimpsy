"""Поиск слова во всех проектах."""

from types import SimpleNamespace

from glimpsy.editor import search
from glimpsy.editor import transcript as tr
from glimpsy.editor.project import Project
from glimpsy.editor.transcript import SourceWords, Word


def make_project(root, name, text, deleted=()):
    video = root / f"{name}.mp4"
    video.write_bytes(b"")
    p = Project.for_videos(root / "projects", [video], [SimpleNamespace(duration=20.0, has_audio=True, width=320,
                                                                         height=180)])
    words = [Word(w, 1.0 + k * 0.5, 1.3 + k * 0.5) for k, w in enumerate(text.split())]
    tr.TranscriptStore(p.dir).put(SourceWords(p.clips[0].src, 20.0, words), None)
    if deleted:
        p.cuts.setdefault("deleted", {})[p.clips[0].src] = list(deleted)
    tr.rebuild_clips(p, tr.TranscriptStore(p.dir))
    p.save()
    return p


def test_find_words_and_phrases():
    words = [(k * 1.0, k * 1.0 + 0.5, w) for k, w in enumerate("Новая Камера снимает. Камеру я купил вчера".split())]
    assert [t for t, _ in search.find(words, "камер")] == [1.0, 3.0]
    assert [t for t, _ in search.find(words, "камера снимает")] == [1.0]
    assert search.find(words, "  ") == []


def test_index_over_projects_and_dialog(tmp_path, qt_app, monkeypatch):
    import time
    from glimpsy.editor import sessions

    make_project(tmp_path, "обзор", "Сегодня покажу новую камеру и микрофон")
    time.sleep(1.1)                                     # у проектов разное время создания в имени папки
    make_project(tmp_path, "влог", "Утром купил камеру потом пошли гулять", deleted=[4])                       # «пошли» вырезано
    root = tmp_path / "projects"
    idx = search.Index(sessions.list_projects(root))
    hits = idx.search("камеру")
    assert sorted(h.name for h in hits) == ["влог", "обзор"]
    assert idx.search("пошли") == []                     # вырезанного в ролике нет — и не находится
    assert search.Index(sessions.list_projects(root)).search("микрофон")[0].context.endswith("микрофон")
    assert idx.search("купил") == idx.search("купил")             # повторный поиск — без перечитывания

    opened = []
    monkeypatch.setattr(sessions, "projects_root", lambda: root)
    dlg = sessions.SessionsDialog("ffmpeg", lambda d, at=None: opened.append((d, at)))
    dlg.search.setText("камеру")
    dlg._do_search()
    assert dlg.results.count() == 2 and not dlg.results.isHidden() and dlg.list.isHidden()
    dlg._open_hit(dlg.results.item(0))
    assert opened and opened[0][1] is not None
    dlg.search.setText("")
    dlg._do_search()
    assert dlg.results.isHidden() and not dlg.list.isHidden()
    dlg.close()
