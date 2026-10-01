"""Голоса для переозвучки: выбрать, чьим голосом сказать фразу, и библиотека сохранённых голосов.

Голос сохраняется один раз — из куска видео (выделенный текст), из звукового файла или записью
с микрофона — и дальше доступен во всех проектах, без интернета.
"""

from __future__ import annotations

import logging
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QVBoxLayout,
)

from glimpsy.editor import voice
from glimpsy.recorder.audio import MicMeter, list_microphones, write_wav

log = logging.getLogger(__name__)

FROM_VIDEO = ""                 # «голос из этого видео» (образец рядом с фразой)
READ_TEXT = ("Прочитайте спокойно, своим обычным голосом:\n\n"
             "«Сегодня отличный день, чтобы выйти из дома. Мы прогуляемся по городу, заглянем в пару "
             "магазинов, а потом поедем за город — посмотрим, какие там виды. Если будет хорошая погода, "
             "поднимемся на холм и снимем закат. Поехали!»")


def _sec(v: float) -> str:
    return f"{v:.0f} с"


class _Saver(QObject):
    done = Signal(str)            # "" — сохранено, иначе ошибка


class RespeakDialog(QDialog):
    """Как должна звучать фраза и чьим голосом."""

    def __init__(self, old_text: str, ref_seconds: float, ffmpeg: str, parent=None) -> None:
        super().__init__(parent)
        self.ffmpeg = ffmpeg
        self.ref_seconds = ref_seconds
        self.setWindowTitle("Переозвучка")
        self.setMinimumWidth(500)
        self.text = QLineEdit(old_text)
        self.text.selectAll()
        self.voice = QComboBox()
        self.voices_btn = QPushButton("Голоса…")
        self.voices_btn.setToolTip("Сохранить новый голос, послушать, переименовать или удалить")
        self.voices_btn.clicked.connect(self._manage)
        vrow = QHBoxLayout()
        vrow.addWidget(self.voice, 1)
        vrow.addWidget(self.voices_btn)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #8b8d98; font-size: 11px;")
        form = QFormLayout()
        form.addRow("Как должна звучать фраза:", self.text)
        form.addRow("Чьим голосом:", vrow)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Озвучить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.note)
        lay.addWidget(buttons)
        self.voice.currentIndexChanged.connect(self._update)
        self._fill(QSettings("Glimpsy", "editor").value("respeak/voice", FROM_VIDEO))

    def _fill(self, select: str) -> None:
        self.voice.blockSignals(True)
        self.voice.clear()
        self.voice.addItem(f"из этого видео (образец рядом с фразой, {_sec(self.ref_seconds)})", FROM_VIDEO)
        for v in voice.list_voices():
            self.voice.addItem(f"{v.name} ({_sec(v.seconds)})", v.id)
        self.voice.setCurrentIndex(max(0, self.voice.findData(select)))
        self.voice.blockSignals(False)
        self._update()

    @property
    def voice_id(self) -> str:
        return self.voice.currentData() or FROM_VIDEO

    def _update(self) -> None:
        if self.voice_id == FROM_VIDEO:
            short = self.ref_seconds < voice.MIN_SAMPLE_S
            self.note.setText(
                (f"Рядом с фразой мало речи ({_sec(self.ref_seconds)}) — голос может получиться непохожим. "
                 f"Лучше выбрать сохранённый голос." if short else
                 "Образец голоса возьмётся из вашей речи рядом с этой фразой.") +
                " Сохранить голос насовсем: выделите в тексте 10–20 секунд чистой речи → правая кнопка → "
                "«Сохранить голос…», или кнопка «Голоса…».")
        else:
            self.note.setText("Сохранённый голос звучит одинаково во всех роликах. Озвучивать только тех, кто "
                              "на это согласен.")

    def _manage(self) -> None:
        dlg = VoicesDialog(self.ffmpeg, self)
        dlg.exec()
        self._fill(dlg.chosen or self.voice_id)

    def _ok(self) -> None:
        if not " ".join(self.text.text().split()):
            return
        QSettings("Glimpsy", "editor").setValue("respeak/voice", self.voice_id)
        self.accept()


