"""Звук: микрофон и то, что играет в колонках («звук экрана»), плюс голосовой режим.

Как и видео, звук пишется в кольцевой буфер — только последние полминуты, в памяти.
Когда сохраняется фрагмент, из буфера вырезается звук за тот же отрезок времени.

Голосовой режим: программа всё время слушает микрофон. Пока вы говорите, фрагмент
не режется и не ускоряется — запись идёт целиком, сколько бы вы ни говорили. Замолчали —
всё снова работает как обычно. Речь отличается от шума по громкости: порог сам
подстраивается под тихую комнату или шумный вентилятор.

Библиотека soundcard (BSD): Windows — WASAPI (микрофон и звук колонок), Linux — PulseAudio
/ PipeWire (то же), macOS — только микрофон (звук колонок без дополнительных программ
записать нельзя).
"""

from __future__ import annotations

import collections
import logging
import sys
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

RATE = 48000
CHUNK_S = 0.1                   # звук читается кусками по 100 мс
SYSTEM_GAIN = 0.8               # звук колонок чуть тише голоса


def _com_init() -> None:
    """Windows: каждому потоку, работающему со звуком, нужен свой COM."""
    if sys.platform.startswith("win"):
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0)     # type: ignore[attr-defined]


def list_microphones() -> list[str]:
    try:
        import soundcard as sc
        return [m.name for m in sc.all_microphones()]
    except Exception:
        log.exception("Не удалось получить список микрофонов")
        return []


def system_audio_supported() -> bool:
    return not sys.platform == "darwin"


# ---------------- кольцевой буфер ----------------

