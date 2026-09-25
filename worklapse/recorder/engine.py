"""Движок записи — «дирижёр», который связывает все части.

Работает в отдельном потоке и 10 раз в секунду:
  1. смотрит, где курсор, и при смене монитора перезапускает буфер на новом мониторе;
  2. проверяет паузу, простой и приватные приложения;
  3. раз в секунду решает (случайно, но с перевесом активных моментов), не сохранить ли
     последние несколько секунд из буфера как кандидата;
  4. выполняет отложенные сохранения «важных моментов».

Интерфейс общается с движком только через команды (очередь) и сигналы Qt, поэтому
ничего не «зависает» и потоки не мешают друг другу.
"""

from __future__ import annotations

import copy
import json
import logging
import queue
import random
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from worklapse import paths
from worklapse.assembler import Assembler
from worklapse.config import Settings
from worklapse.platform.base import Monitor, PlatformServices
from worklapse.platform.common import monitor_at
from worklapse.recorder.activity import ActivityTracker
from worklapse.recorder.candidates import Candidate, CandidatePool
from worklapse.recorder.encoder import Encoder, pick_encoder, software_encoder
from worklapse.recorder.pacing import Plan, make_plan, save_probability
from worklapse.recorder.ring_buffer import BufferRun
from worklapse.recorder.webcam import MODES as CAM_MODES, CamClip, CamStore, Webcam

log = logging.getLogger(__name__)

TICK = 0.1                 # секунд между проверками
MONITOR_DEBOUNCE = 0.4     # курсор должен задержаться на новом мониторе столько секунд
PRIVACY_CHECK_EVERY = 0.5


class State:
    STOPPED = "stopped"
    STARTING = "starting"
    RECORDING = "recording"
    IDLE = "idle"
    PAUSED = "paused"
    PRIVATE = "private"
    ASSEMBLING = "assembling"
    ERROR = "error"


STATE_LABELS = {
    State.STOPPED: "Запись остановлена",
    State.STARTING: "Запуск…",
    State.RECORDING: "Идёт запись",
    State.IDLE: "Пауза: нет активности",
    State.PAUSED: "Пауза",
    State.PRIVATE: "Пауза: приватное приложение",
    State.ASSEMBLING: "Собираю ролик…",
    State.ERROR: "Ошибка записи",
}


@dataclass
class Deferred:
    """Отложенное сохранение: «важный момент» ждёт ещё несколько секунд после нажатия."""
    due: float
    t0: float
    t1: float
    priority: bool


