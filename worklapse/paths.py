"""Где что лежит: настройки, временные файлы, FFmpeg.

Все файлы остаются только на этом компьютере — программа ничего не отправляет в интернет.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir, user_log_dir

from worklapse import APP_NAME

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def config_dir() -> Path:
    p = Path(user_config_dir(APP_NAME, appauthor=False))
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_dir() -> Path:
    """Проекты для будущего редактора (этап 2) — локально, не в облачной папке."""
    p = Path(user_data_dir(APP_NAME, appauthor=False))
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_dir() -> Path:
    p = Path(user_log_dir(APP_NAME, appauthor=False))
    p.mkdir(parents=True, exist_ok=True)
    return p


def temp_root() -> Path:
    """Кольцевой буфер и черновые фрагменты. Удаляются после сборки ролика."""
    p = Path(tempfile.gettempdir()) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def default_output_dir() -> Path:
    home = Path.home()
    videos = home / ("Movies" if IS_MAC else "Videos")
    return videos / APP_NAME


def _bundle_dir() -> Path | None:
    """Папка распакованной сборки PyInstaller (там лежит встроенный FFmpeg)."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def find_executable(name: str) -> str | None:
    """Ищет программу (ffmpeg, whisper-cli): сначала встроенную, потом в системе."""
    exe = name + (".exe" if IS_WINDOWS else "")
    folder = "whisper" if name.startswith("whisper") else "ffmpeg"
    env = os.environ.get(f"WORKLAPSE_{name.upper()}")
    if env and Path(env).exists():
        return env
    candidates = []
    bundle = _bundle_dir()
    if bundle:
        candidates.append(bundle / folder / exe)
    # При запуске из исходников: vendor/ffmpeg/ и vendor/whisper/ (scripts/fetch_ffmpeg.py, build_whisper.py)
    candidates.append(Path(__file__).resolve().parent.parent / "vendor" / folder / exe)
    for c in candidates:
        if c.exists():
            return str(c)
    return shutil.which(name)


def clean_child_environment(env=None) -> None:
    """Собранная программа для Linux (PyInstaller) подсовывает свои библиотеки через
    LD_LIBRARY_PATH, и его наследуют все запущенные программы: FFmpeg, whisper, xdg-open.
    Тогда, например, драйвер видеокарты не загружается («GLIBCXX not found»), и аппаратное
    ускорение не работает. Возвращаем системное значение — сама программа свои библиотеки
    уже загрузила, ей это не мешает."""
    env = os.environ if env is None else env
    if not getattr(sys, "frozen", False) or not sys.platform.startswith("linux"):
        return
    orig = env.get("LD_LIBRARY_PATH_ORIG")
    if orig is not None:
        env["LD_LIBRARY_PATH"] = orig
    else:
        env.pop("LD_LIBRARY_PATH", None)


def subprocess_flags() -> dict:
    """На Windows прячем чёрные консольные окна у FFmpeg."""
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def open_in_file_manager(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif IS_MAC:
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
