"""Экспорт отредактированного ролика через FFmpeg.

Каждый фрагмент по отдельности приводится к единому виду (размер кадра, частота,
звук), затем все склеиваются без перекодирования. Если форма кадра не совпадает
с форматом ролика (например, горизонтальная запись в вертикальном Reels), свободное
место заполняется размытой копией этого же кадра — как в CapCut.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

from worklapse.assembler import unique_path
from worklapse.editor.project import ASPECTS, DEFAULT_FRAME, Clip, Project
from worklapse.paths import subprocess_flags
from worklapse.recorder.encoder import Encoder, software_encoder

log = logging.getLogger(__name__)

BG_BLUR_DIV = 8   # фон размываем в уменьшенном виде — это быстро


class ExportError(RuntimeError):
    pass


class ExportCancelled(RuntimeError):
    pass


def atempo_chain(speed: float) -> list[str]:
    """Фильтр atempo меняет скорость звука без «мультяшного» голоса. Разбиваем на шаги 0.5–2."""
    parts = []
    s = speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    parts.append(f"atempo={s:.5f}")
    return parts


def video_filter(clip: Clip, W: int, H: int, fps: int, encoder_suffix: str, aspect: str = "") -> str:
    """Граф фильтров для картинки одного фрагмента (с учётом кадрирования)."""
    head = f"[0:v]setpts=(PTS-STARTPTS)/{clip.speed:.5f},fps={fps}"
    zoom, fx, fy = clip.frame_for(aspect) if aspect else DEFAULT_FRAME
    src_ar = (clip.width / clip.height) if clip.width and clip.height else W / H
    if (zoom, fx, fy) == DEFAULT_FRAME and abs(src_ar - W / H) < 0.02:
        return f"{head},scale={W}:{H},setsar=1,{encoder_suffix}[v]"
    bw, bh = max(2, W // BG_BLUR_DIV // 2 * 2), max(2, H // BG_BLUR_DIV // 2 * 2)
    # Размер кадра считает сам FFmpeg по реальному размеру видео (iw, ih) — так точнее
    s = f"min({W}/iw,{H}/ih)*{zoom:.5f}"
    return (
        f"{head},split[a][b];"
        f"[a]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
        f"gblur=sigma=6,eq=brightness=-0.06,scale={W}:{H}[bg];"
        f"[b]scale=w='2*trunc(iw*{s}/2)':h='2*trunc(ih*{s}/2)'[fg];"
        f"[bg][fg]overlay=x='(W-w)/2+{fx * W:.2f}':y='(H-h)/2+{fy * H:.2f}',"
        f"setsar=1,{encoder_suffix}[v]"
    )


def segment_command(ffmpeg: str, project: Project, clip: Clip, out: Path, enc: Encoder) -> list[str]:
    W, H = ASPECTS[project.aspect]
    fps = project.fps
    src = project.path_of(clip)
    dur_out = clip.duration
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *enc.global_args]
    if clip.kind == "image":
        cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{dur_out:.3f}", "-i", str(src)]
    else:
        cmd += ["-ss", f"{clip.in_s:.3f}", "-t", f"{clip.out_s - clip.in_s:.3f}", "-i", str(src)]
    use_audio = clip.kind == "video" and clip.has_audio and not clip.muted
    if not use_audio:
        cmd += ["-f", "lavfi", "-t", f"{dur_out:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    graph = video_filter(clip, W, H, fps, enc.filter_suffix, project.aspect)
    if use_audio:
        graph += ";[0:a]asetpts=PTS-STARTPTS," + ",".join(atempo_chain(clip.speed)) + "[a]"
        amap = "[a]"
    else:
        amap = "1:a"
    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", amap,
            *enc.args("final", fps), "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            "-t", f"{dur_out:.3f}", "-video_track_timescale", "90000", str(out)]
    return cmd


def default_output(project: Project, fallback_dir: Path) -> Path:
    suffix = "_edit" if project.aspect == "16:9" else "_edit_9x16"
    if project.source_video:
        src = Path(project.source_video)
        return unique_path(src.with_name(f"{src.stem}{suffix}.mp4"))
    return unique_path(fallback_dir / f"{project.name}{suffix}.mp4")


def export_project(ffmpeg: str, project: Project, out: Path, encoder: Encoder,
                   progress: Callable[[float, str], None] | None = None,
                   cancel: threading.Event | None = None) -> Path:
    progress = progress or (lambda f, t: None)
    cancel = cancel or threading.Event()
    clips = [c for c in project.clips if c.duration > 0.05]
    if not clips:
        raise ExportError("В ролике нет ни одного фрагмента.")
    work = project.dir / "export_tmp"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        parts = []
        enc = encoder
        for i, clip in enumerate(clips):
            if cancel.is_set():
                raise ExportCancelled()
            progress(i / (len(clips) + 1), f"Фрагмент {i + 1} из {len(clips)}")
            part = work / f"part_{i:04d}.mp4"
            try:
                _run(segment_command(ffmpeg, project, clip, part, enc), cancel)
            except ExportError:
                if not enc.hw:
                    raise
                log.warning("Аппаратный кодек не справился при экспорте, пробуем программный")
                enc = software_encoder()
                _run(segment_command(ffmpeg, project, clip, part, enc), cancel)
            parts.append(part)
        progress(len(clips) / (len(clips) + 1), "Склейка")
        lst = work / "concat.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = work / "final.mp4"
        _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
              "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(tmp)], cancel)
        shutil.move(str(tmp), out)
        progress(1.0, "Готово")
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run(cmd: list[str], cancel: threading.Event) -> None:
    log.debug("ffmpeg %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **subprocess_flags())
    while True:
        try:
            _, err = proc.communicate(timeout=0.3)
            break
        except subprocess.TimeoutExpired:
            if cancel.is_set():
                proc.kill()
                proc.communicate()
                raise ExportCancelled()
    if proc.returncode != 0:
        raise ExportError("FFmpeg: " + err.decode("utf-8", "replace")[-800:])