class RecorderEngine(QObject):
    status_changed = Signal(dict)
    notify = Signal(str, str)                 # заголовок, текст
    assembly_progress = Signal(float, str)
    assembly_done = Signal(str)               # путь к ролику
    assembly_failed = Signal(str)

    def __init__(self, settings: Settings, services: PlatformServices, ffmpeg: str) -> None:
        super().__init__()
        self.s = copy.deepcopy(settings)   # своя копия: изменения применяются только через apply_settings
        self.services = services
        self.ffmpeg = ffmpeg
        self.plan: Plan = make_plan(settings)
        self.encoder: Encoder | None = None
        self._cmds: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._rng = random.Random()
        self.state = State.STOPPED
        self._reset_session_fields()

    def _reset_session_fields(self) -> None:
        self.session_dir: Path | None = None
        self.session_start = 0.0
        self.pool: CandidatePool | None = None
        self.activity: ActivityTracker | None = None
        self.run: BufferRun | None = None
        self._run_counter = 0
        self._deferred: list[Deferred] = []
        self._user_paused = False
        self._monitors: list[Monitor] = []
        self._monitors_at = 0.0
        self._pending_monitor: tuple[int, float] | None = None
        self._private = False
        self._privacy_at = 0.0
        self._last_second = 0
        self._active_seconds = 0.0
        self._avg_score = 0.0
        self._last_save_end = 0.0
        self._fail_count = 0
        self._retry_after = 0.0
        self._last_status: dict = {}
        self._status_at = 0.0
        self._error = ""
        self._last_tick = 0.0
        self.cam: Webcam | None = None
        self.cam_store: CamStore | None = None
        self._cam_next = 0.0

    # ======================= публичные команды (из интерфейса) =======================

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start_session(self, existing_dir: Path | None = None) -> None:
        if self.running:
            return
        self._reset_session_fields()
        self.plan = make_plan(self.s)
        self._cmds = queue.Queue()   # старые команды (нажатые, пока запись стояла) не нужны
        self._thread = threading.Thread(target=self._main, args=(existing_dir,), daemon=True, name="engine")
        self._thread.start()

    def assemble_existing(self, session_dir: Path) -> None:
        """Собрать ролик из сессии, оставшейся с прошлого запуска (без новой записи)."""
        if self.running:
            self.notify.emit("Worklapse", "Сначала завершите текущую сессию.")
            return

        def work() -> None:
            self._reset_session_fields()
            self.session_dir = session_dir
            self.session_start = float(_read_json(session_dir / "session.json").get("start", time.time()))
            self.pool = CandidatePool(session_dir)
            self._assemble()

        self._thread = threading.Thread(target=work, daemon=True, name="assemble")
        self._thread.start()

    def _send(self, *cmd) -> None:
        if self.running and self.state != State.ASSEMBLING:
            self._cmds.put(cmd)
        else:
            self.notify.emit("Worklapse", "Запись сейчас не идёт.")

    def toggle_pause(self) -> None:
        self._send("pause")

    def mark_important(self) -> None:
        self._send("important", time.time())

    def finish(self) -> None:
        """Завершить сессию и собрать ролик."""
        self._send("finish")

    def apply_settings(self, s: Settings) -> None:
        s = copy.deepcopy(s)
        if self.running:
            self._cmds.put(("settings", s))
        else:
            self.s, self.plan = s, make_plan(s)

    def reselect_screen(self) -> None:
        """Wayland: заново показать системное окно выбора экрана."""
        self._cmds.put(("reselect",))

    def shutdown(self) -> None:
        """Выход из программы: останавливаем запись, кандидаты остаются на диске до следующего запуска."""
        self._cmds.put(("stop",))
        if self._thread:
            self._thread.join(timeout=10)

    # ======================= основной цикл =======================

    def _main(self, existing_dir: Path | None) -> None:
        try:
            self._set_state(State.STARTING)
            self._open_session(existing_dir)
            if self.encoder is None:
                self.encoder = pick_encoder(self.ffmpeg, self.s.encoder, self.s.fps)
                if not self.encoder.hw:
                    self.notify.emit("Worklapse", "Аппаратный видеокодек не найден — используется программный. "
                                                  "Запись работает, но нагрузка на процессор будет выше.")
            self._set_state(State.RECORDING)
            while True:
                try:
                    cmd = self._cmds.get(timeout=TICK)
                except queue.Empty:
                    cmd = None
                if cmd is not None:
                    if self._handle(cmd) == "exit":
                        return
                    continue
                self._tick(time.time())
        except Exception as e:
            log.exception("Движок упал")
            self._close_run()
            self._error = str(e)
            self._set_state(State.ERROR)
            self.notify.emit("Worklapse: ошибка", str(e))

    def _open_session(self, existing_dir: Path | None) -> None:
        if existing_dir:
            self.session_dir = existing_dir
            meta = _read_json(existing_dir / "session.json")
            self.session_start = float(meta.get("start", existing_dir.stat().st_mtime))
        else:
            self.session_start = time.time()
            self.session_dir = paths.temp_root() / time.strftime("session_%Y%m%d_%H%M%S")
            self.session_dir.mkdir(parents=True, exist_ok=True)
            (self.session_dir / "session.json").write_text(json.dumps({"start": self.session_start}))
        shutil.rmtree(self.session_dir / "buffer", ignore_errors=True)  # старый буфер не нужен
        self.pool = CandidatePool(self.session_dir)
        self.activity = ActivityTracker(self.services.input_events_supported, self.services.input_backend)
        for err in self.activity.start():
            self.notify.emit("Worklapse", err)
        self.cam_store = CamStore(self.session_dir)
        self._setup_camera()

    def _handle(self, cmd: tuple) -> str | None:
        kind = cmd[0]
        if kind == "pause":
            self._user_paused = not self._user_paused
            if self._user_paused:
                self._close_run()
                self._cam_stop()
                self._set_state(State.PAUSED)
            else:
                if self.activity:
                    self.activity.last_input_time = self.activity.last_screen_change_time = time.time()
                self._set_state(State.RECORDING)
        elif kind == "important":
            self._mark_important(cmd[1])
        elif kind == "settings":
            self._apply_settings(cmd[1])
        elif kind == "reselect":
            self._close_run()
            self.services.capture.reset()
            self._monitors = []
        elif kind == "finish":
            self._close_run()
            self._cam_finish()
            self._stop_listeners()
            self._assemble()
            return "exit"
        elif kind == "stop":
            self._close_run()
            self._cam_stop()
            self._stop_listeners()
            if self.pool:
                self.pool.save()
            self._set_state(State.STOPPED)
            return "exit"
        return None

    def _tick(self, now: float) -> None:
        act = self.activity
        assert act is not None and self.pool is not None
        if self._last_tick and now - self._last_tick > 5:
            # Компьютер «спал» или сильно тормозил: время в видео и реальное разошлись —
            # начинаем буфер заново, чтобы не вырезать не тот момент.
            log.info("Перерыв в работе %.0f с — перезапуск буфера", now - self._last_tick)
            self._close_run()
        self._last_tick = now

        # --- 1. список мониторов (обновляем раз в 3 секунды: вдруг подключили новый) ---
        if now - self._monitors_at > 3 or not self._monitors:
            if now < self._retry_after:
                return
            try:
                self._monitors = self.services.capture.monitors()
                self._monitors_at = now
            except Exception as e:
                log.exception("Не удалось получить список мониторов")
                self._fail(f"Не удалось получить доступ к экрану: {e}", now)
                return
            if not self._monitors:
                return

        # --- 2. положение курсора ---
        pos = self.services.cursor.position()
        cursor_mon = monitor_at(self._monitors, *pos) if pos else None
        if pos and cursor_mon:
            act.add_cursor(now, cursor_mon.index, (pos[0] - cursor_mon.x) / cursor_mon.width,
                           (pos[1] - cursor_mon.y) / cursor_mon.height)

        # --- 3. пауза / приватность / простой ---
        if self._user_paused:
            return
        if now - self._privacy_at >= PRIVACY_CHECK_EVERY:
            self._privacy_at = now
            win = self.services.active_window.active()
            hit = win.matched(self.s.blacklist) if win else None
            private = hit is not None
            if private != self._private:
                self._private = private
                log.info("Приватное окно: %s (%s)%s", private, win.app if win else "",
                         f" — совпало со словом «{hit}» из чёрного списка" if hit else "")
        if self._private:
            self._close_run()     # в буфер не попадает ни одного кадра приватного окна
            self._cam_stop()
            self._set_state(State.PRIVATE)
            return

        idle = act.idle_for(now) > self.s.idle_pause_s
        if idle and self.services.input_events_supported:
            # Можем отследить возвращение пользователя по мыши/клавиатуре → запись полностью стоит
            self._close_run()
            self._cam_stop()
            self._set_state(State.IDLE)
            return

        # --- 4. какой монитор снимать ---
        target = self._target_monitor(cursor_mon, now)
        if target is None:
            return
        if self.run and (self.run.monitor != target):
            self._close_run()  # курсор ушёл на другой монитор → фрагмент обрезается здесь

        # --- 5. буфер ---
        if self.run is None or not self.run.alive:
            if self.run is not None:
                self._on_run_died(now)
                if now < self._retry_after:
                    return
            if now < self._retry_after:
                return
            self._start_run(target)
            return
        self.run.poll()
        self.run.trim(self.s.buffer_s + 2)
        if not self.run.segments and now - self.run.started_at > 8:
            self._no_segments()   # запись идёт, но на кусочки не режется — буфер бесполезен
            return
        if self._fail_count and now - self.run.started_at > 10:
            self._fail_count = 0

        self._set_state(State.IDLE if idle else State.RECORDING)

        # --- 6. отложенные сохранения («важные моменты») ---
        for d in [d for d in self._deferred if d.due <= now]:
            self._deferred.remove(d)
            self._save(d.t0, d.t1, d.priority)

        # --- 7. умный рандом: раз в секунду решаем, сохранять ли момент ---
        sec = int(now)
        if sec != self._last_second:
            self._last_second = sec
            if not idle:
                self._active_seconds += 1
                self._maybe_save(now)
                self._maybe_camera()
        self._emit_status(now)

    def _target_monitor(self, cursor_mon: Monitor | None, now: float) -> Monitor | None:
        mons = self._monitors
        if self.s.monitor_mode == "manual" or not self.services.cursor.supported:
            idx = min(max(1, self.s.manual_monitor), len(mons))
            return mons[idx - 1]
        if cursor_mon is None:
            return self.run.monitor if self.run else mons[0]
        if self.run is None:
            return cursor_mon
        if cursor_mon == self.run.monitor:
            self._pending_monitor = None
            return cursor_mon
        # защита от «дребезга»: курсор мог лишь мельком пересечь границу мониторов
        if self._pending_monitor is None or self._pending_monitor[0] != cursor_mon.index:
            self._pending_monitor = (cursor_mon.index, now)
            return self.run.monitor
        if now - self._pending_monitor[1] >= MONITOR_DEBOUNCE:
            self._pending_monitor = None
            return cursor_mon
        return self.run.monitor

    # ======================= буфер =======================

    def _start_run(self, monitor: Monitor) -> None:
        assert self.session_dir and self.activity and self.encoder
        self._run_counter += 1
        try:
            cap = self.services.capture.input_for(monitor, self.s.fps)
        except Exception as e:
            log.exception("Не удалось подготовить захват")
            self._fail(f"Не удалось начать захват экрана: {e}", time.time())
            return
        run = BufferRun(self.ffmpeg, cap, monitor, self.encoder, self.s.fps,
                        self.session_dir / "buffer" / f"run_{self._run_counter:04d}",
                        self.s.record_max_height, on_frame_diff=self.activity.add_frame_diff)
        try:
            run.start()
        except Exception as e:
            log.exception("FFmpeg не запустился")
            run.cleanup()
            self._fail(f"FFmpeg не запустился: {e}", time.time())
            return
        self.run = run
        log.info("Запись монитора %s", monitor.label)

    def _close_run(self) -> None:
        """Останавливает текущий прогон. Недописанные «важные моменты» сохраняются тем, что есть."""
        run = self.run
        if run is None:
            return
        self.run = None
        run.stop()
        for d in list(self._deferred):
            self._deferred.remove(d)
            self._save_from(run, d.t0, min(d.t1, run.ended_at or time.time()), d.priority)
        run.cleanup()

    def _on_run_died(self, now: float) -> None:
        run = self.run
        assert run is not None
        err = run.error_text()
        log.warning("FFmpeg завершился неожиданно:\n%s", err)
        self.run = None
        run.cleanup()
        self._fail_count += 1
        # запасные варианты: сначала другой способ захвата, потом программный кодек
        if self._fail_count == 2 and self.services.capture.name == "ddagrab":
            from worklapse.platform.capture import GdiGrabCapture
            self.services.capture = GdiGrabCapture()
            self.notify.emit("Worklapse", "Быстрый захват (ddagrab) не работает, переключаюсь на gdigrab.")
        elif self._fail_count == 3 and self.encoder and self.encoder.hw:
            self.encoder = software_encoder()
            self.notify.emit("Worklapse", "Аппаратный кодек сбоит, переключаюсь на программный.")
        elif self._fail_count >= 5:
            self._fail("Запись экрана не запускается. Подробности — в журнале.\n" + err[-300:], now)

    def _no_segments(self) -> None:
        """Страховка: кодек пишет видео одним куском (не делает ключевых кадров).
        Переходим на программный кодек, который гарантированно режется посекундно."""
        run = self.run
        log.warning("За 8 с не появилось ни одного кусочка буфера (кодек %s)\n%s",
                    self.encoder.name if self.encoder else "?", run.error_text() if run else "")
        self._deferred.clear()
        self._close_run()
        if self.encoder and self.encoder.hw:
            self.encoder = software_encoder()
            self.notify.emit("Worklapse", "Аппаратный кодек не подошёл для буфера, "
                                          "переключаюсь на программный.")
        else:
            self._fail("Запись не делится на кусочки. Подробности — в журнале.", time.time())

    def _fail(self, message: str, now: float) -> None:
        self._error = message
        self._retry_after = now + 30
        if self.state != State.ERROR:
            self.notify.emit("Worklapse: проблема с записью", message)
        self._set_state(State.ERROR)

    # ======================= сохранение кандидатов =======================

    def _maybe_save(self, now: float) -> None:
        run, act = self.run, self.activity
        if run is None or act is None:
            return
        L = self.plan.candidate_s
        if run.available_from is None or now - run.available_from < L * 0.6:
            return  # прогон только начался — в буфере ещё мало видео
        if now - self._last_save_end < L:
            return  # не перекрываем предыдущего кандидата
        recent = act.score(now - L, now)
        # скользящее среднее «обычной» активности за сессию
        self._avg_score = recent if self._avg_score == 0 else 0.98 * self._avg_score + 0.02 * recent
        p = save_probability(self.plan, self._active_seconds, recent, self._avg_score)
        if self._rng.random() < p:
            self._save(now - L, now, priority=False)

    def _save(self, t0: float, t1: float, priority: bool) -> None:
        if self.run is not None:
            self._save_from(self.run, t0, t1, priority)

    def _save_from(self, run: BufferRun, t0: float, t1: float, priority: bool) -> None:
        pool, act = self.pool, self.activity
        if pool is None or act is None:
            return
        run.poll()
        cid, path = pool.new_file()
        try:
            got = run.save_clip(t0, t1, path)
        except OSError:
            log.exception("Не удалось сохранить фрагмент")
            got = None
        min_len = self.s.clip_min_s * self.plan.speed
        if got is None or (got[1] - got[0]) < min_len * 0.8:
            path.unlink(missing_ok=True)
            if priority:
                self.notify.emit("Worklapse", "Важный момент слишком короткий — не сохранён.")
            return
        ws, we = got
        cursor = [[round(t - ws, 2), round(x, 4), round(y, 4)]
                  for t, m, x, y in act.cursor_between(ws, we) if m == run.monitor.index]
        cand = Candidate(
            id=cid, file=path.name, wall_start=ws, wall_end=we,
            want_start=max(t0, ws), want_end=min(t1, we), monitor=run.monitor.index,
            width=run.capture.width, height=run.capture.height,
            score=act.score(max(t0, ws), min(t1, we)), priority=priority,
            activity=[round(v, 3) for v in act.per_second(ws, we)], cursor=cursor,
            clicks=[[round(t - ws, 2), round(x, 4), round(y, 4)]
                    for t, x, y in act.clicks_between(ws, we, run.monitor.index)],
        )
        pool.add(cand)
        removed = pool.prune(self.plan.pool_size)
        pool.save()
        self._last_save_end = max(self._last_save_end, we)
        log.info("Кандидат #%s: %.1f с, оценка %.2f%s (удалено %s)", cid, we - ws, cand.score,
                 ", ВАЖНЫЙ" if priority else "", len(removed))
        self._emit_status(time.time(), force=True)

    def _mark_important(self, t: float) -> None:
        if self._user_paused:
            self.notify.emit("Worklapse", "Запись на паузе — важный момент не сохранён.")
            return
        if self._private:
            self.notify.emit("Worklapse", "Открыто приватное приложение — момент не сохранён.")
            return
        if self.activity:
            self.activity.last_input_time = t   # пользователь точно активен
        before, after = self.s.important_before_s, self.s.important_after_s
        self._deferred.append(Deferred(due=t + after + 1.2, t0=t - before, t1=t + after, priority=True))
        self.notify.emit("Worklapse", f"⭐ Важный момент отмечен (−{before} с / +{after} с)")

    # ======================= веб-камера =======================

    camera_input_override: list[str] | None = None     # для проверок без настоящей камеры

    def _setup_camera(self) -> None:
        self._cam_stop()
        self.cam = None
        interval = CAM_MODES.get(self.s.camera_mode, 0)
        if not interval:
            return
        cam = Webcam(self.ffmpeg, self.s.camera_device, self.camera_input_override)
        if cam.find() is None:
            log.info("Веб-камера не найдена — фрагменты с камеры пропускаются")
            return
        self.cam = cam
        # первый фрагмент — пораньше, чтобы и короткая сессия получила хотя бы один
        self._cam_next = self._active_seconds + interval * self._rng.uniform(0.15, 0.5)

    def _maybe_camera(self) -> None:
        cam, store = self.cam, self.cam_store
        if cam is None or store is None:
            return
        interval = CAM_MODES.get(self.s.camera_mode, 0) or 300
        if cam.busy:
            got = cam.poll()
            if isinstance(got, CamClip):
                store.add(got, keep=max(3, self.plan.clips_needed // 3))
                log.info("Фрагмент с камеры: %.1f с (всего %s)", got.duration, len(store.items))
                self._cam_next = self._active_seconds + interval * self._rng.uniform(0.6, 1.4)
            elif got is False:
                if cam.gave_up:
                    self.notify.emit("Worklapse", "Не получилось снять с веб-камеры (возможно, она занята "
                                                  "другой программой или нет разрешения). До конца сессии "
                                                  "камера больше не включается.")
                    self.cam = None
                else:
                    self._cam_next = self._active_seconds + 30
            return
        if self._active_seconds >= self._cam_next:
            cam.start(store.new_path(), self.s.camera_clip_s)

    def _cam_stop(self) -> None:
        if self.cam is not None and self.cam.busy:
            self.cam.stop()

    def _cam_finish(self) -> None:
        """Конец сессии: дождаться фрагмента, который уже снимается (несколько секунд)."""
        cam = self.cam
        if cam is None or not cam.busy:
            return
        deadline = time.time() + self.s.camera_clip_s + 8
        while cam.busy and time.time() < deadline:
            time.sleep(0.2)
            got = cam.poll()
            if isinstance(got, CamClip) and self.cam_store is not None:
                self.cam_store.add(got, keep=max(3, self.plan.clips_needed // 3))
        self._cam_stop()

    # ======================= сборка =======================

    def _assemble(self) -> None:
        assert self.pool is not None and self.session_dir is not None
        self._set_state(State.ASSEMBLING)
        pool = self.pool
        if not pool.items:
            self.assembly_failed.emit("За эту сессию не сохранилось ни одного фрагмента.")
            shutil.rmtree(self.session_dir, ignore_errors=True)
            self._set_state(State.STOPPED)
            return
        encoder = self.encoder or pick_encoder(self.ffmpeg, self.s.encoder, self.s.fps)
        project_dir = None
        if self.s.keep_project_for_editor:
            project_dir = paths.data_dir() / "projects" / self.session_dir.name.replace("session_", "project_")
        try:
            out = Assembler(self.ffmpeg, encoder, self.s, self.plan).run(
                self.session_dir, pool.items, self.session_start,
                progress=lambda f, t: self.assembly_progress.emit(f, t), project_dir=project_dir,
            )
        except Exception as e:
            log.exception("Сборка не удалась")
            # черновики не удаляем — можно будет попробовать ещё раз после перезапуска
            self.assembly_failed.emit(str(e))
            self._set_state(State.STOPPED)
            return
        shutil.rmtree(self.session_dir, ignore_errors=True)   # буфер и черновики больше не нужны
        self._set_state(State.STOPPED)
        self.assembly_done.emit(str(out))

    # ======================= разное =======================

    def _apply_settings(self, s: Settings) -> None:
        old = self.s
        restart = (s.fps != self.s.fps or s.encoder != self.s.encoder or
                   s.record_max_height != self.s.record_max_height or
                   s.monitor_mode != self.s.monitor_mode or s.manual_monitor != self.s.manual_monitor)
        if s.encoder != self.s.encoder:
            self.encoder = pick_encoder(self.ffmpeg, s.encoder, s.fps)
        self.s = s
        self.plan = make_plan(s)
        if restart:
            self._close_run()
        camera_changed = s.camera_mode != old.camera_mode or s.camera_device != old.camera_device
        if self.pool:
            self.pool.prune(self.plan.pool_size)
            self.pool.save()
        if camera_changed and self.cam_store is not None:
            self._setup_camera()

    def _stop_listeners(self) -> None:
        if self.activity:
            self.activity.stop()

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self._emit_status(time.time(), force=True)

    def _emit_status(self, now: float, force: bool = False) -> None:
        if not force and now - self._status_at < 2:
            return
        self._status_at = now
        pool = self.pool
        st = {
            "state": self.state,
            "label": STATE_LABELS.get(self.state, self.state),
            "monitor": self.run.monitor.label if self.run else "",
            "candidates": pool.count if pool else 0,
            "important": sum(1 for c in pool.items if c.priority) if pool else 0,
            "needed": self.plan.clips_needed,
            "encoder": self.encoder.label if self.encoder else "",
            "capture": self.services.capture.name,
            "error": self._error if self.state == State.ERROR else "",
        }
        if st != self._last_status or force:
            self._last_status = st
            self.status_changed.emit(st)


def find_unfinished_sessions() -> list[Path]:
    """Сессии, оставшиеся после аварийного закрытия или выхода без сборки."""
    out = []
    for d in sorted(paths.temp_root().glob("session_*")):
        idx = _read_json(d / "candidates.json")
        if idx.get("items"):
            out.append(d)
        elif d.is_dir():
            shutil.rmtree(d, ignore_errors=True)   # пустые — просто убираем
    return out


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
