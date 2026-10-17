"""Озвучка для VideoStudio: голос записывается в Glimpsy, чистится и уходит пакетом в проект VideoStudio.

Проект «Озвучка» — тот же «монтаж по тексту», только его исходники — записанные дубли голоса:
каждый дубль (WAV из окна «Запись») превращается в маленькое видео с волной звука (чтобы его было
видно на ленте и в просмотре) и становится отдельным исходником. Поэтому всё, что умеет монтаж по
тексту, работает и для голоса: расшифровка по словам, неудачные дубли, паузы, вырезы.

Пакет озвучки (формат общий с VideoStudio — не менять без него), папка <проект VideoStudio>/voice/:
  voice.wav   — итоговый голос: после вырезов, с «Чистым голосом», громкость ≈ −16 LUFS, 48 кГц, моно, 16 бит;
  words.json  — слова с временем по voice.wav (секунды от начала), фразы и сведения о пакете;
  README.txt  — откуда пакет и как пересоздать.
Повторный экспорт кладёт прошлый пакет в voice/_old/<дата>/.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
import wave
from pathlib import Path
from typing import Callable

from glimpsy.editor import transcript as tr
from glimpsy.editor.project import Project

log = logging.getLogger(__name__)

FORMAT = "glimpsy-voice"
VERSION = 1
RATE = 48000
LUFS = -16.0                    # громкость пакета (YouTube-подкаст)
EDGE_FADE_S = 0.015             # стыки вырезов — без щелчков
WAVE_SIZE = (640, 360)          # картинка дубля: волна звука
WAVE_FPS = 10
PACKAGE_FILES = ("voice.wav", "words.json", "README.txt")


class VoiceError(RuntimeError):
    pass


def is_voice(project) -> bool:
    return bool(getattr(project, "cuts", {}).get("voice"))


def new_project(root: Path) -> Project:
    """Пустой проект «Озвучка»: без записи экрана, голос пишется кнопкой «Запись»."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    d = Path(root) / f"project_{stamp}_voice"
    n = 2
    while d.exists():
        d = Path(root) / f"project_{stamp}_voice{n}"
        n += 1
    d.mkdir(parents=True)
    p = Project(d, "voice_" + time.strftime("%Y-%m-%d_%H-%M"), created=time.time(), fps=30)
    p.aspect = "16:9"
    p.cuts = {"sources": [], "voice": True}
    p.save()
    return p


# ---------------- дубли ----------------

def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate() or RATE)


def take_source(ffmpeg: str, wav: Path, out: Path, label: str,
                run: Callable[[list[str]], None] | None = None) -> dict:
    """Дубль голоса → видео с волной (для ленты и просмотра). Звук для пакета берётся из самого wav."""
    from glimpsy.editor.export import _run

    run = run or (lambda cmd: _run(cmd, _never()))
    w, h = WAVE_SIZE
    out.parent.mkdir(parents=True, exist_ok=True)
    duration = wav_duration(wav)
    # картинка — волна всего дубля (живая волна showwaves на некоторых записях зависает)
    pic = out.with_suffix(".png")
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav), "-filter_complex",
         f"[0:a]aformat=channel_layouts=mono,showwavespic=s={w}x{h // 2}:colors=0x22AEBB,"
         f"pad={w}:{h}:0:{h // 4}:color=0x14171D", "-frames:v", "1", str(pic)])
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-framerate", str(WAVE_FPS),
         "-t", f"{duration:.3f}", "-i", str(pic), "-i", str(wav), "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-ar", str(RATE), "-t", f"{duration:.3f}",
         "-movflags", "+faststart", str(out)])
    pic.unlink(missing_ok=True)
    return {"src": str(out.resolve()), "duration": round(duration, 3), "has_audio": True,
            "width": w, "height": h, "label": label, "wav": str(wav.resolve())}


def insert_take(project, source: dict, at: float | None = None) -> None:
    """Новый дубль — после того дубля, на котором стоит курсор (или в конец)."""
    sources = project.cuts.setdefault("sources", [])
    pos = len(sources)
    if at is not None and project.clips and at < project.total - 1e-3:
        hit = tr.source_time(project, at)
        if hit is not None:
            pos = next((i + 1 for i, s in enumerate(sources) if s["src"] == hit[0]), pos)
    sources.insert(pos, source)


