"""Проект в один файл (.glimpsy.zip) — чтобы продолжить монтаж на другом компьютере.

В архиве — вся папка проекта (фрагменты, монтаж, тексты, музыка, наложения, расшифровка,
маски фона) и видео, на которые проект только ссылается (монтаж по тексту не копирует свои
видео в проект). Пути к таким видео в проекте записаны целиком — например, «C:\\Users\\…\\a.mp4»
на Windows. При открытии на другом компьютере (хоть Linux, хоть Mac) эти пути заменяются на
новое место — внутри папки проекта, в sources/.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

FORMAT = 1
MANIFEST = "glimpsy-project.json"
SKIP_DIRS = {"export_tmp"}
STORE_EXT = {".mp4", ".mkv", ".mov", ".webm", ".ts", ".m4a", ".mp3", ".aac", ".ogg", ".flac", ".jpg", ".jpeg",
             ".png", ".webp", ".npy", ".raw"}


class PackageError(RuntimeError):
    pass


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _external_files(project_dir: Path) -> list[Path]:
    """Файлы вне папки проекта, на которые он ссылается полным путём."""
    out: dict[str, Path] = {}
    root = project_dir.resolve()
    for name in ("edit.json", "project.json"):
        f = project_dir / name
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for s in _strings(data):
            if len(s) < 4 or not os.path.isabs(s):
                continue
            p = Path(s)
            try:
                if p.is_file() and root not in p.resolve().parents:
                    out[s] = p
            except OSError:
                continue
    return list(out.values())


def _escaped(s: str) -> list[str]:
    """Как путь выглядит внутри JSON (с экранированием и без)."""
    return list({json.dumps(s, ensure_ascii=False)[1:-1], json.dumps(s)[1:-1]})


def export_zip(project_dir: Path, out: Path, progress: Callable[[float, str], None] | None = None,
               cancel: threading.Event | None = None) -> Path:
    """Упаковать проект в один файл. Видео не пережимаются (кладутся как есть)."""
    from glimpsy.editor.project import Project

    progress = progress or (lambda f, t: None)
    cancel = cancel or threading.Event()
    project_dir = Path(project_dir)
    if not (project_dir / "edit.json").exists():
        Project.load(project_dir).save()                  # проект ни разу не правили — сохраним монтаж
    externals = _external_files(project_dir)
    names: dict[str, str] = {}
    used: set[str] = set()
    for p in externals:
        name = p.name
        stem, ext = os.path.splitext(name)
        k = 2
        while name.lower() in used:
            name, k = f"{stem}_{k}{ext}", k + 1
        used.add(name.lower())
        names[str(p)] = name
    files = [f for f in sorted(project_dir.rglob("*")) if f.is_file()
             and not (set(f.relative_to(project_dir).parts) & SKIP_DIRS) and not f.name.endswith(".tmp")]
    total = sum(f.stat().st_size for f in files) + sum(p.stat().st_size for p in externals) or 1
    done = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    manifest = {"format": FORMAT, "name": Project.load(project_dir).name, "dir": project_dir.name,
                "external": {f"sources/{n}": old for old, n in names.items()}}
    try:
        with zipfile.ZipFile(part, "w", allowZip64=True) as z:
            z.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=1))
            for f in files + externals:
                if cancel.is_set():
                    raise PackageError("cancelled")
                arc = ("project/sources/" + names[str(f)]) if str(f) in names else \
                    "project/" + PurePosixPath(*f.relative_to(project_dir).parts).as_posix()
                kind = zipfile.ZIP_STORED if f.suffix.lower() in STORE_EXT else zipfile.ZIP_DEFLATED
                progress(done / total, f"Упаковываю: {f.name}")
                z.write(f, arc, compress_type=kind)
                done += f.stat().st_size
        part.replace(out)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    progress(1.0, "Готово")
    return out


def import_zip(archive: Path, projects_root: Path, progress: Callable[[float, str], None] | None = None) -> Path:
    """Открыть проект из файла: распаковать в папку проектов и поправить пути под этот компьютер."""
    progress = progress or (lambda f, t: None)
    try:
        z = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as e:
        raise PackageError(f"Это не файл проекта Glimpsy или он повреждён: {e}") from e
    with z:
        try:
            manifest = json.loads(z.read(MANIFEST).decode("utf-8"))
        except (KeyError, ValueError) as e:
            raise PackageError("В архиве нет проекта Glimpsy.") from e
        if int(manifest.get("format", 0)) > FORMAT:
            raise PackageError("Проект сохранён более новой версией Glimpsy — обновите программу.")
        base = "".join(ch for ch in str(manifest.get("dir") or "project") if ch.isalnum() or ch in "_-.") or "project"
        dest = projects_root / base
        k = 2
        while dest.exists():
            dest, k = projects_root / f"{base}_{k}", k + 1
        tmp = projects_root / f".{dest.name}.importing"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        members = [m for m in z.infolist() if m.filename.startswith("project/") and not m.is_dir()]
        total = sum(m.file_size for m in members) or 1
        done = 0
        try:
            for m in members:
                rel = PurePosixPath(m.filename).relative_to("project")
                if rel.is_absolute() or ".." in rel.parts:
                    raise PackageError("В архиве подозрительный путь — не открываю.")
                target = tmp.joinpath(*rel.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                progress(done / total, f"Распаковываю: {rel.name}")
                with z.open(m) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                done += m.file_size
            tmp.rename(dest)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    _relink(dest, manifest.get("external") or {})
    progress(1.0, "Готово")
    return dest


def _relink(dest: Path, external: dict[str, str]) -> None:
    """Пути старого компьютера → файлы в этой папке проекта (в JSON и в именах файлов расшифровки)."""
    from glimpsy.editor.transcript import source_key

    pairs = [(old, str(dest / Path(*PurePosixPath(rel).parts))) for rel, old in external.items()]
    for f in [dest / "edit.json", dest / "project.json", *dest.glob("transcript/*.json")]:
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8")
        for old, new in pairs:
            new_esc = json.dumps(new, ensure_ascii=False)[1:-1]
            for o in _escaped(old):
                text = text.replace(o, new_esc)
        f.write_text(text, encoding="utf-8")
    for old, new in pairs:                                  # расшифровка хранится под «ключом» пути
        for ext in (".json", ".npy"):
            f = dest / "transcript" / f"{source_key(old)}{ext}"
            if f.exists():
                f.replace(dest / "transcript" / f"{source_key(new)}{ext}")
    # готовый ролик остался на старом компьютере — новые сохранения пойдут в обычную папку роликов
    for name, key in (("edit.json", "source_video"), ("project.json", "output")):
        f = dest / name
        if f.exists():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if data.get(key) and not Path(data[key]).exists():
                data[key] = ""
                f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
