"""Очистка кэша: всё, что программа хранит «для себя», кроме готовых роликов.

Удаляется:
  * проекты редактора (фрагменты сессий, правки, вставленные копии файлов);
  * миниатюры для ленты и временные файлы экспорта;
  * черновики прошлых сессий записи (кроме той, что идёт прямо сейчас).
Не трогается:
  * готовые и экспортированные ролики (они в папке для роликов из настроек);
  * настройки, журнал и добавленные шрифты.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from worklapse import paths

log = logging.getLogger(__name__)


@dataclass
class CacheReport:
    projects: int
    project_bytes: int
    other_bytes: int
    paths: list[Path]

    @property
    def total_bytes(self) -> int:
        return self.project_bytes + self.other_bytes


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def scan(keep: Path | None = None, output_dir: Path | None = None) -> CacheReport:
    """Что будет удалено. keep — папка текущей сессии записи (её не трогаем)."""
    keep = keep.resolve() if keep else None
    out = output_dir.resolve() if output_dir else None
    projects = [d for d in (paths.data_dir() / "projects").glob("*") if d.is_dir()]
    temp = [d for d in paths.temp_root().iterdir() if d.resolve() != keep] if paths.temp_root().exists() else []
    targets = projects + temp
    # на всякий случай: папку с готовыми роликами никогда не удаляем, даже если она внутри кэша
    if out is not None:
        targets = [t for t in targets if not (out == t.resolve() or out.is_relative_to(t.resolve()))]
        projects = [t for t in projects if t in targets]
    return CacheReport(len(projects), sum(_size(p) for p in projects),
                       sum(_size(p) for p in targets if p not in projects), targets)


def clear(report: CacheReport) -> int:
    """Удалить найденное. Возвращает, сколько байт освобождено."""
    freed = 0
    for p in report.paths:
        size = _size(p)
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            freed += size
        except OSError:
            log.warning("Не удалось удалить %s (возможно, файл открыт)", p, exc_info=True)
    return freed


def human(n: int) -> str:
    for unit in ("байт", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "байт" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ГБ"