class VoicesDialog(QDialog):
    """Библиотека голосов."""

    def __init__(self, ffmpeg: str, parent=None) -> None:
        super().__init__(parent)
        self.ffmpeg = ffmpeg
        self.chosen = ""
        self.setWindowTitle("Голоса для переозвучки")
        self.setMinimumWidth(480)
        self.list = QListWidget()
        self.list.setMinimumHeight(160)
        hint = QLabel("Голос сохраняется один раз и дальше доступен во всех проектах, без интернета. "
                      "Лучший образец — 10–20 секунд спокойной речи без музыки, ветра и чужих голосов. "
                      "Из видео: выделите речь в панели «Текст» → правая кнопка → «Сохранить голос…».")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8b8d98; font-size: 11px;")
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(QAudioOutput(self))
        rec = QPushButton("Записать с микрофона…")
        rec.clicked.connect(self._record)
        file = QPushButton("Из звукового файла…")
        file.clicked.connect(self._from_file)
        play = QPushButton("Послушать")
        play.clicked.connect(self._play)
        ren = QPushButton("Переименовать")
        ren.clicked.connect(self._rename)
        rm = QPushButton("Удалить")
        rm.clicked.connect(self._delete)
        row1 = QHBoxLayout()
        row1.addWidget(rec)
        row1.addWidget(file)
        row1.addStretch(1)
        row2 = QHBoxLayout()
        row2.addWidget(play)
        row2.addWidget(ren)
        row2.addWidget(rm)
        row2.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("Готово")
        close.rejected.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.addWidget(self.list)
        lay.addLayout(row2)
        lay.addLayout(row1)
        lay.addWidget(hint)
        lay.addWidget(close)
        self.refresh()

    def refresh(self, select: str = "") -> None:
        self.list.clear()
        for v in voice.list_voices():
            it = QListWidgetItem(f"{v.name}   — {_sec(v.seconds)}, {time.strftime('%d.%m.%Y', time.localtime(v.created))}")
            it.setData(Qt.ItemDataRole.UserRole, v.id)
            self.list.addItem(it)
            if v.id == select:
                self.list.setCurrentItem(it)
        if not self.list.count():
            it = QListWidgetItem("Голосов пока нет")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(it)

    def _current(self) -> str:
        it = self.list.currentItem()
        return (it.data(Qt.ItemDataRole.UserRole) or "") if it is not None else ""

    def _ask_name(self, default: str = "") -> str | None:
        name, ok = QInputDialog.getText(self, "Голос", "Как назвать голос (например, «Мой голос», «Саша»):",
                                        text=default)
        name = " ".join(name.split())
        return name if ok and name else None

    def _save(self, name: str, source: Path) -> None:
        try:
            v = voice.add_voice(self.ffmpeg, name, source)
        except voice.VoiceError as e:
            QMessageBox.warning(self, "Голос", str(e))
            return
        except Exception as e:                      # noqa: BLE001 — показываем человеку, что пошло не так
            log.exception("Голос не сохранился")
            QMessageBox.warning(self, "Голос", f"Не получилось сохранить голос: {e}")
            return
        self.chosen = v.id
        self.refresh(v.id)

    def _from_file(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "Звук или видео с голосом", str(Path.home()),
                                           "Звук и видео (*.wav *.mp3 *.m4a *.ogg *.flac *.aac *.opus *.mp4 *.mov "
                                           "*.mkv *.webm);;Все файлы (*)")
        if not f:
            return
        name = self._ask_name(Path(f).stem)
        if name:
            self._save(name, Path(f))

    def _record(self) -> None:
        dlg = RecordVoiceDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.wav is None:
            return
        name = self._ask_name("Мой голос")
        if name:
            self._save(name, dlg.wav)
        dlg.wav.unlink(missing_ok=True)

    def _play(self) -> None:
        v = voice.get_voice(self._current())
        if v is not None:
            self.player.setSource(QUrl.fromLocalFile(str(v.sample)))
            self.player.play()

    def _rename(self) -> None:
        v = voice.get_voice(self._current())
        if v is not None:
            name = self._ask_name(v.name)
            if name:
                voice.rename_voice(v.id, name)
                self.refresh(v.id)

    def _delete(self) -> None:
        v = voice.get_voice(self._current())
        if v is None:
            return
        if QMessageBox.question(self, "Голос", f"Удалить голос «{v.name}»? Уже озвученные фразы останутся.") \
                == QMessageBox.StandardButton.Yes:
            self.player.stop()
            voice.delete_voice(v.id)
            self.refresh()


