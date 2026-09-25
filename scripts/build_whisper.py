"""Собирает whisper-cli (whisper.cpp, MIT) — программу распознавания речи для автосубтитров.

    python scripts/build_whisper.py

Нужны CMake и компилятор C++ (Windows — Visual Studio Build Tools, macOS — Xcode
Command Line Tools, Linux — g++). Результат: vendor/whisper/whisper-cli(.exe), ~3 МБ.
Модели распознавания в сборку не входят: программа скачивает их сама, когда вы
первый раз нажимаете «Субтитры» (с вашего согласия).
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "v1.9.4"
URL = f"https://github.com/ggml-org/whisper.cpp/archive/refs/tags/{VERSION}.tar.gz"
ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "build" / "whisper.cpp"
OUT = ROOT / "vendor" / "whisper"
IS_WIN = sys.platform.startswith("win")
EXE = "whisper-cli.exe" if IS_WIN else "whisper-cli"


def main() -> None:
    if (OUT / EXE).exists() and (OUT / "VERSION").exists() and (OUT / "VERSION").read_text().strip() == VERSION:
        print(f"whisper-cli {VERSION} уже собран: {OUT / EXE}")
        return
    src = WORK / f"whisper.cpp-{VERSION.lstrip('v')}"
    if not src.exists():
        print(f"Скачиваю исходники whisper.cpp {VERSION}…")
        data = urllib.request.urlopen(URL, timeout=120).read()
        WORK.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(WORK)
    build = src / "build"
    cfg = ["cmake", "-S", str(src), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release",
           "-DBUILD_SHARED_LIBS=OFF", "-DWHISPER_BUILD_TESTS=OFF", "-DWHISPER_BUILD_SERVER=OFF",
           "-DWHISPER_SDL2=OFF", "-DWHISPER_CURL=OFF",
           "-DGGML_NATIVE=OFF",          # не под процессор сборочной машины, а под любой современный
           "-DGGML_OPENMP=OFF"]          # без лишних системных библиотек
    if IS_WIN:
        cfg.append("-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded")   # без зависимостей от DLL Visual C++
    if sys.platform == "darwin":
        cfg.append("-DCMAKE_OSX_DEPLOYMENT_TARGET=11.0")
    print(" ".join(cfg))
    subprocess.run(cfg, check=True)
    subprocess.run(["cmake", "--build", str(build), "--config", "Release", "-j", "4", "--target", "whisper-cli"],
                   check=True)
    found = [p for p in build.rglob(EXE) if p.is_file()]
    if not found:
        raise SystemExit("whisper-cli не найден после сборки")
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copy2(found[0], OUT / EXE)
    (OUT / "VERSION").write_text(VERSION)
    lic = src / "LICENSE"
    if lic.exists():
        shutil.copy2(lic, OUT / "LICENSE-whisper.cpp.txt")
    print(f"Готово: {OUT / EXE}")


if __name__ == "__main__":
    main()
