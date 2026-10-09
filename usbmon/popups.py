"""Всплывающие окна как отдельные процессы, связь через безымянные каналы.

Для каждого окна создаются два канала системным вызовом pipe(2):
  «главное окно -> окно»   - главное окно пишет, окно читает;
  «окно -> главное окно»   - окно пишет, главное окно читает.
Процесс окна порождается через subprocess.Popen (fork(2) и execve(2));
в него передаются только нужные концы каналов (pass_fds), остальные
дескрипторы созданы с флагом O_CLOEXEC и при execve закрываются.
После запуска главное окно закрывает у себя концы, отданные дочернему
процессу: иначе конец файла в канале не наступит никогда.

Когда главное окно закрывается, оно закрывает свои концы каналов, окна
получают конец файла и завершаются - этим обеспечивается их зависимость
от главного окна. Если главный процесс завершится аварийно, ядро закроет
его дескрипторы само, и результат будет тем же.
"""

import os
import subprocess
import sys
import time

from PyQt5.QtCore import QObject, QSocketNotifier, pyqtSignal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from drvsearch.channel import Channel      # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PopupProcess:
    def __init__(self, kind, title, proc, to_child, from_child):
        self.kind = kind
        self.title = title
        self.proc = proc
        self.pid = proc.pid
        self.started = time.time()
        self.writer = Channel(None, to_child)
        self.reader = Channel(from_child, None)
        self.notifier = None


class PopupManager(QObject):
    """Реестр всплывающих окон-процессов, порождённых приложением."""

    message = pyqtSignal(object, dict)     # (PopupProcess, сообщение)
    finished = pyqtSignal(object, int)     # (PopupProcess, код завершения)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.processes = {}                # pid -> PopupProcess

    def start(self, kind, title, module, payload, args=()):
        """Запускает окно-процесс `python -m module --window` и отправляет ему данные.

        payload - сообщение (dict) или список сообщений, отправляемых по порядку;
        args - дополнительные параметры командной строки (например, --no-llm).
        """
        to_child_r, to_child_w = os.pipe()
        from_child_r, from_child_w = os.pipe()
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", module, "--window",
                 "--in-fd", str(to_child_r), "--out-fd", str(from_child_w)] + list(args),
                pass_fds=(to_child_r, from_child_w), cwd=PROJECT_ROOT)
        finally:
            os.close(to_child_r)            # эти концы теперь принадлежат окну
            os.close(from_child_w)
        popup = PopupProcess(kind, title, proc, to_child_w, from_child_r)
        popup.notifier = QSocketNotifier(from_child_r, QSocketNotifier.Read, self)
        popup.notifier.activated.connect(lambda _fd, p=popup: self._on_readable(p))
        self.processes[popup.pid] = popup
        for message in (payload if isinstance(payload, list) else [payload]):
            popup.writer.send(message)
        return popup

    def broadcast(self, message, kind=None):
        """Отправляет сообщение всем работающим окнам (или окнам вида kind)."""
        for popup in list(self.processes.values()):
            if kind is None or popup.kind == kind:
                popup.writer.send(message)

    def _on_readable(self, popup):
        try:
            messages = popup.reader.receive()
        except OSError:
            messages = None
        if messages is None:                # окно закрыло канал - процесс завершается
            popup.notifier.setEnabled(False)
            popup.reader.close()
            popup.writer.close()
            try:
                code = popup.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                popup.proc.kill()
                code = popup.proc.wait()
            self.processes.pop(popup.pid, None)
            self.finished.emit(popup, code)
            return
        for message in messages:
            self.message.emit(popup, message)

    def close_all(self, timeout=3):
        """Закрывает каналы ко всем окнам: окна получат конец файла и завершатся.

        Возвращает список (окно, код завершения) для записи в лог-файл.
        """
        closed = []
        for popup in list(self.processes.values()):
            popup.writer.close()
        for popup in list(self.processes.values()):
            if popup.notifier:
                popup.notifier.setEnabled(False)
            try:
                code = popup.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                popup.proc.kill()
                code = popup.proc.wait()
            popup.reader.close()
            self.processes.pop(popup.pid, None)
            closed.append((popup, code))
        return closed
