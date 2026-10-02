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
import threading
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


V4L2_CAP_VIDEO_CAPTURE = 0x1
V4L2_CAP_VIDEO_CAPTURE_MPLANE = 0x1000
V4L2_CAP_DEVICE_CAPS = 0x80000000
VIDIOC_QUERYCAP = 0x80685600          # _IOR('V', 0, struct v4l2_capability) — 104 байта


def v4l2_can_capture(dev: str) -> bool | None:
    """Умеет ли /dev/videoN отдавать картинку. None — спросить не удалось (нет прав и т. п.)."""
    try:
        import fcntl
        import os
        import struct

        fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
        try:
            buf = bytearray(104)
            fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf)
        finally:
            os.close(fd)
        caps, device_caps = struct.unpack_from("<II", buf, 84)
        use = device_caps if caps & V4L2_CAP_DEVICE_CAPS else caps
        return bool(use & (V4L2_CAP_VIDEO_CAPTURE | V4L2_CAP_VIDEO_CAPTURE_MPLANE))
    except (OSError, ImportError):
        return None


def linux_cameras(sys_root: Path = Path("/sys/class/video4linux"), probe=v4l2_can_capture) -> list[Camera]:
    """Камеры Linux. У одной камеры бывает несколько /dev/video* (картинка, служебные данные) —
    спрашиваем у системы, какой из них правда отдаёт картинку. Не ответила — берём основной (index 0)."""
    out = []
    for d in sorted(sys_root.glob("video*"), key=lambda p: int(re.sub(r"\D", "", p.name) or 0)):
        dev = f"/dev/{d.name}"
        try:
            name = (d / "name").read_text().strip() if (d / "name").exists() else d.name
            index = (d / "index").read_text().strip() if (d / "index").exists() else "0"
        except OSError:
            continue
        ok = probe(dev)
        log.info("Камера %s «%s»: index %s, %s", dev, name, index,
                 {True: "картинка есть", False: "служебное устройство", None: "не спросить"}[ok])
        if ok is False or (ok is None and index != "0"):
            continue
        if any(c.name == name for c in out) and ok is None:
            continue
        out.append(Camera(name if not any(c.name == name for c in out) else f"{name} ({d.name})", dev))
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
    q = ["-f", "v4l2"]
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
                _remove(self.out)

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


PREVIEW_W, PREVIEW_H = 320, 180          # маленькая картинка с камеры для окна записи


def _remove(path: Path) -> None:
    """Удалить недоснятый файл. На Windows FFmpeg отпускает файл не сразу после остановки —
    тогда немного подождём, а если не вышло — оставим (удалится при уборке проекта), но не упадём."""
    for _ in range(10):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.2)
    log.warning("Камера: не удалось удалить %s — файл занят", path)


class CameraRecorder:
    """Съёмка с камеры, пока не нажмут «Стоп» (кнопка «Запись» в редакторе).

    Заодно отдаёт маленькую картинку для просмотра (latest_frame) — и до записи (out=None:
    только просмотр), и во время неё. Камеру открывает один FFmpeg: два процесса к одной
    камере обычно не подключиться.
    """

    _good: dict[str, int] = {}               # какой способ открыть камеру уже сработал

    def __init__(self, ffmpeg: str, device: str = "", input_override: list[str] | None = None) -> None:
        self.ffmpeg = ffmpeg
        self.device = device
        self.input_override = input_override      # для проверок: вход FFmpeg вместо настоящей камеры
        self.out: Path | None = None
        self._proc: subprocess.Popen | None = None
        self.started_at = 0.0
        self._frame: bytes | None = None
        self._lock = threading.Lock()

    def _variants(self) -> list[tuple[int, list[str]]]:
        if self.input_override:
            return [(0, self.input_override)]
        order = list(range(3))
        good = self._good.get(self.device)
        if good is not None:
            order.remove(good)
            order.insert(0, good)
        return [(i, input_args(self.device, i)) for i in order]

    def _command(self, in_args: list[str], out: Path | None) -> list[str]:
        pv = (f"scale={PREVIEW_W}:{PREVIEW_H}:force_original_aspect_ratio=decrease,"
              f"pad={PREVIEW_W}:{PREVIEW_H}:(ow-iw)/2:(oh-ih)/2,fps=12,format=rgb24")
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *in_args, "-an"]
        if out is None:
            return cmd + ["-vf", pv, "-f", "rawvideo", "pipe:1"]
        # «superfast»: камера кодируется на лету и не должна отставать даже на слабом процессоре
        return cmd + ["-filter_complex",
                      f"[0:v]split=2[r][p];[r]scale=-2:'min({MAX_HEIGHT},ih)',fps=30,format=yuv420p[rv];[p]{pv}[pv]",
                      "-map", "[rv]", "-c:v", "libx264", "-preset", "superfast", "-crf", "21",
                      "-movflags", "+faststart", str(out),
                      "-map", "[pv]", "-f", "rawvideo", "pipe:1"]

    def start(self, out: Path | None = None) -> str:
        """Начать (out=None — только просмотр). Возвращает текст ошибки или пустую строку."""
        if out is not None:
            out.parent.mkdir(parents=True, exist_ok=True)
        err = ""
        for i, in_args in self._variants():
            try:
                self._proc = subprocess.Popen(self._command(in_args, out), stdin=subprocess.PIPE,
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, **subprocess_flags())
            except OSError as e:
                return f"камера не включилась: {e}"
            self.out, self.started_at = out, time.time()
            time.sleep(0.8)
            if self._proc.poll() is None:
                self._good[self.device] = i
                log.info("Камера %s: %s", "пишет" if out else "просмотр", " ".join(in_args))
                threading.Thread(target=self._read, args=(self._proc,), daemon=True, name="camera-preview").start()
                return ""
            text = (self._proc.stderr.read() if self._proc.stderr else b"").decode("utf-8", "replace").strip()
            lines = [x for x in text.splitlines() if x.strip()]
            err = lines[-1] if lines else ""
            log.info("Камера не приняла %s:\n%s", " ".join(in_args), "\n".join(lines[-4:]))
            self._proc = None
        return "камера не включилась" + (f": {err}" if err else "")

    def _read(self, proc: subprocess.Popen) -> None:
        size = PREVIEW_W * PREVIEW_H * 3
        stream = proc.stdout
        while stream is not None:
            data = stream.read(size)
            if not data or len(data) < size:
                return
            with self._lock:
                self._frame = data

    def latest_frame(self) -> bytes | None:
        """Последний кадр для просмотра: PREVIEW_W×PREVIEW_H, RGB, или None."""
        with self._lock:
            return self._frame

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> CamClip | None:
        """Остановить (и дописать файл, если писали). Возвращает снятое или None."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return None
        try:
            if proc.poll() is None and proc.stdin:
                proc.stdin.write(b"q")               # FFmpeg аккуратно дописывает файл
                proc.stdin.flush()
            proc.wait(timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait()
        if self.out is None or not self.out.exists() or self.out.stat().st_size < 1000:
            return None
        w, h, dur = _probe(self.ffmpeg, self.out)
        return CamClip(self.out.name, self.started_at, dur, w, h) if dur > 0.2 else None
