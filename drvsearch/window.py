"""Всплывающее окно «Поиск драйвера» - отдельный процесс.

Главное окно создаёт два безымянных канала системным вызовом pipe(2) и
запускает процесс командой
    python3 -m drvsearch --window --in-fd R --out-fd W
  R - канал «главное окно -> окно поиска»: сведения об устройстве;
  W - канал «окно поиска -> главное окно»: ход поиска и результат.
Сообщения главного окна: device (сведения об устройстве), model (состояние
языковой модели: главное окно скачивает её в фоне и сообщает, когда она
готова), close. Сообщения окна поиска: ready, progress, result, error, closed.
Окно зависит от главного: если главное окно закрыто, ядро закрывает его
конец канала, read(2) возвращает 0 байт, и окно поиска завершается само.
Сетевые запросы выполняются в отдельном потоке (QThread), чтобы окно не
«зависало» на время ответа GitHub и языковой модели.
"""

import os
import sys

from PyQt5.QtCore import QSocketNotifier, QThread, Qt, pyqtSignal
from PyQt5.QtGui import QDesktopServices, QFontDatabase
from PyQt5.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPlainTextEdit,
                             QProgressBar, QPushButton, QTextBrowser,
                             QVBoxLayout, QWidget)

from . import report
from .channel import Channel
from .pipeline import DriverSearch


class SearchThread(QThread):
    progress = pyqtSignal(str)
    done = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, device, use_llm=True, llm_off=None):
        super().__init__()
        self.device = device
        self.use_llm = use_llm
        self.llm_off = llm_off

    def run(self):
        try:
            search = DriverSearch(use_llm=self.use_llm, progress=self.progress.emit,
                                  llm_off=self.llm_off)
            self.done.emit(search.run(self.device))
        except Exception as exc:        # непредвиденная ошибка не должна ронять окно
            self.failed.emit("%s: %s" % (type(exc).__name__, exc))


