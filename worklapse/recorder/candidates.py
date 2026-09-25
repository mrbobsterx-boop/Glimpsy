"""Кандидаты — сохранённые моменты, из которых потом собирается ролик.

Храним в 2–3 раза больше, чем нужно. Когда кандидатов становится слишком много,
выкидываем самого «слабого»: с низкой оценкой и стоящего слишком близко по времени
к соседям. Так остаются интересные моменты, равномерно разбросанные по всему дню.
Важные моменты (горячая клавиша) не выкидываются никогда.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    id: int
    file: str                     # имя файла .ts в папке сессии
    wall_start: float             # реальное время начала файла
    wall_end: float
    want_start: float             # какой отрезок мы хотели (может быть чуть уже файла)
    want_end: float
    monitor: int
    width: int
    height: int
    score: float
    priority: bool = False
    activity: list[float] = field(default_factory=list)    # оценка каждой секунды файла
    cursor: list[list[float]] = field(default_factory=list)  # [t от начала файла, x 0..1, y 0..1]
    clicks: list[list[float]] = field(default_factory=list)  # клики: [t от начала файла, x, y]

    @property
    def duration(self) -> float:
        return self.wall_end - self.wall_start


class CandidatePool:
    def __init__(self, session_dir: Path) -> None:
        self.dir = session_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.items: list[Candidate] = []
        self.next_id = 1
        self._load()

    @property
    def index_path(self) -> Path:
        return self.dir / "candidates.json"

    def _load(self) -> None:
        """Если программа закрылась аварийно, кандидаты не теряются — их можно собрать потом."""
        if not self.index_path.exists():
            return
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            self.items = [Candidate(**c) for c in data.get("items", []) if (self.dir / c["file"]).exists()]
            self.next_id = int(data.get("next_id", len(self.items) + 1))
        except Exception:
            log.exception("Не удалось прочитать список кандидатов")

    def save(self) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"next_id": self.next_id, "items": [asdict(c) for c in self.items]}),
                       encoding="utf-8")
        tmp.replace(self.index_path)

    def new_file(self) -> tuple[int, Path]:
        cid = self.next_id
        self.next_id += 1
        return cid, self.dir / f"cand_{cid:05d}.ts"

    def add(self, c: Candidate) -> None:
        self.items.append(c)
        self.items.sort(key=lambda x: x.wall_start)

    def prune(self, max_size: int) -> list[Candidate]:
        """Удаляет лишних кандидатов (кроме приоритетных). Возвращает удалённых."""
        removed: list[Candidate] = []
        while True:
            regular = [c for c in self.items if not c.priority]
            if len(regular) <= max_size:
                break
            victim = pick_victim(regular)
            self.items.remove(victim)
            removed.append(victim)
            try:
                (self.dir / victim.file).unlink(missing_ok=True)
            except OSError:
                pass
        return removed

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def span(self) -> tuple[float, float] | None:
        if not self.items:
            return None
        return self.items[0].wall_start, self.items[-1].wall_end


def pick_victim(cands: list[Candidate]) -> Candidate:
    """Кого выкинуть: ценность = оценка × «одиночество» во времени."""
    cands = sorted(cands, key=lambda c: c.wall_start)
    if len(cands) <= 2:
        return min(cands, key=lambda c: c.score)
    mids = [(c.wall_start + c.wall_end) / 2 for c in cands]
    gaps = []
    for i in range(len(cands)):
        left = mids[i] - mids[i - 1] if i > 0 else None
        right = mids[i + 1] - mids[i] if i < len(cands) - 1 else None
        near = min(g for g in (left, right) if g is not None)
        gaps.append(max(near, 1.0))
    mean_gap = sum(gaps) / len(gaps)
    mean_score = sum(c.score for c in cands) / len(cands) or 1.0

    def value(i: int) -> float:
        return (0.2 + cands[i].score / mean_score) * (gaps[i] / mean_gap) ** 0.5

    return cands[min(range(len(cands)), key=value)]
