"""Фрагменты по отдельности: каждый кусок ролика — своим файлом с понятным именем + список с описаниями.

Зачем: снятое потом раскладывается по сценарию (например, «V02-chat-big-task.mp4» из списка съёмок).
У каждого фрагмента есть название и короткое описание. Их можно написать самому, а если не написать,
программа предложит своё: какая программа и какое окно были на экране, сколько секунд, когда записано
и что вы говорили (если речь уже распознана).

В выбранную папку (её можно сделать папкой Google Диска на компьютере — тогда файлы сами уедут
в облако) кладутся видео и два списка:
  fragments.md   — для человека и для нейросети: таблица «файл — длительность — что на нём»;
  fragments.json — то же для программ.
Списки дополняются: если в папке уже были фрагменты из другого проекта, они остаются в списке.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from glimpsy.editor.project import Clip, Project

MANIFEST_MD = "fragments.md"
MANIFEST_JSON = "fragments.json"
SPEECH_CHARS = 300

_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                      "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))
# хвосты заголовков окон, которые ничего не говорят о содержимом
_TITLE_TAILS = ("google chrome", "chromium", "mozilla firefox", "firefox", "microsoft edge", "brave", "opera",
                "visual studio code", "telegram", "яндекс браузер", "yandex")


@dataclass
class Entry:
    """Строка списка фрагментов."""
    file: str
    name: str
    note: str
    duration: float
    recorded: str = ""          # когда записано: «2026-10-14 14:03»
    app: str = ""
    title: str = ""
    speech: str = ""
    project: str = ""


def clean_title(title: str, app: str = "") -> str:
    """«Claude Code — чат — Google Chrome» → «Claude Code — чат»."""
    t = (title or "").strip()
    for _ in range(2):
        seps = list(re.finditer(r"\s+[—–-]\s+", t))
        if not seps:
            break
        tail = t[seps[-1].end():].strip().lower()
        if tail in _TITLE_TAILS or (app and tail == app.strip().lower()):
            t = t[:seps[-1].start()].strip()
    return t


def slug(text: str, max_len: int = 48) -> str:
    """Имя для файла латиницей: «Чат с Claude: большая задача» → «chat-s-claude-bolshaya-zadacha»."""
    out = []
    for ch in (text or "").lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii() and ch.isalnum():
            out.append(ch)
        else:
            out.append("-")
    s = re.sub(r"-+", "-", "".join(out)).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rsplit("-", 1)[0] or s[:max_len]
    return s


def safe_file_name(name: str) -> str:
    """Своё название фрагмента → имя файла (буквы оставляем как есть, убираем запрещённые знаки)."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", (name or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .-")
    if s.lower().endswith(".mp4"):
        s = s[:-4].rstrip(" .-")
    return s[:80]


def speech_text(project: Project, clip: Clip, store=None) -> str:
    """Что говорилось во фрагменте (если речь этого видео уже распознана)."""
    if clip.kind != "video":
        return ""
    if store is None:
        from glimpsy.editor.transcript import TranscriptStore

        store = TranscriptStore(project.dir)
    sw = store.get(clip.src)
    if sw is None:
        return ""
    words = [w.text.strip() for w in sw.words if w.end > clip.in_s and w.start < clip.out_s and w.text.strip()]
    text = " ".join(words)
    return text if len(text) <= SPEECH_CHARS else text[:SPEECH_CHARS].rsplit(" ", 1)[0] + "…"


def suggest_name(clip: Clip) -> str:
    """Название по умолчанию: заголовок окна или программа."""
    t = clean_title(clip.title, clip.app)
    if t:
        return " ".join(t.split()[:6])
    if clip.app:
        return clip.app
    return "Фото" if clip.kind == "image" else "Фрагмент"


def suggest_note(clip: Clip, speech: str = "") -> str:
    """Описание по умолчанию — из того, что известно о записи."""
    parts = []
    t = clean_title(clip.title, clip.app)
    if clip.app and t and t.lower() != clip.app.lower():
        parts.append(f"{clip.app}: «{t}»")
    elif clip.app or t:
        parts.append(clip.app or f"«{t}»")
    elif clip.kind == "image":
        parts.append("Фото")
    else:
        parts.append("Запись экрана")
    meta = [f"{clip.duration:.0f} с"]
    if clip.clicks:
        meta.append(f"кликов: {len(clip.clicks)}")
    if clip.priority:
        meta.append("важный момент")
    parts.append(", ".join(meta))
    if speech:
        parts.append(f"Говорю: «{speech}»")
    return ". ".join(parts)


def recorded_text(clip: Clip) -> str:
    if not clip.recorded_at:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(clip.recorded_at + clip.in_s))