class Ring:
    """Последние N секунд звука: [(время начала, отсчёты float32 моно)]."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.chunks: collections.deque[tuple[float, np.ndarray]] = collections.deque()
        self.lock = threading.Lock()

    def add(self, t0: float, data: np.ndarray) -> None:
        with self.lock:
            self.chunks.append((t0, data))
            while self.chunks and self.chunks[0][0] < t0 - self.seconds:
                self.chunks.popleft()

    def extract(self, t0: float, t1: float) -> np.ndarray:
        """Звук за [t0, t1]; где звука не было — тишина."""
        n = max(0, int(round((t1 - t0) * RATE)))
        out = np.zeros(n, dtype=np.float32)
        with self.lock:
            chunks = list(self.chunks)
        for c0, data in chunks:
            a = int(round((c0 - t0) * RATE))
            b = a + len(data)
            if b <= 0 or a >= n:
                continue
            s0, s1 = max(0, a), min(n, b)
            out[s0:s1] = data[s0 - a:s1 - a]
        return out


# ---------------- голос ----------------

@dataclass
class VoiceState:
    speaking: bool = False
    start: float = 0.0          # когда началась речь (с запасом в начале)
    last_voice: float = 0.0     # последний момент с голосом


class VoiceDetector:
    """Речь или нет — по громкости, с порогом, который подстраивается под шум комнаты."""

    PRE_ROLL = 0.5              # начало фразы берём с запасом
    START_S = 0.3               # столько секунд голоса подряд — начало речи
    HANG_S = 1.6                # столько секунд тишины — конец речи
    MIN_RMS = 0.012             # тише этого — точно не речь (≈ −38 дБ)

    FLOOR_WINDOW = 200          # 20 с по 100 мс: по ним оцениваем шум комнаты

    def __init__(self, sensitivity: float = 1.0) -> None:
        self.sensitivity = sensitivity
        self.floor = 0.004
        self._levels: collections.deque[float] = collections.deque(maxlen=self.FLOOR_WINDOW)
        self.state = VoiceState()
        self._run = 0.0
        self.finished: list[tuple[float, float]] = []     # законченные фразы [(начало, конец)]
        self.lock = threading.Lock()

    def feed(self, t0: float, data: np.ndarray) -> None:
        dur = len(data) / RATE
        rms = float(np.sqrt(np.mean(data * data))) if len(data) else 0.0
        # Шум комнаты — самые тихие 10 % последних 20 секунд: в речи всегда есть паузы между
        # словами, а вентилятор или гул гудят ровно — они и становятся «тишиной».
        self._levels.append(rms)
        if len(self._levels) >= 10:
            self.floor = float(np.percentile(self._levels, 10))
        threshold = max(self.MIN_RMS / self.sensitivity, self.floor * 3.5 / self.sensitivity)
        voiced = rms > threshold and len(self._levels) >= 20      # первые 2 с — только слушаем шум
        t1 = t0 + dur
        with self.lock:
            st = self.state
            if voiced:
                self._run += dur
                st.last_voice = t1
                if not st.speaking and self._run >= self.START_S:
                    st.speaking = True
                    st.start = t1 - self._run - self.PRE_ROLL
            else:
                self._run = 0.0
                if st.speaking and t1 - st.last_voice >= self.HANG_S:
                    st.speaking = False
                    self.finished.append((st.start, st.last_voice + 0.3))

    def speaking(self) -> VoiceState:
        with self.lock:
            return VoiceState(self.state.speaking, self.state.start, self.state.last_voice)

    def pop_finished(self) -> list[tuple[float, float]]:
        with self.lock:
            out, self.finished = self.finished, []
            return out


# ---------------- захват ----------------

class _Source(threading.Thread):
    def __init__(self, name: str, open_recorder, ring: Ring, on_chunk=None) -> None:
        super().__init__(daemon=True, name=f"audio-{name}")
        self.label = name
        self.open_recorder = open_recorder
        self.ring = ring
        self.on_chunk = on_chunk
        self.stop_event = threading.Event()
        self.error = ""

    def run(self) -> None:
        _com_init()
        n = int(RATE * CHUNK_S)
        base: float | None = None       # время первого отсчёта
        count = 0                       # сколько отсчётов прочитано
        try:
            with self.open_recorder() as rec:
                while not self.stop_event.is_set():
                    data = rec.record(numframes=n)
                    now = time.time()
                    mono = data.mean(axis=1) if data.ndim > 1 else data
                    mono = np.asarray(mono, dtype=np.float32)
                    # Звук приходит пачками: время считаем по числу отсчётов (оно точное),
                    # а с часами сверяемся только при большом расхождении (устройство «икнуло»).
                    t0 = base + count / RATE if base is not None else now - len(mono) / RATE
                    if base is None or abs((now - len(mono) / RATE) - t0) > 1.0:
                        base, count = now - len(mono) / RATE, 0
                        t0 = base
                    count += len(mono)
                    self.ring.add(t0, mono)
                    if self.on_chunk is not None:
                        self.on_chunk(t0, mono)
        except Exception as e:
            self.error = str(e)
            log.exception("Звук (%s) не записывается", self.label)


class AudioCapture:
    """Микрофон + звук колонок в кольцевые буферы; голосовой детектор на микрофоне."""

    def __init__(self, buffer_s: float, mic: bool = True, mic_device: str = "", system: bool = True,
                 sensitivity: float = 1.0, sources: dict | None = None) -> None:
        self.mic_ring = Ring(buffer_s + 5) if mic else None
        self.sys_ring = Ring(buffer_s + 5) if system and system_audio_supported() else None
        self.voice = VoiceDetector(sensitivity)
        self.mic_device = mic_device
        self._sources_override = sources          # для проверок: готовые «рекордеры» вместо устройств
        self.threads: list[_Source] = []
        self._next_try = 0.0                      # не чаще раза в RETRY_S пробуем снова, если не вышло
        self._reported: set[str] = set()          # о каждой проблеме сообщаем один раз

    RETRY_S = 60.0

    @property
    def enabled(self) -> bool:
        return self.mic_ring is not None or self.sys_ring is not None

    def start(self) -> list[str]:
        """Запустить запись. Возвращает список проблем (устройство не найдено и т. п.)."""
        if self.threads:
            return []
        now = time.monotonic()
        if now < self._next_try:
            return []
        problems: list[str] = []
        openers = self._openers(problems)
        for name, opener, ring, cb in openers:
            t = _Source(name, opener, ring, cb)
            t.start()
            self.threads.append(t)
        if not openers:
            self._next_try = now + self.RETRY_S
        fresh = [p for p in problems if p not in self._reported]
        self._reported.update(problems)
        return fresh

    def _openers(self, problems: list[str]) -> list:
        if self._sources_override is not None:
            out = []
            if self.mic_ring is not None and "mic" in self._sources_override:
                out.append(("mic", self._sources_override["mic"], self.mic_ring, self.voice.feed))
            if self.sys_ring is not None and "system" in self._sources_override:
                out.append(("system", self._sources_override["system"], self.sys_ring, None))
            return out
        try:
            import soundcard as sc
        except Exception as e:
            problems.append(f"звук недоступен: {e}")
            return []
        out = []
        if self.mic_ring is not None:
            try:
                mic = sc.get_microphone(self.mic_device) if self.mic_device else sc.default_microphone()
                out.append(("mic", lambda m=mic: m.recorder(samplerate=RATE, channels=1, blocksize=1024),
                            self.mic_ring, self.voice.feed))
            except Exception as e:
                problems.append(f"микрофон не найден ({e})")
                self.mic_ring = None
        if self.sys_ring is not None:
            try:
                loop = sc.get_microphone(sc.default_speaker().name, include_loopback=True)
                out.append(("system", lambda m=loop: m.recorder(samplerate=RATE, channels=2, blocksize=1024),
                            self.sys_ring, None))
            except Exception as e:
                problems.append(f"звук колонок записать нельзя ({e})")
                self.sys_ring = None
        return out

    def stop(self) -> None:
        for t in self.threads:
            t.stop_event.set()
        for t in self.threads:
            t.join(timeout=2)
        self.threads = []

    @property
    def running(self) -> bool:
        return any(t.is_alive() for t in self.threads)

    def extract(self, t0: float, t1: float) -> np.ndarray | None:
        """Смешанный звук (голос + колонки) за отрезок, или None, если звук не пишется."""
        parts = []
        if self.mic_ring is not None:
            parts.append(self.mic_ring.extract(t0, t1))
        if self.sys_ring is not None:
            parts.append(self.sys_ring.extract(t0, t1) * SYSTEM_GAIN)
        if not parts:
            return None
        mix = parts[0] if len(parts) == 1 else parts[0] + parts[1]
        return np.clip(mix, -1.0, 1.0)


class MicMeter:
    """Проверка микрофона в настройках: слушает выбранный микрофон и показывает громкость.

    keep=True — ещё и записывает всё услышанное (кнопка «Запись» в редакторе), см. samples().
    """

    def __init__(self, device: str = "", opener=None, keep: bool = False) -> None:
        self.device = device
        self._opener = opener                     # для проверок: готовый «рекордер» вместо устройства
        self._rms = 0.0
        self._peak_at = 0.0                       # когда последний раз было громче порога речи
        self._source: _Source | None = None
        self.keep = keep
        self._chunks: list[np.ndarray] = []
        self.started_at = 0.0                     # когда пришёл первый звук (time.time())

    def start(self) -> str:
        """Начать слушать. Возвращает текст ошибки или пустую строку."""
        opener = self._opener
        if opener is None:
            try:
                import soundcard as sc
                mic = sc.get_microphone(self.device) if self.device else sc.default_microphone()
            except Exception as e:
                log.exception("Проверка микрофона: не удалось открыть")
                return f"микрофон не найден ({e})"
            opener = lambda: mic.recorder(samplerate=RATE, channels=1, blocksize=1024)  # noqa: E731
        self._source = _Source("mic-test", opener, Ring(1.0), self._feed)
        self._source.start()
        return ""

    def _feed(self, t0: float, data: np.ndarray) -> None:
        if self.keep:
            if not self._chunks:
                self.started_at = t0
            self._chunks.append(data.copy())
        rms = float(np.sqrt(np.mean(data * data))) if len(data) else 0.0
        self._rms = rms
        if rms >= VoiceDetector.MIN_RMS:
            self._peak_at = time.monotonic()

    @property
    def level(self) -> float:
        """Громкость 0…1 (шкала в децибелах: −60 дБ — 0, 0 дБ — 1)."""
        db = 20 * np.log10(self._rms + 1e-9)
        return float(min(1.0, max(0.0, (db + 60) / 60)))

    @property
    def heard(self) -> bool:
        """Слышен голос (за последние 1,5 с)."""
        return time.monotonic() - self._peak_at < 1.5 if self._peak_at else False

    @property
    def error(self) -> str:
        s = self._source
        return s.error if s is not None and not s.is_alive() else ""

    def stop(self) -> None:
        if self._source is not None:
            self._source.stop_event.set()
            self._source.join(timeout=2)
            self._source = None

    def samples(self) -> np.ndarray:
        """Всё записанное (при keep=True), float32 моно, RATE отсчётов в секунду."""
        return np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype=np.float32)


def write_wav(path: Path, samples: np.ndarray) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
