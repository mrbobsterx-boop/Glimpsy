"""Автосклейка: из кандидатов выбираем лучшие и собираем ролик.

Шаги:
  1. Все «важные моменты» (Ctrl+Alt+S) берём обязательно.
  2. Остальных кандидатов делим по времени на равные группы и из каждой случайно
     берём одного, причём активные моменты выпадают чаще. Так ролик покрывает весь день.
  3. Внутри каждого кандидата выбираем самый живой кусок нужной длины.
  4. Каждый кусок приводим к одному размеру (1920×1080, с полями, если монитор другой
     формы), слегка ускоряем по темпу и склеиваем.
"""

from __future__ import annotations

import json
import logging
import math
import random
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from worklapse.config import Settings
from worklapse.paths import subprocess_flags
from worklapse.recorder.candidates import Candidate
from worklapse.recorder.encoder import Encoder, software_encoder
from worklapse.recorder.pacing import Plan

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None]


@dataclass
class Piece:
    cand: Candidate
    offset: float      # откуда начинаем внутри файла кандидата, с
    source_s: float    # сколько секунд исходника берём
    speed: float

    @property
    def out_s(self) -> float:
        return self.source_s / self.speed


class AssemblyError(RuntimeError):
    pass


# ---------------- выбор фрагментов (чистая логика, легко тестировать) ----------------

def best_window(activity: list[float], length_s: float, duration: float, rng: random.Random) -> float:
    """Смещение начала самого активного окна длиной length_s внутри файла."""
    max_off = max(0.0, duration - length_s)
    n = int(math.ceil(length_s))
    if not activity or len(activity) <= n or max_off <= 0:
        return max_off / 2
    best_i, best_v = 0, -1.0
    for i in range(0, len(activity) - n + 1):
        v = sum(activity[i:i + n]) + rng.random() * 0.05   # немного случайности при равенстве
        if v > best_v:
            best_i, best_v = i, v
    return min(float(best_i), max_off)


def select_pieces(cands: list[Candidate], plan: Plan, s: Settings, rng: random.Random | None = None) -> list[Piece]:
    rng = rng or random.Random()
    usable = [c for c in cands if c.duration >= s.clip_min_s * plan.speed * 0.8]
    priority = [c for c in usable if c.priority]
    regular = sorted([c for c in usable if not c.priority], key=lambda c: c.wall_start)

    pieces: list[Piece] = []
    for c in priority:
        # важный момент берём целиком (сколько просили до/после нажатия)
        start = max(0.0, c.want_start - c.wall_start)
        src = min(c.want_end, c.wall_end) - c.wall_start - start
        pieces.append(Piece(c, start, max(0.5, src), plan.speed))

    budget = s.target_length_s - sum(p.out_s for p in pieces)
    k = max(0, round(budget / plan.clip_out_s))
    chosen: list[Candidate] = []
    if k >= len(regular):
        chosen = regular
    elif k > 0:
        # делим по времени на k примерно равных групп, из каждой — один с перевесом активных
        for g in range(k):
            group = regular[round(g * len(regular) / k): round((g + 1) * len(regular) / k)]
            if not group:
                continue
            weights = [(c.score + 0.05) ** 2 for c in group]
            chosen.append(rng.choices(group, weights=weights, k=1)[0])

    mode = s.clip_min_s + (s.clip_max_s - s.clip_min_s) * plan.length_bias
    for c in chosen:
        out_len = rng.triangular(s.clip_min_s, s.clip_max_s, mode) if s.clip_max_s > s.clip_min_s else s.clip_min_s
        src = min(out_len * plan.speed, c.duration)
        off = best_window(c.activity, src, c.duration, rng)
        pieces.append(Piece(c, off, src, plan.speed))

    pieces.sort(key=lambda p: p.cand.wall_start + p.offset)
    return pieces


# ---------------- рендер ----------------

