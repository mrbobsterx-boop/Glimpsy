"""Описать фрагменты, в том числе старых записей: кадр-картинка, речь и короткое описание от Claude.

Для каждого фрагмента проекта:
  1. кадр из середины — картинка frames/<имя>.jpg в папке проекта (её видно в fragments.md: можно
     отдать папку нейросети вместе со сценарием, она поймёт, что на каком видео);
  2. речь — распознаётся прямо на компьютере (whisper.cpp, как для монтажа по тексту), если у фрагмента
     есть звук; попадает в описание («Говорю: …»);
  3. по желанию — короткое описание кадра от Claude (нужен свой ключ API, кадр отправляется в
     Anthropic). Оно кладётся в Clip.ai_note и становится описанием, пока вы не написали своё.
Своё описание (Clip.note) никогда не перезаписывается.
"""

from __future__ import annotations

import base64
import logging
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from glimpsy.editor.project import Clip, Project
from glimpsy.paths import subprocess_flags

log = logging.getLogger(__name__)

FRAMES_DIR = "frames"
FRAME_WIDTH = 960
AI_MODEL = "claude-opus-5-5"
AI_SYSTEM = (
    "Ты помогаешь монтажёру разобрать записи экрана. Тебе дают кадр из середины фрагмента и, если есть, "
    "заголовок окна и сказанные слова. Напиши по-русски одну короткую фразу (до 15 слов): что на кадре — "
    "какая программа или сайт и что в нём делают. Без вступлений, кавычек и точки в конце. "
    "Пароли, почту и другие личные данные с кадра не переписывай."
)
SPEECH_ORDER = ("turbo", "small", "base")      # какой моделью распознавать, если скачано несколько
SPEECH_DEFAULT = "base"                          # если не скачано ни одной — эту (148 МБ)


class DescribeError(RuntimeError):
    pass


@dataclass
class Options:
    frames: bool = True
    speech: bool = True
    ai_key: str = ""              # пусто — без Claude
    language: str = "auto"


# ---------------- кадр ----------------

def frame_rel(clip: Clip) -> str:
    """Где лежит кадр фрагмента (относительно папки проекта)."""
    stem = Path(clip.src).stem
    return f"{FRAMES_DIR}/{stem}_{int(round(clip.in_s * 10))}.jpg"


def frame_time(clip: Clip) -> float:
    """Момент для кадра: середина фрагмента (в секундах исходного файла)."""
    return clip.in_s + (clip.out_s - clip.in_s) / 2 if clip.kind == "video" else 0.0


def grab_frame(ffmpeg: str, src: Path, t: float, out: Path, width: int = FRAME_WIDTH) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.jpg")
    r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0.0, t):.3f}",
                        "-i", str(src), "-frames:v", "1", "-vf", f"scale='min({width},iw)':-2", "-q:v", "4",
                        str(tmp)], capture_output=True, **subprocess_flags())
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise DescribeError("Не удалось взять кадр: " + r.stderr.decode("utf-8", "replace")[-300:])
    tmp.replace(out)


# ---------------- речь ----------------

def speech_model() -> str | None:
    """Какая модель распознавания уже скачана (лучшая из них) или None."""
    from glimpsy.editor import subtitles as subs

    return next((k for k in SPEECH_ORDER if subs.model_ready(k)), None)


def speech_available() -> bool:
    from glimpsy.editor import subtitles as subs

    return bool(subs.whisper_exe())


# ---------------- Claude ----------------

def ai_describe(key: str, image: Path, title: str = "", speech: str = "") -> str:
    """Короткое описание кадра от Claude. Пустая строка — Claude отказался описывать."""
    import anthropic

    client = anthropic.Anthropic(api_key=key, timeout=90.0, max_retries=2)
    context = []
    if title:
        context.append(f"Заголовок окна: {title}")
    if speech:
        context.append(f"Сказано во фрагменте: {speech}")
    text = "\n".join(context) or "Других сведений нет."
    data = base64.standard_b64encode(image.read_bytes()).decode("ascii")
    response = client.beta.messages.create(
        model=AI_MODEL,
        max_tokens=2000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low"},
        system=AI_SYSTEM,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
            {"type": "text", "text": text},
        ]}],
    )
    if response.stop_reason == "refusal":
        return ""
    out = " ".join(b.text for b in response.content if b.type == "text").strip()
    return out.strip("«»\"' ").rstrip(".")[:200]


