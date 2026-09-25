"""Проект редактора: список фрагментов и всё, что с ними сделали.

Каждая записанная сессия — отдельный проект в папке данных программы
(projects/project_ГГГГММДД_ЧЧММСС). Там лежат:
  * piece_XXXX.mp4 — фрагменты, из которых собран исходный ролик;
  * project.json   — как их собрал автомат (этап 1, не меняется);
  * edit.json      — ваши правки в редакторе (сохраняются автоматически);
  * media/         — копии видео и фото, которые вы вставили.
"""

from __future__ import annotations

import copy
import json
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

ASPECTS = {"16:9": (1920, 1080), "9:16": (1080, 1920)}
MIN_CLIP_S = 0.2          # короче фрагмент не сделать
IMAGE_DEFAULT_S = 3.0     # сколько по умолчанию показывается вставленное фото
IMAGE_MAX_S = 3600.0
MAX_SPEED = 10.0
MIN_SPEED = 0.25
MIN_ZOOM, MAX_ZOOM = 0.2, 5.0
DEFAULT_FRAME = (1.0, 0.0, 0.0)   # масштаб, сдвиг по X и по Y (в долях ширины/высоты кадра ролика)


def new_id() -> str:
    return uuid.uuid4().hex[:10]


@dataclass
class Clip:
    id: str
    kind: str                 # "video" или "image"
    src: str                  # путь к файлу (относительный — внутри папки проекта)
    src_duration: float       # длина исходного файла, с (для фото — IMAGE_MAX_S)
    in_s: float = 0.0         # откуда начинаем внутри файла
    out_s: float = 0.0        # где заканчиваем
    speed: float = 1.0
    muted: bool = False
    has_audio: bool = False
    width: int = 0
    height: int = 0
    label: str = ""
    priority: bool = False
    recorded_at: float | None = None
    cursor: list = field(default_factory=list)
    # Кадрирование — отдельно для каждого формата: {"9:16": [масштаб, x, y]}
    frames: dict = field(default_factory=dict)
    motion: str = "none"          # none / autozoom / follow — движение кадра по курсору
    zoom_strength: float = 1.8    # во сколько раз приближать при автозуме
    clicks: list = field(default_factory=list)   # клики мыши: [t от начала файла, x, y]
    click_fx: bool = True         # подсвечивать клики расходящимся кругом

    def clicks_shown(self) -> list:
        return self.clicks if self.click_fx and self.kind == "video" else []

    def motion_for(self, aspect: str) -> str:
        """Режим движения с учётом формата: «следовать» имеет смысл только для 9:16."""
        if not self.cursor or self.kind != "video":
            return "none"
        if self.motion == "follow" and aspect != "9:16":
            return "none"
        return self.motion

    @property
    def duration(self) -> float:
        """Сколько фрагмент длится в готовом ролике (с учётом скорости)."""
        return max(0.0, (self.out_s - self.in_s) / self.speed)

    def frame_for(self, aspect: str) -> tuple[float, float, float]:
        f = self.frames.get(aspect)
        return (float(f[0]), float(f[1]), float(f[2])) if f else DEFAULT_FRAME

    def set_frame(self, aspect: str, zoom: float, x: float, y: float) -> None:
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        x, y = max(-1.5, min(1.5, x)), max(-1.5, min(1.5, y))
        if (zoom, x, y) == DEFAULT_FRAME:
            self.frames.pop(aspect, None)
        else:
            self.frames[aspect] = [round(zoom, 4), round(x, 4), round(y, 4)]


def frame_rect(src_w: float, src_h: float, W: float, H: float,
               frame: tuple[float, float, float]) -> tuple[float, float, float, float]:
    """Где окажется кадр внутри ролика W×H: (x, y, ширина, высота).

    Масштаб 1 — кадр целиком вписан по центру. Сдвиг x/y — в долях ширины/высоты ролика.
    Одна и та же формула используется и в окне просмотра, и при экспорте.
    """
    zoom, fx, fy = frame
    if src_w <= 0 or src_h <= 0:
        src_w, src_h = W, H
    s = min(W / src_w, H / src_h) * zoom
    w, h = src_w * s, src_h * s
    return (W - w) / 2 + fx * W, (H - h) / 2 + fy * H, w, h


