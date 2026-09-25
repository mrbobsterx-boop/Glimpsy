"""Настройки приложения. Хранятся в JSON-файле в папке настроек пользователя."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from glimpsy import paths

log = logging.getLogger(__name__)

PACES = ("calm", "medium", "dynamic")
PACE_LABELS = {"calm": "Спокойный", "medium": "Средний", "dynamic": "Динамичный"}

DEFAULT_BLACKLIST = [
    # мессенджеры
    "telegram", "whatsapp", "signal", "viber", "discord", "messenger",
    # почта
    "outlook", "thunderbird", "mail", "почта", "gmail",
    # менеджеры паролей
    "1password", "bitwarden", "keepass", "lastpass", "dashlane", "keychain",
    # банки
    "bank", "банк", "сбербанк", "тинькофф", "т-банк", "paypal",
]


@dataclass
class Settings:
    # --- Итоговый ролик ---
    output_dir: str = field(default_factory=lambda: str(paths.default_output_dir()))
    target_length_s: int = 60          # желаемая длина ролика
    clip_min_s: float = 2.0            # минимальная длина одного фрагмента в ролике
    clip_max_s: float = 5.0            # максимальная длина одного фрагмента в ролике
    pace: str = "medium"               # calm / medium / dynamic
    output_width: int = 1920
    output_height: int = 1080
    oversample: float = 2.5            # во сколько раз больше кандидатов хранить, чем нужно
    fx_zoom: bool = True               # плавно приближать кадр к кликам и месту работы
    fx_zoom_strength: float = 1.8      # во сколько раз приближать
    fx_clicks: bool = True             # подсвечивать клики расходящимся кругом

    # --- Запись ---
    fps: int = 30
    buffer_s: int = 30                 # длина кольцевого буфера
    idle_pause_s: int = 60             # пауза, если нет активности столько секунд
    record_max_height: int = 1440      # экраны выше этого уменьшаются при записи (экономия)
    encoder: str = "auto"              # auto или конкретный кодек, например h264_nvenc
    capture_backend: str = "auto"      # auto / ddagrab / gdigrab (Windows)
    monitor_mode: str = "auto"         # auto = монитор под мышкой, manual = выбранный
    manual_monitor: int = 1            # номер монитора (с 1) для ручного режима
    autostart_recording: bool = True   # начинать запись сразу при запуске программы
    launch_at_login: bool = False      # запускать вместе с компьютером (реально хранится в системе)

    # --- Веб-камера (по умолчанию выключена: камера не включается без вашего согласия) ---
    camera_mode: str = "off"           # off / rare / sometimes / often
    camera_device: str = ""            # пусто — первая найденная камера
    camera_clip_s: float = 4.0         # длина одного фрагмента с камеры

    # --- Горячие клавиши ---
    # Цифры одинаковы в любой раскладке и почти не заняты другими программами
    # (Ctrl+Alt+S, например, во многих программах — «Сохранить»)
    hotkey_important: str = "Ctrl+Alt+1"
    hotkey_pause: str = "Ctrl+Alt+2"
    hotkey_finish: str = "Ctrl+Alt+3"
    important_before_s: int = 10
    important_after_s: int = 5

    # --- Приватность ---
    blacklist: list[str] = field(default_factory=lambda: list(DEFAULT_BLACKLIST))

    # --- Служебное ---
    wayland_restore_token: str = ""    # чтобы Wayland не спрашивал разрешение каждый раз
    keep_project_for_editor: bool = True
    shown_limitations: list[str] = field(default_factory=list)
    welcome_shown: bool = False

    def validate(self) -> "Settings":
        """Приводит значения в разумные рамки, чтобы опечатка не сломала запись."""
        # старые сочетания по умолчанию (Ctrl+Alt+S/P/E) мешали другим программам — меняем на новые
        old = {"hotkey_important": "Ctrl+Alt+S", "hotkey_pause": "Ctrl+Alt+P", "hotkey_finish": "Ctrl+Alt+E"}
        if all(getattr(self, k) == v for k, v in old.items()):
            self.hotkey_important, self.hotkey_pause, self.hotkey_finish = "Ctrl+Alt+1", "Ctrl+Alt+2", "Ctrl+Alt+3"
        self.target_length_s = max(10, min(int(self.target_length_s), 3600))
        self.clip_min_s = max(0.5, min(float(self.clip_min_s), 30.0))
        self.clip_max_s = max(self.clip_min_s, min(float(self.clip_max_s), 30.0))
        if self.pace not in PACES:
            self.pace = "medium"
        self.fps = max(5, min(int(self.fps), 60))
        self.important_before_s = max(1, min(int(self.important_before_s), 60))
        self.important_after_s = max(0, min(int(self.important_after_s), 30))
        # Буфер должен вмещать «важный момент» и самый длинный фрагмент с запасом
        need = max(self.important_before_s + self.important_after_s, self.clip_max_s * 2) + 3
        self.buffer_s = max(int(need), min(int(self.buffer_s), 300))
        self.idle_pause_s = max(10, min(int(self.idle_pause_s), 3600))
        self.oversample = max(1.5, min(float(self.oversample), 4.0))
        self.fx_zoom_strength = max(1.2, min(float(self.fx_zoom_strength), 3.0))
        self.record_max_height = max(480, min(int(self.record_max_height), 4320))
        if self.camera_mode not in ("off", "rare", "sometimes", "often"):
            self.camera_mode = "off"
        self.camera_clip_s = max(2.0, min(float(self.camera_clip_s), 10.0))
        self.blacklist = [s.strip() for s in self.blacklist if s and s.strip()]
        return self


def settings_path() -> Path:
    return paths.config_dir() / "settings.json"


def load_settings(path: Path | None = None) -> Settings:
    path = path or settings_path()
    s = Settings()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            known = {f.name for f in fields(Settings)}
            for k, v in data.items():
                if k in known:
                    setattr(s, k, v)
        except Exception:  # повреждённый файл не должен мешать запуску
            log.exception("Не удалось прочитать настройки, используются значения по умолчанию")
    return s.validate()


def save_settings(s: Settings, path: Path | None = None) -> None:
    path = path or settings_path()
    s.validate()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(s), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
