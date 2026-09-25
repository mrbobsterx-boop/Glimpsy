"""Автосубтитры: речь из ролика → тексты на ленте.

Как это работает:
  1. FFmpeg собирает весь звук ролика (вставленные видео со звуком, звук наложений)
     в один файл — так, как он звучит в готовом ролике, с учётом обрезки и скорости.
     Фоновая музыка не входит — её распознавать не нужно.
  2. whisper.cpp (открытая программа, MIT) распознаёт речь прямо на компьютере.
     Нужна модель распознавания — её один раз скачивает сама программа (с вашего согласия).
  3. Каждая фраза становится обычным текстом на ленте: общий стиль, правка, удаление —
     как у любого текста.

Whisper на тишине иногда «придумывает» фразы («Продолжение следует…», «Субтитры
сделал…»). Против этого три защиты: детектор речи (VAD), проверка громкости каждой
фразы и список таких типичных фраз.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from worklapse import paths
from worklapse.editor.export import atempo_chain
from worklapse.editor.project import Project, new_id
from worklapse.editor.text import TextItem
from worklapse.paths import subprocess_flags

log = logging.getLogger(__name__)

HF = "https://huggingface.co/"
MODELS = {
    # ключ: (файл, адрес, размер в МБ, описание)
    "base": ("ggml-base.bin", HF + "ggerganov/whisper.cpp/resolve/main/ggml-base.bin", 148,
             "Быстрая (148 МБ) — хватает для чёткой речи"),
    "small": ("ggml-small.bin", HF + "ggerganov/whisper.cpp/resolve/main/ggml-small.bin", 488,
              "Точная (488 МБ) — лучше для русского, но медленнее"),
}
VAD_MODEL = ("ggml-silero-v5.1.2.bin", HF + "ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin", 1)
LANGUAGES = [("auto", "Определить самому"), ("ru", "Русский"), ("en", "English"), ("uk", "Українська"),
             ("de", "Deutsch"), ("es", "Español"), ("fr", "Français"), ("it", "Italiano"),
             ("pt", "Português"), ("pl", "Polski"), ("tr", "Türkçe"), ("kk", "Қазақша")]
LENGTHS = {"short": (24, "Короткие строки (Reels, Shorts)"), "normal": (42, "Обычные строки (YouTube)")}

SAMPLE_RATE = 16000
MIN_RMS = 0.006               # тише этого — не речь, а тишина/шум
HALLUCINATIONS = [            # фразы, которые Whisper «слышит» в тишине
    "продолжение следует", "субтитры сделал", "субтитры создавал", "субтитры подогнал",
    "редактор субтитров", "корректор", "спасибо за просмотр", "подписывайтесь на канал",
    "dimatorzok", "amara.org", "thanks for watching", "thank you for watching",
    "please subscribe", "subtitles by",
]


class SubtitleError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


@dataclass
class Segment:
    start: float
    end: float
    text: str


# ---------------- модели ----------------

def models_dir() -> Path:
    d = paths.data_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def model_path(key: str) -> Path:
    return models_dir() / MODELS[key][0]


def vad_path() -> Path:
    return models_dir() / VAD_MODEL[0]


def model_ready(key: str) -> bool:
    p = model_path(key)
    return p.exists() and p.stat().st_size > MODELS[key][2] * 1_000_000 * 0.9


def vad_ready() -> bool:
    p = vad_path()
    return p.exists() and p.stat().st_size > 100_000


def whisper_exe() -> str | None:
    return paths.find_executable("whisper-cli")


# ---------------- звук ролика ----------------

def has_speech_audio(project: Project) -> bool:
    """Есть ли в ролике хоть какой-то свой звук (без музыки)."""
    if any(c.kind == "video" and c.has_audio and not c.muted for c in project.clips):
        return True
    return any(o.kind == "video" and o.has_audio and not o.muted for o in getattr(project, "overlays", []))


def audio_command(ffmpeg: str, project: Project, out: Path) -> list[str] | None:
    """Команда FFmpeg: весь звук ролика (как он будет звучать) → WAV 16 кГц моно."""
    total = project.total
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    parts: list[str] = []
    n = 0
    t = 0.0
    for c in project.clips:
        if c.kind == "video" and c.has_audio and not c.muted:
            cmd += ["-ss", f"{c.in_s:.3f}", "-t", f"{c.out_s - c.in_s:.3f}", "-i", str(project.path_of(c))]
            parts.append(f"[{n}:a]asetpts=PTS-STARTPTS,{','.join(atempo_chain(c.speed))},"
                         f"adelay=delays={t * 1000:.0f}:all=1[a{n}]")
            n += 1
        t += c.duration
    for o in getattr(project, "overlays", []):
        if o.kind == "video" and o.has_audio and not o.muted and o.start < total:
            dur = min(o.duration, total - o.start)
            cmd += ["-ss", f"{o.in_s:.3f}", "-t", f"{dur:.3f}", "-i", str(project.dir / o.src)]
            parts.append(f"[{n}:a]asetpts=PTS-STARTPTS,adelay=delays={o.start * 1000:.0f}:all=1[a{n}]")
            n += 1
    if n == 0:
        return None
    labels = "".join(f"[a{i}]" for i in range(n))
    parts.append(f"{labels}amix=inputs={n}:duration=longest:normalize=0:dropout_transition=0,"
                 f"aresample={SAMPLE_RATE},aformat=sample_fmts=s16:channel_layouts=mono[out]")
    return cmd + ["-filter_complex", ";".join(parts), "-map", "[out]", "-t", f"{total:.3f}",
                  "-c:a", "pcm_s16le", str(out)]


def read_wav(path: Path):
    import numpy as np
    with wave.open(str(path), "rb") as w:
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype="<i2").astype("float32") / 32768.0


def loud_enough(samples, start: float, end: float) -> bool:
    import numpy as np
    a, b = int(start * SAMPLE_RATE), int(end * SAMPLE_RATE)
    chunk = samples[max(0, a):max(a + 1, b)]
    if len(chunk) == 0:
        return False
    return float(np.sqrt(np.mean(chunk * chunk))) >= MIN_RMS


# ---------------- распознавание ----------------

def _short_path(p: Path) -> str:
    """whisper.cpp на Windows не всегда открывает пути с русскими буквами — берём короткое имя 8.3."""
    s = str(p)
    if sys.platform != "win32" or s.isascii():
        return s
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    if ctypes.windll.kernel32.GetShortPathNameW(s, buf, 1024):   # type: ignore[attr-defined]
        return buf.value
    return s


def whisper_command(exe: str, model: Path, wav: Path, out_base: Path, language: str, max_chars: int,
                    vad: Path | None) -> list[str]:
    cmd = [exe, "-m", _short_path(model), "-f", _short_path(wav), "-l", language,
           "-ml", str(max_chars), "-sow", "-oj", "-of", _short_path(out_base.parent) + "/" + out_base.name,
           "-pp", "-np", "-t", "4"]
    if vad is not None:
        cmd += ["--vad", "-vm", _short_path(vad)]
    return cmd


def parse_whisper_json(text: str) -> tuple[list[Segment], str]:
    data = json.loads(text)
    segs = []
    for s in data.get("transcription", []):
        off = s.get("offsets", {})
        segs.append(Segment(off.get("from", 0) / 1000.0, off.get("to", 0) / 1000.0, s.get("text", "")))
    return segs, data.get("result", {}).get("language", "")


def clean_segments(segs: list[Segment], samples=None) -> list[Segment]:
    """Убираем пустое, «придуманное» и то, что звучит в тишине; склеиваем обрывки."""
    out: list[Segment] = []
    for s in segs:
        text = re.sub(r"\s+", " ", s.text).strip()
        bare = re.sub(r"[\[\(\*♪].*?[\]\)\*♪]", "", text).strip(" .,!?…-—♪")
        if not bare:
            continue                                     # [музыка], (смех), ♪
        low = bare.lower()
        if any(h in low for h in HALLUCINATIONS):
            continue
        if s.end <= s.start:
            continue
        if samples is not None and not loud_enough(samples, s.start, s.end):
            continue
        if out and len(bare) <= 3 and s.start - out[-1].end < 0.3:
            out[-1] = Segment(out[-1].start, s.end, f"{out[-1].text} {text}")   # одинокое короткое слово
            continue
        out.append(Segment(s.start, s.end, text))
    # фраза держится на экране хотя бы 0,8 с, но не наезжает на следующую
    for i, s in enumerate(out):
        nxt = out[i + 1].start if i + 1 < len(out) else s.end + 1.0
        s.end = max(s.end, min(s.start + 0.8, nxt))
    return out


def run_whisper(cmd: list[str], progress: Callable[[float], None], cancel: threading.Event) -> str:
    """Запустить whisper-cli, следить за процентами, вернуть stderr (для ошибок)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **subprocess_flags())
    err: list[str] = []

    def read_err() -> None:
        assert proc.stderr is not None
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace")
            err.append(line)
            m = re.search(r"progress\s*=\s*(\d+)%", line)
            if m:
                progress(int(m.group(1)) / 100)

    th = threading.Thread(target=read_err, daemon=True)
    th.start()
    assert proc.stdout is not None
    while proc.poll() is None:
        if cancel.is_set():
            proc.kill()
            proc.wait()
            raise Cancelled()
        th.join(0.2)
    proc.stdout.read()
    th.join(2)
    if proc.returncode != 0:
        raise SubtitleError("Распознавание не удалось: " + "".join(err[-8:])[-600:])
    return "".join(err)


