import plistlib
import sys

import pytest

from glimpsy import autostart


@pytest.mark.skipif(sys.platform.startswith("win"), reason="на Windows — реестр")
def test_enable_disable(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("APPIMAGE", "/opt/My Apps/Glimpsy.AppImage")
    monkeypatch.setattr(autostart.Path, "home", lambda: tmp_path)
    assert not autostart.is_enabled()
    assert autostart.set_enabled(True) is None
    assert autostart.is_enabled()
    if sys.platform == "darwin":
        data = plistlib.loads(autostart._mac_file().read_bytes())
        assert data["ProgramArguments"] == ["/opt/My Apps/Glimpsy.AppImage", "--autostart"]
    else:
        text = autostart._linux_file().read_text()
        assert "Exec='/opt/My Apps/Glimpsy.AppImage' --autostart" in text   # путь с пробелом в кавычках
    autostart.set_enabled(False)
    assert not autostart.is_enabled()


def test_command_from_source(monkeypatch):
    monkeypatch.delenv("APPIMAGE", raising=False)
    cmd = autostart.command()
    assert cmd[-2:] == ["glimpsy", "--autostart"] or cmd[-1] == "--autostart"
