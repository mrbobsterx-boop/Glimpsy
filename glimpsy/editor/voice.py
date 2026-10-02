"""Переозвучка вашим голосом: исправили фразу в тексте — программа говорит её вашим голосом.

Как устроено:
  * озвучку делает открытая нейросеть Chatterbox (лицензия MIT — можно и для коммерческих роликов),
    она понимает русский и ещё 22 языка и повторяет голос по образцу в 5–10 секунд;
  * нейросеть большая (≈ 4–5 ГБ вместе с её Python), поэтому в саму программу она не входит:
    это «голосовой модуль», который скачивается один раз в папку данных Glimpsy и дальше
    работает без интернета;
  * образец голоса берётся из того же видео — несколько секунд вашей речи рядом с фразой;
  * модуль работает отдельным процессом и держит нейросеть загруженной: первая фраза
    ждёт загрузки (около минуты), следующие — быстрее (десятки секунд на короткую фразу).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import http.client
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from glimpsy import paths
from glimpsy.paths import subprocess_flags

log = logging.getLogger(__name__)

PY_TAG = "20250317"
PY_VERSION = "3.11.11"
PY_URL = ("https://github.com/astral-sh/python-build-standalone/releases/download/{tag}/"
          "cpython-{ver}%2B{tag}-{target}-install_only.tar.gz")
TORCH = ["torch==2.6.0", "torchaudio==2.6.0"]
TORCH_INDEX = "https://download.pytorch.org/whl/cpu"
# сама нейросеть и то, что ей нужно (без её демо-интерфейса gradio — он тяжёлый и не нужен)
PACKAGES = ["chatterbox-tts==0.1.7", "numpy<2", "librosa==0.11.0", "s3tokenizer", "transformers==5.2.0",
            "diffusers==0.29.0", "resemble-perth>=1.0.0", "conformer==0.3.2", "safetensors==0.5.3",
            "pyloudnorm", "omegaconf", "huggingface_hub"]
SIZE_GB = 5
LANGS = {"ru", "en", "de", "uk", "es", "fr", "it", "pt", "pl", "tr", "ar", "da", "el", "fi", "he", "hi", "ja",
         "ko", "ms", "nl", "no", "sv", "sw", "zh"}
SAMPLE_RATE = 24000


class VoiceError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


def target() -> str | None:
    """Под какую систему качать Python для модуля (None — модуль здесь не работает)."""
    m = platform.machine().lower()
    if sys.platform.startswith("linux") and m in ("x86_64", "amd64"):
        return "x86_64-unknown-linux-gnu"
    if sys.platform.startswith("win") and m in ("amd64", "x86_64"):
        return "x86_64-pc-windows-msvc"
    if sys.platform == "darwin" and m == "arm64":
        return "aarch64-apple-darwin"
    return None                                    # Mac на Intel: для него нейросеть больше не собирают


def supported() -> bool:
    return target() is not None


def home() -> Path:
    return paths.data_dir() / "voice"


def python_exe() -> Path:
    base = home() / "python"
    return base / "python.exe" if sys.platform.startswith("win") else base / "bin" / "python3"


def installed() -> bool:
    return (home() / "ready").exists() and python_exe().exists()


def clean_env() -> dict:
    """Окружение для Python модуля: без переменных, которые подставляет сборка Glimpsy
    (иначе чужой Python подхватит наши библиотеки и не запустится)."""
    env = dict(os.environ)
    for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONNOUSERSITE", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH"):
        env.pop(k, None)
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH_ORIG")
    elif getattr(sys, "frozen", False):
        env.pop("LD_LIBRARY_PATH", None)
    env["HF_HOME"] = str(home() / "hf")
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    env["HF_HUB_DOWNLOAD_TIMEOUT"] = "120"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


# ---------------- установка ----------------

# Где системы держат список сертификатов (по нему проверяется, что сайт настоящий). Собранная
# программа ищет его там, где он был на сборочной машине, — на Steam Deck (SteamOS) и других
# Linux его там нет, и скачивание падало с «CERTIFICATE_VERIFY_FAILED». Ищем сами.
CA_FILES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem", "/etc/pki/tls/certs/ca-bundle.crt",
            "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem", "/etc/ssl/ca-bundle.pem",
            "/usr/local/etc/openssl/cert.pem", "/opt/homebrew/etc/openssl@3/cert.pem")


def ssl_context():
    """Проверка сайтов: системный список сертификатов + свой (certifi), если он есть."""
    import ssl

    ctx = ssl.create_default_context()
    for f in CA_FILES:
        if os.path.isfile(f):
            try:
                ctx.load_verify_locations(cafile=f)
            except (OSError, ssl.SSLError):
                continue
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError, ssl.SSLError):
        pass
    return ctx


def _download(url: str, dest: Path, progress: Callable[[float], None], cancel: threading.Event,
              size: int = 0, attempts: int = 40) -> None:
    """Скачать файл с докачкой: связь оборвалась — продолжаем с того же места (до attempts раз)."""
    import time as _time

    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, attempts + 1):
        have = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": "Glimpsy"}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60, context=ssl_context()) as r:
                if have and r.status != 206:                 # сервер не умеет докачку — сначала
                    have = 0
                total = size or (have + int(r.headers.get("Content-Length") or 0))
                with open(part, "ab" if have else "wb") as f:
                    got = have
                    while True:
                        if cancel.is_set():
                            raise Cancelled()
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            progress(min(1.0, got / total))
            if size and part.stat().st_size < size:
                raise OSError(f"скачано {part.stat().st_size} из {size} байт")
            part.replace(dest)
            return
        except Cancelled:
            raise
        except (OSError, http.client.HTTPException) as e:          # обрыв, тайм-аут, сброс соединения
            log.warning("Скачивание %s прервалось (попытка %d): %s", dest.name, attempt, e)
            if attempt == attempts:
                raise VoiceError(f"Не удалось скачать {dest.name}: связь с интернетом всё время обрывается "
                                 f"({e}). Попробуйте ещё раз — скачанное не пропадёт.") from e
            for _ in range(min(30, 2 * attempt) * 10):
                if cancel.is_set():
                    raise Cancelled()
                _time.sleep(0.1)


MEMORY_HINT = ("Похоже, компьютеру не хватило памяти — система остановила нейросеть. Закройте браузер, игры "
               "и другие тяжёлые программы и попробуйте ещё раз (уже скачанное заново не качается).")


MODEL_URL = "https://huggingface.co/ResembleAI/chatterbox/resolve/main/"
MODEL_FILES = {                    # файл: размер, байт
    "ve.pt": 5698626,
    "t3_mtl23ls_v2.safetensors": 2143989752,
    "s3gen.pt": 1057165844,
    "grapheme_mtl_merged_expanded_v1.json": 69989,
    "conds.pt": 107374,
    "Cangjie5_TC.json": 1920163,
}


def model_dir() -> Path:
    return home() / "model"


def model_ready() -> bool:
    d = model_dir()
    return all((d / f).exists() and (d / f).stat().st_size == n for f, n in MODEL_FILES.items())


def _take_from_old_cache(name: str, size: int, dest: Path) -> None:
    """Прошлые версии качали нейросеть в другой папке (hf/) — готовый файл берём оттуда."""
    for f in (home() / "hf" / "hub").glob(f"models--ResembleAI--chatterbox/snapshots/*/{name}"):
        try:
            real = f.resolve()
            if real.is_file() and real.stat().st_size == size:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(real), str(dest))
                return
        except OSError:
            continue


def download_model(progress: Callable[[float, str], None], cancel: threading.Event) -> None:
    """Веса нейросети (≈ 3,2 ГБ) — напрямую, с докачкой и понятным ходом скачивания."""
    total = sum(MODEL_FILES.values())
    done = 0
    for f, n in MODEL_FILES.items():
        dest = model_dir() / f
        if not (dest.exists() and dest.stat().st_size == n):
            _take_from_old_cache(f, n, dest)
        if not (dest.exists() and dest.stat().st_size == n):
            def prog(x: float, before=done, n=n) -> None:
                got = before + x * n
                progress(got / total, f"Скачиваю голосовую нейросеть: {got / 1e9:.1f} из {total / 1e9:.1f} ГБ"
                         .replace(".", ","))
            _download(MODEL_URL + f, dest, prog, cancel, size=n)
        done += n
        progress(done / total, f"Скачиваю голосовую нейросеть: {done / 1e9:.1f} из {total / 1e9:.1f} ГБ"
                 .replace(".", ","))


def install_log() -> Path:
    return home() / "install.log"


def _killed(code: int) -> bool:
    """Процесс остановила система (чаще всего — кончилась память)."""
    return code in (-9, 137, -6) or (sys.platform.startswith("win") and code in (3221225477, -1073741819))


def _run(cmd: list[str], cancel: threading.Event, on_line: Callable[[str], None] | None = None,
         offline: bool = False) -> None:
    log.info("Голосовой модуль: %s", " ".join(cmd))
    env = clean_env()
    if offline:
        env["HF_HUB_OFFLINE"] = "1"
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", env=env, **subprocess_flags())
    tail: list[str] = []
    assert proc.stdout is not None
    with open(install_log(), "a", encoding="utf-8") as full:          # весь вывод — в журнал установки
        full.write("\n$ " + " ".join(cmd) + "\n")
        for line in proc.stdout:
            full.write(line)
            if line.strip() and not line.lstrip().startswith("Warning: You are sending unauthenticated"):
                tail = (tail + [line.rstrip()])[-30:]
            if on_line is not None:
                on_line(line)
            if cancel.is_set():
                proc.kill()
                proc.wait()
                raise Cancelled()
        code = proc.wait()
        full.write(f"[код завершения: {code}]\n")
    if code != 0:
        log.error("Голосовой модуль: шаг завершился с кодом %s; вывод:\n%s", code, "\n".join(tail))
        why = MEMORY_HINT if _killed(code) else "\n".join(tail[-8:]) or f"код {code}"
        raise VoiceError(f"Установка голосового модуля не удалась.\n\n{why}\n\nПодробности — в файле {install_log()}")


def install(progress: Callable[[float, str], None], cancel: threading.Event | None = None) -> None:
    """Скачать и поставить голосовой модуль (долго: несколько гигабайт)."""
    cancel = cancel or threading.Event()
    t = target()
    if t is None:
        raise VoiceError("На этом компьютере голосовой модуль не работает (нужен Linux или Windows на x86-64 "
                         "либо Mac на Apple Silicon).")
    root = home()
    root.mkdir(parents=True, exist_ok=True)
    (root / "ready").unlink(missing_ok=True)
    # 1. свой Python — отдельно от системы и от Glimpsy
    if not python_exe().exists():
        progress(0.0, "Скачиваю Python для голосового модуля…")
        tgz = root / "python.tar.gz"
        _download(PY_URL.format(tag=PY_TAG, ver=PY_VERSION, target=t), tgz,
                  lambda f: progress(0.05 * f, "Скачиваю Python для голосового модуля…"), cancel)
        shutil.rmtree(root / "python", ignore_errors=True)
        with tarfile.open(tgz) as tf:
            tf.extractall(root)                    # внутри — папка python/
        tgz.unlink(missing_ok=True)
    py = str(python_exe())
    pip = [py, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
           "--progress-bar", "off"]
    lines = {"n": 0}

    def step(lo: float, hi: float, text: str, expect: int) -> Callable[[str], None]:
        def on_line(_line: str) -> None:
            lines["n"] += 1
            progress(min(hi, lo + (hi - lo) * lines["n"] / expect), text)
        lines["n"] = 0
        progress(lo, text)
        return on_line

    # 2. PyTorch (самое большое — около гигабайта)
    torch_args = TORCH if sys.platform == "darwin" else TORCH + ["--index-url", TORCH_INDEX]
    _run(pip + torch_args, cancel, step(0.05, 0.45, "Ставлю PyTorch (≈ 1 ГБ)…", 40))
    # 3. нейросеть озвучки
    _run(pip + PACKAGES, cancel, step(0.45, 0.7, "Ставлю нейросеть озвучки…", 120))
    # 4. веса нейросети (≈ 3,2 ГБ) — сами, с докачкой
    download_model(lambda f, t: progress(0.7 + 0.25 * f, t), cancel)
    shutil.rmtree(home() / "hf", ignore_errors=True)    # старая папка скачивания больше не нужна
    # 5. пробный запуск: нейросеть загружается с диска (без интернета)
    write_worker()
    _run([py, str(root / "glimpsy_voice.py"), "--warmup", "--model", str(model_dir())], cancel,
         step(0.95, 1.0, "Проверяю голосовую нейросеть (до минуты)…", 12), offline=True)
    (root / "ready").write_text(PY_TAG, encoding="utf-8")
    progress(1.0, "Голосовой модуль готов")


def remove() -> None:
    shutil.rmtree(home(), ignore_errors=True)


# ---------------- программа-«озвучиватель» (работает в Python модуля) ----------------

WORKER = r'''
import json, os, sys, time, warnings
warnings.filterwarnings("ignore")


def low_memory_loading(torch):
    # Без этого нейросеть при запуске держит в памяти две копии весов (~7 ГБ) — на Steam Deck
    # памяти может не хватить. Веса читаются прямо из файла и не копируются (~2,5 ГБ).
    _load = torch.load

    def load(*a, **k):
        k.setdefault("mmap", True)
        return _load(*a, **k)

    torch.load = load
    _lsd = torch.nn.Module.load_state_dict

    def lsd(self, state, strict=True, assign=False):
        return _lsd(self, state, strict=strict, assign=True)

    torch.nn.Module.load_state_dict = lsd


def mem_free():
    """Сколько памяти свободно (Linux), для журнала."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return f"{int(line.split()[1]) // 1024} МБ свободно"
    except OSError:
        pass
    return ""


def stage(text):
    print(f"[шаг] {text} {mem_free()}", flush=True)


def on_term(signum, frame):
    # систему попросили закрыть процесс — оставим в журнале, на каком шаге и сколько было памяти
    print(f"[остановлено системой: сигнал {signum}] {mem_free()}", flush=True)
    sys.exit(128 + signum)


def main():
    import signal
    signal.signal(signal.SIGTERM, on_term)
    stage("запуск")
    import torch
    torch.set_num_threads(max(2, min(8, __import__("os").cpu_count() or 4)))
    low_memory_loading(torch)
    # китайская часть при каждом запуске качает из интернета свою модель (~35 МБ) — нам не нужна
    sys.modules["spacy_pkuseg"] = None
    stage("загружаю библиотеки")
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS, Conditionals
    import torchaudio
    stage("загружаю нейросеть с диска")
    if "--model" in sys.argv:
        model = ChatterboxMultilingualTTS.from_local(sys.argv[sys.argv.index("--model") + 1], "cpu")
    else:
        model = ChatterboxMultilingualTTS.from_pretrained(device="cpu")
    stage("нейросеть загружена")
    if "--warmup" in sys.argv:
        print("warmup ok", flush=True)
        return
    print(json.dumps({"ready": True, "sr": model.sr}), flush=True)
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        try:
            t = time.time()
            exa = float(req.get("exaggeration", 0.5))
            conds = req.get("conds") or ""
            if conds and os.path.exists(conds):
                # сохранённый голос: уже разобранный образец — без повторного разбора
                model.conds = Conditionals.load(conds).to(model.device)
            elif req.get("ref"):
                model.prepare_conditionals(req["ref"], exaggeration=exa)
                if conds:
                    model.conds.save(conds)
            wav = model.generate(req["text"], language_id=req.get("lang", "ru"), exaggeration=exa,
                                 cfg_weight=float(req.get("cfg", 0.5)))
            torchaudio.save(req["out"], wav, model.sr)
            print(json.dumps({"ok": True, "out": req["out"], "seconds": round(time.time() - t, 1)}), flush=True)
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}), flush=True)