def cover_zoom(src_w: float, src_h: float, W: float, H: float) -> float:
    """Масштаб, при котором кадр заполняет весь экран без полей."""
    if src_w <= 0 or src_h <= 0:
        return 1.0
    return max(W / src_w, H / src_h) / min(W / src_w, H / src_h)


@dataclass
class Project:
    dir: Path
    name: str
    clips: list[Clip] = field(default_factory=list)
    aspect: str = "16:9"
    fps: int = 30
    source_video: str = ""    # исходный ролик из этапа 1 (рядом с ним сохраняется правка)
    created: float = 0.0
    version: int = 1
    texts: list = field(default_factory=list)       # TextItem — тексты поверх ролика
    text_style: dict = field(default_factory=dict)  # общий стиль текстов (отличия от стандартного)
    overlays: list = field(default_factory=list)    # OverlayItem — картинки/видео поверх ролика
    music: object = None                             # MusicTrack — фоновая музыка или None

    # ---------- время ----------

    @property
    def total(self) -> float:
        return sum(c.duration for c in self.clips)

    def start_of(self, index: int) -> float:
        return sum(c.duration for c in self.clips[:index])

    def locate(self, t: float) -> tuple[int | None, float]:
        """Какой фрагмент играет в момент t и сколько секунд от его начала."""
        acc = 0.0
        for i, c in enumerate(self.clips):
            if t < acc + c.duration or i == len(self.clips) - 1:
                return i, min(max(0.0, t - acc), c.duration)
            acc += c.duration
        return None, 0.0

    def path_of(self, clip: Clip) -> Path:
        p = Path(clip.src)
        return p if p.is_absolute() else self.dir / p

    def index_of(self, clip_id: str) -> int:
        return next((i for i, c in enumerate(self.clips) if c.id == clip_id), -1)

    def text_by_id(self, text_id: str):
        return next((t for t in self.texts if t.id == text_id), None)

    def overlay_by_id(self, oid: str):
        return next((o for o in self.overlays if o.id == oid), None)

    def texts_at(self, t: float) -> list:
        return [x for x in self.texts if x.start <= t < x.end]

    # ---------- правки ----------

    def delete(self, index: int) -> None:
        del self.clips[index]

    def move(self, src: int, dst: int) -> None:
        """Переставить фрагмент src так, чтобы он оказался на позиции dst."""
        clip = self.clips.pop(src)
        if dst > src:
            dst -= 1
        self.clips.insert(max(0, min(dst, len(self.clips))), clip)

    def split(self, t: float) -> int | None:
        """Разрезать фрагмент под курсором. Возвращает индекс правой половины."""
        idx, local = self.locate(t)
        if idx is None:
            return None
        c = self.clips[idx]
        if local < MIN_CLIP_S or c.duration - local < MIN_CLIP_S:
            return None
        cut = c.in_s + local * c.speed
        right = copy.deepcopy(c)
        right.id = new_id()
        right.in_s = cut
        c.out_s = cut
        self.clips.insert(idx + 1, right)
        return idx + 1

    def trim(self, index: int, in_s: float, out_s: float) -> None:
        c = self.clips[index]
        min_src = MIN_CLIP_S * c.speed
        in_s = max(0.0, min(in_s, c.src_duration - min_src))
        out_s = max(in_s + min_src, min(out_s, c.src_duration))
        c.in_s, c.out_s = in_s, out_s

    def set_speed(self, index: int, speed: float) -> None:
        self.clips[index].speed = max(MIN_SPEED, min(MAX_SPEED, float(speed)))

    def insert(self, index: int, clips: list[Clip]) -> None:
        for k, c in enumerate(clips):
            self.clips.insert(index + k, c)

    # ---------- файлы ----------

    def copy_media(self, path: Path) -> str:
        """Копирует файл в папку проекта (media/) и возвращает относительный путь."""
        media = self.dir / "media"
        media.mkdir(parents=True, exist_ok=True)
        dest = media / path.name
        n = 2
        while dest.exists() and not _same_file(dest, path):
            dest = media / f"{path.stem}_{n}{path.suffix}"
            n += 1
        if not dest.exists():
            shutil.copyfile(path, dest)
        return dest.relative_to(self.dir).as_posix()

    def import_file(self, path: Path, info) -> Clip:
        """Копирует вставленный файл в папку проекта (чтобы проект не сломался,
        если оригинал удалят) и создаёт для него фрагмент."""
        media = self.dir / "media"
        media.mkdir(parents=True, exist_ok=True)
        dest = media / path.name
        n = 2
        while dest.exists() and not _same_file(dest, path):
            dest = media / f"{path.stem}_{n}{path.suffix}"
            n += 1
        if not dest.exists():
            shutil.copyfile(path, dest)   # только содержимое: системные флаги файла (macOS) не копируем
        rel = dest.relative_to(self.dir).as_posix()
        if info.is_image:
            return Clip(new_id(), "image", rel, IMAGE_MAX_S, 0.0, IMAGE_DEFAULT_S,
                        width=info.width, height=info.height, label=path.name)
        return Clip(new_id(), "video", rel, info.duration, 0.0, info.duration,
                    has_audio=info.has_audio, width=info.width, height=info.height, label=path.name)

    # ---------- сохранение ----------

    def to_dict(self) -> dict:
        return {
            "version": self.version, "name": self.name, "aspect": self.aspect, "fps": self.fps,
            "source_video": self.source_video, "created": self.created,
            "clips": [asdict(c) for c in self.clips],
            "texts": [asdict(t) for t in self.texts],
            "text_style": copy.deepcopy(self.text_style),
            "overlays": [asdict(o) for o in self.overlays],
            "music": asdict(self.music) if self.music is not None else None,
        }

    def restore(self, data: dict) -> None:
        """Вернуть состояние из словаря (для отмены/повтора)."""
        self.aspect = data.get("aspect", "16:9")
        known = {f.name for f in fields(Clip)}
        self.clips = [Clip(**{k: v for k, v in c.items() if k in known}) for c in data.get("clips", [])]
        from worklapse.editor.text import TextItem

        tknown = {f.name for f in fields(TextItem)}
        self.texts = [TextItem(**{k: v for k, v in t.items() if k in tknown}) for t in data.get("texts", [])]
        self.text_style = copy.deepcopy(data.get("text_style", {}))
        from worklapse.editor.overlay import OverlayItem

        oknown = {f.name for f in fields(OverlayItem)}
        self.overlays = [OverlayItem(**{k: v for k, v in o.items() if k in oknown})
                         for o in data.get("overlays", [])]
        from worklapse.editor.music import MusicTrack

        m = data.get("music")
        mknown = {f.name for f in fields(MusicTrack)}
        self.music = MusicTrack(**{k: v for k, v in m.items() if k in mknown}) if m else None

    def save(self) -> None:
        tmp = self.dir / "edit.json.tmp"
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.dir / "edit.json")

    @property
    def edited(self) -> bool:
        return (self.dir / "edit.json").exists()

    @classmethod
    def load(cls, directory: Path) -> "Project":
        directory = Path(directory)
        edit = directory / "edit.json"
        if edit.exists():
            data = json.loads(edit.read_text(encoding="utf-8"))
            p = cls(directory, data.get("name", directory.name), fps=int(data.get("fps", 30)),
                    source_video=data.get("source_video", ""), created=float(data.get("created", 0)))
            p.restore(data)
            return p
        return cls._from_recording(directory)

    @classmethod
    def _from_recording(cls, directory: Path) -> "Project":
        """Первое открытие: берём фрагменты, которые автоматически собрал этап 1."""
        meta = json.loads((directory / "project.json").read_text(encoding="utf-8"))
        clips = []
        for c in meta.get("clips", []):
            when = c.get("recorded_at")
            label = time.strftime("%H:%M:%S", time.localtime(when)) if when else c["file"]
            if c.get("monitor"):
                label += f" · монитор {c['monitor']}"
            w, h = (c.get("source_size") or [0, 0])[:2]
            dur = float(c.get("duration", 0))
            # Фрагменты уже ускорены при автосборке, поэтому здесь их скорость — ×1
            clips.append(Clip(new_id(), "video", c["file"], dur, 0.0, dur, width=w, height=h,
                              label=label, priority=bool(c.get("priority")), recorded_at=when,
                              cursor=c.get("cursor", []), clicks=c.get("clicks", []),
                              motion=c.get("motion", "none"), click_fx=bool(c.get("click_fx", True)),
                              zoom_strength=float(c.get("zoom_strength", 1.8))))
        output = meta.get("output", "")
        try:   # время записи — из имени папки project_ГГГГММДД_ЧЧММСС
            created = time.mktime(time.strptime(directory.name.split("_", 1)[1], "%Y%m%d_%H%M%S"))
        except (IndexError, ValueError):
            created = directory.stat().st_mtime
        name = Path(output).stem if output else directory.name
        project = cls(directory, name, clips, fps=int(meta.get("fps", 30)), source_video=output, created=created)
        # окошки с веб-камеры, которые автосборка поставила в ролик, — как обычные наложения
        from worklapse.editor.overlay import camera_item
        for c in meta.get("camera", []):
            when = c.get("recorded_at")
            label = "Камера " + time.strftime("%H:%M:%S", time.localtime(when)) if when else "Веб-камера"
            item = camera_item(new_id(), c["file"], float(c["start"]), float(c["duration"]),
                               int(c.get("width", 0)), int(c.get("height", 0)), label)
            item.src_duration = float(c.get("src_duration", c["duration"]))
            project.overlays.append(item)
        return project


