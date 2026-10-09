"""Разбор базы идентификаторов USB (файл usb.ids).

Формат файла описан в его заголовке:
    vendor  vendor_name
    <TAB>device  device_name
    <TAB><TAB>interface  interface_name
    ...
    C class  class_name
    <TAB>subclass  subclass_name
    <TAB><TAB>protocol  protocol_name

В Arch Linux файл устанавливается пакетом hwdata в /usr/share/hwdata/usb.ids.
"""

import os
import re

from . import sysio

DEFAULT_PATHS = (
    "/usr/share/hwdata/usb.ids",      # Arch Linux, Fedora (пакет hwdata)
    "/usr/share/misc/usb.ids",        # Debian, Ubuntu
    "/usr/share/usb.ids",
    "/var/lib/usbutils/usb.ids",
)

_VENDOR_RE = re.compile(r"^([0-9a-fA-F]{4})\s+(.*)$")
_CLASS_RE = re.compile(r"^C ([0-9a-fA-F]{2})\s+(.*)$")
_ITEM_RE = re.compile(r"^([0-9a-fA-F]{2,4})\s+(.*)$")

# Русские названия стандартных классов USB (коды назначает USB-IF).
CLASS_NAMES_RU = {
    0x01: "Аудио",
    0x02: "Связь (CDC)",
    0x03: "Устройство ввода (HID)",
    0x05: "Физический интерфейс",
    0x06: "Изображения (камера, телефон PTP/MTP)",
    0x07: "Принтер",
    0x08: "Накопитель (Mass Storage)",
    0x09: "Концентратор (хаб)",
    0x0A: "Данные CDC",
    0x0B: "Считыватель смарт-карт",
    0x0D: "Защита контента",
    0x0E: "Видео (веб-камера)",
    0x0F: "Медицинское устройство",
    0x10: "Аудио/видео",
    0x11: "Billboard",
    0x12: "Мост USB Type-C",
    0xDC: "Диагностическое устройство",
    0xE0: "Беспроводной контроллер (Bluetooth и др.)",
    0xEF: "Разное (составное устройство)",
    0xFE: "Специального назначения (DFU и др.)",
    0xFF: "Определяется производителем",
}


class UsbIds:
    """Справочник производителей, устройств и классов USB."""

    def __init__(self):
        self.path = None
        self.vendors = {}   # vid -> (имя, {pid: имя})
        self.classes = {}   # класс -> (имя, {подкласс: (имя, {протокол: имя})})

    @classmethod
    def load(cls, path=None):
        """Загружает базу из указанного файла или из стандартных мест."""
        db = cls()
        candidates = [path] if path else list(DEFAULT_PATHS)
        env_path = os.environ.get("USBMON_USB_IDS")
        if env_path and not path:
            candidates.insert(0, env_path)
        for candidate in candidates:
            if candidate and sysio.exists(candidate):
                db._parse(sysio.read_bytes(candidate))
                db.path = candidate
                break
        return db

    @property
    def loaded(self):
        return self.path is not None

    def _parse(self, raw):
        # Исходный файл с linux-usb.org в кодировке latin-1, hwdata
        # перекодирует его в UTF-8; поддерживаются оба варианта.
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")

        mode = None          # "vendor", "class" или None (прочие разделы)
        vendor_items = None
        class_entry = None
        sub_entry = None

        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            if not line.startswith("\t"):
                m = _VENDOR_RE.match(line)
                if m:
                    mode = "vendor"
                    vendor_items = {}
                    self.vendors[int(m.group(1), 16)] = (m.group(2).strip(),
                                                         vendor_items)
                    continue
                m = _CLASS_RE.match(line)
                if m:
                    mode = "class"
                    class_entry = {}
                    self.classes[int(m.group(1), 16)] = (m.group(2).strip(),
                                                         class_entry)
                    continue
                mode = None  # разделы AT, HID, R, BIAS и т.д. не нужны
                continue

            if mode is None:
                continue
            depth = len(line) - len(line.lstrip("\t"))
            m = _ITEM_RE.match(line.strip())
            if not m:
                continue
            code = int(m.group(1), 16)
            name = m.group(2).strip()
            if mode == "vendor" and depth == 1:
                vendor_items[code] = name
            elif mode == "class" and depth == 1:
                sub_entry = {}
                class_entry[code] = (name, sub_entry)
            elif mode == "class" and depth == 2 and sub_entry is not None:
                sub_entry[code] = name

    def vendor_name(self, vid):
        entry = self.vendors.get(vid)
        return entry[0] if entry else None

    def product_name(self, vid, pid):
        entry = self.vendors.get(vid)
        return entry[1].get(pid) if entry else None

    def class_name(self, cls, subclass=None, protocol=None):
        """Название класса с уточнением подкласса и протокола (англ.)."""
        entry = self.classes.get(cls)
        if not entry:
            return None
        parts = [entry[0]]
        if subclass is not None and subclass in entry[1]:
            sub_name, protocols = entry[1][subclass]
            if sub_name and sub_name != "?":
                parts.append(sub_name)
            if protocol is not None and protocol in protocols:
                parts.append(protocols[protocol])
        return " / ".join(parts)


def class_name_ru(cls):
    """Краткое русское название класса USB."""
    return CLASS_NAMES_RU.get(cls, "Класс 0x%02x" % cls)
