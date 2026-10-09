"""Окно «Монитор USB» (PyQt5).

События ядра принимаются в главном потоке: дескриптор сокета netlink
подключён к циклу событий Qt через QSocketNotifier, поэтому отдельные
потоки не нужны. Если netlink недоступен, окно опрашивает sysfs по таймеру.
"""

import html
import json
import os
import signal
from time import strftime

from PyQt5.QtCore import Qt, QSocketNotifier, QTimer
from PyQt5.QtGui import QColor, QFontDatabase, QIcon, QKeySequence, QPainter, \
    QPixmap
from PyQt5.QtWidgets import QAbstractItemView, QAction, QApplication, \
    QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, \
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter, \
    QTextBrowser, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from . import kernel_drivers as kd
from . import tracker as tr
from .devices import speed_text
from .modelthread import ModelSetupThread
from .popups import PopupManager
from drvsearch import selftest
from drvsearch.llm import DEFAULT_MODEL
from drvsearch.runtime import human
from .uevent import UeventSocket, UeventOverflow

POLL_MS = 2000

# Цвет текста, цвет фона плашки, краткая подпись в списке
VERDICTS = {
    "ok": ("#256B43", "#E6F2EA", "работает"),
    "info": ("#2B5F87", "#E6EEF5", "не требуется"),
    "warn": ("#8A4E05", "#FAF0DE", "не работает"),
    "missing": ("#A3241B", "#FBE9E7", "не найден"),
    "blocked": ("#4F5864", "#ECEEF1", "заблокировано"),
}

STATUS_COLORS = {
    kd.BOUND: "ok", kd.USBFS: "info", kd.USERSPACE: "info",
    kd.AVAILABLE: "warn", kd.BLACKLISTED: "warn", kd.LOADED_UNBOUND: "warn",
    kd.UNKNOWN: "warn", kd.MISSING: "missing",
}


def dot_icon(color, size=12):
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return QIcon(pixmap)


def esc(text):
    return html.escape(str(text)) if text else ""


