import pytest

from worklapse.platform import hotkey_format as hf


def test_pynput_format():
    assert hf.to_pynput("Ctrl+Alt+S") == "<ctrl>+<alt>+s"
    assert hf.to_pynput("ctrl + shift + F9") == "<ctrl>+<shift>+<f9>"
    assert hf.to_pynput("Cmd+Option+P") == "<cmd>+<alt>+p"


def test_portal_format():
    assert hf.to_portal("Ctrl+Alt+S") == "CTRL+ALT+s"
    assert hf.to_portal("Win+F5") == "LOGO+F5"


def test_normalize():
    assert hf.normalize("ctrl+alt+e") == "Ctrl+Alt+E"


@pytest.mark.parametrize("bad", ["", "S", "Ctrl+Alt", "Ctrl+A+B", "Ctrl+Бяка"])
def test_invalid(bad):
    with pytest.raises(hf.HotkeyError):
        hf.parse(bad)


def test_win32_codes():
    assert hf.to_win32("Ctrl+Alt+S") == (0x2 | 0x1, ord("S"))
    assert hf.to_win32("Ctrl+Shift+F9") == (0x2 | 0x4, 0x78)
    assert hf.to_win32("Win+Alt+1") == (0x8 | 0x1, ord("1"))
