"""Короткие фрагменты с веб-камеры (этап 3).

Камера включается только если это разрешено в настройках, и не постоянно: изредка,
пока вы работаете, программа снимает несколько секунд (у камеры в это время горит
лампочка) и сразу её выключает. Потом эти кусочки попадают в ролик маленьким окошком
в углу («картинка в картинке») — в редакторе их можно подвинуть или удалить.

Если камеры нет или она занята другой программой — фрагменты просто пропускаются.

Снимает сам FFmpeg:
  Windows — dshow, macOS — avfoundation, Linux — v4l2 (/dev/videoN).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from glimpsy.paths import subprocess_flags

log = logging.getLogger(__name__)

WARMUP_S = 1.5          # первые мгновения камера подстраивает яркость — их отрезаем
MAX_HEIGHT = 720

# среднее время активной работы между фрагментами, с
MODES = {"off": 0, "rare": 600, "sometimes": 300, "often": 120}
MODE_LABELS = {"off": "Не снимать", "rare": "Редко (≈ раз в 10 минут)",
               "sometimes": "Иногда (≈ раз в 5 минут)", "often": "Часто (≈ раз в 2 минуты)"}


@dataclass
class Camera:
    name: str       # как показывать человеку
    device: str     # что передать FFmpeg


@dataclass
class CamClip:
    file: str
    start: float    # время начала (как time.time())
    duration: float
    width: int = 0
    height: int = 0


# ---------------- поиск камер ----------------

def parse_dshow(text: str) -> list[Camera]:
    """Список видеоустройств из `ffmpeg -list_devices true -f dshow -i dummy`."""
    cams = [Camera(m.group(1), m.group(1)) for m in re.finditer(r'"([^"]+)"\s+\(video\)', text)]
    if cams:
        return cams
    # старые версии FFmpeg: заголовок «DirectShow video devices», затем имена в кавычках
    out, inside = [], False
    for line in text.splitlines():
        if "DirectShow video devices" in line:
            inside = True
        elif "DirectShow audio devices" in line:
            break
        elif inside and "Alternative name" not in line:
            m = re.search(r'"([^"]+)"', line)
            if m:
                out.append(Camera(m.group(1), m.group(1)))
    return out


def parse_avfoundation(text: str) -> list[Camera]:
    """Список из `ffmpeg -f avfoundation -list_devices true -i ""` (экраны пропускаем)."""
    out, inside = [], False
    for line in text.splitlines():
        if "AVFoundation video devices" in line:
            inside = True
        elif "AVFoundation audio devices" in line:
            break
        elif inside:
            m = re.search(r"\]\s*\[(\d+)\]\s*(.+?)\s*$", line)
            if m and not m.group(2).startswith("Capture screen"):
                out.append(Camera(m.group(2), m.group(2)))
    return out


def linux_cameras(sys_root: Path = Path("/sys/class/video4linux")) -> list[Camera]:
    """Камеры Linux. У одной камеры бывает несколько /dev/video* — берём основной (index 0)."""
    out = []
    for d in sorted(sys_root.glob("video*"), key=lambda p: int(re.sub(r"\D", "", p.name) or 0)):
        try:
            if (d / "index").exists() and (d / "index").read_text().strip() != "0":
                continue
            name = (d / "name").read_text().strip() if (d / "name").exists() else d.name
        except OSError:
            continue
        out.append(Camera(name, f"/dev/{d.name}"))
    return out


def list_cameras(ffmpeg: str) -> list[Camera]:
    try:
        if sys.platform.startswith("linux"):
            return linux_cameras()
        if sys.platform == "win32":
            args = ["-list_devices", "true", "-f", "dshow", "-i", "dummy"]
            parse = parse_dshow
        elif sys.platform == "darwin":
            args = ["-f", "avfoundation", "-list_devices", "true", "-i", ""]
            parse = parse_avfoundation
        else:
            return []
        r = subprocess.run([ffmpeg, "-hide_banner", *args], capture_output=True, timeout=15,
                           **subprocess_flags())
        return parse(r.stderr.decode("utf-8", "replace"))
    except (OSError, subprocess.TimeoutExpired):
        log.exception("Не удалось получить список камер")
        return []


def input_args(device: str, attempt: int = 0) -> list[str]:
    """Аргументы FFmpeg для чтения с камеры. attempt — номер попытки (другая частота кадров)."""
    if sys.platform == "win32":
        return ["-f", "dshow", "-rtbufsize", "64M", "-i", f"video={device}"]
    if sys.platform == "darwin":
        # камеры Mac капризны к частоте кадров: пробуем 30, потом 15, потом 25
        fps = (30, 15, 25)[attempt % 3]
        return ["-f", "avfoundation", "-framerate", str(fps), "-i", f"{device}:none"]
    # Linux: большинство веб-камер без сжатия отдают 720p лишь 10 кадров в секунду — видео дёргается.
    # Плавные 30 кадров — только в сжатом виде (MJPEG), его и просим; не умеет — попроще.
    q = ["-f", "v4l2", "-thread_queue_size", "512"]
    return (q + ["-input_format", "mjpeg", "-framerate", "30", "-video_size", "1280x720", "-i", device],
            q + ["-framerate", "30", "-i", device],
            q + ["-i", device])[attempt % 3]


def record_command(ffmpeg: str, in_args: list[str], seconds: float, out: Path) -> list[str]:
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-t", f"{seconds + WARMUP_S:.2f}", *in_args,
            "-ss", f"{WARMUP_S:.2f}", "-an",
            "-vf", f"scale=-2:'min({MAX_HEIGHT},ih)',fps=30,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-movflags", "+faststart", str(out)]


# ---------------- съёмка ----------------

class Webcam:
    """Снимает один фрагмент в фоне: start() → poll() до результата."""

    MAX_FAILS = 3

    def __init__(self, ffmpeg: str, preferred: str = "", input_override: list[str] | None = None) -> None:
        self.ffmpeg = ffmpeg
        self.preferred = preferred
        self._override = input_override        # для проверок: например, lavfi вместо камеры
        self.camera: Camera | None = None
        self.proc: subprocess.Popen | None = None
        self.out: Path | None = None
        self.started = 0.0
        self.seconds = 0.0
        self.fails = 0
        self.last_error = ""

    def find(self) -> Camera | None:
        if self._override is not None:
            self.camera = Camera("тест", "test")
            return self.camera
        cams = list_cameras(self.ffmpeg)
        pick = next((c for c in cams if self.preferred and c.name == self.preferred), None)
        self.camera = pick or (cams[0] if cams else None)
        log.info("Камеры: %s → %s", [c.name for c in cams], self.camera.name if self.camera else "нет")
        return self.camera

    @property
    def busy(self) -> bool:
        return self.proc is not None

    @property
    def gave_up(self) -> bool:
        return self.fails >= self.MAX_FAILS

    def start(self, out: Path, seconds: float) -> bool:
        if self.camera is None or self.busy or self.gave_up:
            return False
        in_args = self._override if self._override is not None else input_args(self.camera.device, self.fails)
        cmd = record_command(self.ffmpeg, in_args, seconds, out)
        log.debug("камера: %s", " ".join(cmd))
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, **subprocess_flags())
        except OSError as e:
            self._failed(str(e))
            return False
        self.out, self.seconds = out, seconds
        self.started = time.time()
        return True

    def poll(self) -> CamClip | None | bool:
        """None — ещё снимает; CamClip — готово; False — не получилось."""
        if self.proc is None:
            return False
        code = self.proc.poll()
        if code is None:
            if time.time() - self.started > self.seconds + WARMUP_S + 20:
                self.stop()                       # камера зависла
                self._failed("камера не отвечает")
                return False
            return None
        err = self.proc.stderr.read().decode("utf-8", "replace") if self.proc.stderr else ""
        self.proc = None
        out = self.out
        if code != 0 or out is None or not out.exists() or out.stat().st_size < 1000:
            if out is not None:
                out.unlink(missing_ok=True)
            self._failed(err.strip()[-300:] or f"код {code}")
            return False
        self.fails = 0
        w, h, dur = _probe(self.ffmpeg, out)
        if dur < 0.5:
            out.unlink(missing_ok=True)
            self._failed("слишком короткая запись")
            return False
        return CamClip(out.name, self.started + WARMUP_S, round(dur, 3), w, h)

    def stop(self) -> None:
        """Прервать съёмку (пауза, приватное окно, конец сессии)."""
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self.proc = None
            if self.out is not None:
                self.out.unlink(missing_ok=True)

    def _failed(self, msg: str) -> None:
        self.fails += 1
        self.last_error = msg
        log.warning("Камера: не получилось снять (%s/%s): %s", self.fails, self.MAX_FAILS, msg)


def _probe(ffmpeg: str, path: Path) -> tuple[int, int, float]:
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, **subprocess_flags())
    text = r.stderr.decode("utf-8", "replace")
    size = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", text)
    dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    w, h = (int(size.group(1)), int(size.group(2))) if size else (0, 0)
    d = int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3)) if dur else 0.0
    return w, h, d


# ---------------- хранилище фрагментов сессии ----------------

class CamStore:
    """Папка camera/ в сессии и список camera.json."""

    def __init__(self, session_dir: Path) -> None:
        self.dir = session_dir / "camera"
        self.index = self.dir / "camera.json"
        self.items: list[CamClip] = load_clips(session_dir)

    def new_path(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        n = len(list(self.dir.glob("cam_*.mp4")))
        while (p := self.dir / f"cam_{n:04d}.mp4").exists():
            n += 1
        return p

    def add(self, clip: CamClip, keep: int) -> None:
        self.items.append(clip)
        self.items.sort(key=lambda c: c.start)
        # слишком много — убираем тот, что ближе всех по времени к соседу (оставляем разнообразие)
        while len(self.items) > max(1, keep):
            gaps = [(self.items[i + 1].start - self.items[i].start, i + 1) for i in range(len(self.items) - 1)]
            _, drop = min(gaps)
            (self.dir / self.items.pop(drop).file).unlink(missing_ok=True)
        self.save()

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index.write_text(json.dumps({"items": [asdict(c) for c in self.items]}, indent=1), encoding="utf-8")


def load_clips(session_dir: Path) -> list[CamClip]:
    try:
        data = json.loads((session_dir / "camera" / "camera.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for d in data.get("items", []):
        try:
            c = CamClip(**{k: d[k] for k in ("file", "start", "duration", "width", "height") if k in d})
        except TypeError:
            continue
        if (session_dir / "camera" / c.file).exists():
            out.append(c)
    return out


class CameraRecorder:
    """Съёмка с камеры, пока не нажмут «Стоп» (кнопка «Запись» в редакторе)."""

    def __init__(self, ffmpeg: str, device: str = "", input_override: list[str] | None = None) -> None:
        self.ffmpeg = ffmpeg
        self.device = device
        self.input_override = input_override      # для проверок: вход FFmpeg вместо настоящей камеры
        self.out: Path | None = None
        self._proc: subprocess.Popen | None = None
        self.started_at = 0.0

    def start(self, out: Path) -> str:
        """Начать. Возвращает текст ошибки или пустую строку."""
        out.parent.mkdir(parents=True, exist_ok=True)
        variants = [self.input_override] if self.input_override else [input_args(self.device, i) for i in range(3)]
        err = ""
        for in_args in variants:
            # «superfast»: камера кодируется на лету и не должна отставать даже на слабом процессоре
            cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *in_args, "-an",
                   "-vf", f"scale=-2:'min({MAX_HEIGHT},ih)',fps=30,format=yuv420p",
                   "-c:v", "libx264", "-preset", "superfast", "-crf", "21", "-movflags", "+faststart", str(out)]
            try:
                self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.PIPE, **subprocess_flags())
            except OSError as e:
                return f"камера не включилась: {e}"
            self.out, self.started_at = out, time.time()
            time.sleep(0.8)
            if self._proc.poll() is None:
                log.info("Камера пишет: %s", " ".join(in_args))
                return ""
            text = (self._proc.stderr.read() if self._proc.stderr else b"").decode("utf-8", "replace").strip()
            err = text.splitlines()[-1] if text else ""
            log.info("Камера не приняла %s: %s", " ".join(in_args), err)
            self._proc = None
        return "камера не включилась" + (f": {err}" if err else "")

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> CamClip | None:
        """Остановить и дописать файл. Возвращает снятое или None."""
        proc = self._proc
        self._proc = None
        if proc is None or self.out is None:
            return None
        try:
            if proc.poll() is None and proc.stdin:
                proc.stdin.write(b"q")               # FFmpeg аккуратно дописывает файл
                proc.stdin.flush()
            proc.wait(timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait()
        if not self.out.exists() or self.out.stat().st_size < 1000:
            return None
        w, h, dur = _probe(self.ffmpeg, self.out)
        return CamClip(self.out.name, self.started_at, dur, w, h) if dur > 0.2 else None
