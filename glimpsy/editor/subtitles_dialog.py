"""Окно «Автосубтитры»: язык, модель, длина строк и загрузка модели при первом запуске."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QProgressBar, QVBoxLayout,
)

from glimpsy.editor import subtitles as subs

log = logging.getLogger(__name__)


class Downloader(QObject):
    """Скачивает файлы по очереди (модель распознавания и детектор речи). Qt сам следует
    за переадресацией и проверяет сертификаты средствами системы."""

    progress = Signal(int, int)       # скачано, всего (байт)
    finished = Signal(str)            # "" — всё хорошо, иначе текст ошибки

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.net = QNetworkAccessManager(self)
        self.queue: list[tuple[str, Path]] = []
        self.reply: QNetworkReply | None = None
        self.file = None
        self.part: Path | None = None
        self.dest: Path | None = None
        self._cancelled = False

    def start(self, items: list[tuple[str, Path]]) -> None:
        self._cancelled = False
        self.queue = list(items)
        self._next()

    def cancel(self) -> None:
        self._cancelled = True
        if self.reply is not None:
            self.reply.abort()

    def _next(self) -> None:
        if not self.queue:
            self.finished.emit("")
            return
        url, self.dest = self.queue.pop(0)
        self.part = self.dest.with_name(self.dest.name + ".part")
        self.dest.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.part, "wb")
        req = QNetworkRequest(QUrl(url))
        req.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                         QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        req.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, "Glimpsy")
        self.reply = self.net.get(req)
        self.reply.readyRead.connect(self._on_data)
        self.reply.downloadProgress.connect(lambda got, total: self.progress.emit(int(got), int(total)))
        self.reply.finished.connect(self._on_done)

    def _on_data(self) -> None:
        if self.reply is not None and self.file is not None:
            self.file.write(bytes(self.reply.readAll()))

    def _on_done(self) -> None:
        reply, self.reply = self.reply, None
        assert reply is not None and self.part is not None and self.dest is not None and self.file is not None
        self.file.write(bytes(reply.readAll()))
        self.file.close()
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        failed = reply.error() != QNetworkReply.NetworkError.NoError or (status and int(status) >= 400)
        message = f"Не удалось скачать: {reply.errorString() or status}"
        reply.deleteLater()
        if self._cancelled or failed:
            self.part.unlink(missing_ok=True)
            self.queue.clear()
            self.finished.emit("cancelled" if self._cancelled else message)
            return
        self.part.replace(self.dest)
        self._next()


class SubtitlesDialog(QDialog):
    def __init__(self, aspect: str, has_auto: bool, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Автосубтитры")
        self.setMinimumWidth(460)
        st = QSettings("Glimpsy", "editor")
        self.lang = QComboBox()
        for code, label in subs.LANGUAGES:
            self.lang.addItem(label, code)
        self.lang.setCurrentIndex(max(0, self.lang.findData(st.value("subs/lang", "auto"))))
        self.model = QComboBox()
        for key, (_f, _u, _mb, label) in subs.MODELS.items():
            self.model.addItem(label + ("  ✓ скачана" if subs.model_ready(key) else ""), key)
        self.model.setCurrentIndex(max(0, self.model.findData(st.value("subs/model", "base"))))
        self.length = QComboBox()
        for key, (_n, label) in subs.LENGTHS.items():
            self.length.addItem(label, key)
        self.length.setCurrentIndex(self.length.findData("short" if aspect == "9:16" else "normal"))
        self.replace = QCheckBox("Заменить прежние автосубтитры")
        self.replace.setChecked(True)
        self.replace.setVisible(has_auto)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color: #8b8d98; font-size: 11px;")
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        self.status = QLabel()
        self.status.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Язык речи:", self.lang)
        form.addRow("Модель:", self.model)
        form.addRow("Строки:", self.length)
        form.addRow(self.replace)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        self.buttons.accepted.connect(self._go)
        self.buttons.rejected.connect(self._cancel)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.info)
        lay.addWidget(self.bar)
        lay.addWidget(self.status)
        lay.addWidget(self.buttons)
        self.model.currentIndexChanged.connect(self._update)
        self.downloader: Downloader | None = None
        self._update()

    # ---------- выбор ----------

    @property
    def model_key(self) -> str:
        return self.model.currentData()

    @property
    def language(self) -> str:
        return self.lang.currentData()

    @property
    def max_chars(self) -> int:
        return subs.LENGTHS[self.length.currentData()][0]

    def _missing(self) -> list[tuple[str, Path, int]]:
        out = []
        key = self.model_key
        if not subs.model_ready(key):
            _f, url, mb, _l = subs.MODELS[key]
            out.append((url, subs.model_path(key), mb))
        if not subs.vad_ready():
            out.append((subs.VAD_MODEL[1], subs.vad_path(), subs.VAD_MODEL[2]))
        return out

    def _update(self) -> None:
        missing = self._missing()
        if any(m[1] == subs.model_path(self.model_key) for m in missing):
            mb = sum(m[2] for m in missing)
            self.ok.setText(f"Скачать модель ({mb} МБ) и создать")
            self.info.setText(f"Модель распознавания скачается один раз (≈ {mb} МБ, с huggingface.co) "
                              f"и останется на компьютере. Сама речь распознаётся прямо здесь — звук "
                              f"ролика никуда не отправляется.")
        else:
            self.ok.setText("Создать субтитры")
            self.info.setText("Речь распознаётся прямо на компьютере, без интернета. Субтитры появятся "
                              "как обычные тексты: их можно поправить, сдвинуть или удалить. Стиль — "
                              "общий для всех текстов.")

    # ---------- загрузка и запуск ----------

    def _go(self) -> None:
        st = QSettings("Glimpsy", "editor")
        st.setValue("subs/lang", self.language)
        st.setValue("subs/model", self.model_key)
        missing = self._missing()
        if not missing:
            self.accept()
            return
        for w in (self.lang, self.model, self.length, self.ok):
            w.setEnabled(False)
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)
        total_mb = sum(m[2] for m in missing)
        self.status.setText(f"Скачиваю модель… (≈ {total_mb} МБ)")
        self.downloader = Downloader(self)

        def on_progress(got: int, total: int) -> None:
            if total > 0:
                self.bar.setRange(0, 1000)
                self.bar.setValue(int(got * 1000 / total))
                self.status.setText(f"Скачиваю модель… {got / 1e6:.0f} из {total / 1e6:.0f} МБ")

        def on_done(err: str) -> None:
            self.downloader = None
            if not err:
                self.accept()
                return
            for w in (self.lang, self.model, self.length, self.ok):
                w.setEnabled(True)
            self.bar.setVisible(False)
            self.status.setText("" if err == "cancelled" else err + "\nПроверьте интернет и попробуйте ещё раз.")
            self._update()

        self.downloader.progress.connect(on_progress)
        self.downloader.finished.connect(on_done)
        self.downloader.start([(url, dest) for url, dest, _mb in missing])

    def _cancel(self) -> None:
        if self.downloader is not None:
            self.downloader.cancel()        # диалог остаётся открытым — можно выбрать другую модель
            return
        self.reject()
