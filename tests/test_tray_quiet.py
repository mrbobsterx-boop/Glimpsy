"""Во время записи уведомления программы не всплывают (иначе они попадут в ролик)."""

from glimpsy.recorder.engine import State


class _Engine:
    running = True


def test_messages_are_held_while_recording(qt_app, monkeypatch):
    from glimpsy.ui.tray import TrayController

    shown = []
    t = TrayController.__new__(TrayController)          # без трея и окон: проверяем только логику
    t.engine, t.tray, t.window, t._muted = _Engine(), None, None, []
    t.status = {"state": State.RECORDING}
    monkeypatch.setattr(t, "_flash_star", lambda: shown.append("star"))
    real = TrayController.show_message

    def spy(self, title, text, force=False):
        if force or not self._quiet():
            shown.append((title, text))
        real(self, title, text, force)
    monkeypatch.setattr(TrayController, "show_message", spy)

    t.show_message("Glimpsy", "⭐ Важный момент отмечен (−10 с / +5 с)")
    t.show_message("Glimpsy", "Аппаратный видеокодек не найден")
    assert shown == ["star"] and len(t._muted) == 1
    t.status = {"state": State.PAUSED}                  # пауза — теперь можно показать накопленное
    t._flush_muted()
    assert shown[-1] == ("Glimpsy", "Аппаратный видеокодек не найден") and t._muted == []
