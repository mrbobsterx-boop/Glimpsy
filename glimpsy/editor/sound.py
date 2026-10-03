"""Обработка звука ролика: «чистый голос» (шумоподавление) и выравнивание громкости.

Настройки — в project.sound:
  denoise  0–100   сколько шума убирать (0 — выключено). Голос не «срезается»: убранный шум
                   смешивается с исходным звуком в нужной доле, поэтому даже на 100 голос живой;
  level    bool    выровнять громкость ролика;
  lufs     число   итоговая громкость (−14 — как на YouTube, −11 — громко, как в Reels/TikTok);
  even     bool    подтягивать тихие места (если в одном месте говорили тише, в другом громче).

Шум убирает нейросеть RNNoise (встроена в FFmpeg, её «веса» — файл ≈0,3 МБ — скачиваются один
раз). Пока файла нет — обычный фильтр FFmpeg (afftdn), похуже, но работает без интернета.
Громкость выравнивается в два прохода: сначала измеряем, потом точно подгоняем (loudnorm).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Callable

from glimpsy.paths import subprocess_flags

log = logging.getLogger(__name__)

DEFAULTS = {"denoise": 0, "level": False, "lufs": -14.0, "even": False}
MODEL_FILE = "sh.rnnn"
MODEL_URL = ("https://raw.githubusercontent.com/GregorR/rnnoise-models/master/"
             "somnolent-hogwash-2018-09-01/sh.rnnn")
LUFS_PRESETS = [(-18.0, "Спокойно, тихо"), (-16.0, "Подкаст"), (-14.0, "YouTube"), (-11.0, "Громко — Reels, TikTok")]


def settings(project) -> dict:
    d = dict(DEFAULTS)
    d.update(getattr(project, "sound", None) or {})
    d["denoise"] = max(0, min(100, int(d["denoise"])))
    d["lufs"] = max(-24.0, min(-8.0, float(d["lufs"])))
    return d


def active(project) -> bool:
    s = settings(project)
    return bool(s["denoise"] or s["level"] or s["even"])


def model_path() -> Path:
    from glimpsy.editor import subtitles as subs

    return subs.models_dir() / "rnnoise" / MODEL_FILE


def model_ready() -> bool:
    p = model_path()
    return p.exists() and p.stat().st_size > 10_000


def lufs_label(value: float) -> str:
    near = min(LUFS_PRESETS, key=lambda p: abs(p[0] - value))
    return f"{value:g} LUFS — {near[1]}" if abs(near[0] - value) < 1.01 else f"{value:g} LUFS"


def denoise_chain(strength: int, model: Path | None, floor_db: float = -50.0) -> str:
    """Фильтры шумоподавления. Модель передаётся по имени файла — FFmpeg запускается в её папке
    (так не нужно экранировать путь вроде «C:\\…» внутри фильтра). floor_db — уровень шума
    в паузах (его меряет noise_floor): по нему обычный фильтр понимает, что убирать."""
    if strength <= 0:
        return ""
    mix = strength / 100
    # гул ниже 70 Гц (вентилятор, стол) — убираем всегда, голосу он не нужен
    chain = ["highpass=f=70"]
    if model is not None:
        # кусками ровно по 480 отсчётов: на неполном последнем куске RNNoise выдаёт мусор (NaN)
        chain.append(f"asetnsamples=n=480:p=1,arnndn=m={model.name}:mix={mix:.2f}")
    else:
        # фильтру нужен порог чуть выше настоящего шума: чем сильнее чистка, тем выше
        nf = max(-80.0, min(-20.0, floor_db + 10 + 15 * mix))
        chain.append(f"afftdn=nr={10 + 30 * mix:.1f}:nf={nf:.1f}")
    return ",".join(chain)


def noise_floor(ffmpeg: str, src: Path) -> float:
    """Уровень шума в паузах, дБ: самые тихие 10 % записи (по кусочкам в 50 мс)."""
    import numpy as np

    proc = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1",
                             "-ar", "48000", "-f", "f32le", "-"], stdout=subprocess.PIPE, **subprocess_flags())
    win = 2400
    levels = []
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read(win * 4 * 200)
        if not chunk:
            break
        a = np.frombuffer(chunk[: len(chunk) // (win * 4) * win * 4], np.float32)
        if a.size:
            levels.append(np.sqrt(np.mean(a.reshape(-1, win) ** 2, axis=1)))
    proc.wait()
    if not levels:
        return -50.0
    rms = np.concatenate(levels)
    rms = rms[rms > 1e-6]                          # цифровая тишина (склейки) — не шум
    if not rms.size:
        return -80.0
    return float(20 * np.log10(np.percentile(rms, 10)))


def pre_chain(project, ffmpeg: str = "", src: Path | None = None) -> str:
    """Обработка голоса — всё, что делается до выравнивания общей громкости."""
    s = settings(project)
    parts = []
    if s["denoise"]:
        model = model_path() if model_ready() else None
        floor = noise_floor(ffmpeg, src) if model is None and ffmpeg and src is not None else -50.0
        parts.append(denoise_chain(s["denoise"], model, floor))
    if s["even"]:
        # тихие фразы подтягиваются к громким — плавно, по 0,5 с, без «качания»
        parts.append("dynaudnorm=f=500:g=31:p=0.9:m=8")
    return ",".join(parts)


def measure(ffmpeg: str, src: Path, chain: str, lufs: float, run_cwd: Path | None = None) -> dict | None:
    """Первый проход loudnorm: сколько сейчас громкость (результат — в его отчёте)."""
    graph = (chain + "," if chain else "") + f"loudnorm=I={lufs:g}:TP=-1.5:LRA=11:print_format=json"
    r = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-i", str(src), "-vn", "-af", graph, "-f", "null", "-"],
                       capture_output=True, cwd=run_cwd, **subprocess_flags())
    text = r.stderr.decode("utf-8", "replace")
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", text)
    if r.returncode != 0 or not m:
        log.warning("Не удалось измерить громкость: %s", text[-400:])
        return None
    try:
        data = json.loads(m.group(0))
        float(data["input_i"])
    except (ValueError, KeyError):
        return None
    return data


def level_filter(lufs: float, measured: dict | None) -> str:
    base = f"loudnorm=I={lufs:g}:TP=-1.5:LRA=11"
    if not measured or measured.get("input_i") in ("-inf", "inf"):
        return base
    return (base + f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}:linear=true")


def process(ffmpeg: str, project, src: Path, out: Path, run: Callable[..., None],
            voice: bool = True, level: bool = True) -> bool:
    """Обработать звук готового файла; картинка копируется как есть. False — делать нечего.
    voice — шумоподавление и подтягивание тихих мест, level — общая громкость."""
    s = settings(project)
    chain = pre_chain(project, ffmpeg, src) if voice else ""
    use_level = level and s["level"]
    cwd = model_path().parent if "arnndn" in chain else None
    if not chain and not use_level:
        return False
    graph = chain
    if use_level:
        measured = measure(ffmpeg, src, chain, s["lufs"], cwd)
        graph = (chain + "," if chain else "") + level_filter(s["lufs"], measured) + ",aresample=48000"
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-map", "0:v?", "-map", "0:a",
         "-c:v", "copy", "-af", graph, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)], cwd=cwd)
    return True


def preview(ffmpeg: str, project, src: Path, start: float, out_before: Path, out_after: Path,
            length: float = 8.0) -> None:
    """«Послушать до / после»: кусок записи как есть и с обработкой (громкость — по этому куску)."""
    base = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0.0, start):.3f}", "-t",
            f"{length:.3f}", "-i", str(src), "-vn", "-ac", "2", "-ar", "48000"]
    subprocess.run(base + [str(out_before)], check=True, capture_output=True, **subprocess_flags())
    s = settings(project)
    chain = pre_chain(project, ffmpeg, out_before)
    cwd = model_path().parent if "arnndn" in chain else None
    graph = chain
    if s["level"]:
        graph = (chain + "," if chain else "") + f"loudnorm=I={s['lufs']:g}:TP=-1.5:LRA=11,aresample=48000"
    subprocess.run(base + (["-af", graph] if graph else []) + [str(out_after)], check=True, capture_output=True,
                   cwd=cwd, **subprocess_flags())
