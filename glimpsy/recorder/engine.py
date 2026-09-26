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

from glimpsy import paths
from glimpsy.assembler import Assembler
from glimpsy.config import Settings
from glimpsy.platform.base import Monitor, PlatformServices
from glimpsy.platform.common import monitor_at
from glimpsy.recorder.activity import ActivityTracker
from glimpsy.recorder.candidates import Candidate, CandidatePool
from glimpsy.recorder.encoder import Encoder, pick_encoder, software_encoder
from glimpsy.recorder.pacing import Plan, make_plan, save_probability
from glimpsy.recorder.ring_buffer import BufferRun
from glimpsy.recorder.audio import AudioCapture, write_wav
from glimpsy.recorder import streams as streams_mod
from glimpsy.recorder.streams import StreamSpec
from glimpsy.recorder.webcam import MODES as CAM_MODES, CamClip, CamStore, Webcam

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
    WAITING = "waiting"          # потоки: впереди не то окно — ждём, пока вернётесь
    ASSEMBLING = "assembling"
    ERROR = "error"


STATE_LABELS = {
    State.STOPPED: "Запись остановлена",
    State.STARTING: "Запуск…",
    State.RECORDING: "Идёт запись",
    State.IDLE: "Пауза: нет активности",
    State.PAUSED: "Пауза",
    State.PRIVATE: "Пауза: приватное приложение",
    State.WAITING: "Пауза: ждёт окна потока",
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
    voice: tuple[int, int] | None = None     # (номер речи, номер куска) — для голосового режима


@dataclass
class _Background:
    """Фоновая запись потока-экрана: пишется одновременно с главной, но без звука и камеры."""
    sid: int
    run: BufferRun
    tracker: ActivityTracker                 # «интересность» только по изменениям картинки этого экрана
    last_save_end: float = 0.0
    active_seconds: float = 0.0
    avg_score: float = 0.0
    last_second: int = 0

    def stop(self) -> None:
        self.run.stop()
        self.run.cleanup()


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
        self.streams: list[StreamSpec] = []      # пусто — весь экран; иначе только эти окна
        self._streams_lock = threading.Lock()
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
        # статистика дня: только для вас, в ролик не попадает
        self.stats = {"active_s": 0, "idle_s": 0, "paused_s": 0, "private_s": 0, "apps": {}, "important": 0}
        self._stats_second = 0
        self.audio: AudioCapture | None = None
        self._voice: dict | None = None          # идущая речь: {"id", "from", "part"}
        self._voice_count = 0
        self._stats_saved = 0.0
        self._cur_app = ""
        self._cam_next = 0.0
        self._cam_seconds = 0.0
        # потоки: у каждого свой набор кандидатов и свои счётчики
        self.pools: dict[int, CandidatePool] = {}
        self._stream = 0                          # 0 — весь экран
        self._stream_names: dict[int, str] = {}
        self._stream_state: dict[int, tuple[float, float, float]] = {}
        self._waiting = False
        self._win_rect: tuple[int, int, int, int] | None = None
        self._rects: list[tuple[float, tuple[int, int, int, int] | None]] = []
        # потоки-экраны пишутся все сразу: «главный» — self.run, остальные — фоновые
        self._bg: dict[int, _Background] = {}
        self._bg_retry: dict[int, float] = {}
        self._screen_active = False               # сейчас главный — поток-экран под курсором
        self._screen_wait = False                 # курсор на экране, который не записывается

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
            self.notify.emit("Glimpsy", "Сначала завершите текущую сессию.")
            return

        def work() -> None:
            self._reset_session_fields()
            self.session_dir = session_dir
            self.session_start = float(_read_json(session_dir / "session.json").get("start", time.time()))
            self._load_pools(session_dir)
            self._assemble()

        self._thread = threading.Thread(target=work, daemon=True, name="assemble")
        self._thread.start()

    def _send(self, *cmd) -> None:
        if self.running and self.state != State.ASSEMBLING:
            self._cmds.put(cmd)
        else:
            self.notify.emit("Glimpsy", "Запись сейчас не идёт.")

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

    def set_streams(self, specs: list[StreamSpec]) -> None:
        """Какие окна записывать (до трёх, каждое — в свой ролик). Пустой список — весь экран."""
        specs = [copy.deepcopy(x) for x in specs[:streams_mod.MAX_STREAMS]]
        with self._streams_lock:
            self.streams = specs
        if self.running:
            self._cmds.put(("streams", specs))

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
                    self.notify.emit("Glimpsy", "Аппаратный видеокодек не найден — используется программный. "
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
            self.notify.emit("Glimpsy: ошибка", str(e))

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
        self._load_pools(self.session_dir)
        self._set_streams(self.streams)
        self.activity = ActivityTracker(self.services.input_events_supported, self.services.input_backend)
        for err in self.activity.start():
            self.notify.emit("Glimpsy", err)
        self.cam_store = CamStore(self.session_dir)
        self._setup_camera()
        self._setup_audio()

    def _handle(self, cmd: tuple) -> str | None:
        kind = cmd[0]
        if kind == "pause":
            self._user_paused = not self._user_paused
            if self._user_paused:
                self._close_run()
                self._bg_stop_all()
                self._cam_stop()
                self._audio_run(False)
                self._set_state(State.PAUSED)
            else:
                if self.activity:
                    self.activity.last_input_time = self.activity.last_screen_change_time = time.time()
                self._set_state(State.RECORDING)
        elif kind == "important":
            self._mark_important(cmd[1])
        elif kind == "settings":
            self._apply_settings(cmd[1])
        elif kind == "streams":
            self._set_streams(cmd[1])
        elif kind == "reselect":
            self._close_run()
            self._bg_stop_all()
            self.services.capture.reset()
            self._monitors = []
        elif kind == "finish":
            self._save_stats()
            self._close_run()
            self._bg_stop_all()
            self._audio_run(False)
            self._cam_finish()
            self._stop_listeners()
            self._assemble()
            return "exit"
        elif kind == "stop":
            self._save_stats()
            self._close_run()
            self._bg_stop_all()
            self._audio_run(False)
            self._cam_stop()
            self._stop_listeners()
            for pool in self.pools.values():
                pool.save()
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
        self._count_stats(now)

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
            self._cur_app = "" if win is None else ("Приватное приложение" if hit else _app_name(win.app))
            self._check_stream(win, now)
            private = hit is not None
            if private != self._private:
                self._private = private
                log.info("Приватное окно: %s (%s)%s", private, win.app if win else "",
                         f" — совпало со словом «{hit}» из чёрного списка" if hit else "")
        if self._private:
            self._close_run()     # в буфер не попадает ни одного кадра приватного окна
            self._bg_stop_all()
            self._cam_stop()
            self._audio_run(False)    # и ни звука (например, звонок в мессенджере)
            self._set_state(State.PRIVATE)
            return

        if self._waiting:
            # потоки: впереди окно, которое не записывается — ждём, пока вы к нему вернётесь
            # (потоки-экраны при этом пишутся дальше)
            self._close_run()
            self._cam_stop()
            self._audio_run(False)
            self._set_state(State.WAITING)
            self._bg_tick(now)
            return

        self._audio_run(True)
        if self.audio is not None and self.audio.voice.speaking().speaking:
            act.last_input_time = now     # вы говорите — значит, вы здесь (не автопауза)
        idle = act.idle_for(now) > self.s.idle_pause_s
        if idle and self.services.input_events_supported:
            # Можем отследить возвращение пользователя по мыши/клавиатуре → запись полностью стоит
            self._close_run()
            self._bg_stop_all()
            self._cam_stop()
            self._set_state(State.IDLE)
            return

        # --- 4. какой монитор снимать ---
        target = self._target_monitor(cursor_mon, now)
        if self._screen_active and target is not None:
            # потоки-экраны: главный — экран под курсором; остальные пишутся в фоне
            spec = streams_mod.screen_for(self._specs(), target.index)
            self._screen_wait = spec is None
            if spec is None:
                self._close_run()
                self._cam_stop()
                self._audio_run(False)
                self._set_state(State.WAITING)
                self._bg_tick(now)
                return
            self._switch_stream(spec.id)
        elif self._stream:        # поток-окно: снимаем тот монитор, где его окно
            target = streams_mod.monitor_for(self._monitors, self._win_rect) or target
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

        # --- 6. голос и отложенные сохранения («важные моменты», концы фраз) ---
        self._voice_tick(now)
        for d in [d for d in self._deferred if d.due <= now]:
            self._deferred.remove(d)
            self._save(d.t0, d.t1, d.priority, d.voice)

        # --- 7. умный рандом: раз в секунду решаем, сохранять ли момент ---
        sec = int(now)
        if sec != self._last_second:
            self._last_second = sec
            if not idle:
                self._active_seconds += 1
                self._cam_seconds += 1
                self._maybe_save(now)
                self._maybe_camera()
        self._bg_tick(now)
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

    # ======================= потоки (только выбранные окна) =======================

    def _load_pools(self, session_dir: Path) -> None:
        self.pools = {0: CandidatePool(session_dir)}
        for d in sorted(session_dir.glob("stream_*")):
            try:
                self.pools[int(d.name.split("_", 1)[1])] = CandidatePool(d)
            except ValueError:
                continue
        for sid, name in _read_json(session_dir / "streams.json").items():
            self._stream_names[int(sid)] = name
        self.pool = self.pools[0]

    def _pool_for(self, sid: int) -> CandidatePool:
        assert self.session_dir is not None
        if sid not in self.pools:
            self.pools[sid] = CandidatePool(self.session_dir / f"stream_{sid}" if sid else self.session_dir)
        return self.pools[sid]

    @property
    def candidate_count(self) -> int:
        return sum(p.count for p in self.pools.values())

    def _set_streams(self, specs: list[StreamSpec]) -> None:
        for sp in specs:
            self._stream_names[sp.id] = sp.name
        if self.session_dir is not None and self._stream_names:
            try:
                (self.session_dir / "streams.json").write_text(
                    json.dumps({str(k): v for k, v in self._stream_names.items()}, ensure_ascii=False),
                    encoding="utf-8")
            except OSError:
                log.exception("Не удалось сохранить список потоков")
        if specs:
            log.info("Потоки: %s", ", ".join(f"«{sp.name}» ({sp.mode})" for sp in specs))
        else:
            log.info("Потоки выключены — снимается весь экран")
            self._waiting = False
            self._switch_stream(0)
        self._privacy_at = 0.0          # сразу проверить, какое окно впереди

    def _check_stream(self, win, now: float) -> None:
        """Какой поток сейчас впереди. Нет подходящего окна — запись ждёт."""
        specs = self._specs()
        self._screen_active = False
        if not specs:
            self._waiting = False
            return
        spec = streams_mod.match(specs, win)
        if spec is None and any(sp.is_screen for sp in specs) and all(sp.is_screen for sp in specs):
            # только экраны: главный (со звуком и камерой) — экран под курсором, решается в _tick
            self._waiting = False
            self._screen_active = True
            return
        # есть потоки-окна: экраны пишутся только в фоне — картинка без звука и камеры,
        # голос и камера идут в ролик окна, которое впереди
        self._waiting = spec is None
        if spec is None:
            return
        self._switch_stream(spec.id)
        self._win_rect = win.rect if win else None
        self._rects.append((now, self._win_rect))
        cutoff = now - self.s.buffer_s - 10
        while self._rects and self._rects[0][0] < cutoff:
            self._rects.pop(0)

    def _specs(self) -> list[StreamSpec]:
        with self._streams_lock:
            return list(self.streams)

    def _screen_ids(self) -> set[int]:
        return {sp.id for sp in self._specs() if sp.is_screen}

    def _switch_stream(self, sid: int) -> None:
        if sid == self._stream or self.session_dir is None:
            return
        old, run, incoming = self._stream, self.run, self._bg.pop(sid, None)
        if run is not None and old in self._screen_ids() and run.alive and self.activity is not None:
            # экран, с которого ушли, не останавливается — его запись уходит в фон без разрыва
            self._flush_run(run, time.time())
            bg = _Background(old, run, ActivityTracker(False), self._last_save_end,
                             self._active_seconds, self._avg_score)
            run.on_frame_diff = bg.tracker.add_frame_diff
            self._bg[old] = bg
            self.run = None
        else:
            self._close_run()            # фрагмент прошлого окна заканчивается здесь
        self._stream_state[old] = (self._last_save_end, self._active_seconds, self._avg_score)
        self._stream = sid
        self._last_save_end, self._active_seconds, self._avg_score = self._stream_state.get(sid, (0.0, 0.0, 0.0))
        if incoming is not None and incoming.run.alive and self.activity is not None:
            # а фоновая запись нового экрана становится главной — тоже без разрыва
            self.run = incoming.run
            self.run.on_frame_diff = self.activity.add_frame_diff
            self._last_save_end, self._active_seconds, self._avg_score = \
                incoming.last_save_end, incoming.active_seconds, incoming.avg_score
        elif incoming is not None:
            incoming.stop()
        self.pool = self._pool_for(sid)
        self._rects = []
        self._win_rect = None
        self._pending_monitor = None
        log.info("Запись переключилась на %s", f"поток «{self._stream_names.get(sid, sid)}»" if sid else "весь экран")

    # ---------- фоновые записи экранов ----------

    BG_RETRY_S = 10.0

    def _bg_stop_all(self) -> None:
        for bg in self._bg.values():
            bg.stop()
        self._bg.clear()

    def _bg_tick(self, now: float) -> None:
        """Все потоки-экраны, кроме главного, пишутся в фоне; раз в секунду — может, сохранить момент."""
        mons = {m.index: m for m in self._monitors}
        main = self._stream if self.run is not None else None
        want = {sp.id: mons.get(sp.monitor) for sp in self._specs() if sp.is_screen and sp.id != main}
        for sid in [s for s in self._bg if s not in want or want[s] is None or want[s] != self._bg[s].run.monitor]:
            self._bg.pop(sid).stop()
        for sid, mon in want.items():
            if mon is None:
                continue
            bg = self._bg.get(sid)
            if bg is not None and not bg.run.alive:
                log.warning("Фоновая запись «%s» остановилась:\n%s", self._stream_names.get(sid, sid),
                            bg.run.error_text()[-500:])
                self._bg.pop(sid).stop()
                self._bg_retry[sid] = now + self.BG_RETRY_S
                continue
            if bg is None:
                if now >= self._bg_retry.get(sid, 0.0):
                    self._bg_start(sid, mon, now)
                continue
            bg.run.poll()
            bg.run.trim(self.s.buffer_s + 2)
            sec = int(now)
            if sec != bg.last_second:
                bg.last_second = sec
                bg.active_seconds += 1
                self._bg_maybe_save(bg, now)

    def _bg_start(self, sid: int, mon: Monitor, now: float) -> None:
        assert self.session_dir and self.encoder
        self._run_counter += 1
        capture = self.services.capture
        try:
            cap = capture.input_for(mon, self.s.fps)
        except Exception:
            log.exception("Фоновая запись экрана %s не подготовилась", mon.label)
            self._bg_retry[sid] = now + self.BG_RETRY_S
            return
        tracker = ActivityTracker(False)
        run = BufferRun(self.ffmpeg, cap, mon, self.encoder, self.s.fps,
                        self.session_dir / "buffer" / f"run_{self._run_counter:04d}",
                        self.s.record_max_height, on_frame_diff=tracker.add_frame_diff)
        try:
            run.start()
        except Exception:
            log.exception("Фоновая запись экрана %s не запустилась", mon.label)
            run.cleanup()
            self._bg_retry[sid] = now + self.BG_RETRY_S
            return
        run.own_cursor = capture.cursor_hidden
        last_end, active, avg = self._stream_state.get(sid, (0.0, 0.0, 0.0))
        self._bg[sid] = _Background(sid, run, tracker, last_end, active, avg)
        log.info("Фоновая запись: экран %s → поток «%s»", mon.label, self._stream_names.get(sid, sid))

    def _bg_maybe_save(self, bg: _Background, now: float) -> None:
        run, L = bg.run, self.plan.candidate_s
        if run.available_from is None or now - run.available_from < L * 0.6 or now - bg.last_save_end < L:
            return
        recent = bg.tracker.score(now - L, now)
        bg.avg_score = recent if bg.avg_score == 0 else 0.98 * bg.avg_score + 0.02 * recent
        if self._rng.random() < save_probability(self.plan, bg.active_seconds, recent, bg.avg_score):
            self._bg_save(bg, now - L, now)

    def _bg_save(self, bg: _Background, t0: float, t1: float) -> None:
        act, run = self.activity, bg.run
        if act is None or self.session_dir is None:
            return
        pool = self._pool_for(bg.sid)
        run.poll()
        cid, path = pool.new_file()
        try:
            got = run.save_clip(t0, t1, path)
        except OSError:
            log.exception("Не удалось сохранить фоновый фрагмент")
            got = None
        if got is None or (got[1] - got[0]) < self.plan.clip_min_s * self.plan.speed * 0.8:
            path.unlink(missing_ok=True)
            return
        ws, we = got
        mi = run.monitor.index
        pool.add(Candidate(
            id=cid, file=path.name, wall_start=ws, wall_end=we, want_start=max(t0, ws), want_end=min(t1, we),
            monitor=mi, width=run.capture.width, height=run.capture.height,
            score=bg.tracker.score(max(t0, ws), min(t1, we)),
            activity=[round(v, 3) for v in bg.tracker.per_second(ws, we)],
            cursor=[[round(t - ws, 2), round(x, 4), round(y, 4)] for t, m, x, y in act.cursor_between(ws, we)
                    if m == mi],
            clicks=[[round(t - ws, 2), round(x, 4), round(y, 4)] for t, x, y in act.clicks_between(ws, we, mi)],
            own_cursor=run.own_cursor,
        ))
        removed = pool.prune(self.plan.pool_size)
        pool.save()
        bg.last_save_end = max(bg.last_save_end, we)
        log.info("Кандидат #%s: %.1f с, фон, поток «%s» (удалено %s)", cid, we - ws,
                 self._stream_names.get(bg.sid, bg.sid), len(removed))

    def _crop(self, mon: Monitor, t0: float, t1: float) -> list[float] | None:
        rects = [r for t, r in self._rects if r and t0 - 1 <= t <= t1 + 1]
        if not rects and self._win_rect:
            rects = [self._win_rect]
        return streams_mod.crop_fraction(rects, mon)

    # ======================= буфер =======================

    def _start_run(self, monitor: Monitor) -> None:
        assert self.session_dir and self.activity and self.encoder
        self._run_counter += 1
        capture = self.services.capture
        # плавный курсор: снимаем без системного, если знаем, где курсор (на Wayland — нет)
        capture.hide_cursor = self.s.smooth_cursor and self.services.cursor.supported
        try:
            cap = capture.input_for(monitor, self.s.fps)
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
        run.own_cursor = capture.cursor_hidden
        self.run = run
        log.info("Запись монитора %s%s", monitor.label, " (курсор рисует Glimpsy)" if run.own_cursor else "")

    def _close_run(self) -> None:
        """Останавливает текущий прогон. Недописанные «важные моменты» сохраняются тем, что есть."""
        run = self.run
        if run is None:
            return
        self.run = None
        run.stop()
        self._flush_run(run, run.ended_at or time.time())
        run.cleanup()

    def _flush_run(self, run: BufferRun, end: float) -> None:
        """Главная запись заканчивается (или уходит в фон): сохранить «важные моменты» и речь."""
        for d in list(self._deferred):
            self._deferred.remove(d)
            self._save_from(run, d.t0, min(d.t1, end), d.priority, d.voice)
        if self._voice is not None:
            # речь продолжается, а прогон закончился (другой монитор, пауза) — сохраняем сказанное
            v = self._voice
            got = self._save_from(run, v["from"], end, True, (v["id"], v["part"]))
            v["part"] += 1
            v["from"] = got[1] if got else end
            if self._user_paused or self._private:
                self._voice = None

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
            from glimpsy.platform.capture import GdiGrabCapture
            self.services.capture = GdiGrabCapture()
            self.notify.emit("Glimpsy", "Быстрый захват (ddagrab) не работает, переключаюсь на gdigrab.")
        elif self._fail_count == 3 and self.encoder and self.encoder.hw:
            self.encoder = software_encoder()
            self.notify.emit("Glimpsy", "Аппаратный кодек сбоит, переключаюсь на программный.")
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
            self.notify.emit("Glimpsy", "Аппаратный кодек не подошёл для буфера, "
                                          "переключаюсь на программный.")
        else:
            self._fail("Запись не делится на кусочки. Подробности — в журнале.", time.time())

    def _fail(self, message: str, now: float) -> None:
        self._error = message
        self._retry_after = now + 30
        if self.state != State.ERROR:
            self.notify.emit("Glimpsy: проблема с записью", message)
        self._set_state(State.ERROR)

    # ======================= сохранение кандидатов =======================

    def _count_stats(self, now: float) -> None:
        """Раз в секунду: чем была занята эта секунда (для вкладки «Статистика»)."""
        sec = int(now)
        if sec == self._stats_second or self.activity is None:
            return
        self._stats_second = sec
        st = self.stats
        if self._user_paused:
            st["paused_s"] += 1
        elif self._private:
            st["private_s"] += 1
        elif self.activity.idle_for(now) > self.s.idle_pause_s:
            st["idle_s"] += 1
        else:
            st["active_s"] += 1
            if self._cur_app:
                st["apps"][self._cur_app] = st["apps"].get(self._cur_app, 0) + 1
        if now - self._stats_saved > 30:
            self._save_stats(now)

    def _save_stats(self, now: float | None = None) -> None:
        if self.session_dir is None or self.activity is None:
            return
        now = now or time.time()
        self._stats_saved = now
        data = dict(self.stats, start=self.session_start, end=now, **{
            k: round(v) for k, v in self.activity.totals.items()})
        try:
            (self.session_dir / "stats.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            log.exception("Не удалось сохранить статистику")

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

    def _save(self, t0: float, t1: float, priority: bool, voice: tuple[int, int] | None = None):
        if self.run is not None:
            return self._save_from(self.run, t0, t1, priority, voice)
        return None

    def _save_from(self, run: BufferRun, t0: float, t1: float, priority: bool,
                   voice: tuple[int, int] | None = None) -> tuple[float, float] | None:
        """Сохранить фрагмент [t0, t1] из буфера. Возвращает реальное время (начало, конец) или None."""
        pool, act = self.pool, self.activity
        if pool is None or act is None:
            return None
        run.poll()
        cid, path = pool.new_file()
        try:
            got = run.save_clip(t0, t1, path)
        except OSError:
            log.exception("Не удалось сохранить фрагмент")
            got = None
        min_len = 0.8 if voice else self.plan.clip_min_s * self.plan.speed
        if got is None or (got[1] - got[0]) < min_len * 0.8:
            path.unlink(missing_ok=True)
            if priority and not voice:
                self.notify.emit("Glimpsy", "Важный момент слишком короткий — не сохранён.")
            return None
        ws, we = got
        audio_name = ""
        if self.audio is not None:
            samples = self.audio.extract(ws, we)
            if samples is not None and len(samples) and float(abs(samples).max()) > 1e-4:
                audio_name = path.with_suffix(".wav").name
                try:
                    write_wav(path.with_suffix(".wav"), samples)
                except OSError:
                    log.exception("Не удалось сохранить звук")
                    audio_name = ""
        crop = self._crop(run.monitor, ws, we) if self._stream and self._stream not in self._screen_ids() else None
        cursor = streams_mod.to_crop([[round(t - ws, 2), round(x, 4), round(y, 4)]
                                      for t, m, x, y in act.cursor_between(ws, we) if m == run.monitor.index], crop)
        clicks = streams_mod.to_crop([[round(t - ws, 2), round(x, 4), round(y, 4)]
                                      for t, x, y in act.clicks_between(ws, we, run.monitor.index)], crop)
        cand = Candidate(
            id=cid, file=path.name, wall_start=ws, wall_end=we,
            want_start=max(t0, ws), want_end=min(t1, we), monitor=run.monitor.index,
            width=run.capture.width, height=run.capture.height,
            score=act.score(max(t0, ws), min(t1, we)), priority=priority,
            activity=[round(v, 3) for v in act.per_second(ws, we)], cursor=cursor,
            clicks=clicks, crop=crop or [], own_cursor=run.own_cursor,
            audio=audio_name, voice_id=voice[0] if voice else 0, voice_part=voice[1] if voice else 0,
        )
        pool.add(cand)
        removed = pool.prune(self.plan.pool_size)
        pool.save()
        self._last_save_end = max(self._last_save_end, we)
        log.info("Кандидат #%s: %.1f с, оценка %.2f%s%s%s%s (удалено %s)", cid, we - ws, cand.score,
                 ", ВАЖНЫЙ" if priority and not voice else "", f", РЕЧЬ {voice[0]}.{voice[1]}" if voice else "",
                 ", со звуком" if audio_name else "",
                 f", поток «{self._stream_names.get(self._stream, '')}»" if self._stream else "", len(removed))
        self._emit_status(time.time(), force=True)
        return ws, we

    def _mark_important(self, t: float) -> None:
        if self._user_paused:
            self.notify.emit("Glimpsy", "Запись на паузе — важный момент не сохранён.")
            return
        if self._private:
            self.notify.emit("Glimpsy", "Открыто приватное приложение — момент не сохранён.")
            return
        if self._waiting:
            self.notify.emit("Glimpsy", "Впереди окно, которое не записывается, — момент не сохранён.")
            return
        if self.activity:
            self.activity.last_input_time = t   # пользователь точно активен
        before, after = self.s.important_before_s, self.s.important_after_s
        self._deferred.append(Deferred(due=t + after + 1.2, t0=t - before, t1=t + after, priority=True))
        self.stats["important"] += 1
        self.notify.emit("Glimpsy", f"⭐ Важный момент отмечен (−{before} с / +{after} с)")

    # ======================= звук и голос =======================

    VOICE_CHUNK_S = 15.0      # длинная речь сохраняется кусками, чтобы не выпасть из буфера

    def _setup_audio(self) -> None:
        self._audio_run(False)
        self.audio = None
        if not (self.s.audio_mic or self.s.audio_system):
            return
        self.audio = AudioCapture(self.s.buffer_s, mic=self.s.audio_mic, mic_device=self.s.audio_mic_device,
                                  system=self.s.audio_system, sensitivity=self.s.voice_sensitivity,
                                  sources=self.audio_sources_override)

    audio_sources_override: dict | None = None      # для проверок: «рекордеры» без настоящих устройств

    def _audio_run(self, on: bool) -> None:
        a = self.audio
        if a is None:
            return
        if on and not a.running:
            for problem in a.start():
                self.notify.emit("Glimpsy", f"Звук: {problem}")
        elif not on and a.running:
            a.stop()

    def _voice_tick(self, now: float) -> None:
        """Голосовой режим: пока вы говорите — запись идёт целиком, кусками по 15 с."""
        a = self.audio
        if a is None or not self.s.voice_mode or a.mic_ring is None or self.run is None:
            return
        st = a.voice.speaking()
        if st.speaking and self._voice is None:
            self._voice_count += 1
            start = st.start
            if self.run.available_from is not None:
                start = max(start, self.run.available_from)
            self._voice = {"id": self._voice_count, "from": start, "part": 0}
            self.stats["voice_count"] = self.stats.get("voice_count", 0) + 1
            log.info("Речь №%s началась", self._voice_count)
        v = self._voice
        if v is not None and st.speaking and now - v["from"] >= self.VOICE_CHUNK_S:
            got = self._save(v["from"], now - 0.5, True, (v["id"], v["part"]))
            v["part"] += 1
            v["from"] = got[1] if got else now - 0.5
        for _start, end in a.voice.pop_finished():
            if v is None:
                continue
            # конец фразы — сохраняем чуть позже, когда эти секунды точно окажутся в буфере
            self._deferred.append(Deferred(due=end + 1.5, t0=v["from"], t1=end, priority=True,
                                           voice=(v["id"], v["part"])))
            self.stats["voice_s"] = self.stats.get("voice_s", 0) + round(end - _start)
            log.info("Речь №%s закончилась (%.1f с)", v["id"], end - _start)
            self._voice = v = None

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
        self._cam_next = self._cam_seconds + interval * self._rng.uniform(0.15, 0.5)

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
                self._cam_next = self._cam_seconds + interval * self._rng.uniform(0.6, 1.4)
            elif got is False:
                if cam.gave_up:
                    self.notify.emit("Glimpsy", "Не получилось снять с веб-камеры (возможно, она занята "
                                                  "другой программой или нет разрешения). До конца сессии "
                                                  "камера больше не включается.")
                    self.cam = None
                else:
                    self._cam_next = self._cam_seconds + 30
            return
        if self._cam_seconds >= self._cam_next:
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
        assert self.session_dir is not None
        self._set_state(State.ASSEMBLING)
        pools = {sid: p for sid, p in sorted(self.pools.items()) if p.items}
        if not pools:
            self.assembly_failed.emit("За эту сессию не сохранилось ни одного фрагмента.")
            shutil.rmtree(self.session_dir, ignore_errors=True)
            self._set_state(State.STOPPED)
            return
        encoder = self.encoder or pick_encoder(self.ffmpeg, self.s.encoder, self.s.fps)
        base = self.session_dir.name.replace("session_", "project_")
        done: list[str] = []
        errors: list[str] = []
        for n, (sid, pool) in enumerate(pools.items()):
            # у каждого потока — свой ролик и свой проект в редакторе
            name = self._stream_names.get(sid, f"Поток {sid}") if sid else ("весь экран" if len(pools) > 1 else "")
            project_dir = None
            if self.s.keep_project_for_editor:
                project_dir = paths.data_dir() / "projects" / (base + (f"_{sid}" if sid else ""))
            prefix = f"«{name}» ({n + 1} из {len(pools)}): " if len(pools) > 1 else ""

            def progress(f: float, t: str, n=n, prefix=prefix) -> None:
                self.assembly_progress.emit((n + f) / len(pools), prefix + t)

            try:
                out = Assembler(self.ffmpeg, encoder, self.s, self.plan).run(
                    pool.dir, pool.items, self.session_start, progress=progress, project_dir=project_dir,
                    name=name if len(pools) > 1 or sid else "", shared_dir=self.session_dir,
                )
            except Exception as e:
                log.exception("Сборка не удалась (%s)", name or "весь экран")
                errors.append(f"{name}: {e}" if name else str(e))
                continue
            done.append(str(out))
            # собранный поток повторно не собираем, даже если другой не получился
            pool.index_path.unlink(missing_ok=True)
        if errors and not done:
            # черновики не удаляем — можно будет попробовать ещё раз после перезапуска
            self.assembly_failed.emit("\n".join(errors))
            self._set_state(State.STOPPED)
            return
        if errors:
            self.notify.emit("Glimpsy: собрано не всё", "\n".join(errors))
        else:
            shutil.rmtree(self.session_dir, ignore_errors=True)   # буфер и черновики больше не нужны
        self._set_state(State.STOPPED)
        self.assembly_done.emit("\n".join(done))

    # ======================= разное =======================

    def _apply_settings(self, s: Settings) -> None:
        old = self.s
        restart = (s.fps != self.s.fps or s.encoder != self.s.encoder or
                   s.record_max_height != self.s.record_max_height or
                   s.monitor_mode != self.s.monitor_mode or s.manual_monitor != self.s.manual_monitor or
                   s.smooth_cursor != self.s.smooth_cursor)
        if s.encoder != self.s.encoder:
            self.encoder = pick_encoder(self.ffmpeg, s.encoder, s.fps)
        self.s = s
        self.plan = make_plan(s)
        if restart:
            self._close_run()
        camera_changed = s.camera_mode != old.camera_mode or s.camera_device != old.camera_device
        audio_changed = (s.audio_mic, s.audio_mic_device, s.audio_system, s.voice_sensitivity) != \
            (old.audio_mic, old.audio_mic_device, old.audio_system, old.voice_sensitivity)
        for pool in self.pools.values():
            pool.prune(self.plan.pool_size)
            pool.save()
        if camera_changed and self.cam_store is not None:
            self._setup_camera()
        if audio_changed and self.cam_store is not None:
            self._setup_audio()

    def _stop_listeners(self) -> None:
        if self.activity:
            self.activity.stop()

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self._emit_status(time.time(), force=True)

    def _emit_status(self, now: float, force: bool = False) -> None:
        # пишется речь — красная точка на значке; появляется и пропадает сразу, без задержки
        voice = self._voice is not None and self.state == State.RECORDING
        if not force and now - self._status_at < 2 and voice == self._last_status.get("voice", False):
            return
        self._status_at = now
        pool = self.pool
        st = {
            "state": self.state,
            "label": STATE_LABELS.get(self.state, self.state),
            "monitor": self.run.monitor.label if self.run else "",
            "stream": self._stream_names.get(self._stream, "") if self._stream else "",
            "candidates": pool.count if pool else 0,
            "important": sum(1 for c in pool.items if c.priority) if pool else 0,
            "needed": self.plan.clips_needed,
            "encoder": self.encoder.label if self.encoder else "",
            "capture": self.services.capture.name,
            "error": self._error if self.state == State.ERROR else "",
            "voice": voice,
            "parallel": [self._stream_names.get(s, str(s)) for s in sorted(self._bg)],
        }
        if st != self._last_status or force:
            self._last_status = st
            self.status_changed.emit(st)


def _app_name(app: str) -> str:
    """«chrome.exe» → «Chrome», «/usr/bin/krita» → «Krita»."""
    name = Path(app or "").name
    for ext in (".exe", ".app", ".bin"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
    return name[:1].upper() + name[1:] if name else ""


def find_unfinished_sessions() -> list[Path]:
    """Сессии, оставшиеся после аварийного закрытия или выхода без сборки."""
    out = []
    for d in sorted(paths.temp_root().glob("session_*")):
        indexes = [d / "candidates.json", *d.glob("stream_*/candidates.json")]
        if any(_read_json(i).get("items") for i in indexes):
            out.append(d)
        elif d.is_dir():
            shutil.rmtree(d, ignore_errors=True)   # пустые — просто убираем
    return out


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
