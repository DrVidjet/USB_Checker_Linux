"""Приём событий ядра о подключении устройств (uevent) через netlink.

Ядро рассылает события в сокеты семейства NETLINK_KOBJECT_UEVENT,
многоадресная группа 1 (lib/kobject_uevent.c: parms->dst_group = 1).
Сообщение имеет вид "ACTION@DEVPATH\\0KEY=VALUE\\0KEY=VALUE\\0...".
Получать сообщения этого семейства может любой пользователь (netlink(7)),
права root не нужны.
"""

import errno
import os
import socket
from dataclasses import dataclass, field

# include/uapi/linux/netlink.h; в модуле socket Python константы нет
NETLINK_KOBJECT_UEVENT = 15
KERNEL_GROUP = 1
# Размер переменных события ограничен UEVENT_BUFFER_SIZE = 2048 байт
# (include/linux/kobject.h), плюс заголовок ACTION@DEVPATH.
RECV_SIZE = 16384


@dataclass
class Uevent:
    action: str            # add, remove, change, move, online, offline, bind, unbind
    devpath: str           # путь в /sys без префикса /sys
    env: dict = field(default_factory=dict)

    @property
    def name(self):
        return os.path.basename(self.devpath)

    @property
    def subsystem(self):
        return self.env.get("SUBSYSTEM", "")

    @property
    def devtype(self):
        return self.env.get("DEVTYPE", "")


def parse_uevent(data):
    """Разбирает сообщение ядра. Возвращает None для чужих форматов."""
    parts = data.split(b"\0")
    header = parts[0].decode("utf-8", errors="replace")
    if "@" not in header:
        return None   # например, сообщения libudev начинаются с "libudev"
    action, devpath = header.split("@", 1)
    env = {}
    for part in parts[1:]:
        if b"=" in part:
            key, value = part.decode("utf-8", errors="replace").split("=", 1)
            env[key] = value
    return Uevent(action, devpath, env)


class UeventOverflow(Exception):
    """Буфер сокета переполнился, часть событий потеряна (ENOBUFS)."""


class UeventSocket:
    """Неблокирующий сокет netlink для приёма событий ядра."""

    def __init__(self, rcvbuf=1 << 20):
        self.sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW,
                                  NETLINK_KOBJECT_UEVENT)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        except OSError:
            pass
        # Адрес (pid, groups): pid = 0 - номер назначит ядро,
        # groups - битовая маска групп, бит 0 соответствует группе 1.
        self.sock.bind((0, 1 << (KERNEL_GROUP - 1)))
        self.sock.setblocking(False)

    def fileno(self):
        return self.sock.fileno()

    def receive(self):
        """Забирает все накопившиеся события."""
        events = []
        while True:
            try:
                data, (sender_pid, _groups) = self.sock.recvfrom(RECV_SIZE)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno == errno.ENOBUFS:
                    raise UeventOverflow() from exc
                raise
            if sender_pid != 0:
                continue   # принимаются только сообщения от самого ядра
            event = parse_uevent(data)
            if event is not None:
                events.append(event)
        return events

    def close(self):
        self.sock.close()