def _never():
    import threading

    return threading.Event()


# ---------------- пакет для VideoStudio ----------------

def output_words(project, store: tr.TranscriptStore) -> list[tuple[float, float, str, float]]:
    """Оставленные слова во времени итоговой дорожки: (начало, конец, текст, уверенность)."""
    cuts = tr.cuts_of(project)
    deleted_all = cuts.get("deleted", {})
    fixed_all = cuts.get("word_text", {})
    out = []
    t0 = 0.0
    for c in project.clips:
        sw = store.get(c.src)
        if sw is not None and not c.muted:
            gone = set(deleted_all.get(c.src, [])) | set(cuts.get("muted", {}).get(c.src, []))
            fixed = fixed_all.get(c.src, {})
            for i, w in enumerate(sw.words):
                if i in gone or w.end <= c.in_s or w.start >= c.out_s:
                    continue
                text = fixed.get(str(i), w.text).strip()
                if text:
                    a = t0 + (max(w.start, c.in_s) - c.in_s) / c.speed
                    b = t0 + (min(w.end, c.out_s) - c.in_s) / c.speed
                    out.append((a, b, text, float(w.p)))
        t0 += c.duration
    return out


def tidy_words(words: list[tuple[float, float, str, float]], duration: float) -> list[dict]:
    """Время по порядку, без наложений, в пределах файла; три знака после запятой."""
    out: list[dict] = []
    last = 0.0
    for a, b, text, p in sorted(words, key=lambda w: w[0]):
        a = max(a, last)
        b = min(max(b, a + 0.01), duration)
        if a >= duration or b <= a:
            continue
        out.append({"w": text, "start": round(a, 3), "end": round(b, 3), "p": round(max(0.0, min(1.0, p)), 3)})
        last = out[-1]["end"]
    return out


def audio_plan(project) -> list[tuple[str, float, float, bool]]:
    """Куски итоговой дорожки: (звук, откуда, докуда, без звука). Соседние куски одного дубля — одним."""
    sources = {s["src"]: s for s in project.cuts.get("sources", [])}
    plan: list[list] = []
    for c in project.clips:
        if c.duration <= 0.001:
            continue
        s = sources.get(c.src, {})
        audio = s.get("wav") if s.get("wav") and Path(s["wav"]).exists() else str(project.path_of(c))
        mute = bool(c.muted) or not c.has_audio
        if plan and plan[-1][0] == audio and abs(plan[-1][2] - c.in_s) < 1e-3 and plan[-1][3] == mute \
                and abs(c.speed - 1.0) < 1e-6:
            plan[-1][2] = c.out_s
        else:
            plan.append([audio, c.in_s, c.out_s, mute])
    return [tuple(p) for p in plan]


def concat_graph(plan: list[tuple[str, float, float, bool]]) -> tuple[list[str], str]:
    """Входы FFmpeg и граф: куски подряд, на стыках — короткое затухание."""
    files: list[str] = []
    for audio, *_ in plan:
        if audio not in files:
            files.append(audio)
    uses = {f: [i for i, p in enumerate(plan) if p[0] == f] for f in files}
    parts, labels = [], {}
    for k, f in enumerate(files):
        n = len(uses[f])
        base = f"[{k}:a]aformat=sample_fmts=fltp:sample_rates={RATE}:channel_layouts=mono"
        if n == 1:
            parts.append(f"{base}[in{k}_0]")
        else:
            parts.append(f"{base},asplit={n}" + "".join(f"[in{k}_{j}]" for j in range(n)))
        for j, i in enumerate(uses[f]):
            labels[i] = f"in{k}_{j}"
    seg = []
    for i, (_audio, a, b, mute) in enumerate(plan):
        length = b - a
        fade = min(EDGE_FADE_S, length / 4)
        chain = f"[{labels[i]}]atrim=start={a:.4f}:end={b:.4f},asetpts=PTS-STARTPTS"
        if mute:
            chain += ",volume=0"
        chain += f",afade=t=in:d={fade:.4f},afade=t=out:st={max(0.0, length - fade):.4f}:d={fade:.4f}[s{i}]"
        parts.append(chain)
        seg.append(f"[s{i}]")
    parts.append("".join(seg) + f"concat=n={len(seg)}:v=0:a=1[cat]")
    inputs = []
    for f in files:
        inputs += ["-i", f]
    return inputs, ";".join(parts)