def ai_error_text(e: Exception) -> str:
    """Понятная причина, почему Claude не ответил."""
    import anthropic

    if isinstance(e, anthropic.AuthenticationError):
        return "Ключ API не подходит — проверьте его."
    if isinstance(e, anthropic.PermissionDeniedError):
        return "У ключа API нет доступа к этой модели."
    if isinstance(e, anthropic.RateLimitError):
        return "Слишком много запросов к Claude — попробуйте позже."
    if isinstance(e, anthropic.APIConnectionError):
        return "Нет связи с Claude — проверьте интернет."
    if isinstance(e, anthropic.APIStatusError):
        if e.status_code == 402 or "credit" in str(e).lower():
            return "На счёте API закончились деньги."
        return f"Claude ответил ошибкой {e.status_code}."
    return str(e)


# ---------------- всё вместе ----------------

def describe_project(ffmpeg: str, project_dir: Path, opts: Options,
                     progress: Callable[[float, str], None] | None = None,
                     cancel: threading.Event | None = None) -> tuple[int, list[str]]:
    """Описать фрагменты одного проекта. Возвращает (сколько фрагментов обработано, предупреждения)."""
    from glimpsy.editor import fragments as fr
    from glimpsy.editor import transcript as tr

    progress = progress or (lambda f, t: None)
    cancel = cancel or threading.Event()
    project = Project.load(project_dir)
    clips = [c for c in project.clips if c.duration > 0.05]
    warnings: list[str] = []
    if not clips:
        return 0, warnings
    store = tr.TranscriptStore(project.dir)
    model = speech_model() if opts.speech and speech_available() else None
    if opts.speech and model is None:
        warnings.append("Речь не распознана: нет модели распознавания.")
    ai_ok = bool(opts.ai_key)
    done = 0
    for i, c in enumerate(clips):
        if cancel.is_set():
            break
        step = f"{i + 1} из {len(clips)}"
        src = project.path_of(c)
        frame = project.dir / frame_rel(c)
        if (opts.frames or ai_ok) and not frame.exists() and src.exists():
            progress(i / len(clips), f"Кадр {step}")
            try:
                grab_frame(ffmpeg, src, frame_time(c), frame)
            except DescribeError as e:
                log.warning("%s: %s", src.name, e)
        if model and c.kind == "video" and c.has_audio and not store.has(c.src) and src.exists():
            progress(i / len(clips), f"Речь {step}")
            try:
                tr.transcribe_source(ffmpeg, src, c.src, c.src_duration, model, opts.language, store,
                                     lambda f, _t, i=i: progress((i + f * 0.9) / len(clips), f"Речь {step}"),
                                     cancel)
            except tr.TranscribeError as e:
                warnings.append(f"{src.name}: {e}")
            except Exception as e:                      # отмена или сбой одного файла — идём дальше
                if cancel.is_set():
                    break
                log.exception("Речь %s не распознана", src.name)
                warnings.append(f"{src.name}: {e}")
        if ai_ok and not c.note.strip() and not c.ai_note and frame.exists():
            progress(i / len(clips), f"Описание {step}")
            try:
                c.ai_note = ai_describe(opts.ai_key, frame, fr.clean_title(c.title, c.app),
                                        fr.speech_text(project, c, store))
            except Exception as e:
                msg = ai_error_text(e)
                log.warning("Claude не описал кадр: %s", msg)
                warnings.append("Claude: " + msg)
                ai_ok = False                           # ключ не работает — не мучаем каждый фрагмент
        done += 1
    project.save()                                      # и fragments.md с кадрами и речью
    progress(1.0, "Готово")
    return done, warnings
