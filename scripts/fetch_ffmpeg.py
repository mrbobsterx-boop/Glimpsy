"""Скачивает готовый FFmpeg для текущей системы в папку vendor/ffmpeg/.

Используется и при сборке в GitHub Actions, и при запуске из исходников:
    python scripts/fetch_ffmpeg.py

Источники (бесплатные сборки с открытым кодом):
  Windows, Linux — https://github.com/BtbN/FFmpeg-Builds
  macOS          — https://ffmpeg.martin-riedl.de
"""

from __future__ import annotations

import io
import os
import platform
import shutil
import stat
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "vendor" / "ffmpeg"

BTBN = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"


def source() -> tuple[str, str]:
    """(url, путь к ffmpeg внутри архива — по окончанию имени)"""
    machine = platform.machine().lower()
    if sys.platform.startswith("win"):
        return BTBN + "ffmpeg-master-latest-win64-gpl.zip", "bin/ffmpeg.exe"
    if sys.platform == "darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
        return f"https://ffmpeg.martin-riedl.de/redirect/latest/macos/{arch}/release/ffmpeg.zip", "ffmpeg"
    arch = "linuxarm64" if machine in ("arm64", "aarch64") else "linux64"
    return BTBN + f"ffmpeg-master-latest-{arch}-gpl.tar.xz", "bin/ffmpeg"


def download(url: str) -> bytes:
    print(f"Скачиваю {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "glimpsy-build"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def extract(data: bytes, url: str, member_suffix: str, out: Path) -> None:
    if url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = next(n for n in z.namelist() if n.endswith(member_suffix) and not n.endswith("/"))
            with z.open(name) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as t:
            m = next(m for m in t.getmembers() if m.name.endswith(member_suffix) and m.isfile())
            with t.extractfile(m) as src, open(out, "wb") as dst:  # type: ignore[union-attr]
                shutil.copyfileobj(src, dst)
    out.chmod(out.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def main() -> None:
    # На Windows консоль может быть не в UTF-8 — чтобы русский текст не ронял скрипт
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    DEST.mkdir(parents=True, exist_ok=True)
    url, member = source()
    exe = DEST / ("ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg")
    if exe.exists() and "--force" not in sys.argv:
        print(f"Уже есть: {exe}")
        return
    extract(download(url), url, member, exe)
    print(f"Готово: {exe} ({exe.stat().st_size // 1_000_000} МБ)")
    if os.environ.get("GITHUB_ACTIONS"):
        os.system(f'"{exe}" -hide_banner -version')


if __name__ == "__main__":
    main()
