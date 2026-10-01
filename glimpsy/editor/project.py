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

# Дорожки над видео — сверху вниз (верхняя дорожка перекрывает нижние).
# Субтитры и текст — надписи; наложение, медиа и камера — картинки и видео поверх ролика;
# голос — записанный в редакторе звук (без картинки).
TRACK_KINDS = ("subtitles", "text", "overlay", "media", "camera", "voice")
TRACK_NAMES = {"subtitles": "Субтитры", "text": "Текст", "overlay": "Наложение", "media": "Медиа",
               "camera": "Камера", "voice": "Голос"}
TEXT_KINDS = ("subtitles", "text")
DEFAULT_TRACKS = ("subtitles", "text", "overlay")      # есть всегда; медиа и камера — когда понадобятся


@dataclass
class Track:
    id: str
    kind: str          # один из TRACK_KINDS
    name: str


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
    motion: str = "none"          # none / autozoom / follow_hard / follow / follow_zoom — движение по курсору
    zoom_strength: float = 1.8    # во сколько раз приближать при автозуме
    clicks: list = field(default_factory=list)   # клики мыши: [t от начала файла, x, y]
    click_fx: bool = True         # подсвечивать клики расходящимся кругом
    # движение для другого формата, если отличается: {"9:16": "follow"} (иначе — как motion)
    motions: dict = field(default_factory=dict)
    score: float = -1.0           # насколько активным был момент при записи (0…1; −1 — неизвестно)
    base_speed: float = 0.0       # скорость до автомонтажа (чтобы повторный запуск не ускорял ещё раз)
    region: list = field(default_factory=list)   # «зум на область»: [x, y, ширина, высота] в долях кадра
    own_cursor: bool = False      # записано без курсора — курсор рисует Glimpsy (вид и размер — у проекта)
    hidden: bool = False          # картинка убрана (чёрный кадр), звук идёт — монтаж по тексту

    def clicks_shown(self) -> list:
        return self.clicks if self.click_fx and self.kind == "video" else []

    def motion_raw(self, aspect: str) -> str:
        """Выбранный режим движения для формата (без проверок)."""
        return self.motions.get(aspect, self.motion)

    def set_motion(self, aspect: str, mode: str) -> None:
        """16:9 — основной режим; 9:16 может отличаться (например, «за курсором»)."""
        if aspect == "9:16":
            self.motions["9:16"] = mode
        else:
            self.motion = mode

    def motion_for(self, aspect: str) -> str:
        """Режим движения с учётом формата: «за курсором» имеет смысл только для 9:16."""
        from glimpsy.editor.motion import needs_cursor

        mode = self.motion_raw(aspect)
        if self.kind != "video" or (needs_cursor(mode) and not self.cursor):
            return "none"
        if mode == "region" and not self.region:
            return "none"
        if mode.startswith("follow") and aspect != "9:16":
            return "none"
        return mode

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
    cursor: dict = field(default_factory=dict)       # свой курсор: {"style", "size", "show"}
    tracks: list = field(default_factory=list)       # Track — дорожки над видео, сверху вниз
    # монтаж по тексту: исходные видео, что вырезано и настройки пауз (см. transcript.py);
    # пусто — обычный проект
    cuts: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.normalize_tracks()

    def cursor_style(self) -> tuple[str, float, bool]:
        """Вид своего курсора: (стиль, размер, показывать ли)."""
        from glimpsy.editor.cursor import DEFAULT_STYLE, STYLES

        st = self.cursor.get("style", DEFAULT_STYLE)
        return (st if st in STYLES else DEFAULT_STYLE, float(self.cursor.get("size", 1.0)),
                bool(self.cursor.get("show", True)))

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

    # ---------- дорожки ----------

    def track_by_id(self, track_id: str) -> Track | None:
        return next((tr for tr in self.tracks if tr.id == track_id), None)

    def track_for(self, kind: str) -> Track:
        """Первая дорожка этого типа; нет — создаётся на своём месте (см. TRACK_KINDS)."""
        tr = next((x for x in self.tracks if x.kind == kind), None)
        return tr if tr is not None else self.add_track(kind)

    def add_track(self, kind: str, after: str | None = None) -> Track:
        """Новая дорожка: после указанной или последней того же типа (или на своё место по порядку)."""
        n = 1
        while self.track_by_id(kind if n == 1 else f"{kind}-{n}") is not None:
            n += 1
        tr = Track(kind if n == 1 else f"{kind}-{n}", kind, TRACK_NAMES[kind] + ("" if n == 1 else f" {n}"))
        ids = [x.id for x in self.tracks]
        if after in ids:
            pos = ids.index(after) + 1
        else:
            same = [i for i, x in enumerate(self.tracks) if x.kind == kind]
            order = TRACK_KINDS.index(kind)
            pos = same[-1] + 1 if same else sum(1 for x in self.tracks if TRACK_KINDS.index(x.kind) < order)
        self.tracks.insert(pos, tr)
        return tr

    def remove_track(self, track_id: str) -> None:
        """Убрать дорожку вместе со всем, что на ней."""
        self.tracks = [x for x in self.tracks if x.id != track_id]
        self.texts = [t for t in self.texts if t.track != track_id]
        self.overlays = [o for o in self.overlays if o.track != track_id]

    def track_items(self, track_id: str) -> list:
        tr = self.track_by_id(track_id)
        if tr is None:
            return []
        pool = self.texts if tr.kind in TEXT_KINDS else self.overlays
        return [x for x in pool if x.track == track_id]

    def overlays_by_depth(self) -> list:
        """Картинки и видео поверх ролика в порядке рисования: сначала нижние дорожки, верхние — поверх."""
        rank = {tr.id: i for i, tr in enumerate(self.tracks)}
        return sorted((o for o in self.overlays if o.kind != "audio"), key=lambda o: -rank.get(o.track, len(rank)))

    def voices(self) -> list:
        """Записанный голос (звук без картинки)."""
        return sorted((o for o in self.overlays if o.kind == "audio"), key=lambda o: o.start)

    def normalize_tracks(self) -> None:
        """Каждая вещь — на подходящей дорожке; обязательные дорожки есть всегда."""
        self.tracks = [x for x in self.tracks if x.kind in TRACK_KINDS]
        for kind in DEFAULT_TRACKS:
            self.track_for(kind)
        for t in self.texts:
            tr = self.track_by_id(t.track)
            if tr is None or tr.kind not in TEXT_KINDS:
                t.track = self.track_for("subtitles" if t.auto else "text").id
        for o in self.overlays:
            tr = self.track_by_id(o.track)
            audio = o.kind == "audio"
            if tr is None or tr.kind in TEXT_KINDS or (tr.kind == "voice") != audio:
                kind = "voice" if audio else (o.track if o.track in ("camera", "media") else "overlay")
                o.track = self.track_for(kind).id

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
            "cursor": dict(self.cursor),
            "tracks": [asdict(t) for t in self.tracks],
            "cuts": copy.deepcopy(self.cuts),
        }

    def restore(self, data: dict) -> None:
        """Вернуть состояние из словаря (для отмены/повтора)."""
        self.aspect = data.get("aspect", "16:9")
        known = {f.name for f in fields(Clip)}
        self.clips = [Clip(**{k: v for k, v in c.items() if k in known}) for c in data.get("clips", [])]
        from glimpsy.editor.text import TextItem

        tknown = {f.name for f in fields(TextItem)}
        self.texts = [TextItem(**{k: v for k, v in t.items() if k in tknown}) for t in data.get("texts", [])]
        self.text_style = copy.deepcopy(data.get("text_style", {}))
        from glimpsy.editor.overlay import OverlayItem

        oknown = {f.name for f in fields(OverlayItem)}
        self.overlays = [OverlayItem(**{k: v for k, v in o.items() if k in oknown})
                         for o in data.get("overlays", [])]
        from glimpsy.editor.music import MusicTrack

        m = data.get("music")
        mknown = {f.name for f in fields(MusicTrack)}
        self.music = MusicTrack(**{k: v for k, v in m.items() if k in mknown}) if m else None
        self.cursor = dict(data.get("cursor") or {})
        self.tracks = [Track(str(t["id"]), str(t["kind"]), str(t.get("name", ""))) for t in data.get("tracks", [])
                       if t.get("kind") in TRACK_KINDS]
        self.cuts = copy.deepcopy(data.get("cuts") or {})
        self.normalize_tracks()

    def save(self) -> None:
        tmp = self.dir / "edit.json.tmp"
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.dir / "edit.json")

    @property
    def text_edit(self) -> bool:
        """Проект «монтаж по тексту» (фрагменты строятся из расшифровки)."""
        return bool(self.cuts.get("sources"))

    @classmethod
    def for_videos(cls, root: Path, files: list[Path], infos: list) -> "Project":
        """Новый проект из своих видео — «монтаж по тексту». Файлы НЕ копируются: проект
        ссылается на них, исходники не меняются."""
        stamp = time.strftime("%Y%m%d_%H%M%S")
        d = Path(root) / f"project_{stamp}_video"
        n = 2
        while d.exists():
            d = Path(root) / f"project_{stamp}_video{n}"
            n += 1
        d.mkdir(parents=True)
        sources = [{"src": str(Path(f).resolve()), "duration": float(i.duration), "has_audio": bool(i.has_audio),
                    "width": int(i.width), "height": int(i.height), "label": Path(f).name}
                   for f, i in zip(files, infos)]
        fps = next((float(getattr(i, "fps", 0) or 0) for i in infos if getattr(i, "fps", 0)), 30.0)
        # готовый ролик — рядом с первым исходником (…_edit.mp4), с частотой кадров исходника
        p = cls(d, Path(files[0]).stem, created=time.time(), fps=max(10, min(120, int(round(fps)))),
                source_video=sources[0]["src"])
        w, h = sources[0]["width"], sources[0]["height"]
        p.aspect = "9:16" if h > w else "16:9"
        p.cuts = {"sources": sources}
        p.clips = [Clip(new_id(), "video", s["src"], s["duration"], 0.0, s["duration"], has_audio=s["has_audio"],
                        width=s["width"], height=s["height"], label=s["label"]) for s in sources]
        p.save()
        return p

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
            if c.get("voice"):
                label += " · речь"
            w, h = (c.get("source_size") or [0, 0])[:2]
            dur = float(c.get("duration", 0))
            # Фрагменты уже ускорены при автосборке, поэтому здесь их скорость — ×1
            clips.append(Clip(new_id(), "video", c["file"], dur, 0.0, dur, width=w, height=h,
                              label=label, priority=bool(c.get("priority")), recorded_at=when,
                              cursor=c.get("cursor", []), clicks=c.get("clicks", []),
                              has_audio=bool(c.get("has_audio")), muted=bool(c.get("muted")),
                              motion=c.get("motion", "none"), click_fx=bool(c.get("click_fx", True)),
                              zoom_strength=float(c.get("zoom_strength", 1.8)),
                              score=float(c.get("score", -1)), own_cursor=bool(c.get("own_cursor"))))
        output = meta.get("output", "")
        try:   # время записи — из имени папки project_ГГГГММДД_ЧЧММСС
            # у потоков к имени добавлен номер: project_ГГГГММДД_ЧЧММСС_1
            created = time.mktime(time.strptime("_".join(directory.name.split("_")[1:3]), "%Y%m%d_%H%M%S"))
        except (IndexError, ValueError):
            created = directory.stat().st_mtime
        name = Path(output).stem if output else directory.name
        project = cls(directory, name, clips, fps=int(meta.get("fps", 30)), source_video=output, created=created)
        # окошки с веб-камеры, которые автосборка поставила в ролик, — как обычные наложения
        from glimpsy.editor.overlay import camera_item
        for c in meta.get("camera", []):
            when = c.get("recorded_at")
            label = "Камера " + time.strftime("%H:%M:%S", time.localtime(when)) if when else "Веб-камера"
            item = camera_item(new_id(), c["file"], float(c["start"]), float(c["duration"]),
                               int(c.get("width", 0)), int(c.get("height", 0)), label)
            item.src_duration = float(c.get("src_duration", c["duration"]))
            project.overlays.append(item)
        project.normalize_tracks()
        return project