class Assembler:
    def __init__(self, ffmpeg: str, encoder: Encoder, settings: Settings, plan: Plan) -> None:
        self.ffmpeg = ffmpeg
        self.encoder = encoder
        self.s = settings
        self.plan = plan

    def run(self, session_dir: Path, cands: list[Candidate], session_start: float,
            progress: Progress | None = None, project_dir: Path | None = None) -> Path:
        progress = progress or (lambda f, t: None)
        pieces = select_pieces(cands, self.plan, self.s)
        if not pieces:
            raise AssemblyError("Нет подходящих фрагментов: запись была слишком короткой "
                                "или почти всё время стояла на паузе.")
        work = session_dir / "render"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        rendered: list[Path] = []
        for i, p in enumerate(pieces):
            progress(i / (len(pieces) + 1), f"Фрагмент {i + 1} из {len(pieces)}")
            out = work / f"piece_{i:04d}.mp4"
            fade = "in" if i == 0 else ("out" if i == len(pieces) - 1 else "")
            self._render_piece(session_dir / p.cand.file, p, out, fade)
            rendered.append(out)

        progress(len(pieces) / (len(pieces) + 1), "Склейка")
        out_dir = Path(self.s.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        final = unique_path(out_dir / time.strftime("Worklapse_%Y-%m-%d_%H-%M.mp4", time.localtime(session_start)))
        list_file = work / "concat.txt"
        list_file.write_text("".join(f"file '{r.name}'\n" for r in rendered), encoding="utf-8")
        tmp_final = work / "final.mp4"
        self._ffmpeg(["-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy",
                      "-movflags", "+faststart", str(tmp_final)])
        shutil.move(str(tmp_final), final)

        if project_dir is not None:
            self._save_project(project_dir, pieces, rendered, final)
        progress(1.0, "Готово")
        return final

    def _render_piece(self, src: Path, p: Piece, out: Path, fade: str) -> None:
        W, H, fps = self.s.output_width, self.s.output_height, self.s.fps
        vf = [f"setpts=(PTS-STARTPTS)/{p.speed:.4f}", f"fps={fps}",
              f"scale={W}:{H}:force_original_aspect_ratio=decrease",
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x111114", "setsar=1"]
        if fade == "in":
            vf.append("fade=t=in:st=0:d=0.4")
        elif fade == "out":
            vf.append(f"fade=t=out:st={max(0.0, p.out_s - 0.5):.3f}:d=0.5")

        def cmd(enc: Encoder) -> list[str]:
            return [*enc.global_args, "-ss", f"{p.offset:.3f}", "-t", f"{p.source_s:.3f}", "-i", str(src),
                    "-vf", ",".join(vf + [enc.filter_suffix]), "-an", *enc.args("final", fps),
                    "-video_track_timescale", "90000", str(out)]

        try:
            self._ffmpeg(cmd(self.encoder))
        except AssemblyError:
            if not self.encoder.hw:
                raise
            # аппаратный кодек подвёл — пробуем программный, результат важнее скорости
            log.warning("Аппаратный кодек не справился, пробуем программный")
            self.encoder = software_encoder()
            self._ffmpeg(cmd(self.encoder))

    def _ffmpeg(self, args: list[str]) -> None:
        full = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args]
        log.debug("ffmpeg %s", " ".join(full))
        r = subprocess.run(full, capture_output=True, **subprocess_flags())
        if r.returncode != 0:
            raise AssemblyError("FFmpeg: " + r.stderr.decode("utf-8", "replace")[-800:])

    def _save_project(self, project_dir: Path, pieces: list[Piece], rendered: list[Path], final: Path) -> None:
        """Сохраняем готовые фрагменты и данные для будущего редактора (этап 2)."""
        project_dir.mkdir(parents=True, exist_ok=True)
        items = []
        for p, r in zip(pieces, rendered):
            dest = project_dir / r.name
            shutil.copy2(r, dest)
            c = p.cand
            cursor = [[round((t - p.offset) / p.speed, 3), x, y] for t, x, y in c.cursor
                      if p.offset <= t <= p.offset + p.source_s]
            items.append({
                "file": dest.name, "duration": round(p.out_s, 3), "speed": p.speed,
                "recorded_at": c.wall_start + p.offset, "monitor": c.monitor,
                "source_size": [c.width, c.height], "priority": c.priority, "score": round(c.score, 3),
                "cursor": cursor,
            })
        meta = {"version": 1, "output": str(final), "width": self.s.output_width,
                "height": self.s.output_height, "fps": self.s.fps, "clips": items}
        (project_dir / "project.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    for i in range(2, 1000):
        q = p.with_name(f"{p.stem}_{i}{p.suffix}")
        if not q.exists():
            return q
    return p

