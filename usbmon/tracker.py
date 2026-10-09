"""Отслеживание состояния устройств USB по событиям ядра.

Логика не зависит от интерфейса: окно PyQt5 и консольный режим получают
от трекера список устройств, которые нужно перечитать (с задержкой), и
список уведомлений для журнала.

Задержка нужна потому, что после события add для устройства ядро ещё
создаёт интерфейсы, а udev загружает модули и привязывает драйверы.
Каждое новое событие того же устройства откладывает проверку заново.
"""

import os
import time
from dataclasses import dataclass

from .devices import is_device_name

SETTLE_DELAY = 0.8   # секунд тишины перед проверкой устройства

CONNECTED = "connected"
PRESENT = "present"
DISCONNECTED = "disconnected"
CHANGED = "changed"
BIND = "bind"
UNBIND = "unbind"

LOG_TITLES = {
    CONNECTED: "ПОДКЛЮЧЕНО",
    PRESENT: "ОБНАРУЖЕНО ПРИ ЗАПУСКЕ",
    DISCONNECTED: "ОТКЛЮЧЕНО",
    CHANGED: "ИЗМЕНЕНИЕ ДРАЙВЕРОВ",
    BIND: "ДРАЙВЕР ПРИВЯЗАН",
    UNBIND: "ДРАЙВЕР ОТВЯЗАН",
}


@dataclass
class Notice:
    kind: str
    name: str
    device: object = None
    text: str = ""


class LogFile:
    """Лог-файл, запись системными вызовами open(O_APPEND)/write/close.

    Флаг O_APPEND гарантирует, что каждая запись попадает в конец файла
    целиком, даже если в файл пишут несколько процессов одновременно.
    """

    def __init__(self, path):
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def write(self, *fields):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = " | ".join((stamp,) + tuple(str(f) for f in fields)) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT |
                     os.O_CLOEXEC, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)


class DeviceTracker:
    def __init__(self, inspector, log=None):
        self.inspector = inspector
        self.log = log
        self.devices = {}

    # --- начальное состояние -------------------------------------------
    def scan(self):
        """Перечитывает все подключённые устройства (при запуске)."""
        notices = []
        for name in self.inspector.device_names():
            dev = self.inspector.read_device(name)
            if dev:
                self.devices[name] = dev
                notices.append(self._notice(PRESENT, name, dev))
        return notices

    def resync(self):
        """Сверка с sysfs после потери событий или в режиме опроса.

        Возвращает (имена для проверки, уведомления об отключении).
        """
        current = set(self.inspector.device_names())
        to_check = sorted(current - set(self.devices))
        notices = [self._notice(DISCONNECTED, name, self.devices.pop(name))
                   for name in sorted(set(self.devices) - current)]
        return to_check, notices

    # --- обработка событий ----------------------------------------------
    def handle(self, event):
        """Возвращает (имена устройств для отложенной проверки, уведомления)."""
        if event.subsystem != "usb":
            return [], []
        if event.devtype == "usb_device":
            name = event.name
            if not is_device_name(name):
                return [], []
            if event.action == "remove":
                if name in self.devices:
                    dev = self.devices.pop(name)
                    return [], [self._notice(DISCONNECTED, name, dev)]
                return [], []
            return [name], []

        if event.devtype == "usb_interface":
            dev_name = event.name.split(":")[0]
            if not is_device_name(dev_name):
                return [], []
            notices = []
            if event.action == "bind":
                driver = event.env.get("DRIVER", "?")
                notices.append(self._notice(
                    BIND, dev_name, self.devices.get(dev_name),
                    "драйвер %s привязан к интерфейсу %s" % (driver, event.name)))
            elif event.action == "unbind":
                notices.append(self._notice(
                    UNBIND, dev_name, self.devices.get(dev_name),
                    "драйвер отвязан от интерфейса %s" % event.name))
            return [dev_name], notices
        return [], []

    def inspect(self, name):
        """Читает устройство после затишья. Возвращает уведомления."""
        dev = self.inspector.read_device(name)
        old = self.devices.get(name)
        if dev is None:
            if old is not None:
                del self.devices[name]
                return [self._notice(DISCONNECTED, name, old)]
            return []
        self.devices[name] = dev
        if old is None:
            return [self._notice(CONNECTED, name, dev)]
        if old.signature() != dev.signature() or old.verdict != dev.verdict:
            return [self._notice(CHANGED, name, dev)]
        return []

    # --- журнал ---------------------------------------------------------
    def _notice(self, kind, name, dev, text=""):
        notice = Notice(kind, name, dev, text)
        if self.log:
            title = LOG_TITLES.get(kind, kind.upper())
            if dev is not None and kind == DISCONNECTED:
                self.log.write(title, "порт " + name, dev.id_text,
                               dev.full_name)
            elif dev is not None:
                self.log.write(title, "порт " + name, dev.id_text,
                               dev.full_name, text or dev.verdict_text)
            else:
                self.log.write(title, "порт " + name, text)
        return notice


def describe(notice):
    """Строка для экранного журнала."""
    dev = notice.device
    title = "%s [%s]" % (dev.full_name, dev.id_text) if dev else notice.name
    if notice.kind == CONNECTED:
        return "Подключено: %s, порт %s" % (title, notice.name)
    if notice.kind == PRESENT:
        return "Уже подключено: %s, порт %s" % (title, notice.name)
    if notice.kind == DISCONNECTED:
        return "Отключено: %s" % title
    if notice.kind == CHANGED:
        return "Изменилось состояние драйверов: %s" % title
    return "%s: %s" % (title, notice.text)


def default_log_path():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "logs", "usb_monitor.log")
