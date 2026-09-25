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
from dataclasses import dataclass
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


@dataclass
class TextLayer:
    """Заранее нарисованный текст: картинка + где и когда её показать."""
    png: Path
    item: object
    style: dict
    x: float
    y: float
    w: int
    h: int


def render_text_layers(project: Project, out_dir: Path) -> list[TextLayer]:
    """Нарисовать все тексты в PNG. Вызывать из основного потока (нужен Qt)."""
    from worklapse.editor.text import effective_style, placement, render_text

    W, H = ASPECTS[project.aspect]
    out_dir.mkdir(parents=True, exist_ok=True)
    layers = []
    total = project.total
    for i, t in enumerate(sorted(project.texts, key=lambda x: x.start)):
        if not t.text.strip() or t.start >= total:
            continue
        style = effective_style(t, project.text_style)
        img = render_text(t.text, style, W, H)
        png = out_dir / f"text_{i:03d}.png"
        img.save(str(png))
        x, y = placement(img.width(), img.height(), t.pos_for(project.aspect), W, H)
        layers.append(TextLayer(png, t, style, x, y, img.width(), img.height()))
    return layers


@dataclass
class OverlayLayer:
    """Наложение, подготовленное к экспорту."""
    item: object
    x: float                  # левый верхний угол самого наложения (без запаса под тень)
    y: float
    w: int
    h: int
    pad: int                  # запас под тень вокруг картинки
    png: Path | None = None           # картинка с оформлением (для фото)
    shadow_png: Path | None = None    # только тень (для видео)
    video: Path | None = None


def render_overlay_layers(project: Project, out_dir: Path) -> list[OverlayLayer]:
    """Подготовить наложения. Вызывать из основного потока (рисует Qt)."""
    from PySide6.QtGui import QImage

    from worklapse.editor.overlay import overlay_rect, shadow_image, shadow_pad, styled_image

    W, H = ASPECTS[project.aspect]
    out_dir.mkdir(parents=True, exist_ok=True)
    layers = []
    for i, ov in enumerate(getattr(project, "overlays", [])):
        x, y, w, h = overlay_rect(ov, project.aspect, W, H)
        w, h = max(2, int(round(w / 2)) * 2), max(2, int(round(h / 2)) * 2)
        pad = shadow_pad(w, h)
        path = project.dir / ov.src
        layer = OverlayLayer(ov, round(x), round(y), w, h, pad)
        if ov.kind == "image":
            png = out_dir / f"overlay_{i:03d}.png"
            styled_image(QImage(str(path)), w, h, ov.radius, ov.shadow, ov.opacity).save(str(png))
            layer.png = png
        else:
            layer.video = path
            if ov.shadow:
                sp = out_dir / f"overlay_{i:03d}_shadow.png"
                shadow_image(w, h, ov.radius, ov.opacity).save(str(sp))
                layer.shadow_png = sp
        layers.append(layer)
    return layers


def default_output(project: Project, fallback_dir: Path) -> Path:
    suffix = "_edit" if project.aspect == "16:9" else "_edit_9x16"
    if project.source_video:
        src = Path(project.source_video)
        return unique_path(src.with_name(f"{src.stem}{suffix}.mp4"))
    return unique_path(fallback_dir / f"{project.name}{suffix}.mp4")


def export_project(ffmpeg: str, project: Project, out: Path, encoder: Encoder,
                   progress: Callable[[float, str], None] | None = None,
                   cancel: threading.Event | None = None,
                   text_layers: list[TextLayer] | None = None,
                   overlay_layers: list["OverlayLayer"] | None = None) -> Path:
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
        layered = bool(text_layers or overlay_layers)
        joined = work / "joined.mp4" if layered else tmp
        _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
              "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(joined)], cancel)
        if layered:
            progress(len(clips) / (len(clips) + 1), "Тексты и наложения")
            _compose_layers(ffmpeg, project, joined, tmp, text_layers or [], overlay_layers or [], enc, cancel)
        shutil.move(str(tmp), out)
        progress(1.0, "Готово")
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _compose_layers(ffmpeg: str, project: Project, src: Path, out: Path, texts: list[TextLayer],
                    overlays: list["OverlayLayer"], enc: Encoder, cancel: threading.Event) -> None:
    """Один проход FFmpeg: сначала наложения (картинки/видео), сверху — тексты."""
    from worklapse.editor.overlay import video_alpha_filter
    from worklapse.editor.text import ffmpeg_overlay

    W, H = ASPECTS[project.aspect]
    total, fps = project.total, project.fps
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *enc.global_args, "-i", str(src)]
    parts: list[str] = []
    audio: list[str] = []
    prev, n = "0:v", 0

    def still(png: Path) -> int:
        nonlocal n
        cmd.extend(["-loop", "1", "-framerate", str(fps), "-t", f"{total:.3f}", "-i", str(png)])
        n += 1
        return n

    def put(label_in: str, x: float, y: float, S: float, E: float) -> None:
        nonlocal prev
        out_label = f"v{len(parts)}"
        parts.append(f"[{prev}][{label_in}]overlay=x={x:.2f}:y={y:.2f}:eof_action=pass:"
                     f"enable='between(t,{S:.3f},{E:.3f})'[{out_label}]")
        prev = out_label

    for ov in overlays:
        it = ov.item
        S, E = it.start, min(it.end, total)
        if S >= total:
            continue
        if it.kind == "image":
            k = still(ov.png)
            parts.append(f"[{k}:v]format=rgba[o{k}]")
            put(f"o{k}", ov.x - ov.pad, ov.y - ov.pad, S, E)
            continue
        if ov.shadow_png is not None:
            k = still(ov.shadow_png)
            parts.append(f"[{k}:v]format=rgba[o{k}]")
            put(f"o{k}", ov.x - ov.pad, ov.y - ov.pad, S, E)
        cmd.extend(["-ss", f"{it.in_s:.3f}", "-t", f"{E - S:.3f}", "-i", str(ov.video)])
        n += 1
        k = n
        parts.append(f"[{k}:v]setpts=PTS-STARTPTS+{S:.3f}/TB,"
                     f"{video_alpha_filter(ov.w, ov.h, it.radius, it.opacity)}[o{k}]")
        put(f"o{k}", ov.x, ov.y, S, E)
        if it.has_audio and not it.muted:
            parts.append(f"[{k}:a]asetpts=PTS-STARTPTS,adelay=delays={S * 1000:.0f}:all=1[a{k}]")
            audio.append(f"[a{k}]")

    for layer in texts:
        k = still(layer.png)
        chain = ffmpeg_overlay(k, prev, f"t{len(parts)}", layer.item, layer.style, layer.x, layer.y,
                               layer.w, layer.h, H)
        parts.append(chain)
        prev = f"t{len(parts) - 1}"

    parts.append(f"[{prev}]{enc.filter_suffix}[vout]")
    if audio:
        parts.append(f"[0:a]{''.join(audio)}amix=inputs={len(audio) + 1}:duration=first:"
                     f"normalize=0:dropout_transition=0[aout]")
        amap, acodec = "[aout]", ["-c:a", "aac", "-b:a", "160k"]
    else:
        amap, acodec = "0:a?", ["-c:a", "copy"]
    cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]", "-map", amap, *acodec,
            *enc.args("final", fps), "-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]
    _run(cmd, cancel)


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