def render_voice(ffmpeg: str, project, out: Path, work: Path, run: Callable[..., None]) -> float:
    """Итоговый голос → out (WAV 48 кГц, моно, 16 бит, ≈ −16 LUFS). Возвращает длину, с."""
    from glimpsy.editor import sound

    plan = audio_plan(project)
    if not plan:
        raise VoiceError("В проекте нет записанного голоса.")
    work.mkdir(parents=True, exist_ok=True)
    cat = work / "voice_cat.wav"
    inputs, graph = concat_graph(plan)
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", graph,
         "-map", "[cat]", "-c:a", "pcm_s16le", "-ar", str(RATE), "-ac", "1", str(cat)])
    chain = sound.pre_chain(project, ffmpeg, cat)
    cwd = sound.model_path().parent if "arnndn" in chain else None
    measured = sound.measure(ffmpeg, cat, chain, LUFS, cwd)
    graph = (chain + "," if chain else "") + sound.level_filter(LUFS, measured) + f",aresample={RATE}"
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(cat), "-af", graph,
         "-c:a", "pcm_s16le", "-ar", str(RATE), "-ac", "1", str(out)], cwd=cwd)
    cat.unlink(missing_ok=True)
    return wav_duration(out)


def _move_old(folder: Path) -> None:
    present = [folder / n for n in PACKAGE_FILES if (folder / n).exists()]
    if not present:
        return
    old = folder / "_old" / time.strftime("%Y-%m-%d_%H-%M-%S")
    k = 2
    while old.exists():
        old = old.with_name(old.name.split("~")[0] + f"~{k}")
        k += 1
    old.mkdir(parents=True)
    for f in present:
        shutil.move(str(f), old / f.name)


def export_package(ffmpeg: str, project, studio_dir: Path,
                   progress: Callable[[float, str], None] | None = None,
                   run: Callable[..., None] | None = None) -> Path:
    """Записать пакет озвучки в <studio_dir>/voice/. Возвращает эту папку."""
    from glimpsy.editor.export import _run

    progress = progress or (lambda f, t: None)
    cancel = _never()
    run = run or (lambda cmd, cwd=None: _run(cmd, cancel, cwd))
    store = tr.TranscriptStore(project.dir)
    srcs = [s["src"] for s in project.cuts.get("sources", [])]
    if not srcs:
        raise VoiceError("В проекте нет записанного голоса.")
    missing = [s for s in srcs if store.get(s) is None]
    if missing:
        raise VoiceError("Сначала распознайте речь всех дублей (панель «Текст» → «Распознать»).")
    folder = Path(studio_dir) / "voice"
    folder.mkdir(parents=True, exist_ok=True)
    work = Path(project.dir) / "export_tmp" / "voice"
    progress(0.05, "Собираю голос")
    tmp = folder / "voice.part.wav"
    try:
        duration = render_voice(ffmpeg, project, tmp, work, run)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    progress(0.85, "Время слов")
    words = tidy_words(output_words(project, store), duration)
    segments = [{"start": round(a, 3), "end": round(min(b, duration), 3), "text": text}
                for a, b, text in tr.cues([(w["start"], w["end"], w["w"]) for w in words])]
    language = next((store.get(s).language for s in srcs if store.get(s) and store.get(s).language), "") or "ru"
    data = {"format": FORMAT, "version": VERSION, "audio": "voice.wav", "sample_rate": RATE,
            "duration": round(duration, 3), "language": language, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "glimpsy_project": project.name, "words": words, "segments": segments}
    _move_old(folder)
    tmp.replace(folder / "voice.wav")
    (folder / "words.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (folder / "README.txt").write_text(
        f"Озвучка из Glimpsy: проект «{project.name}», {time.strftime('%Y-%m-%d %H:%M')}.\n"
        "Пересоздать: Glimpsy → «Мои сессии» → этот проект → «Экспорт» → «Озвучка для VideoStudio…» → эта папка.\n",
        encoding="utf-8")
    progress(1.0, "Готово")
    return folder