class History:
    """Отмена и повтор. Храним снимки проекта до каждой правки.

    Если одно и то же поле меняется много раз подряд (например, крутят скорость),
    это считается одной правкой — чтобы Ctrl+Z не приходилось жать 20 раз.
    """

    LIMIT = 200
    MERGE_WINDOW_S = 1.5

    def __init__(self) -> None:
        self._undo: list[dict] = []
        self._redo: list[dict] = []
        self._last_key: str | None = None
        self._last_time = 0.0

    def push(self, state: dict, key: str | None = None) -> None:
        now = time.monotonic()
        if key and key == self._last_key and now - self._last_time < self.MERGE_WINDOW_S:
            self._last_time = now
            return
        self._undo.append(copy.deepcopy(state))
        del self._undo[:-self.LIMIT]
        self._redo.clear()
        self._last_key, self._last_time = key, now

    def undo(self, current: dict) -> dict | None:
        if not self._undo:
            return None
        self._redo.append(copy.deepcopy(current))
        self._last_key = None
        return self._undo.pop()

    def redo(self, current: dict) -> dict | None:
        if not self._redo:
            return None
        self._undo.append(copy.deepcopy(current))
        self._last_key = None
        return self._redo.pop()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)


def list_projects(root: Path) -> list[Path]:
    """Все проекты, новые сверху."""
    if not root.exists():
        return []
    dirs = [d for d in root.iterdir() if d.is_dir() and ((d / "project.json").exists() or (d / "edit.json").exists())]
    return sorted(dirs, key=lambda d: d.name, reverse=True)


def _same_file(a: Path, b: Path) -> bool:
    """Файл с тем же именем и размером уже скопирован — второй раз не копируем."""
    try:
        return a.stat().st_size == b.stat().st_size
    except OSError:
        return False
