"""Фоновая музыка для ролика: один трек на всю длину, с плавным появлением и затуханием."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from glimpsy.paths import subprocess_flags

AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wma", ".aiff", ".aif"}


@dataclass
class MusicTrack:
    src: str                    # файл в папке проекта (media/…)
    duration: float             # длина трека, с
    label: str = ""
    volume: float = 0.6         # громкость музыки (0…1)
    in_s: float = 0.0           # с какого места трека начинать
    fade_in: float = 1.0
    fade_out: float = 2.0
    loop: bool = True           # повторять, если трек короче ролика
    duck: bool = True           # тише, когда в ролике есть свой звук (голос, видео с телефона)


def probe_audio(ffmpeg: str, path: Path) -> float:
    """Длительность аудиофайла, с. Бросает ValueError, если звука нет."""
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, **subprocess_flags())
    text = r.stderr.decode("utf-8", "replace")
    if not re.search(r"Stream #\S+.*?Audio:", text):
        raise ValueError("в файле нет звука")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not m:
        raise ValueError("не удалось узнать длину трека")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def music_filter(index: int, track: MusicTrack, total: float, voice_label: str | None) -> tuple[str, str]:
    """Цепочка FFmpeg для музыки. Возвращает (фильтры, метка готового звука музыки)."""
    fi = min(track.fade_in, total / 2)
    fo = min(track.fade_out, total / 2)
    chain = (f"[{index}:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,aformat=sample_rates=48000:"
             f"channel_layouts=stereo,volume={track.volume:.3f}")
    if fi > 0:
        chain += f",afade=t=in:st=0:d={fi:.3f}"
    if fo > 0:
        chain += f",afade=t=out:st={max(0.0, total - fo):.3f}:d={fo:.3f}"
    if track.duck and voice_label:
        # «приглушение»: когда в ролике звучит свой звук, музыка автоматически становится тише
        chain += f"[mraw];[mraw][{voice_label}]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400[mus]"
    else:
        chain += "[mus]"
    return chain, "[mus]"


def music_input_args(project_dir: Path, track: MusicTrack) -> list[str]:
    args = []
    if track.loop:
        args += ["-stream_loop", "-1"]
    args += ["-ss", f"{track.in_s:.3f}", "-i", str(project_dir / track.src)]
    return args