class History:
    """Отмена и повтор — одна на всё, что меняет проект. Храним снимки проекта до каждой правки.

    Если одно и то же поле меняется много раз подряд (например, крутят скорость),
    это считается одной правкой — чтобы Ctrl+Z не приходилось жать 20 раз.
    Снимок делается «на всякий случай» и перед простым щелчком (выбрали текст, но не сдвинули) —
    такие пустые шаги при отмене пропускаются, поэтому Ctrl+Z всегда отменяет настоящую правку.
    """

    LIMIT = 200
    MERGE_WINDOW_S = 1.5

    def __init__(self) -> None:
        self._undo: list[dict] = []
        self._redo: list[dict] = []
        self._last_key: str | None = None
        self._last_time = 0.0
        self._restored: dict | None = None      # что вернули последней отменой/повтором

    def push(self, state: dict, key: str | None = None) -> None:
        now = time.monotonic()
        if key and key == self._last_key and now - self._last_time < self.MERGE_WINDOW_S:
            self._last_time = now
            return
        self._last_key, self._last_time = key, now
        if self._undo and self._undo[-1] == state:
            return                               # тот же снимок ещё раз — ничего нового
        self._undo.append(copy.deepcopy(state))
        del self._undo[:-self.LIMIT]
        # «Повтор» не сбрасываем сразу: снимок мог быть перед щелчком без правки.
        # Он станет недействительным, как только проект правда изменится (см. redo).

    def undo(self, current: dict) -> dict | None:
        self._drop_stale_redo(current)
        while self._undo:
            prev = self._undo.pop()
            if prev != current:
                self._redo.append(copy.deepcopy(current))
                self._last_key = None
                self._restored = copy.deepcopy(prev)
                return prev
        return None

    def redo(self, current: dict) -> dict | None:
        self._drop_stale_redo(current)
        while self._redo:
            nxt = self._redo.pop()
            if nxt != current:
                self._undo.append(copy.deepcopy(current))
                self._last_key = None
                self._restored = copy.deepcopy(nxt)
                return nxt
        return None

    def _drop_stale_redo(self, current: dict) -> None:
        """После отмены что-то поменяли — повторять уже нечего."""
        if self._redo and self._restored is not None and current != self._restored:
            self._redo.clear()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def settle(self, current: dict) -> None:
        """Проект изменился — если это не отмена/повтор, «повтор» больше не действует."""
        self._drop_stale_redo(current)


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
