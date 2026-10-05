"""Автосклейка: из кандидатов выбираем лучшие и собираем ролик.

Шаги:
  1. Все «важные моменты» (Ctrl+Alt+1) берём обязательно.
  2. Остальных кандидатов делим по времени на равные группы и из каждой случайно
     берём одного, причём активные моменты выпадают чаще. Так ролик покрывает весь день.
  3. Внутри каждого кандидата выбираем самый живой кусок нужной длины.
  4. Каждый кусок приводим к одному размеру (1920×1080, с полями, если монитор другой
     формы), слегка ускоряем по темпу и склеиваем.
  5. Если были фрагменты с веб-камеры — ставим их окошком в углу рядом с теми
     моментами, когда они сняты.
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

from glimpsy.config import Settings
from glimpsy.paths import subprocess_flags
from glimpsy.recorder.candidates import Candidate
from glimpsy.recorder.encoder import Encoder, software_encoder
from glimpsy.recorder.pacing import Plan
from glimpsy.recorder.streams import crop_filter
from glimpsy.recorder.webcam import CamClip, load_clips

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None]


def _atempo(speed: float) -> list[str]:
    """Скорость звука без «мультяшного» голоса (atempo работает шагами 0.5–2)."""
    parts, s = [], speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    parts.append(f"atempo={s:.5f}")
    return parts


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
    voice = sorted((c for c in cands if c.voice_id), key=lambda c: (c.voice_id, c.voice_part))
    spoken = [(c.wall_start, c.wall_end) for c in voice]

    def overlaps_voice(c: Candidate) -> bool:
        return any(c.wall_start < b and a < c.wall_end for a, b in spoken)

    usable = [c for c in cands if not c.voice_id and c.duration >= plan.clip_min_s * plan.speed * 0.8
              and not overlaps_voice(c)]
    priority = [c for c in usable if c.priority]
    regular = sorted([c for c in usable if not c.priority], key=lambda c: c.wall_start)

    pieces: list[Piece] = []
    # речь — целиком, без ускорения и без обрезки; в «бюджет» длины ролика она не входит
    for c in voice:
        start = max(0.0, c.want_start - c.wall_start)
        src = min(c.want_end, c.wall_end) - c.wall_start - start
        if src > 0.3:
            pieces.append(Piece(c, start, src, 1.0))
    voice_s = sum(p.out_s for p in pieces)
    for c in priority:
        # важный момент берём целиком (сколько просили до/после нажатия)
        start = max(0.0, c.want_start - c.wall_start)
        src = min(c.want_end, c.wall_end) - c.wall_start - start
        pieces.append(Piece(c, start, max(0.5, src), plan.speed))

    budget = s.target_length_s - (sum(p.out_s for p in pieces) - voice_s)
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

    lo, hi = plan.clip_min_s, plan.clip_max_s
    mode = lo + (hi - lo) * plan.length_bias
    for c in chosen:
        out_len = rng.triangular(lo, hi, mode) if hi > lo else lo
        src = min(out_len * plan.speed, c.duration)
        off = best_window(c.activity, src, c.duration, rng)
        pieces.append(Piece(c, off, src, plan.speed))

    pieces.sort(key=lambda p: p.cand.wall_start + p.offset)
    return pieces


CAM_EVERY_S = 12.0       # не чаще одного окошка с камеры на столько секунд ролика
CAM_MIN_S = 1.5


@dataclass
class CamPlacement:
    clip: CamClip
    start: float             # секунда итогового ролика
    duration: float


def place_camera(pieces: list[Piece], cams: list[CamClip]) -> list[CamPlacement]:
    """Каждый фрагмент с камеры — к тому куску ролика, который снят ближе всего по времени."""
    if not pieces or not cams:
        return []
    starts, t = [], 0.0
    for p in pieces:
        starts.append(t)
        t += p.out_s
    total = t
    limit = max(1, int(total // CAM_EVERY_S))
    cams = sorted(cams, key=lambda c: c.start)
    if len(cams) > limit:        # равномерно по сессии
        cams = [cams[round(i * (len(cams) - 1) / max(1, limit - 1))] for i in range(limit)] if limit > 1 \
            else [cams[len(cams) // 2]]
    out: list[CamPlacement] = []
    used: set[int] = set()
    for c in cams:
        order = sorted(range(len(pieces)), key=lambda i: abs(pieces[i].cand.wall_start + pieces[i].offset - c.start))
        for i in order[:3]:     # только среди ближайших по времени — иначе окошко окажется «не к месту»
            s = starts[i] + (0.3 if i else 0.8)
            d = min(c.duration, total - s - 0.4)
            if i in used or d < CAM_MIN_S:
                continue
            if any(s < o.start + o.duration + 2 and o.start < s + d + 2 for o in out):
                continue       # не накладываем окошки друг на друга
            used.add(i)
            out.append(CamPlacement(c, round(s, 3), round(d, 3)))
            break
    out.sort(key=lambda o: o.start)
    return out


# ---------------- рендер ----------------

class Assembler:
    def __init__(self, ffmpeg: str, encoder: Encoder, settings: Settings, plan: Plan) -> None:
        self.ffmpeg = ffmpeg
        self.encoder = encoder
        self.s = settings
        self.plan = plan

    def run(self, session_dir: Path, cands: list[Candidate], session_start: float,
            progress: Progress | None = None, project_dir: Path | None = None,
            name: str = "", shared_dir: Path | None = None) -> Path:
        """session_dir — где лежат кандидаты; shared_dir — общая папка сессии (камера, статистика),
        если кандидаты в подпапке потока; name — название потока (добавляется к имени ролика)."""
        progress = progress or (lambda f, t: None)
        shared_dir = shared_dir or session_dir
        pieces = select_pieces(cands, self.plan, self.s)
        if not pieces:
            raise AssemblyError("Нет подходящих фрагментов: запись была слишком короткой "
                                "или почти всё время стояла на паузе.")
        work = session_dir / "render"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        rendered: list[Path] = []
        clean: dict[int, Path] = {}
        # звук: в ролике — только речь (голосовой режим); у обычных фрагментов звук сохраняется
        # в проекте редактора (выключенным) — его можно включить у любого фрагмента
        has_audio = any(p.cand.audio for p in pieces)
        self._session_dir = session_dir
        for i, p in enumerate(pieces):
            progress(i / (len(pieces) + 1), f"Фрагмент {i + 1} из {len(pieces)}")
            out = work / f"piece_{i:04d}.mp4"
            fade = "in" if i == 0 else ("out" if i == len(pieces) - 1 else "")
            self._render_piece(session_dir / p.cand.file, p, out, fade, audio="voice" if has_audio else "none")
            rendered.append(out)
            audio_differs = bool(p.cand.audio) and not p.cand.voice_id
            if project_dir is not None and (self._has_effects(p) or audio_differs or p.cand.own_cursor):
                # в редактор — чистый фрагмент: там зум и клики накладываются заново и их можно выключить
                clean_out = work / f"clean_{i:04d}.mp4"
                self._render_piece(session_dir / p.cand.file, p, clean_out, fade, effects=False,
                                   audio="all" if has_audio else "none", cursor=False)
                clean[i] = clean_out

        progress(len(pieces) / (len(pieces) + 1), "Склейка")
        out_dir = Path(self.s.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = time.strftime("Glimpsy_%Y-%m-%d_%H-%M", time.localtime(session_start))
        if name:
            stem += "_" + safe_name(name)
        final = unique_path(out_dir / f"{stem}.mp4")
        list_file = work / "concat.txt"
        list_file.write_text("".join(f"file '{r.name}'\n" for r in rendered), encoding="utf-8")
        tmp_final = work / "final.mp4"
        self._ffmpeg(["-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy",
                      "-movflags", "+faststart", str(tmp_final)])
        cams = place_camera(pieces, load_clips(shared_dir))
        if cams:
            progress(len(pieces) / (len(pieces) + 1), "Окошко с веб-камеры")
            with_cam = work / "final_cam.mp4"
            try:
                self._add_camera(tmp_final, cams, shared_dir / "camera", with_cam)
                tmp_final = with_cam
            except AssemblyError:
                log.exception("Не удалось добавить фрагменты с камеры — ролик без них")
                cams = []
        shutil.move(str(tmp_final), final)

        if project_dir is not None:
            self._save_project(project_dir, pieces, [clean.get(i, r) for i, r in enumerate(rendered)], final,
                               cams, shared_dir / "camera", name)
            stats = shared_dir / "stats.json"
            if stats.exists():                 # статистика дня — для вкладки в редакторе
                shutil.copy2(stats, project_dir / "stats.json")
        progress(1.0, "Готово")
        return final

    def _effects_graph(self, p: Piece, W: int, H: int, fps: int, effects: bool = True,
                       cursor_idx: int | None = None) -> tuple[str, str]:
        """Круги кликов и приближение к месту работы. Возвращает (граф до ускорения, метку выхода)."""
        from glimpsy.editor import motion
        from glimpsy.editor.clicks import ripple_graph

        c = p.cand
        t0, t1 = p.offset, p.offset + p.source_s
        parts, label = [], "0:v"
        if c.crop:                       # поток «только окно»: сначала вырезаем окно из кадра
            parts.append(f"[0:v]{crop_filter(c.crop)}[crp]")
            label = "crp"
        # ширина видео в записи (высокие экраны при записи уменьшаются до record_max_height)
        rh = min(c.height, self.s.record_max_height) if c.height else 0
        stream_w = int(c.width * rh / c.height) if c.height else c.width
        stream_h = rh
        if c.crop:
            stream_w, stream_h = int(stream_w * c.crop[2]), int(rh * c.crop[3])
        cw, ch = c.size
        if effects and self.s.fx_clicks and c.clicks:
            rip = ripple_graph(label, "rip", c.clicks, t0, t1, p.speed, stream_w)
            if rip:
                parts.append(rip)
                label = "rip"
        if cursor_idx is not None:        # свой плавный курсор — поверх кругов, до зума
            from glimpsy.editor import cursor as cur

            g = cur.overlay_graph(label, "cur", cursor_idx, c.cursor, c.duration, t0, t1, stream_w, stream_h,
                                  cur.DEFAULT_STYLE, 1.0)
            if g:
                parts.append(g)
                label = "cur"
        if effects and self.s.fx_zoom and (c.cursor or c.clicks) and cw and ch:
            k = min(W / cw, H / ch)
            zw, zh = max(2, int(cw * k) // 2 * 2), max(2, int(ch * k) // 2 * 2)
            track = motion.autozoom_track(c.cursor, c.duration, self.s.fx_zoom_strength, c.clicks)
            parts.append(f"[{label}]{motion.autozoom_filter(track, t0, t1, zw, zh, fps)}[zm]")
            label = "zm"
        return ";".join(parts), label

    def _render_piece(self, src: Path, p: Piece, out: Path, fade: str, effects: bool = True,
                      audio: str = "none", cursor: bool = True) -> None:
        """audio: none — без звука; voice — звук только у речи (у остальных тишина); all — звук у всех.
        cursor — рисовать свой курсор (если запись без курсора); в проект редактора идёт без него."""
        W, H, fps = self.s.output_width, self.s.output_height, self.s.fps
        vf = [f"setpts=(PTS-STARTPTS)/{p.speed:.4f}", f"fps={fps}",
              f"scale={W}:{H}:force_original_aspect_ratio=decrease",
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x111114", "setsar=1"]
        if fade == "in":
            vf.append("fade=t=in:st=0:d=0.4")
        elif fade == "out":
            vf.append(f"fade=t=out:st={max(0.0, p.out_s - 0.5):.3f}:d=0.5")

        c = p.cand
        a_in: list[str] = []
        a_graph, a_map = "", ["-an"]
        if audio != "none":
            wav = self._session_dir / c.audio if c.audio else None
            if wav is not None and wav.exists() and (audio == "all" or c.voice_id):
                a_in = ["-ss", f"{p.offset:.3f}", "-t", f"{p.source_s:.3f}", "-i", str(wav)]
                chain = "asetpts=PTS-STARTPTS," + ",".join(_atempo(p.speed))
                if not c.voice_id:                           # мягкие края — без щелчков на склейках
                    chain += f",afade=t=in:d=0.03,afade=t=out:st={max(0.0, p.out_s - 0.05):.3f}:d=0.05"
            else:
                a_in = ["-f", "lavfi", "-t", f"{p.out_s:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
                chain = "asetpts=PTS-STARTPTS"
            a_graph = f";[1:a]{chain},aformat=sample_rates=48000:channel_layouts=stereo,apad[a]"
            a_map = ["-map", "[a]", "-c:a", "aac", "-b:a", "160k", "-shortest"]

        cur_in: list[str] = []
        draw_cursor = cursor and c.own_cursor and bool(c.cursor)
        if draw_cursor:
            from glimpsy.editor import cursor as cur

            rh = min(c.height, self.s.record_max_height) if c.height else 0
            sw = int(c.width * rh / c.height * (c.crop[2] if c.crop else 1)) if c.height else c.width
            cur_in = cur.input_args(cur.DEFAULT_STYLE, cur.height_px(sw, 1.0), p.source_s + 1)
        pre, label = self._effects_graph(p, W, H, fps, effects, (2 if a_in else 1) if draw_cursor else None)

        def cmd(enc: Encoder) -> list[str]:
            graph = (pre + ";" if pre else "") + f"[{label}]" + ",".join(vf + [enc.filter_suffix]) + "[v]" + a_graph
            return [*enc.global_args, "-ss", f"{p.offset:.3f}", "-t", f"{p.source_s:.3f}", "-i", str(src), *a_in, *cur_in,
                    "-filter_complex", graph, "-map", "[v]", *a_map, *enc.args("final", fps),
                    "-video_track_timescale", "90000", str(out)]

        try:
            self._ffmpeg(cmd(self.encoder))
        except AssemblyError:
            if self.encoder.hw:
                # аппаратный кодек подвёл — пробуем программный, результат важнее скорости
                log.warning("Аппаратный кодек не справился, пробуем программный")
                self.encoder = software_encoder()
                try:
                    self._ffmpeg(cmd(self.encoder))
                    return
                except AssemblyError:
                    if not pre or not (effects or draw_cursor):
                        raise
            elif not pre or not (effects or draw_cursor):
                raise
            if effects and self._has_effects(p):
                log.exception("Эффекты (зум/клики) не получились — фрагмент без них")
                self._render_piece(src, p, out, fade, effects=False, audio=audio, cursor=cursor)
            else:
                log.exception("Курсор не получился — фрагмент без него")
                self._render_piece(src, p, out, fade, effects=False, audio=audio, cursor=False)

    def _has_effects(self, p: Piece) -> bool:
        c = p.cand
        return bool((self.s.fx_clicks and c.clicks) or (self.s.fx_zoom and (c.cursor or c.clicks)))

    def _add_camera(self, src: Path, cams: list[CamPlacement], cam_dir: Path, out: Path) -> None:
        """Второй проход: окошки с камеры поверх склеенного ролика."""
        from glimpsy.editor.overlay import camera_item, overlay_rect, video_alpha_filter

        W, H, fps = self.s.output_width, self.s.output_height, self.s.fps
        aspect = "9:16" if H > W else "16:9"

        def cmd(enc: Encoder) -> list[str]:
            args = [*enc.global_args, "-i", str(src)]
            parts, prev = [], "0:v"
            for k, c in enumerate(cams, start=1):
                item = camera_item("cam", c.clip.file, c.start, c.duration, c.clip.width, c.clip.height)
                x, y, w, h = overlay_rect(item, aspect, W, H)
                w, h = max(2, int(round(w / 2)) * 2), max(2, int(round(h / 2)) * 2)
                S, E = c.start, c.start + c.duration
                args += ["-t", f"{c.duration:.3f}", "-i", str(cam_dir / c.clip.file)]
                fade = min(0.25, c.duration / 4)
                parts.append(f"[{k}:v]setpts=PTS-STARTPTS+{S:.3f}/TB,"
                             f"{video_alpha_filter(w, h, item.radius, 1.0)},"
                             f"fade=t=in:st={S:.3f}:d={fade:.2f}:alpha=1,"
                             f"fade=t=out:st={E - fade:.3f}:d={fade:.2f}:alpha=1[c{k}]")
                parts.append(f"[{prev}][c{k}]overlay=x={round(x)}:y={round(y)}:eof_action=pass:"
                             f"enable='between(t,{S:.3f},{E:.3f})'[v{k}]")
                prev = f"v{k}"
            parts.append(f"[{prev}]{enc.filter_suffix}[vout]")
            return [*args, "-filter_complex", ";".join(parts), "-map", "[vout]", "-map", "0:a?", "-c:a", "copy",
                    *enc.args("final", fps), "-movflags", "+faststart", str(out)]

        try:
            self._ffmpeg(cmd(self.encoder))
        except AssemblyError:
            if not self.encoder.hw:
                raise
            self.encoder = software_encoder()
            self._ffmpeg(cmd(self.encoder))

    def _ffmpeg(self, args: list[str]) -> None:
        full = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args]
        log.debug("ffmpeg %s", " ".join(full))
        from glimpsy.paths import short_command

        with short_command(full) as cmd:
            r = subprocess.run(cmd, capture_output=True, **subprocess_flags())
        if r.returncode != 0:
            raise AssemblyError("FFmpeg: " + r.stderr.decode("utf-8", "replace")[-800:])

    def _save_project(self, project_dir: Path, pieces: list[Piece], rendered: list[Path], final: Path,
                      cams: list[CamPlacement] | None = None, cam_dir: Path | None = None,
                      name: str = "") -> None:
        """Сохраняем готовые фрагменты и данные для будущего редактора (этап 2)."""
        project_dir.mkdir(parents=True, exist_ok=True)
        items = []
        for p, r in zip(pieces, rendered):
            dest = project_dir / r.name
            shutil.copy2(r, dest)
            c = p.cand
            cursor = [[round((t - p.offset) / p.speed, 3), x, y] for t, x, y in c.cursor
                      if p.offset <= t <= p.offset + p.source_s]
            clicks = [[round((t - p.offset) / p.speed, 3), x, y] for t, x, y in c.clicks
                      if p.offset <= t <= p.offset + p.source_s]
            items.append({
                "file": dest.name, "duration": round(p.out_s, 3), "speed": p.speed,
                "recorded_at": c.wall_start + p.offset, "monitor": c.monitor,
                "source_size": list(c.size), "priority": c.priority, "score": round(c.score, 3),
                "cursor": cursor, "clicks": clicks,
                "has_audio": bool(c.audio), "muted": bool(c.audio) and not c.voice_id, "voice": c.voice_id,
                "own_cursor": c.own_cursor,
                # эффекты, которые были в автосборке, — в редакторе их можно выключить у любого фрагмента
                "motion": "autozoom" if self.s.fx_zoom and (cursor or clicks) else "none",
                "click_fx": bool(self.s.fx_clicks),
                "zoom_strength": self.s.fx_zoom_strength,
            })
        camera = []
        for i, c in enumerate(cams or []):
            if cam_dir is None:
                break
            name = f"camera_{i:03d}.mp4"
            shutil.copy2(cam_dir / c.clip.file, project_dir / name)
            camera.append({"file": name, "start": c.start, "duration": c.duration, "src_duration": c.clip.duration,
                           "width": c.clip.width, "height": c.clip.height, "recorded_at": c.clip.start})
        meta = {"version": 1, "output": str(final), "width": self.s.output_width,
                "height": self.s.output_height, "fps": self.s.fps, "clips": items, "camera": camera}
        if name:
            meta["stream"] = name
        (project_dir / "project.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def safe_name(name: str) -> str:
    """Название потока → часть имени файла (без символов, запрещённых в Windows)."""
    bad = '<>:"/\\|?*'
    out = "".join("_" if ch in bad or ord(ch) < 32 else ch for ch in name).strip(" .")
    return out[:40] or "поток"


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    for i in range(2, 1000):
        q = p.with_name(f"{p.stem}_{i}{p.suffix}")
        if not q.exists():
            return q
    return p

