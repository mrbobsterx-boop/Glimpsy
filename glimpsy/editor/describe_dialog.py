"""Окно «Описать фрагменты»: что сделать (кадры, речь, Claude) и ход работы.

Работает для одного проекта (из редактора) или для всех записей сразу (из списка записей).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton, QVBoxLayout,
)

from glimpsy.editor import describe as ds
from glimpsy.ui import theme

log = logging.getLogger(__name__)

KEY_SETTING = "ai/anthropic_key"
KEYS_URL = "https://console.anthropic.com/settings/keys"


class _Bridge(QObject):
    progress = Signal(float, str)
    done = Signal(int, object)          # фрагментов, предупреждения


class DescribeDialog(QDialog):
    """project_dirs — какие проекты описать. finished_ok — после конца (для обновления редактора)."""

    finished_ok = Signal()

    def __init__(self, ffmpeg: str, project_dirs: list[Path], parent=None) -> None:
        super().__init__(parent)
        self.ffmpeg = ffmpeg
        self.dirs = list(project_dirs)
        self.cancel = threading.Event()
        self.running = False
        self._dl = None
        self.setWindowTitle("Описать фрагменты")
        self.setMinimumWidth(520)
        st = QSettings("Glimpsy", "editor")
        many = len(self.dirs) > 1
        intro = QLabel(("Для всех записей " + f"({len(self.dirs)})" if many else "Для каждого фрагмента этого проекта")
                       + " программа подготовит то, из чего складываются название и описание. Своё описание, "
                       "если вы его написали, не меняется. Всё попадёт в файл fragments.md в папке записи.")
        intro.setWordWrap(True)
        self.frames = QCheckBox("Кадр-картинка из середины каждого фрагмента")
        self.frames.setToolTip("Картинка лежит рядом с фрагментами (папка frames) и видна в fragments.md — "
                               "можно отдать папку нейросети вместе со сценарием.")
        self.frames.setChecked(True)
        self.speech = QCheckBox("Распознать речь (что вы говорили) — прямо на компьютере")
        self.speech.setChecked(st.value("describe/speech", True, type=bool))
        if not ds.speech_available():
            self.speech.setChecked(False)
            self.speech.setEnabled(False)
            self.speech.setText("Распознать речь — в этой сборке нет программы распознавания")
        self.ai = QCheckBox("Описать картинку через Claude (нужен свой ключ API, это платно — копейки за ролик)")
        self.ai.setChecked(st.value("describe/ai", False, type=bool))
        self.key = QLineEdit(st.value(KEY_SETTING, "", type=str))
        self.key.setPlaceholderText("Ключ API Anthropic: sk-ant-…")
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        link = QLabel(f'<a href="{KEYS_URL}">Где взять ключ</a>')
        link.setOpenExternalLinks(True)
        key_row = QHBoxLayout()
        key_row.addWidget(self.key, 1)
        key_row.addWidget(link)
        self.ai_hint = theme.mark(QLabel("Кадры отправляются в Anthropic. Если на них пароли, почта или другое "
                                         "личное — сначала закройте это размытием или не включайте этот пункт."),
                                  "muted")
        self.ai_hint.setWordWrap(True)
        self.ai.toggled.connect(self._sync)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setVisible(False)
        self.go = theme.mark(QPushButton("Описать"), "primary")
        self.go.clicked.connect(self.start)
        self.close_btn = theme.mark(QPushButton("Закрыть"), "ghost")
        self.close_btn.clicked.connect(self._close)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.close_btn)
        buttons.addWidget(self.go)
        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addSpacing(6)
        lay.addWidget(self.frames)
        lay.addWidget(self.speech)
        lay.addWidget(self.ai)
        lay.addLayout(key_row)
        lay.addWidget(self.ai_hint)
        lay.addSpacing(6)
        lay.addWidget(self.status)
        lay.addWidget(self.bar)
        lay.addLayout(buttons)
        self._sync()

    def _sync(self) -> None:
        on = self.ai.isChecked()
        self.key.setEnabled(on)
        self.ai_hint.setVisible(on)

    def options(self) -> ds.Options:
        return ds.Options(frames=self.frames.isChecked(), speech=self.speech.isChecked(),
                          ai_key=self.key.text().strip() if self.ai.isChecked() else "")

    # ---------- работа ----------

    def start(self) -> None:
        opts = self.options()
        if self.ai.isChecked() and not opts.ai_key:
            self.status.setText("Вставьте ключ API или выключите пункт про Claude.")
            return
        st = QSettings("Glimpsy", "editor")
        st.setValue("describe/speech", self.speech.isChecked())
        st.setValue("describe/ai", self.ai.isChecked())
        st.setValue(KEY_SETTING, self.key.text().strip())
        for w in (self.frames, self.speech, self.ai, self.key, self.go):
            w.setEnabled(False)
        self.bar.setVisible(True)
        self.running = True
        self.close_btn.setText("Остановить")
        if opts.speech and ds.speech_model() is None:
            self._download_model(opts)
            return
        self._run(opts)

    def _download_model(self, opts: ds.Options) -> None:
        """Модели распознавания ещё нет — скачать маленькую (один раз)."""
        from glimpsy.editor import subtitles as subs
        from glimpsy.editor.subtitles_dialog import Downloader

        key = ds.SPEECH_DEFAULT
        _file, url, mb, _desc = subs.MODELS[key]
        self.status.setText(f"Скачиваю модель распознавания речи (≈{mb} МБ, один раз)…")
        self._dl = Downloader(self)
        self._dl.progress.connect(lambda got, total: self.bar.setValue(int(got * 1000 / max(1, total or mb << 20))))

        def finished(err: str) -> None:
            self._dl = None
            if err:
                self.status.setText("Модель не скачалась — описываю без речи. " + err)
                opts.speech = False
            self._run(opts)

        self._dl.finished.connect(finished)
        self._dl.start([(url, subs.model_path(key))])

    def _run(self, opts: ds.Options) -> None:
        bridge = self._bridge = _Bridge(self)
        bridge.progress.connect(self._on_progress)
        bridge.done.connect(self._on_done)
        dirs, ffmpeg, cancel = list(self.dirs), self.ffmpeg, self.cancel

        def work() -> None:
            total, warnings = 0, []
            for n, d in enumerate(dirs):
                if cancel.is_set():
                    break
                prefix = f"Запись {n + 1} из {len(dirs)}: " if len(dirs) > 1 else ""
                try:
                    count, warn = ds.describe_project(
                        ffmpeg, d, opts, lambda f, t, n=n, p=prefix: bridge.progress.emit((n + f) / len(dirs), p + t),
                        cancel)
                    total += count
                    warnings += warn
                    if any(w.startswith("Claude:") for w in warn):
                        opts.ai_key = ""                    # ключ не работает — дальше без Claude
                except Exception as e:
                    log.exception("Не удалось описать %s", d)
                    warnings.append(f"{d.name}: {e}")
            bridge.done.emit(total, warnings)

        threading.Thread(target=work, daemon=True, name="describe").start()

    def _on_progress(self, f: float, text: str) -> None:
        self.bar.setValue(int(max(0.0, min(1.0, f)) * 1000))
        self.status.setText(text)

    def _on_done(self, count: int, warnings: list) -> None:
        self.running = False
        self.bar.setValue(1000)
        uniq = list(dict.fromkeys(warnings))
        text = ("Остановлено. " if self.cancel.is_set() else "Готово. ") + f"Описано фрагментов: {count}."
        if uniq:
            text += "\n\nНе всё получилось:\n" + "\n".join(uniq[:6]) + ("\n…" if len(uniq) > 6 else "")
        self.status.setText(text)
        self.close_btn.setText("Закрыть")
        self.finished_ok.emit()

    def _close(self) -> None:
        if self.running:
            self.cancel.set()
            if self._dl is not None:
                self._dl.cancel()
            self.status.setText("Останавливаю…")
            return
        self.accept()

    def reject(self) -> None:                       # Esc и крестик — пока идёт работа, только останавливают
        if self.running:
            self._close()
        else:
            super().reject()