class RecordVoiceDialog(QDialog):
    """Записать образец голоса с микрофона (текст для чтения — на экране)."""

    def __init__(self, parent=None, opener=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Записать голос")
        self.setMinimumWidth(480)
        self._opener = opener                     # для проверок: «микрофон» без устройства
        self.meter: MicMeter | None = None
        self.wav: Path | None = None
        self._t0 = 0.0
        text = QLabel(READ_TEXT)
        text.setWordWrap(True)
        self.mic = QComboBox()
        self.mic.addItem("Микрофон по умолчанию", "")
        for name in ([] if opener else list_microphones()):
            self.mic.addItem(name, name)
        self.level = QProgressBar()
        self.level.setRange(0, 100)
        self.level.setTextVisible(False)
        self.level.setFixedHeight(8)
        self.status = QLabel("Нажмите «Начать» и читайте текст — хватит 15–20 секунд.")
        self.btn = QPushButton("Начать запись")
        self.btn.clicked.connect(self._toggle)
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(self.btn)
        row.addStretch(1)
        row.addWidget(cancel)
        form = QFormLayout()
        form.addRow("Микрофон", self.mic)
        form.addRow("Громкость", self.level)
        lay = QVBoxLayout(self)
        lay.addWidget(text)
        lay.addLayout(form)
        lay.addWidget(self.status)
        lay.addLayout(row)
        self.timer = QTimer(self, interval=100)
        self.timer.timeout.connect(self._tick)

    def _toggle(self) -> None:
        if self.meter is None:
            self.meter = MicMeter(self.mic.currentData() or "", opener=self._opener, keep=True)
            err = self.meter.start()
            if err:
                self.meter = None
                self.status.setText("Не удалось включить микрофон: " + err)
                return
            self._t0 = time.monotonic()
            self.mic.setEnabled(False)
            self.btn.setText("Готово")
            self.timer.start()
            return
        self.timer.stop()
        self.meter.stop()
        samples = self.meter.samples()
        self.meter = None
        if len(samples) / 48000 < voice.MIN_SAMPLE_S:
            self.status.setText(f"Слишком коротко — нужно хотя бы {voice.MIN_SAMPLE_S:.0f} секунд. Попробуйте ещё раз.")
            self.btn.setText("Начать запись")
            self.mic.setEnabled(True)
            return
        self.wav = Path(tempfile.mkstemp(suffix=".wav", prefix="glimpsy_voice_")[1])
        write_wav(self.wav, samples)
        self.accept()

    def _tick(self) -> None:
        if self.meter is None:
            return
        self.level.setValue(int(self.meter.level * 100))
        n = time.monotonic() - self._t0
        self.status.setText(f"Идёт запись: {n:.0f} с" + ("  — уже хватит, можно нажать «Готово»" if n >= 15 else ""))
        if n >= voice.MAX_SAMPLE_S + 5:
            self._toggle()

    def reject(self) -> None:
        self.timer.stop()
        if self.meter is not None:
            self.meter.stop()
            self.meter = None
        super().reject()


def save_voice_async(ffmpeg: str, name: str, source: Path, spans, parent, on_done) -> None:
    """Сохранить голос из видео в фоне (вырезать звук — пару секунд)."""
    saver = _Saver(parent)

    def run() -> None:
        try:
            v = voice.add_voice(ffmpeg, name, source, spans)
            saver.done.emit("ok:" + v.id)
        except Exception as e:                      # noqa: BLE001 — показываем человеку, что пошло не так
            log.exception("Голос не сохранился")
            saver.done.emit(str(e))

    saver.done.connect(on_done)
    threading.Thread(target=run, daemon=True, name="voice-save").start()