def file_names(clips: list[Clip]) -> list[str]:
    """Имена файлов: своё название как есть, иначе «03-claude-code-chat.mp4». Без повторов."""
    out, used = [], set()
    for i, c in enumerate(clips, 1):
        own = safe_file_name(c.name)
        base = own or f"{i:02d}-{slug(suggest_name(c)) or 'fragment'}"
        name, k = f"{base}.mp4", 2
        while name.lower() in used:
            name, k = f"{base}-{k}.mp4", k + 1
        used.add(name.lower())
        out.append(name)
    return out


def entries(project: Project, clips: list[Clip], store=None) -> list[Entry]:
    out = []
    for c, f in zip(clips, file_names(clips)):
        speech = speech_text(project, c, store)
        out.append(Entry(f, c.name.strip() or suggest_name(c), c.note.strip() or suggest_note(c, speech),
                         round(c.duration, 2), recorded_text(c), c.app, clean_title(c.title, c.app), speech,
                         project.name))
    return out


def clip_project(project: Project, clip: Clip) -> Project:
    """Проект из одного фрагмента — для сохранения его отдельным файлом. Кадр, движение, клики и курсор —
    как у фрагмента; из наложений остаётся только размытие (спрятанное должно остаться спрятанным)."""
    snap = Project(project.dir, project.name, fps=project.fps, source_video=project.source_video)
    snap.restore(project.to_dict())
    idx = project.index_of(clip.id)
    start = project.start_of(idx)
    end = start + clip.duration
    snap.clips = [c for c in snap.clips if c.id == clip.id]
    blurs = []
    for o in snap.overlays:
        if o.kind != "blur" or o.end <= start or o.start >= end:
            continue
        a, b = max(o.start, start), min(o.end, end)
        o.start, o.duration = round(a - start, 3), round(b - a, 3)
        blurs.append(o)
    snap.overlays = blurs
    snap.texts = []
    snap.music = None
    snap.cuts = {k: v for k, v in snap.cuts.items() if k not in ("chapters",)}
    return snap


# ---------------- список в папке ----------------

def _cell(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def render_md(items: list[Entry]) -> str:
    lines = [
        "# Фрагменты",
        "",
        "Видео в этой папке и что на каждом — для сценария и монтажа. Список ведёт Glimpsy: при каждом "
        "сохранении фрагментов он дополняется. Название и описание фрагмента меняются в редакторе Glimpsy "
        "(справа, когда фрагмент выбран).",
        "",
        "| Файл | Длительность | Название | Что на нём | Записано |",
        "|---|---|---|---|---|",
    ]
    for e in items:
        lines.append(f"| `{_cell(e.file)}` | {e.duration:.0f} с | {_cell(e.name)} | {_cell(e.note)} | "
                     f"{_cell(e.recorded)} |")
    talk = [e for e in items if e.speech]
    if talk:
        lines += ["", "## Что говорится", ""]
        for e in talk:
            lines += [f"- `{_cell(e.file)}`: «{_cell(e.speech)}»"]
    return "\n".join(lines) + "\n"


def load_manifest(folder: Path) -> list[Entry]:
    f = Path(folder) / MANIFEST_JSON
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    known = set(Entry.__dataclass_fields__)
    out = []
    for d in data.get("fragments", []) if isinstance(data, dict) else []:
        try:
            out.append(Entry(**{k: v for k, v in d.items() if k in known}))
        except TypeError:
            continue
    return out


def write_manifest(folder: Path, new: list[Entry]) -> list[Entry]:
    """Дополнить списки в папке: новые строки заменяют старые с тем же именем файла.
    Строки о файлах, которых в папке больше нет, убираются."""
    folder = Path(folder)
    names = {e.file.lower() for e in new}
    items = [e for e in load_manifest(folder) if e.file.lower() not in names and (folder / e.file).exists()]
    items += new
    items.sort(key=lambda e: e.file.lower())
    (folder / MANIFEST_JSON).write_text(json.dumps({"fragments": [asdict(e) for e in items]}, ensure_ascii=False,
                                                   indent=1), encoding="utf-8")
    (folder / MANIFEST_MD).write_text(render_md(items), encoding="utf-8")
    return items