class UsbMonitorWindow(QMainWindow):
    def __init__(self, tracker, model_setup=True, model=None, parent=None):
        super().__init__(parent)
        self.tracker = tracker
        self.model_name = model
        self.model_thread = None
        self.model_stage = None
        self.model_state = "не проверялась" if model_setup else "отключена"
        self.model_url = None
        self.server_pid = None    # PID ollama serve, уже записанный в лог-файл
        self.model_weak = None    # почему модель отключена из-за слабого компьютера
        self.items = {}       # имя устройства -> QTreeWidgetItem
        self.timers = {}      # имя устройства -> QTimer отложенной проверки
        self.sock = None
        self.notifier = None
        self.poll_timer = None
        self.mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self.icons = {key: dot_icon(v[0]) for key, v in VERDICTS.items()}
        self.popups = PopupManager(self)
        self.popups.message.connect(self._on_popup_message)
        self.popups.finished.connect(self._on_popup_finished)

        self.setWindowTitle("Монитор USB - Суперапп")
        self.resize(1180, 800)
        self._build_ui()
        self._build_menu()
        self._start_watching()
        self._initial_scan()
        if model_setup:
            self.start_model_setup()
        else:
            self.model_label.setText("Модель: отключена")

    # --- интерфейс -------------------------------------------------------
    def _build_ui(self):
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Устройство", "VID:PID", "Порт",
                                   "Драйвер"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(2, Qt.AscendingOrder)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.tree.currentItemChanged.connect(self._show_selected)

        self.details = QTextBrowser()
        self.details.setOpenLinks(False)

        self.copy_button = QPushButton("Копировать данные")
        self.copy_button.setToolTip("Скопировать сведения об устройстве в "
                                    "формате JSON (Ctrl+Shift+C)")
        self.copy_button.clicked.connect(self.copy_json)
        self.search_button = QPushButton("Найти драйвер")
        self.search_button.setEnabled(False)
        self.search_button.setToolTip("Найти внешний драйвер на GitHub в "
                                      "отдельном окне (Ctrl+F)")
        self.search_button.clicked.connect(lambda *_: self.start_driver_search())
        self.plain_search_button = QPushButton("Найти драйвер без ИИ")
        self.plain_search_button.setEnabled(False)
        self.plain_search_button.setToolTip(
            "Найти драйвер только по GitHub и README, без языковой модели: "
            "быстрее и не занимает память (Ctrl+Shift+F)")
        self.plain_search_button.clicked.connect(
            lambda *_: self.start_driver_search(use_llm=False))
        buttons = QHBoxLayout()
        buttons.addWidget(self.copy_button)
        buttons.addWidget(self.search_button)
        buttons.addWidget(self.plain_search_button)
        buttons.addStretch(1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.details, 1)
        right_layout.addLayout(buttons)

        top = QSplitter(Qt.Horizontal)
        top.addWidget(self.tree)
        top.addWidget(right)
        top.setSizes([600, 560])

        self.journal = QPlainTextEdit()
        self.journal.setReadOnly(True)
        self.journal.setMaximumBlockCount(2000)
        journal_box = QWidget()
        journal_layout = QVBoxLayout(journal_box)
        journal_layout.setContentsMargins(0, 6, 0, 0)
        journal_layout.addWidget(QLabel("Журнал событий"))
        journal_layout.addWidget(self.journal)

        main = QSplitter(Qt.Vertical)
        main.addWidget(top)
        main.addWidget(journal_box)
        main.setSizes([600, 150])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(main)
        self.setCentralWidget(central)

        self.model_label = QLabel("Модель: проверка")
        self.model_label.setStyleSheet("padding-left: 12px")
        self.model_bar = QProgressBar()
        self.model_bar.setRange(0, 100)
        self.model_bar.setTextVisible(False)      # проценты уже есть в надписи
        self.model_bar.setFixedWidth(120)
        self.model_bar.setMaximumHeight(12)
        self.model_bar.hide()
        self.watch_label = QLabel()
        self.watch_label.setStyleSheet("padding-left: 12px")
        self.statusBar().addPermanentWidget(self.model_label)
        self.statusBar().addPermanentWidget(self.model_bar)
        self.statusBar().addPermanentWidget(self.watch_label)

    def _build_menu(self):
        def action(menu, text, slot, shortcut=None):
            act = QAction(text, self)
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
            act.triggered.connect(slot)
            menu.addAction(act)
            return act

        file_menu = self.menuBar().addMenu("&Файл")
        action(file_menu, "Показать лог-файл", self.show_log, "Ctrl+O")
        file_menu.addSeparator()
        action(file_menu, "Закрыть", self.close, "Ctrl+Q")

        edit_menu = self.menuBar().addMenu("&Правка")
        action(edit_menu, "Копировать данные устройства", self.copy_json,
               "Ctrl+Shift+C")
        action(edit_menu, "Очистить журнал", self.journal.clear, "Ctrl+L")
        edit_menu.addSeparator()
        action(edit_menu, "Найти драйвер на GitHub", lambda *_: self.start_driver_search(),
               "Ctrl+F")
        action(edit_menu, "Найти драйвер без ИИ",
               lambda *_: self.start_driver_search(use_llm=False), "Ctrl+Shift+F")

        view_menu = self.menuBar().addMenu("&Вид")
        action(view_menu, "Перечитать все устройства", self.rescan, "F5")

        model_menu = self.menuBar().addMenu("&Модель")
        action(model_menu, "Сведения о модели", self.show_model_info)
        action(model_menu, "Остановить загрузку", self.stop_model_setup)
        action(model_menu, "Повторить подготовку и проверку",
               lambda *_: self.start_model_setup(retest=True))

        help_menu = self.menuBar().addMenu("&Справка")
        action(help_menu, "Горячие клавиши", self.show_hotkeys, "F1")
        action(help_menu, "О модуле", self.show_about)

    # --- слежение --------------------------------------------------------
    def _start_watching(self):
        try:
            self.sock = UeventSocket()
        except OSError as exc:
            self.poll_timer = QTimer(self)
            self.poll_timer.timeout.connect(self._poll)
            self.poll_timer.start(POLL_MS)
            self.watch_label.setText("Слежение: опрос sysfs каждые %d с"
                                     % (POLL_MS // 1000))
            self.log_line("netlink недоступен (%s), включён опрос sysfs" % exc)
            return
        self.notifier = QSocketNotifier(self.sock.fileno(),
                                        QSocketNotifier.Read, self)
        self.notifier.activated.connect(self._on_socket)
        self.watch_label.setText("Слежение: события ядра (netlink)")

    def _initial_scan(self):
        modules = self.tracker.inspector.modules
        ids = self.tracker.inspector.ids
        self.log_line("Ядро %s, шаблонов USB в базе модулей: %d" % (
            modules.release, modules.alias_count))
        if not modules.available:
            self.log_line("Проверка драйверов недоступна: " + modules.error)
        if not ids.loaded:
            self.log_line("База usb.ids не найдена: названия берутся только "
                          "из самих устройств (базу ставит пакет hwdata в Arch "
                          "и Fedora, пакет usb.ids в Debian и Ubuntu)")
        notices = self.tracker.scan()
        for notice in notices:
            self._apply(notice, quiet=True)
        self.log_line("Подключено устройств: %d" % len(notices))
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        else:
            self._show_selected(None)

    def _on_socket(self):
        try:
            events = self.sock.receive()
        except UeventOverflow:
            self.log_line("Часть событий потеряна, сверка с sysfs")
            self.rescan()
            return
        for event in events:
            names, notices = self.tracker.handle(event)
            for notice in notices:
                self._apply(notice)
            for name in names:
                self._schedule(name)

    def _poll(self):
        names, notices = self.tracker.resync()
        for notice in notices:
            self._apply(notice)
        for name in names:
            self._schedule(name, 0)

    def _schedule(self, name, delay=None):
        timer = self.timers.get(name)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda n=name: self._inspect(n))
            self.timers[name] = timer
        timer.start(int((tr.SETTLE_DELAY if delay is None else delay) * 1000))

    def _inspect(self, name):
        timer = self.timers.pop(name, None)
        if timer:
            timer.deleteLater()
        for notice in self.tracker.inspect(name):
            self._apply(notice)

    def rescan(self):
        names, notices = self.tracker.resync()
        for notice in notices:
            self._apply(notice)
        for name in names + list(self.tracker.devices):
            self._schedule(name, 0)
        self.statusBar().showMessage("Список устройств перечитан", 3000)

    # --- отображение -----------------------------------------------------
    def _apply(self, notice, quiet=False):
        if not quiet:
            self.log_line(tr.describe(notice))
        dev = notice.device
        if notice.kind == tr.DISCONNECTED:
            timer = self.timers.pop(notice.name, None)
            if timer:
                timer.stop()
                timer.deleteLater()
            item = self.items.pop(notice.name, None)
            if item:
                index = self.tree.indexOfTopLevelItem(item)
                self.tree.takeTopLevelItem(index)
            self.statusBar().showMessage(tr.describe(notice), 5000)
            if not self.tree.currentItem():
                self._show_selected(None)
            return
        if dev is None or notice.kind in (tr.BIND, tr.UNBIND):
            return
        item = self.items.get(notice.name)
        if item is None:
            item = QTreeWidgetItem()
            self.items[notice.name] = item
            self.tree.addTopLevelItem(item)
        self._fill_item(item, dev)
        if notice.kind == tr.CONNECTED:
            self.log_line("    " + dev.verdict_text)
            self.tree.setCurrentItem(item)
            self.statusBar().showMessage(tr.describe(notice), 5000)
            QApplication.alert(self)
        elif self.tree.currentItem() is item:
            self._show_selected(item)

    def _fill_item(self, item, dev):
        item.setData(0, Qt.UserRole, dev.name)
        item.setText(0, dev.full_name)
        item.setIcon(0, self.icons.get(dev.verdict))
        item.setToolTip(0, dev.verdict_text)
        item.setText(1, dev.id_text)
        item.setFont(1, self.mono)
        item.setText(2, dev.name)
        item.setText(3, VERDICTS.get(dev.verdict, ("", "", ""))[2])
        item.setForeground(3, QColor(VERDICTS.get(dev.verdict, ("#000",))[0]))

    def current_device(self):
        item = self.tree.currentItem()
        if not item:
            return None
        return self.tracker.devices.get(item.data(0, Qt.UserRole))

    def _show_selected(self, item, _previous=None):
        dev = self.tracker.devices.get(item.data(0, Qt.UserRole)) if item else None
        self.copy_button.setEnabled(dev is not None)
        self.search_button.setEnabled(dev is not None)
        self.plain_search_button.setEnabled(dev is not None)
        self.search_button.setDefault(dev is not None and dev.verdict == "missing")
        if dev is None:
            text = ("Выберите устройство в списке слева." if self.items else
                    "USB-устройства не подключены. Вставьте устройство: "
                    "оно появится в списке через секунду, а здесь будет "
                    "результат проверки драйвера.")
            self.details.setHtml("<p style='color:gray'>%s</p>" % text)
            return
        self.details.setHtml(device_html(dev, self.mono.family()))

    def log_line(self, text):
        self.journal.appendPlainText("%s  %s" % (strftime("%H:%M:%S"), text))

    # --- действия --------------------------------------------------------
    def copy_json(self):
        dev = self.current_device()
        if dev is None:
            return
        QApplication.clipboard().setText(
            json.dumps(dev.to_dict(), ensure_ascii=False, indent=2))
        self.statusBar().showMessage("Сведения об устройстве скопированы", 3000)

    def start_driver_search(self, use_llm=True):
        """Открывает окно поиска драйвера - отдельный процесс с каналами.

        use_llm=False - кнопка «Найти драйвер без ИИ»: окно запускается с
        параметром --no-llm и не обращается к языковой модели совсем.
        """
        dev = self.current_device()
        if dev is None:
            return
        messages = [{"type": "device", "device": dev.to_dict()}]
        if use_llm:
            messages.insert(0, self.model_message())
        try:
            popup = self.popups.start("drvsearch", dev.full_name, "drvsearch", messages,
                                      args=() if use_llm else ("--no-llm",))
        except OSError as exc:
            QMessageBox.warning(self, "Поиск драйвера",
                                "Не удалось запустить окно поиска: %s" % exc)
            return
        popup.device = dev
        mode = "с ИИ" if use_llm else "без ИИ"
        self.log_line("Запущено окно поиска драйвера %s для %s (PID %d)"
                      % (mode, dev.full_name, popup.pid))
        self.tracker.log.write("ПОИСК ДРАЙВЕРА ЗАПУЩЕН", "порт " + dev.name,
                               dev.id_text, dev.full_name, "PID %d" % popup.pid, mode)

    def _on_popup_message(self, popup, message):
        kind = message.get("type")
        if kind == "progress":
            self.statusBar().showMessage("Поиск драйвера (PID %d): %s"
                                         % (popup.pid, message.get("text", "")), 4000)
        elif kind == "result":
            top = message.get("top") or []
            text = "Поиск драйвера для %s: найдено репозиториев %d" % (
                popup.title, message.get("count", 0))
            if message.get("from_cache"):
                text += " (из кэша)"
            self.log_line(text)
            for repo in top:
                self.log_line("    " + repo.get("url", ""))
            dev = popup.device
            self.tracker.log.write(
                "ПОИСК ДРАЙВЕРА: РЕЗУЛЬТАТ", "порт " + dev.name, dev.id_text,
                dev.full_name, "найдено %d; модель: %s; %s" % (
                    message.get("count", 0), message.get("llm") or "нет",
                    ", ".join(r.get("url", "") for r in top) or "ссылок нет"))
        elif kind == "error":
            self.log_line("Поиск драйвера (PID %d): ошибка %s"
                          % (popup.pid, message.get("text", "")))

    def _on_popup_finished(self, popup, code):
        self.log_line("Окно поиска драйвера закрыто (PID %d, код завершения %d)"
                      % (popup.pid, code))
        dev = popup.device
        self.tracker.log.write("ПОИСК ДРАЙВЕРА ЗАВЕРШЁН", "порт " + dev.name,
                               dev.id_text, dev.full_name,
                               "PID %d, код %d" % (popup.pid, code))

    def show_log(self):
        path = self.tracker.log.path
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as exc:
            QMessageBox.warning(self, "Лог-файл", "Не удалось открыть %s: %s"
                                % (path, exc.strerror))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Лог-файл: " + path)
        dialog.resize(900, 500)
        view = QPlainTextEdit(text)
        view.setReadOnly(True)
        view.setFont(self.mono)
        view.moveCursor(view.textCursor().End)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        layout = QVBoxLayout(dialog)
        layout.addWidget(view)
        layout.addWidget(buttons)
        dialog.exec_()

    def show_hotkeys(self):
        QMessageBox.information(self, "Горячие клавиши", (
            "F5 - перечитать все устройства\n"
            "Ctrl+F - найти драйвер на GitHub\n"
            "Ctrl+Shift+F - найти драйвер без ИИ\n"
            "Ctrl+Shift+C - копировать данные выбранного устройства\n"
            "Ctrl+L - очистить журнал событий\n"
            "Ctrl+O - показать лог-файл\n"
            "F1 - эта справка\n"
            "Ctrl+Q - закрыть окно"))

    def show_about(self):
        QMessageBox.about(self, "О модуле", (
            "<b>Монитор USB</b> - модуль приложения «Суперапп».<br><br>"
            "Следит за подключением устройств USB через сокет netlink "
            "(NETLINK_KOBJECT_UEVENT), показывает полное наименование "
            "устройства и проверяет, есть ли для него драйвер в ядре. Если "
            "драйвера нет, ищет его на GitHub с помощью локальной языковой "
            "модели в отдельном окне-процессе.<br><br>"
            "Дисциплина: Операционные системы и оболочки<br>"
            "Язык программирования: Python 3, PyQt5<br>"
            "Разработчик: Бунтовской Илья Владиславович, группа ИВТз-46ду"))

    # --- языковая модель ---------------------------------------------------
    def start_model_setup(self, retest=False):
        """Проверяет модель и при отсутствии скачивает её в фоновом потоке."""
        if self.model_thread and self.model_thread.isRunning():
            return
        server = self.model_thread.setup.server if self.model_thread else None
        self.model_thread = ModelSetupThread(self.model_name, server, retest, self)
        self.model_thread.progress.connect(self._on_model_progress)
        self.model_thread.ready.connect(self._on_model_ready)
        self.model_thread.failed.connect(self._on_model_failed)
        self.model_thread.cancelled.connect(self._on_model_cancelled)
        self.model_thread.weak.connect(self._on_model_weak)
        self.model_stage = None
        self.model_state = "подготовка"
        self.model_url = None
        self.model_weak = None
        self.model_label.setText("Модель: проверка")
        self.model_label.setToolTip("")
        self.model_thread.start()

    def model_message(self):
        """Состояние модели для окон поиска (сообщение по каналу)."""
        model = (self.model_thread.setup.model if self.model_thread
                 else self.model_name or DEFAULT_MODEL)
        if self.model_url:
            return {"type": "model", "state": "ready", "url": self.model_url,
                    "model": model}
        if self.model_state == "отключена":
            text = "подготовка модели отключена параметром --no-model"
        elif self.model_weak:
            return {"type": "model", "state": "weak", "model": model,
                    "text": self.model_weak + "; поиск идёт без модели"}
        else:
            text = "%s ещё не готова (%s); поиск пойдёт без неё" % (model, self.model_state)
        return {"type": "model", "state": "not_ready", "model": model, "text": text}

    def stop_model_setup(self):
        if self.model_thread and self.model_thread.isRunning():
            self.model_thread.stop()
            self.model_label.setText("Модель: остановка загрузки")

    def _on_model_progress(self, stage, text, done, total):
        setup = self.model_thread.setup
        if stage == "ready":                  # итог показывает _on_model_ready
            return
        if done is not None and total:
            percent = int(done * 100 / total)
            what = "Ollama" if stage == "runtime" else setup.model
            self.model_bar.setValue(percent)
            self.model_bar.show()
            self.model_label.setText("Загрузка %s: %d%% (%s из %s)"
                                     % (what, percent, human(done), human(total)))
            self.model_state = "загрузка %s: %d%%" % (what, percent)
            if stage != self.model_stage:     # размер записывается один раз на этап
                self.model_stage = stage
                self.log_line("Загрузка %s: размер %s, уже скачано %s"
                              % (what, human(total), human(done)))
                self.tracker.log.write("МОДЕЛЬ", "загрузка " + what,
                                       "размер " + human(total), "скачано " + human(done))
            return
        self.model_bar.hide()
        if text[:1].isupper() and "А" <= text[:1] <= "Я":
            text = text[:1].lower() + text[1:]    # «Служба ...» -> «Модель: служба ...»
        short = text.split(". ")[0]           # полный текст - в журнале и подсказке
        self.model_label.setText("Модель: " + (short if len(short) < 70 else short[:67] + "..."))
        self.model_label.setToolTip(text)
        self.log_line("Языковая модель: " + text)
        server = setup.server
        if stage == "server" and server is not None and server.pid != self.server_pid:
            self.server_pid = server.pid      # процесс, порождённый приложением
            self.tracker.log.write("ЗАПУЩЕН ПРОЦЕСС", "ollama serve", "PID %d" % server.pid,
                                   setup.app_url)
        elif stage in ("runtime", "extract", "server", "selftest"):
            self.tracker.log.write("МОДЕЛЬ", text)

    def _on_model_ready(self, url, model):
        self.model_url = url
        self.model_state = "готова"
        os.environ["SUPERAPP_OLLAMA_URL"] = url     # окна поиска наследуют адрес
        os.environ["SUPERAPP_LLM_MODEL"] = model
        self.model_bar.hide()
        self.model_label.setText("Модель %s готова" % model)
        where = "Ollama из папки model проекта"
        self.model_label.setToolTip("%s, %s" % (url, where))
        test = self.model_thread.setup.test
        if test:
            self.log_line(selftest.describe(test)[0])
        self.log_line("Языковая модель %s готова (%s, %s)" % (model, where, url))
        self.tracker.log.write("МОДЕЛЬ ГОТОВА", model, url, where,
                               selftest.describe(test)[0] if test else "")
        self.popups.broadcast(self.model_message(), "drvsearch")

    def _on_model_failed(self, text):
        self.model_state = "недоступна: " + text
        self.model_bar.hide()
        self.model_label.setText("Модель недоступна")
        self.model_label.setToolTip(text)
        self.log_line("Языковая модель недоступна: %s. Поиск драйверов работает "
                      "без неё; что проверено - «Модель → Сведения о модели», "
                      "повторить - «Модель → Повторить подготовку и проверку»" % text)
        self.tracker.log.write("МОДЕЛЬ НЕДОСТУПНА", text)

    def _on_model_weak(self, text, label):
        """Слабый компьютер или модель не прошла самопроверку: поиск без неё."""
        self.model_weak = text
        self.model_state = "отключена: " + text
        self.model_bar.hide()
        self.model_label.setText(label)
        self.model_label.setToolTip(text)
        self.log_line("Языковая модель отключена: %s. Поиск драйверов работает без "
                      "неё (подробности - меню «Модель → Сведения о модели»)" % text)
        self.tracker.log.write("МОДЕЛЬ ОТКЛЮЧЕНА", text)
        self.popups.broadcast(self.model_message(), "drvsearch")

    def _on_model_cancelled(self, text):
        self.model_state = "загрузка остановлена"
        self.model_bar.hide()
        self.model_label.setText("Загрузка модели остановлена")
        self.log_line("Языковая модель: " + text)
        self.tracker.log.write("МОДЕЛЬ", "загрузка остановлена")

    def show_model_info(self):
        setup = self.model_thread.setup if self.model_thread else None
        model = setup.model if setup else (self.model_name or DEFAULT_MODEL)
        lines = ["Модель: %s" % model, "Состояние: %s" % self.model_state]
        if self.model_url:
            lines.append("Служба Ollama: %s" % self.model_url)
        if setup and setup.hw:
            lines += setup.hw.summary()
        if setup and setup.test:
            lines += selftest.describe(setup.test)
        if setup:
            lines.append("Папка модели: %s" % setup.base)
        lines.append("")
        lines.append("Модель точнее, но вдвое больше: run_usb_monitor.py "
                     "--model qwen2.5:3b (1,9 ГБ, нужно 4,1 ГБ ОЗУ).")
        box = QMessageBox(QMessageBox.Information, "Языковая модель", "\n".join(lines),
                          QMessageBox.Close, self)
        # текст длинный: без минимальной ширины Qt переносит почти каждую строку
        box.setStyleSheet("QLabel#qt_msgbox_label { min-width: 660px; }")
        if setup and setup.report:
            # что программа проверила по шагам - для поиска причин неполадок
            box.setDetailedText("\n".join(setup.report))
        box.exec_()

    def closeEvent(self, event):
        for popup, code in self.popups.close_all():
            self._on_popup_finished(popup, code)
        if self.model_thread:
            self.model_thread.stop()
            self.model_thread.wait(15000)
            pid = self.model_thread.setup.server.pid if self.model_thread.setup.server else None
            code = self.model_thread.setup.stop_server()
            # поток должен завершиться до уничтожения окна, иначе Qt аварийно
            # завершит процесс; после остановки службы его чтению нечего ждать
            self.model_thread.wait(10000)
            if pid is not None:
                self.tracker.log.write("ПРОЦЕСС ЗАВЕРШЁН", "ollama serve", "PID %d" % pid,
                                       exit_text(code))
        if self.notifier:
            self.notifier.setEnabled(False)
        if self.sock:
            self.sock.close()
        super().closeEvent(event)


def exit_text(code):
    """Код завершения процесса: отрицательный у subprocess - номер сигнала."""
    if code is None:
        return "код неизвестен"
    if code < 0:
        try:
            return "завершён сигналом %s (%d)" % (signal.Signals(-code).name, -code)
        except ValueError:
            return "завершён сигналом %d" % -code
    return "код %d" % code


def device_html(dev, mono_family):
    fg, bg, _label = VERDICTS.get(dev.verdict, ("#000", "#fff", ""))
    mono = "font-family:'%s';" % mono_family
    parts = [
        "<h2 style='margin:0 0 2px 0'>%s</h2>" % esc(dev.full_name),
        "<p style='margin:0 0 10px 0; color:gray'>%s, порт %s</p>"
        % (esc(dev.kind_text), esc(dev.name)),
        "<table width='100%%' cellpadding='10' cellspacing='0' "
        "style='background-color:%s'><tr><td>"
        "<span style='color:%s; font-size:large; font-weight:600'>%s</span>"
        "</td></tr></table>" % (bg, fg, esc(dev.verdict_text)),
    ]

    if dev.interfaces:
        parts.append("<h3 style='margin:12px 0 4px 0'>Интерфейсы и драйверы</h3>"
                     "<table width='100%' cellpadding='4' cellspacing='0' "
                     "border='0'>")
        parts.append("<tr style='color:gray'><td>№</td><td>Класс</td>"
                     "<td>Драйвер</td><td>Состояние</td></tr>")
        for intf in dev.interfaces:
            d = intf.driver
            color = VERDICTS[STATUS_COLORS.get(d["status"], "warn")][0]
            driver = esc(d["driver"]) or "-"
            if d["driver"] and d["module"] and d["module"] != d["driver"]:
                driver += " <span style='color:gray'>(модуль %s)</span>" % \
                    esc(d["module"])
            klass = "%s <span style=\"color:gray; %s\">%02x/%02x/%02x</span>" % (
                esc(intf.class_ru), mono, intf.cls, intf.subclass, intf.protocol)
            parts.append(
                "<tr><td>%d</td><td>%s</td><td>%s</td>"
                "<td style='color:%s'>%s</td></tr>" % (
                    intf.number, klass, driver, color,
                    esc(kd.STATUS_RU.get(d["status"], d["status"]))))
            extra = []
            if d["note"] and d["status"] != kd.BOUND:
                extra.append(esc(d["note"]))
            if d["status"] == kd.AVAILABLE and d["candidates"]:
                extra.append("Загрузить вручную: <span style=\"%s\">sudo modprobe "
                             "%s</span>" % (mono, esc(d["candidates"][0][0])))
            extra.append("<span style=\"%s\">%s</span>" % (mono, esc(intf.modalias)))
            parts.append("<tr><td></td><td colspan='3' style='color:gray'>%s"
                         "</td></tr>" % "<br>".join(extra))
        parts.append("</table>")
    rows = [
        ("Идентификатор", "<span style=\"%s\">%s</span>" % (mono, dev.id_text)),
        ("Название от устройства", esc(dev.name_from_device) or
         "<span style='color:gray'>устройство не сообщает строк</span>"),
        ("Название по базе usb.ids", esc(dev.name_from_db) or
         "<span style='color:gray'>нет записи</span>"),
        ("Серийный номер", esc(dev.serial) or "<span style='color:gray'>нет</span>"),
        ("Скорость", esc(speed_text(dev.speed))),
        ("Версия USB", esc(dev.usb_version.strip())),
        ("Шина и адрес", "%d / %d" % (dev.busnum, dev.devnum)),
    ]
    parts.append("<h3 style='margin:14px 0 4px 0'>Идентификация</h3>"
                 "<table cellpadding='3' cellspacing='0'>")
    for key, value in rows:
        parts.append("<tr><td style='color:gray; padding-right:14px'>%s</td>"
                     "<td>%s</td></tr>" % (key, value))
    parts.append("</table>")

    if dev.verdict == "missing":
        parts.append("<p style='color:gray'>Нажмите «Найти драйвер», чтобы "
                     "найти внешний драйвер на GitHub, или «Найти драйвер без ИИ» - "
                     "без языковой модели.</p>")
    return "".join(parts)