class DriverSearchWindow(QWidget):
    def __init__(self, channel, device=None, use_llm=True, model_setup=None):
        super().__init__()
        self.channel = channel
        self.device = device
        self.use_llm = use_llm
        self.model_setup = model_setup    # служба Ollama, запущенная этим окном
        self.model_ready = None           # модель, о готовности которой сообщило главное окно
        # почему поиск идёт без модели (кнопка «без ИИ» или решение главного окна)
        self.llm_off = None if use_llm else "поиск без ИИ"
        self.thread = None
        self.result = None
        self.mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self.setWindowTitle("Поиск драйвера%s - Суперапп" % ("" if use_llm else " без ИИ"))
        self.resize(860, 720)
        self._build_ui()

        if channel.in_fd is not None:
            self.notifier = QSocketNotifier(channel.in_fd, QSocketNotifier.Read, self)
            self.notifier.activated.connect(self._on_pipe)
        self.channel.send({"type": "ready", "pid": os.getpid()})
        if device:
            self.start(device)

    def _build_ui(self):
        self.title = QLabel("Ожидание сведений об устройстве от главного окна")
        font = self.title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self.title.setFont(font)
        self.status = QLabel("")
        self.status.setStyleSheet("color: gray")
        self.status.setWordWrap(True)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setMaximumHeight(8)
        self.busy.setTextVisible(False)
        self.busy.hide()

        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(QDesktopServices.openUrl)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(130)
        self.log.setFont(self.mono)

        self.retry_button = QPushButton("Повторить поиск")
        self.retry_button.clicked.connect(lambda: self.start(self.device))
        self.retry_button.setEnabled(False)
        self.copy_button = QPushButton("Копировать отчёт")
        self.copy_button.clicked.connect(self.copy_report)
        self.copy_button.setEnabled(False)
        close_button = QPushButton("Закрыть")
        close_button.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.retry_button)
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.status)
        layout.addWidget(self.busy)
        layout.addWidget(self.view, 1)
        layout.addWidget(QLabel("Ход поиска"))
        layout.addWidget(self.log)
        layout.addLayout(buttons)

    # --- канал от главного окна ----------------------------------------------
    def _on_pipe(self):
        messages = self.channel.receive()
        if messages is None:
            self.notifier.setEnabled(False)
            self._exit("главное окно закрыто")
            return
        for message in messages:
            if message.get("type") == "device":
                self.start(message.get("device") or {})
            elif message.get("type") == "model":
                self.on_model(message)
            elif message.get("type") == "close":
                self.close()

    def on_model(self, message):
        """Состояние языковой модели от главного окна."""
        if not self.use_llm:
            return
        if message.get("state") == "weak":
            # слабый компьютер или модель не прошла проверку: без модели
            self.use_llm = False
            self.llm_off = message.get("text")
            self.log.appendPlainText("Языковая модель отключена: %s" % self.llm_off)
            return
        if message.get("state") != "ready" or not message.get("url"):
            if message.get("text"):
                self.log.appendPlainText("Языковая модель (главное окно): "
                                         + message["text"])
            return
        # следующие поиски в этом процессе пойдут через готовую модель
        os.environ["SUPERAPP_OLLAMA_URL"] = message["url"]
        os.environ["SUPERAPP_LLM_MODEL"] = message.get("model") or ""
        self.model_ready = message.get("model")
        self.log.appendPlainText("Языковая модель %s готова" % self.model_ready)
        if self.result and not self.result["llm"].get("used") and not (
                self.thread and self.thread.isRunning()):
            self.status.setText(self.status.text().rstrip(".") + ". " + self.retry_hint())

    def retry_hint(self):
        return ("Модель %s готова - нажмите «Повторить поиск», чтобы искать с ней."
                % self.model_ready)

    # --- поиск ---------------------------------------------------------------
    def start(self, device):
        if not device or (self.thread and self.thread.isRunning()):
            return
        self.device = device
        self.title.setText(device.get("full_name") or "Устройство")
        self.status.setText("VID:PID %04x:%04x - идёт поиск драйвера"
                            % (device["vid"], device["pid"]))
        self.view.setHtml("<p style='color:gray'>Поиск выполняется. Шаги видны "
                          "в журнале внизу окна.</p>")
        self.busy.show()
        self.retry_button.setEnabled(False)
        self.thread = SearchThread(device, self.use_llm, self.llm_off)
        self.thread.progress.connect(self._on_progress)
        self.thread.done.connect(self._on_done)
        self.thread.failed.connect(self._on_failed)
        self.thread.start()

    def _on_progress(self, text):
        self.log.appendPlainText(text)
        self.status.setText(text)
        self.channel.send({"type": "progress", "text": text})

    def _on_done(self, result):
        self.result = result
        self.busy.hide()
        self.retry_button.setEnabled(True)
        self.copy_button.setEnabled(True)
        count = len(result["results"])
        model = result["llm"].get("model") if result["llm"].get("used") else None
        self.status.setText("VID:PID %s, ядро %s. Найдено репозиториев: %d%s. %s" % (
            result["device"]["id"], result["kernel"], count,
            " (из кэша)" if result.get("from_cache") else "",
            "Модель: " + model if model else "Без языковой модели"))
        if not model and self.model_ready and self.use_llm:
            self.status.setText(self.status.text().rstrip(".") + ". " + self.retry_hint())
        self.view.setHtml(report.to_html(result, self.mono.family(), header=False))
        self.channel.send({
            "type": "result", "count": count,
            "from_cache": result.get("from_cache", False),
            "llm": result["llm"].get("model") if result["llm"].get("used") else None,
            "top": [{"name": r["full_name"], "url": r["url"]}
                    for r in result["results"][:3]],
        })

    def _on_failed(self, text):
        self.busy.hide()
        self.retry_button.setEnabled(True)
        self.status.setText("Ошибка: " + text)
        self.log.appendPlainText("Ошибка: " + text)
        self.channel.send({"type": "error", "text": text})

    def copy_report(self):
        if self.result:
            QApplication.clipboard().setText(report.to_text(self.result))

    # --- завершение ----------------------------------------------------------
    def _exit(self, reason):
        self.log.appendPlainText("Окно закрывается: " + reason)
        self.close()

    def closeEvent(self, event):
        self.channel.send({"type": "closed"})
        self.channel.close()
        if self.model_setup is not None:
            self.model_setup.stop_server()
        if self.thread and self.thread.isRunning():
            # Поток ждёт ответа сети; ждать его завершения незачем:
            # процесс окна завершается сразу, ядро освободит ресурсы.
            os._exit(0)
        super().closeEvent(event)


def main(in_fd=None, out_fd=None, device=None, use_llm=True):
    import qtenv          # обход ошибки PyQt5 с кириллицей в пути (см. qtenv.py)
    qtenv.prepare()
    app = QApplication(sys.argv[:1])
    qtenv.translate(app)
    app.setQuitOnLastWindowClosed(True)
    setup = None
    if in_fd is None and use_llm:
        # окно запущено само по себе, а не из главного окна: найти уже скачанную
        # модель (скачивает её главное окно или python -m drvsearch --setup-model)
        from .runtime import locate
        setup = locate()
    window = DriverSearchWindow(Channel(in_fd, out_fd), device, use_llm, setup)
    window.setAttribute(Qt.WA_DeleteOnClose)
    window.show()
    return app.exec_()
