"""Кольцевой буфер: FFmpeg непрерывно пишет экран кусочками по ~1 секунде.

Как это работает (простыми словами):
  * FFmpeg снимает монитор и сохраняет видео маленькими файлами: s_000001.ts, s_000002.ts…
  * Старые кусочки (старше длины буфера, по умолчанию 30 с) удаляются.
  * Когда нужно сохранить момент, мы просто копируем нужные кусочки в один файл —
    без перекодирования, поэтому это мгновенно и не грузит процессор.
  * Параллельно FFmpeg выдаёт крошечные серые миниатюры 64×36 два раза в секунду —
    по ним мы понимаем, насколько меняется картинка.

Один «прогон» (BufferRun) = один непрерывный кусок записи одного монитора.
Смена монитора, пауза или приватное окно завершают прогон; потом начинается новый.
"""

from __future__ import annotations

import collections
import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from worklapse.paths import IS_WINDOWS, subprocess_flags
from worklapse.platform.base import CaptureInput, Monitor
from worklapse.recorder.encoder import Encoder

log = logging.getLogger(__name__)

THUMB_W, THUMB_H, THUMB_FPS = 64, 36, 2


@dataclass
class Segment:
    path: Path
    stream_start: float   # время внутри видеопотока, с
    stream_end: float
    wall_start: float = 0.0   # «настоящее» время (time.time())
    wall_end: float = 0.0

    @property
    def duration(self) -> float:
        return self.stream_end - self.stream_start