def transcribe(ffmpeg: str, project: Project, model_key: str, language: str, max_chars: int, work: Path,
               progress: Callable[[float, str], None] | None = None,
               cancel: threading.Event | None = None) -> tuple[list[Segment], str]:
    """Весь путь: звук → распознавание → очищенные фразы. Возвращает (фразы, язык)."""
    progress = progress or (lambda f, t: None)
    cancel = cancel or threading.Event()
    exe = whisper_exe()
    if not exe:
        raise SubtitleError("В этой сборке нет программы распознавания речи (whisper-cli).")
    if not model_ready(model_key):
        raise SubtitleError("Модель распознавания ещё не скачана.")
    work.mkdir(parents=True, exist_ok=True)
    wav = work / "speech.wav"
    cmd = audio_command(ffmpeg, project, wav)
    if cmd is None:
        raise SubtitleError("В ролике нет звука с речью: у фрагментов и наложений звук выключен или его нет.")
    progress(0.02, "Собираю звук ролика…")
    r = subprocess.run(cmd, capture_output=True, **subprocess_flags())
    if r.returncode != 0 or not wav.exists():
        raise SubtitleError("FFmpeg: " + r.stderr.decode("utf-8", "replace")[-600:])
    if cancel.is_set():
        raise Cancelled()
    samples = read_wav(wav)
    if len(samples) == 0 or float(abs(samples).max()) < MIN_RMS:
        raise SubtitleError("Звук в ролике почти беззвучный — распознавать нечего.")
    out_base = work / "speech"
    (work / "speech.json").unlink(missing_ok=True)
    wcmd = whisper_command(exe, model_path(model_key), wav, out_base, language, max_chars,
                           vad_path() if vad_ready() else None)
    log.info("whisper: %s", " ".join(wcmd))
    progress(0.05, "Распознаю речь…")
    run_whisper(wcmd, lambda f: progress(0.05 + 0.93 * f, "Распознаю речь…"), cancel)
    js = work / "speech.json"
    if not js.exists():
        raise SubtitleError("Программа распознавания не вернула результат.")
    segs, lang = parse_whisper_json(js.read_text(encoding="utf-8", errors="replace"))
    return clean_segments(segs, samples), lang


def make_texts(segs: list[Segment], total: float) -> list[TextItem]:
    items = []
    for s in segs:
        if s.start >= total:
            continue
        end = min(s.end, total)
        items.append(TextItem(new_id(), s.text, round(s.start, 2), round(max(0.3, end - s.start), 2), auto=True))
    return items