if __name__ == "__main__":
    main()
'''


def write_worker() -> Path:
    f = home() / "glimpsy_voice.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(WORKER, encoding="utf-8")
    return f


class Speaker:
    """Запущенный голосовой модуль: нейросеть загружается один раз и озвучивает фразы по очереди."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def _start(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        if not installed():
            raise VoiceError("Голосовой модуль ещё не установлен.")
        worker = write_worker()                    # свежая версия — вдруг Glimpsy обновился
        args = ["--model", str(model_dir())] if model_ready() else []
        env = clean_env()
        if args:
            env["HF_HUB_OFFLINE"] = "1"                # всё уже на диске — в интернет не ходим
        self.proc = subprocess.Popen([str(python_exe()), str(worker), *args], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=open(home() / "worker.log", "w"), text=True,
                                     encoding="utf-8", errors="replace", env=env, **subprocess_flags())
        self._read(lambda d: d.get("ready"))

    def _read(self, done: Callable[[dict], bool]) -> dict:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue                           # служебный вывод нейросети
            data = json.loads(line)
            if done(data):
                return data
        code = self.proc.wait() if self.proc is not None else 0
        self.proc = None
        raise VoiceError(("Голосовой модуль неожиданно закрылся. " + (MEMORY_HINT if _killed(code) else
                          f"Подробности — в файле {home() / 'worker.log'}")))

    def say(self, text: str, lang: str, ref: Path | None, out: Path, conds: Path | None = None) -> Path:
        """Озвучить фразу (долго — вызывать не из окна, а в фоне). ref — образец голоса; conds — где
        хранится уже разобранный образец сохранённого голоса (нет файла — разобрать и сохранить)."""
        with self.lock:
            self._start()
            assert self.proc is not None and self.proc.stdin is not None
            req = {"text": text, "lang": lang if lang in LANGS else "ru", "ref": str(ref) if ref else "",
                   "out": str(out), "conds": str(conds) if conds else ""}
            self.proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
            res = self._read(lambda d: "ok" in d)
            if not res["ok"]:
                raise VoiceError("Озвучка не получилась: " + res.get("error", ""))
            return out

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.stdin.close()            # type: ignore[union-attr]
                self.proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
        self.proc = None


_speaker: Speaker | None = None


def speaker() -> Speaker:
    global _speaker
    if _speaker is None:
        _speaker = Speaker()
    return _speaker


# ---------------- библиотека голосов ----------------

MIN_SAMPLE_S = 5.0            # короче — голос получается непохожим
MAX_SAMPLE_S = 20.0           # нейросеть всё равно слушает только первые ~10 с


@dataclass
class Voice:
    """Сохранённый голос: образец речи и его разобранная версия (для скорости)."""
    id: str
    name: str
    seconds: float
    created: float

    @property
    def folder(self) -> Path:
        return voices_dir() / self.id

    @property
    def sample(self) -> Path:
        return self.folder / "sample.wav"

    @property
    def conds(self) -> Path:
        return self.folder / "conds.pt"


def voices_dir() -> Path:
    """Голоса — отдельно от модуля: переустановка модуля их не трогает."""
    d = paths.data_dir() / "voices"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_voices() -> list[Voice]:
    out = []
    for d in sorted(voices_dir().iterdir()) if voices_dir().exists() else []:
        meta = d / "voice.json"
        if not meta.exists() or not (d / "sample.wav").exists():
            continue
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
            out.append(Voice(d.name, str(m["name"]), float(m.get("seconds", 0)), float(m.get("created", 0))))
        except (OSError, ValueError, KeyError):
            log.warning("Голос %s не читается", d)
    return sorted(out, key=lambda v: v.created)


def get_voice(voice_id: str) -> Voice | None:
    return next((v for v in list_voices() if v.id == voice_id), None)


def _write_meta(v: Voice) -> None:
    (v.folder / "voice.json").write_text(json.dumps({"name": v.name, "seconds": round(v.seconds, 2),
                                                     "created": v.created}, ensure_ascii=False), encoding="utf-8")


def add_voice(ffmpeg: str, name: str, source: Path, spans: list[tuple[float, float]] | None = None) -> Voice:
    """Сохранить голос: из куска видео (spans — где говорит человек) или из звукового файла/записи
    (тишина по краям убирается). Берётся не больше 20 секунд."""
    import time as _time
    import uuid

    from glimpsy.editor.music import probe_audio

    vid = uuid.uuid4().hex[:12]
    folder = voices_dir() / vid
    folder.mkdir(parents=True)
    sample = folder / "sample.wav"
    try:
        if spans:
            make_reference(ffmpeg, source, spans, sample, limit=MAX_SAMPLE_S)
        else:
            edge = "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.1"
            r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-vn",
                                "-af", f"{edge},areverse,{edge},areverse", "-t", str(MAX_SAMPLE_S), "-ac", "1",
                                "-ar", str(SAMPLE_RATE), str(sample)], capture_output=True, **subprocess_flags())
            if r.returncode != 0 or not sample.exists():
                raise VoiceError("Не удалось прочитать звук: " + r.stderr.decode("utf-8", "replace")[-300:])
        try:
            seconds = probe_audio(ffmpeg, sample)
        except ValueError:
            seconds = 0.0
        if seconds < MIN_SAMPLE_S:
            raise VoiceError(f"Для голоса нужно хотя бы {MIN_SAMPLE_S:.0f} секунд речи, а здесь "
                             f"{seconds:.1f} с. Возьмите кусок подлиннее.".replace(".", ",", 1))
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    v = Voice(vid, name.strip() or "Голос", seconds, _time.time())
    _write_meta(v)
    return v


def rename_voice(voice_id: str, name: str) -> None:
    v = get_voice(voice_id)
    if v is not None and name.strip():
        v.name = name.strip()
        _write_meta(v)


def delete_voice(voice_id: str) -> None:
    if voice_id and "/" not in voice_id and "\\" not in voice_id:
        shutil.rmtree(voices_dir() / voice_id, ignore_errors=True)


# ---------------- звук: образец голоса и подгонка фразы ----------------

def reference_spans(words, i: int, j: int, deleted: set[int], want: float = 9.0) -> list[tuple[float, float]]:
    """Где взять образец голоса: оставленная речь рядом с фразой [i..j] (сама фраза не годится —
    её как раз меняют), всего около want секунд."""
    order = sorted((k for k in range(len(words)) if (k < i or k > j) and k not in deleted),
                   key=lambda k: min(abs(k - i), abs(k - j)))
    picked: list[int] = []
    total = 0.0
    for k in order:
        picked.append(k)
        total += words[k].end - words[k].start
        if total >= want:
            break
    spans: list[tuple[float, float]] = []
    for k in sorted(picked):
        a, b = words[k].start - 0.05, words[k].end + 0.05
        if spans and a - spans[-1][1] < 0.35:
            spans[-1] = (spans[-1][0], b)
        else:
            spans.append((max(0.0, a), b))
    return spans


def make_reference(ffmpeg: str, src: Path, spans: list[tuple[float, float]], out: Path,
                   limit: float = 0.0) -> Path:
    """Вырезать образец голоса из видео (без пауз между кусками; limit — не длиннее стольких секунд)."""
    if not spans:
        raise VoiceError("Рядом с фразой нет вашей речи для образца голоса.")
    sel = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in spans)
    cut = ["-t", f"{limit:.2f}"] if limit > 0 else []
    r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vn",
                        "-af", f"aselect='{sel}',asetpts=N/SR/TB", *cut, "-ac", "1", "-ar", str(SAMPLE_RATE),
                        str(out)],
                       capture_output=True, **subprocess_flags())
    if r.returncode != 0 or not out.exists():
        raise VoiceError("Не удалось взять образец голоса: " + r.stderr.decode("utf-8", "replace")[-300:])
    return out


def atempo(factor: float) -> str:
    """Цепочка atempo для любого коэффициента (один фильтр — только 0,5…2)."""
    parts = []
    while factor > 2.0:
        parts.append("atempo=2.0")
        factor /= 2.0
    while factor < 0.5:
        parts.append("atempo=0.5")
        factor /= 0.5
    parts.append(f"atempo={factor:.4f}")
    return ",".join(parts)


def fit_phrase(ffmpeg: str, raw: Path, out: Path, target_s: float, src: Path, a: float, b: float,
               max_stretch: float = 1.3) -> float:
    """Подогнать озвученную фразу под место в видео: обрезать тишину по краям, ускорить или
    замедлить (не больше чем в max_stretch раз — иначе звучит неестественно) и сделать такой же
    громкости, как исходная речь. Возвращает длину результата."""
    from glimpsy.editor.music import probe_audio as probe_duration

    trimmed = out.with_name(out.stem + "_trim.wav")
    edge = "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05"
    r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw), "-af",
                        f"{edge},areverse,{edge},areverse", "-ac", "1", "-ar", "48000", str(trimmed)],
                       capture_output=True, **subprocess_flags())
    if r.returncode != 0:
        raise VoiceError("Не удалось обработать озвученную фразу.")
    dur = probe_duration(ffmpeg, trimmed) or 0.0
    factor = 1.0
    if dur > 0.05 and target_s > 0.05:
        factor = max(1 / max_stretch, min(max_stretch, dur / target_s))
    level = _loudness(ffmpeg, src, a, b)
    vol = f",loudnorm=I={level:.1f}:TP=-1.5:LRA=11" if level is not None else ""
    r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(trimmed), "-af",
                        atempo(factor) + vol, "-ac", "2", "-ar", "48000", str(out)],
                       capture_output=True, **subprocess_flags())
    trimmed.unlink(missing_ok=True)
    if r.returncode != 0:
        raise VoiceError("Не удалось подогнать озвученную фразу.")
    return probe_duration(ffmpeg, out) or dur / factor


def _loudness(ffmpeg: str, src: Path, a: float, b: float) -> float | None:
    """Громкость исходной речи (LUFS) — чтобы новая фраза звучала так же."""
    r = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-ss", f"{max(0.0, a - 2):.3f}", "-t", f"{b - a + 4:.3f}",
                        "-i", str(src), "-vn", "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True, errors="replace", **subprocess_flags())
    try:
        js = r.stderr[r.stderr.rindex("{"):r.stderr.rindex("}") + 1]
        v = float(json.loads(js)["input_i"])
    except (ValueError, KeyError):
        return None
    return max(-30.0, min(-10.0, v)) if v > -70 else None