class BufferRun:
    def __init__(self, ffmpeg: str, capture: CaptureInput, monitor: Monitor, encoder: Encoder,
                 fps: int, run_dir: Path, max_height: int,
                 on_frame_diff: Callable[[float, float], None] | None = None) -> None:
        self.ffmpeg = ffmpeg
        self.capture = capture
        self.monitor = monitor
        self.encoder = encoder
        self.fps = fps
        self.dir = run_dir
        self.max_height = max_height
        self.on_frame_diff = on_frame_diff
        self.segments: collections.deque[Segment] = collections.deque()
        self.started_at = 0.0
        self.ended_at: float | None = None
        self._wall_origin: float | None = None   # wall-время, соответствующее t=0 потока
        self._list_pos = 0
        self._proc: subprocess.Popen | None = None
        self._producer: subprocess.Popen | None = None
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=30)
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    # ---------- запуск / остановка ----------

    def build_command(self) -> list[str]:
        cap = self.capture
        # Уменьшаем слишком большие экраны (4K/5K) — меньше нагрузка и размер файлов.
        # Размеры делаем чётными: этого требуют видеокодеки.
        scale = (f"scale='trunc(min(iw,iw*{self.max_height}/ih)/2)*2':"
                 f"'trunc(min(ih,{self.max_height})/2)*2'")
        graph = (
            f"[0:v]{cap.pre_filter}fps={self.fps},split=2[m][t];"
            f"[m]{scale},{self.encoder.filter_suffix}[enc];"
            f"[t]fps={THUMB_FPS},scale={THUMB_W}:{THUMB_H},format=gray[th]"
        )
        return [
            self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats",
            *self.encoder.global_args,
            *cap.args,
            "-filter_complex", graph,
            "-map", "[enc]", *self.encoder.args("buffer", self.fps),
            "-force_key_frames", "expr:gte(t,n_forced*1)",
            "-f", "segment", "-segment_time", "1", "-segment_format", "mpegts",
            "-segment_list", str(self.dir / "list.csv"), "-segment_list_type", "csv",
            str(self.dir / "s_%06d.ts"),
            "-map", "[th]", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
        ]

    def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        cmd = self.build_command()
        log.info("Запуск буфера: %s", " ".join(cmd))
        stdin = subprocess.PIPE
        if self.capture.producer:
            # Wayland: GStreamer выдаёт сырые кадры → они идут прямо в FFmpeg (минуя Python)
            self._producer = subprocess.Popen(
                self.capture.producer, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                pass_fds=self.capture.producer_pass_fds,
            )
            stdin = self._producer.stdout
        self._proc = subprocess.Popen(cmd, stdin=stdin, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, **subprocess_flags())
        if self._producer and self._producer.stdout:
            self._producer.stdout.close()  # теперь поток принадлежит FFmpeg
        self.started_at = time.time()
        for target, name in ((self._read_thumbs, "thumbs"), (self._read_stderr, "stderr")):
            t = threading.Thread(target=target, daemon=True, name=f"buffer-{name}")
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 6.0) -> None:
        """Аккуратно останавливает FFmpeg, чтобы последний кусочек дописался целиком."""
        proc = self._proc
        if proc is None:
            return
        try:
            if self._producer:
                self._producer.terminate()     # конец потока → FFmpeg завершится сам
            elif proc.stdin and proc.poll() is None:
                proc.stdin.write(b"q")         # «q» — штатная команда остановки FFmpeg
                proc.stdin.flush()
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("FFmpeg не остановился вовремя, завершаем принудительно")
            proc.kill() if not IS_WINDOWS else proc.terminate()
            proc.wait(timeout=3)
        if self._producer:
            try:
                self._producer.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._producer.kill()
        self.poll()  # подхватить последние кусочки
        self.ended_at = time.time()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def error_text(self) -> str:
        return "\n".join(self._stderr_tail)

    # ---------- чтение данных от FFmpeg ----------

    def _read_thumbs(self) -> None:
        size = THUMB_W * THUMB_H
        prev = None
        stdout = self._proc.stdout if self._proc else None
        if stdout is None:
            return
        while True:
            data = stdout.read(size)
            if not data or len(data) < size:
                break
            frame = np.frombuffer(data, dtype=np.uint8).astype(np.int16)
            if prev is not None and self.on_frame_diff:
                diff = float(np.abs(frame - prev).mean())   # 0 — ничего не изменилось, 255 — всё
                try:
                    self.on_frame_diff(diff, time.time())
                except Exception:
                    log.exception("on_frame_diff")
            prev = frame

    def _read_stderr(self) -> None:
        stderr = self._proc.stderr if self._proc else None
        if stderr is None:
            return
        for raw in iter(stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self._stderr_tail.append(line)
                log.debug("ffmpeg: %s", line)

    def poll(self) -> None:
        """Читает новые строки из списка кусочков (FFmpeg дописывает строку, когда кусочек готов)."""
        list_file = self.dir / "list.csv"
        if not list_file.exists():
            return
        now = time.time()
        with open(list_file, "rb") as f:
            f.seek(self._list_pos)
            chunk = f.read()
        # берём только завершённые строки
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        self._list_pos += end + 1
        with self._lock:
            for line in chunk[: end + 1].decode("utf-8", "replace").splitlines():
                parts = line.strip().split(",")
                if len(parts) < 3:
                    continue
                try:
                    seg = Segment(self.dir / parts[0], float(parts[1]), float(parts[2]))
                except ValueError:
                    continue
                # Сопоставляем время потока с настоящим временем. Мы узнаём о кусочке
                # чуть позже, чем он закончился, поэтому берём минимальную оценку.
                origin = now - seg.stream_end
                if self._wall_origin is None or origin < self._wall_origin:
                    self._wall_origin = origin
                self.segments.append(seg)
            for s in self.segments:
                s.wall_start = self._wall_origin + s.stream_start
                s.wall_end = self._wall_origin + s.stream_end

    def trim(self, keep_seconds: float) -> None:
        """Удаляет кусочки старше keep_seconds — это и есть «кольцо»."""
        cutoff = time.time() - keep_seconds
        with self._lock:
            while self.segments and self.segments[0].wall_end < cutoff:
                seg = self.segments.popleft()
                try:
                    seg.path.unlink(missing_ok=True)
                except OSError:
                    pass  # на Windows файл может быть ещё открыт — удалим позже вместе с папкой

    # ---------- сохранение фрагмента ----------

    @property
    def available_from(self) -> float | None:
        with self._lock:
            return self.segments[0].wall_start if self.segments else None

    @property
    def available_to(self) -> float | None:
        with self._lock:
            return self.segments[-1].wall_end if self.segments else None

    def save_clip(self, t0: float, t1: float, dest: Path) -> tuple[float, float] | None:
        """Склеивает кусочки, покрывающие [t0, t1], в один файл (без перекодирования).

        Возвращает реальные границы (wall_start, wall_end) сохранённого файла или None.
        Файлы MPEG-TS можно просто дописывать друг за другом — это валидное видео.
        """
        with self._lock:
            segs = [s for s in self.segments if s.wall_end > t0 and s.wall_start < t1]
        if not segs:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as out:
            for s in segs:
                with open(s.path, "rb") as f:
                    shutil.copyfileobj(f, out)
        return segs[0].wall_start, segs[-1].wall_end

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
