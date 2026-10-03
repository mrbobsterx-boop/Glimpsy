import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from glimpsy import updater


def manifest(build, files=None):
    return json.dumps({"build": build, "version": "0.2.0", "files": files or {}}).encode()


def test_manifest_and_newer():
    rel = updater.parse_manifest(manifest(42, {"Glimpsy-x86_64.AppImage": 1234}), "appimage")
    assert rel.build == 42 and rel.file == "Glimpsy-x86_64.AppImage" and rel.size == 1234
    assert updater.is_newer(rel, updater.Build(41, "dev"))
    assert not updater.is_newer(rel, updater.Build(42, "dev"))
    assert not updater.is_newer(rel, None)                       # запуск из исходников не обновляется
    assert updater.parse_manifest(b"<html>not found</html>", "appimage") is None
    assert updater.parse_manifest(b"[]", "appimage") is None


def test_urls():
    assert updater.download_url("dev", "a.json").endswith("/releases/download/dev/a.json")
    assert updater.download_url("stable", "a.json").endswith("/releases/latest/download/a.json")
    assert updater.asset_name("exe") == "Glimpsy-Windows-x64-portable.exe"


@pytest.mark.skipif(sys.platform == "win32", reason="AppImage — только Linux")
def test_appimage_replaced_in_place(tmp_path):
    app = tmp_path / "Glimpsy-x86_64.AppImage"
    app.write_text("old")
    new = updater.download_path("appimage", app)
    assert new.parent == app.parent                              # рядом — замена мгновенная
    new.write_text("new")
    cmd = updater.install("appimage", new, app, 123)
    assert app.read_text() == "new" and os.access(app, os.X_OK) and not new.exists()
    assert cmd == [str(app), "--after-update", "123"]
    assert updater.target_path("appimage", env={"APPIMAGE": str(app)}) == app


def test_exe_renamed_then_replaced(tmp_path):
    exe = tmp_path / "Glimpsy.exe"
    exe.write_text("old")
    new = updater.download_path("exe", exe)
    new.write_text("new")
    cmd = updater.install("exe", new, exe, 7)
    assert exe.read_text() == "new" and (tmp_path / "Glimpsy.exe.old").read_text() == "old"
    assert cmd[1:] == ["--after-update", "7"]


def test_scripts_wait_and_restart(tmp_path):
    bat = updater.windows_script(55, Path("C:/t/new/Glimpsy"), Path("C:/Apps/Glimpsy"))
    assert "PID eq 55" in bat and "robocopy" in bat and "Glimpsy.exe" in bat
    sh = updater.mac_script(55, tmp_path / "new" / "Glimpsy.app", tmp_path / "My Apps" / "Glimpsy.app")
    assert "kill -0 55" in sh and "'" in sh and "open " in sh     # путь с пробелом — в кавычках


def test_target_of_mac_app():
    exe = "/Applications/Glimpsy.app/Contents/MacOS/Glimpsy"
    assert updater.target_path("app", executable=exe) == Path("/Applications/Glimpsy.app")


def test_release_info_script():
    root = Path(__file__).resolve().parent.parent
    script = root / "scripts" / "release_info.py"
    build = root / "glimpsy" / "_build.py"
    had = build.read_text(encoding="utf-8") if build.exists() else None
    try:
        subprocess.run([sys.executable, str(script), "build", "17", "dev"], check=True)
        ns: dict = {}
        exec(build.read_text(encoding="utf-8"), ns)
        assert ns["BUILD"] == 17 and ns["CHANNEL"] == "dev"
    finally:
        if had is None:
            build.unlink(missing_ok=True)
        else:
            build.write_text(had, encoding="utf-8")


def test_wait_for_exit_returns_for_dead_process():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    updater.wait_for_exit(p.pid, timeout=5)                       # не висит


@pytest.mark.skipif(sys.platform == "win32", reason="AppImage — только Linux")
def test_full_update_flow(qt_app, tmp_path, monkeypatch):
    """Нашла новую версию → «Обновить сейчас» → скачала → заменила файл → перезапуск и выход."""
    import functools
    import http.server
    import threading

    from PySide6.QtCore import QCoreApplication, QObject, QTimer

    from glimpsy.ui import update as ui

    site = tmp_path / "site"
    site.mkdir()
    (site / "Glimpsy-x86_64.AppImage").write_bytes(b"NEW" * 1000)
    (site / f"version-{updater.platform_key()}.json").write_bytes(
        manifest(9, {"Glimpsy-x86_64.AppImage": 3000}))
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"

    app = tmp_path / "Glimpsy-x86_64.AppImage"
    app.write_bytes(b"OLD")
    monkeypatch.setenv("APPIMAGE", str(app))
    monkeypatch.setattr(updater, "current_build", lambda: updater.Build(8, "dev"))
    monkeypatch.setattr(updater, "install_kind", lambda *a, **k: "appimage")
    monkeypatch.setattr(updater, "manifest_url", lambda ch: base + f"version-{updater.platform_key()}.json")
    monkeypatch.setattr(updater, "download_url", lambda ch, name: base + name)
    launched = []
    monkeypatch.setattr(updater, "launch", launched.append)

    class Engine:
        running = False

    class Settings:
        check_updates = False

    class Tray(QObject):
        def __init__(self):
            super().__init__()
            self.engine, self.s, self.quits, self.messages = Engine(), Settings(), [], []

        def exporting(self):
            return False

        def quit(self, force=False):
            self.quits.append(force)

        def show_message(self, *a):
            self.messages.append(a)

    tray = Tray()
    mgr = ui.UpdateManager(tray)
    found = []
    mgr.available.connect(found.append)

    def spin(cond, ms=10_000):
        t = QTimer()
        t.setSingleShot(True)
        t.start(ms)
        while not cond() and t.isActive():
            QCoreApplication.processEvents()
        assert cond()

    mgr.check(manual=False)
    spin(lambda: mgr._dialog is not None)
    assert found and found[0].build == 9
    mgr._dialog.b_go.click()
    spin(lambda: tray.quits)
    server.shutdown()
    assert tray.quits == [True]
    assert app.read_bytes() == b"NEW" * 1000
    assert launched == [[str(app), "--after-update", str(os.getpid())]]
