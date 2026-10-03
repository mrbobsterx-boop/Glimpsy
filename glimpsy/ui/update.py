"""Окно «Есть новая версия» и проверка обновлений в фоне (раз в несколько часов)."""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QMessageBox, QProgressBar, QVBoxLayout

from glimpsy import updater

log = logging.getLogger(__name__)

FIRST_CHECK_MS = 20_000                 # после запуска — не сразу, чтобы не мешать
CHECK_EVERY_MS = 4 * 3600 * 1000


class UpdateManager(QObject):
    available = Signal(object)          # Release — есть новая версия

    def __init__(self, tray) -> None:
        super().__init__(tray)
        self.tray = tray
        self.build = updater.current_build()
        self.kind = updater.install_kind()
        self.release: updater.Release | None = None
        self._dismissed = 0              # «Позже» — больше не напоминаем об этой сборке до перезапуска
        self._dialog: UpdateDialog | None = None
        self._reply: QNetworkReply | None = None
        self.net = QNetworkAccessManager(self)
        self.timer = QTimer(self)
        self.timer.setInterval(CHECK_EVERY_MS)
        self.timer.timeout.connect(lambda: self.check(manual=False))
        if self.build is not None and self.kind is not None and tray.s.check_updates:
            QTimer.singleShot(FIRST_CHECK_MS, lambda: self.check(manual=False))
            self.timer.start()

    def set_enabled(self, on: bool) -> None:
        if on and self.build is not None and self.kind is not None:
            self.timer.start()
        else:
            self.timer.stop()

    # ---------- проверка ----------

    def check(self, manual: bool = True) -> None:
        if self.build is None or self.kind is None:
            if manual:
                self._manual_download("Эта копия Glimpsy не умеет обновляться сама "
                                      + ("(запущена из исходников)." if self.build is None else
                                         "(запущена не из файла AppImage)."))
            return
        if self._reply is not None:
            return
        req = QNetworkRequest(QUrl(updater.manifest_url(self.build.channel)))
        req.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                         QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        req.setAttribute(QNetworkRequest.Attribute.CacheLoadControlAttribute,
                         QNetworkRequest.CacheLoadControl.AlwaysNetwork)
        req.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, "Glimpsy")
        req.setTransferTimeout(30_000)
        self._reply = reply = self.net.get(req)
        reply.finished.connect(lambda: self._on_manifest(reply, manual))

    def _on_manifest(self, reply: QNetworkReply, manual: bool) -> None:
        self._reply = None
        data = bytes(reply.readAll())
        error = reply.error() != QNetworkReply.NetworkError.NoError
        text = reply.errorString()
        reply.deleteLater()
        rel = None if error else updater.parse_manifest(data, self.kind or "")
        if rel is None:
            log.info("Проверка обновлений не удалась: %s", text if error else "непонятный ответ")
            if manual:
                QMessageBox.warning(None, "Обновление", "Не удалось узнать, есть ли новая версия. Проверьте "
                                    "интернет и попробуйте ещё раз." + (f"\n\n{text}" if error else ""))
            return
        assert self.build is not None
        log.info("Обновления: у нас сборка %d, вышла %d", self.build.number, rel.build)
        if not updater.is_newer(rel, self.build):
            if manual:
                QMessageBox.information(None, "Обновление", "У вас последняя версия Glimpsy.")
            return
        self.release = rel
        self.available.emit(rel)
        if manual or (rel.build != self._dismissed and not self.tray.engine.running):
            self.show_dialog()
        elif rel.build != self._dismissed:
            # идёт запись — окно попало бы в ролик; только напоминание в меню
            self.tray.show_message("Есть новая версия Glimpsy",
                                   "Обновить можно в меню значка в трее: «Обновить Glimpsy».")
            self._dismissed = rel.build

    def _manual_download(self, why: str) -> None:
        box = QMessageBox(QMessageBox.Icon.Information, "Обновление",
                          why + "\n\nНовую версию можно скачать со страницы программы.")
        b = box.addButton("Открыть страницу", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Закрыть", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() == b:
            QDesktopServices.openUrl(QUrl(updater.releases_page()))

    # ---------- окно ----------

    def show_dialog(self) -> None:
        if self.release is None:
            self.check(manual=True)
            return
        if self._dialog is not None:
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        self._dialog = UpdateDialog(self, self.release)
        self._dialog.finished.connect(self._on_dialog_closed)
        self._dialog.show()

    def _on_dialog_closed(self, _result: int) -> None:
        if self._dialog is not None and not self._dialog.installing and self.release is not None:
            self._dismissed = self.release.build
        self._dialog = None


class UpdateDialog(QDialog):
    def __init__(self, mgr: UpdateManager, rel: updater.Release) -> None:
        super().__init__()
        self.mgr = mgr
        self.rel = rel
        self.installing = False
        self.downloader = None
        self.setWindowTitle("Обновление Glimpsy")
        self.setMinimumWidth(440)
        size = f" (около {max(1, rel.size >> 20)} МБ)" if rel.size else ""
        text = (f"<b>Вышла новая версия Glimpsy.</b><br>Установить сейчас?<br><br>"
                f"Программа скачает её{size} и сама перезапустится — это займёт пару минут. "
                f"Проекты, настройки и скачанные модели останутся на месте.")
        if rel.notes:
            text += f"<br><br><b>Что нового:</b><br>{rel.notes}"
        assert mgr.build is not None
        text += (f"<br><br><span style='color:#8b8d98'>Ваша сборка: {mgr.build.number}, "
                 f"новая: {rel.build}</span>")
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.buttons = QDialogButtonBox()
        self.b_go = self.buttons.addButton("Обновить сейчас", QDialogButtonBox.ButtonRole.AcceptRole)
        self.b_later = self.buttons.addButton("Позже", QDialogButtonBox.ButtonRole.RejectRole)
        self.b_go.clicked.connect(self._go)
        self.b_later.clicked.connect(self._cancel)
        lay = QVBoxLayout(self)
        lay.addWidget(self.label)
        lay.addWidget(self.bar)
        lay.addWidget(self.status)
        lay.addWidget(self.buttons)

    def _go(self) -> None:
        tray = self.mgr.tray
        if tray.exporting():
            self.status.setText("Сейчас сохраняется видео. Дождитесь конца сохранения и нажмите ещё раз.")
            return
        if tray.engine.running:
            ans = QMessageBox.question(
                self, "Обновление", "Идёт запись. Остановить её и обновиться?\n\nУже записанное сохранится — "
                "после перезапуска можно будет собрать ролик или продолжить запись.")
            if ans != QMessageBox.StandardButton.Yes:
                return
        from glimpsy.editor.subtitles_dialog import Downloader

        kind = self.mgr.kind
        assert kind is not None and self.mgr.build is not None
        self.target = updater.target_path(kind)
        self.dest = updater.download_path(kind, self.target)
        self.installing = True
        self.b_go.setEnabled(False)
        self.b_later.setText("Отмена")
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)
        self.status.setText("Скачиваю новую версию…")
        self.downloader = Downloader(self)
        self.downloader.progress.connect(self._on_progress)
        self.downloader.finished.connect(self._on_downloaded)
        url = updater.download_url(self.mgr.build.channel, self.rel.file)
        try:
            self.downloader.start([(url, self.dest)])
        except OSError as e:
            self._failed(f"Не удалось сохранить файл рядом с программой: {e}")

    def _on_progress(self, got: int, total: int) -> None:
        total = total if total > 0 else self.rel.size
        if total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(1000 * got / total))
            self.status.setText(f"Скачиваю новую версию… {got >> 20} из {total >> 20} МБ")

    def _on_downloaded(self, error: str) -> None:
        if error == "cancelled":
            return
        if error:
            self._failed(error)
            return
        if self.rel.size and self.dest.stat().st_size != self.rel.size:
            self.dest.unlink(missing_ok=True)
            self._failed("Файл скачался не полностью. Попробуйте ещё раз.")
            return
        self.status.setText("Устанавливаю…")
        self.bar.setRange(0, 0)
        assert self.mgr.kind is not None
        try:
            cmd = updater.install(self.mgr.kind, self.dest, self.target, os.getpid())
            updater.launch(cmd)
        except (OSError, updater.UpdateError) as e:
            log.exception("Обновление не установилось")
            self._failed(f"Не удалось установить: {e}")
            return
        log.info("Обновление установлено, перезапуск: %s", cmd)
        self.mgr.tray.quit(force=True)

    def _failed(self, msg: str) -> None:
        self.installing = False
        self.bar.setVisible(False)
        self.b_go.setEnabled(True)
        self.b_go.setText("Попробовать ещё раз")
        self.b_later.setText("Закрыть")
        self.status.setText(msg + "<br><br>Можно скачать новую версию и вручную — "
                            f"<a href='{updater.releases_page()}'>страница программы</a>.")
        self.status.setOpenExternalLinks(True)

    def _cancel(self) -> None:
        if self.downloader is not None and self.installing:
            self.downloader.cancel()
            self.installing = False
        self.reject()
