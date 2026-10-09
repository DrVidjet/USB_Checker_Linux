"""Консольный режим монитора USB.

    python -m usbmon           список устройств, затем слежение за событиями
    python -m usbmon --list    только список устройств
    python -m usbmon --json    вывод в формате JSON
"""

import argparse
import json
import select
import sys
import time

from . import create_tracker
from . import tracker as tr
from .devices import speed_text
from .kernel_drivers import STATUS_RU
from .uevent import UeventSocket, UeventOverflow

POLL_INTERVAL = 2.0   # период опроса sysfs, если netlink недоступен
MARKS = {"ok": "[OK]", "info": "[i]", "warn": "[!]", "missing": "[X]",
         "blocked": "[-]"}


def format_device(dev):
    lines = [
        "%s %s [%s]" % (MARKS.get(dev.verdict, ""), dev.full_name, dev.id_text),
        "    порт: %s (шина %d, адрес %d), скорость: %s" % (
            dev.name, dev.busnum, dev.devnum, speed_text(dev.speed)),
        "    по данным устройства: %s" % (dev.name_from_device or "нет строк"),
        "    по базе usb.ids:      %s" % (dev.name_from_db or "нет записи"),
        "    тип: %s" % dev.kind_text,
    ]
    for intf in dev.interfaces:
        d = intf.driver
        status = STATUS_RU.get(d["status"], d["status"])
        if d["driver"]:
            status += " (%s)" % d["driver"]
        lines.append("    интерфейс №%d %02x/%02x/%02x %s: %s" % (
            intf.number, intf.cls, intf.subclass, intf.protocol,
            intf.class_ru, status))
    lines.append("    итог: " + dev.verdict_text)
    return "\n".join(lines)


def emit(notices, as_json):
    for notice in notices:
        if as_json:
            print(json.dumps({"event": notice.kind, "port": notice.name,
                              "text": notice.text,
                              "device": notice.device.to_dict()
                              if notice.device else None},
                             ensure_ascii=False), flush=True)
            continue
        print(time.strftime("%H:%M:%S"), tr.describe(notice), flush=True)
        if notice.device and notice.kind in (tr.CONNECTED, tr.CHANGED,
                                             tr.PRESENT):
            print(format_device(notice.device), flush=True)


def watch(tracker, as_json):
    try:
        sock = UeventSocket()
    except OSError as exc:
        print("netlink недоступен (%s), опрос sysfs каждые %.0f с"
              % (exc, POLL_INTERVAL), file=sys.stderr)
        sock = None

    poller = select.poll()
    if sock:
        poller.register(sock.fileno(), select.POLLIN)
    deadlines = {}      # имя устройства -> момент проверки
    next_poll = time.monotonic() + POLL_INTERVAL

    while True:
        now = time.monotonic()
        waits = [d - now for d in deadlines.values()]
        if sock is None:
            waits.append(next_poll - now)
        timeout = max(0, min(waits)) if waits else None
        ready = poller.poll(None if timeout is None else int(timeout * 1000))

        if ready and sock:
            try:
                events = sock.receive()
            except UeventOverflow:
                events = []
                names, notices = tracker.resync()
                emit(notices, as_json)
                for name in names + list(tracker.devices):
                    deadlines[name] = time.monotonic() + tr.SETTLE_DELAY
            for event in events:
                names, notices = tracker.handle(event)
                emit(notices, as_json)
                for name in names:
                    deadlines[name] = time.monotonic() + tr.SETTLE_DELAY
                for notice in notices:
                    if notice.kind == tr.DISCONNECTED:
                        deadlines.pop(notice.name, None)

        if sock is None and time.monotonic() >= next_poll:
            names, notices = tracker.resync()
            emit(notices, as_json)
            for name in names:
                deadlines[name] = time.monotonic()
            next_poll = time.monotonic() + POLL_INTERVAL

        now = time.monotonic()
        for name in [n for n, d in deadlines.items() if d <= now]:
            del deadlines[name]
            emit(tracker.inspect(name), as_json)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m usbmon",
                                     description="Монитор устройств USB")
    parser.add_argument("--list", action="store_true",
                        help="вывести подключённые устройства и выйти")
    parser.add_argument("--json", action="store_true", help="вывод в JSON")
    parser.add_argument("--sys-root", default="/sys", help=argparse.SUPPRESS)
    parser.add_argument("--proc-root", default="/proc", help=argparse.SUPPRESS)
    parser.add_argument("--modules-dir", help=argparse.SUPPRESS)
    parser.add_argument("--usb-ids", help="путь к файлу usb.ids")
    parser.add_argument("--log", help="путь к лог-файлу")
    args = parser.parse_args(argv)

    tracker = create_tracker(args.sys_root, args.modules_dir, args.usb_ids,
                             args.log, args.proc_root)
    modules = tracker.inspector.modules
    if not args.json:
        print("Ядро %s; шаблонов USB в базе модулей: %d; usb.ids: %s" % (
            modules.release, modules.alias_count,
            tracker.inspector.ids.path or "не найден"))
        if not modules.available:
            print("Внимание: " + modules.error)
    notices = tracker.scan()
    emit(notices, args.json)
    if not notices and not args.json:
        print("Устройства USB не подключены.")
    if args.list:
        return 0
    if not args.json:
        print("Слежение за подключениями. Выход - Ctrl+C.", flush=True)
    try:
        watch(tracker, args.json)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
