"""Автообновление: какая версия вышла, где её взять и как заменить себя новой.

Сборки лежат в GitHub Releases (скачиваются без входа на сайт):
  • тестовые (ветки claude/…) — в релизе с тегом «dev», он обновляется при каждой сборке;
  • обычные (main) — в последнем релизе vX.Y.Z.
Рядом с файлами программы — маленький version-<система>.json с номером сборки. Номер своей
сборки программа знает из glimpsy/_build.py: его пишет сборка на GitHub. Запуск из исходников
(файла нет) не обновляется.

Здесь — только расчёты и файлы; окна и скачивание — в glimpsy/ui/update.py.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shlex
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from glimpsy import paths

log = logging.getLogger(__name__)

REPO = "mrbobsterx-boop/Glimpsy"
DEV_TAG = "dev"


@dataclass
class Build:
    number: int
    channel: str            # "dev" — тестовые сборки, "stable" — обычные


@dataclass
class Release:
    build: int
    version: str
    file: str               # имя файла программы в релизе
    size: int = 0
    notes: str = ""


def current_build() -> Build | None:
    try:
        from glimpsy import _build                    # type: ignore[attr-defined]
    except ImportError:
        return None
    return Build(int(getattr(_build, "BUILD", 0)), str(getattr(_build, "CHANNEL", "stable")))


def platform_key() -> str:
    if sys.platform == "win32":
        return "windows-x64"
    if sys.platform == "darwin":
        return "macos-arm64" if platform.machine() == "arm64" else "macos-x86_64"
    return "linux-x64"


def install_kind(executable: str | None = None, env: dict | None = None) -> str | None:
    """Как установлена программа — от этого зависит, как её заменить:
    appimage (Linux), exe (Windows, один файл), folder (Windows, папка), app (macOS); None — никак."""
    env = os.environ if env is None else env
    exe = executable or sys.executable
    if executable is None and not getattr(sys, "frozen", False):
        return None
    if sys.platform.startswith("linux"):
        return "appimage" if env.get("APPIMAGE") else None
    if sys.platform == "win32":
        meipass = getattr(sys, "_MEIPASS", "")
        # один файл распаковывается во временную папку, сборка-папка лежит рядом с exe
        return "folder" if meipass and Path(meipass).parent == Path(exe).parent else "exe"
    if sys.platform == "darwin":
        return "app" if ".app/Contents/MacOS" in exe else None
    return None


def asset_name(kind: str) -> str:
    return {
        "appimage": "Glimpsy-x86_64.AppImage",
        "exe": "Glimpsy-Windows-x64-portable.exe",
        "folder": "Glimpsy-Windows-x64.zip",
        "app": f"Glimpsy-{platform_key().replace('macos', 'macOS')}.zip",
    }[kind]


def download_url(channel: str, name: str) -> str:
    if channel == "dev":
        return f"https://github.com/{REPO}/releases/download/{DEV_TAG}/{name}"
    return f"https://github.com/{REPO}/releases/latest/download/{name}"


def manifest_url(channel: str) -> str:
    return download_url(channel, f"version-{platform_key()}.json")


def releases_page() -> str:
    return f"https://github.com/{REPO}/releases"


def parse_manifest(data: bytes, kind: str) -> Release | None:
    try:
        d = json.loads(data.decode("utf-8"))
        files = d.get("files") or {}
        name = asset_name(kind)
        return Release(int(d["build"]), str(d.get("version", "")), name, int(files.get(name, 0)),
                       str(d.get("notes", "")))
    except (ValueError, KeyError, TypeError, UnicodeDecodeError, AttributeError):
        return None


def is_newer(release: Release | None, build: Build | None) -> bool:
    return release is not None and build is not None and release.build > build.number


# ---------------- замена программы ----------------

def target_path(kind: str, executable: str | None = None, env: dict | None = None) -> Path:
    """Что заменяем: файл AppImage, exe, папку программы или Glimpsy.app."""
    env = os.environ if env is None else env
    exe = Path(executable or sys.executable)
    if kind == "appimage":
        return Path(env["APPIMAGE"])
    if kind in ("exe", "folder"):
        return exe if kind == "exe" else exe.parent
    s = (executable or sys.executable).replace("\\", "/")
    return Path(s[: s.index(".app/Contents/MacOS") + 4])


def download_path(kind: str, target: Path) -> Path:
    """Куда скачивать. Файл — рядом с программой: тогда замена мгновенная (тот же диск)."""
    if kind == "appimage":
        return target.with_name(".Glimpsy-update.AppImage")
    if kind == "exe":
        return target.with_name("Glimpsy-update.exe")
    return paths.temp_root() / "update" / asset_name(kind)


def cleanup() -> None:
    """Хвосты прошлого обновления: старый exe (Windows не даёт удалить запущенный) и недокачанное."""
    kind = install_kind()
    if kind is None:
        return
    try:
        target = target_path(kind)
    except (KeyError, ValueError):
        return
    leftovers = [download_path(kind, target), download_path(kind, target).with_name(
        download_path(kind, target).name + ".part")]
    if kind == "exe":
        leftovers.append(target.with_name(target.name + ".old"))
    for f in leftovers:
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass
    if kind in ("folder", "app"):
        shutil.rmtree(paths.temp_root() / "update", ignore_errors=True)


class UpdateError(Exception):
    pass


def _find(root: Path, name: str) -> Path | None:
    for p in sorted(root.rglob(name), key=lambda p: len(p.parts)):
        return p
    return None


def install(kind: str, downloaded: Path, target: Path, pid: int) -> list[str]:
    """Ставит скачанное на место программы. Возвращает команду, которую надо запустить
    перед выходом: она дождётся закрытия этой копии (pid) и откроет новую."""
    if kind == "appimage":
        downloaded.chmod(0o755)
        os.replace(downloaded, target)                 # запущенный файл заменять можно
        return [str(target), "--after-update", str(pid)]
    if kind == "exe":
        old = target.with_name(target.name + ".old")
        old.unlink(missing_ok=True)
        os.replace(target, old)                        # запущенный exe можно только переименовать
        try:
            os.replace(downloaded, target)
        except OSError:
            os.replace(old, target)
            raise
        return [str(target), "--after-update", str(pid)]
    work = downloaded.parent / "new"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    if kind == "folder":
        try:
            with zipfile.ZipFile(downloaded) as z:
                z.extractall(work)
        except (zipfile.BadZipFile, OSError) as e:
            raise UpdateError(f"Скачанный файл повреждён: {e}") from e
        exe = _find(work, "Glimpsy.exe")
        if exe is None:
            raise UpdateError("В скачанном архиве нет Glimpsy.exe")
        script = downloaded.parent / "update.bat"
        script.write_text(windows_script(pid, exe.parent, target), encoding="utf-8")
        return ["cmd", "/c", str(script)]
    # macOS: zip с Glimpsy.app (ditto сохраняет права и подпись)
    r = subprocess.run(["ditto", "-x", "-k", str(downloaded), str(work)], capture_output=True)
    app = _find(work, "Glimpsy.app")
    if r.returncode != 0 or app is None:
        raise UpdateError("Не удалось распаковать обновление")
    script = downloaded.parent / "update.sh"
    script.write_text(mac_script(pid, app, target), encoding="utf-8")
    return ["/bin/sh", str(script)]


def windows_script(pid: int, new_dir: Path, target: Path) -> str:
    return (
        "@echo off\r\n"
        ":wait\r\n"
        f'tasklist /FI "PID eq {pid}" | find "{pid}" >nul && (timeout /t 1 /nobreak >nul & goto wait)\r\n'
        f'robocopy "{new_dir}" "{target}" /MIR /R:5 /W:1 /NFL /NDL /NJH /NJS >nul\r\n'
        f'start "" "{target / "Glimpsy.exe"}"\r\n'
        f'rmdir /s /q "{new_dir}"\r\n'
    )


def mac_script(pid: int, new_app: Path, target: Path) -> str:
    q = shlex.quote
    return (
        f"while kill -0 {pid} 2>/dev/null; do sleep 0.3; done\n"
        f"rm -rf {q(str(target))} && mv {q(str(new_app))} {q(str(target))}\n"
        f"xattr -dr com.apple.quarantine {q(str(target))} 2>/dev/null\n"
        f"open {q(str(target))}\n"
    )


def child_environment() -> dict:
    """Окружение для новой копии программы. Собранная программа (PyInstaller) кладёт в окружение
    свои служебные переменные; новая копия, получив их, решила бы, что она — часть старой, и
    стала бы брать файлы старой версии из её временной папки (ошибка «cannot import name … numpy»).
    Говорим ей: ты самостоятельная программа."""
    env = dict(os.environ)
    paths.clean_child_environment(env)
    for k in list(env):
        if k.startswith("_PYI_") or k in ("_MEIPASS2", "_PYI_APPLICATION_HOME_DIR"):
            env.pop(k, None)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def launch(cmd: list[str]) -> None:
    """Запустить команду отдельно от этой копии программы — она переживёт её выход."""
    env = child_environment()
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
        subprocess.Popen(cmd, env=env, creationflags=flags, close_fds=True)
    else:
        for k in ("APPDIR", "APPIMAGE", "ARGV0", "OWD"):   # новой копии AppImage их выставит сам
            env.pop(k, None)
        subprocess.Popen(cmd, env=env, start_new_session=True, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_exit(pid: int, timeout: float = 60.0) -> None:
    """Новая копия после обновления ждёт, пока старая закроется (иначе решит, что та ещё работает)."""
    import time

    import psutil

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return
        except psutil.Error:
            return
        time.sleep(0.2)
