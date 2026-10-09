"""Сведения об устройствах USB из sysfs и итоговая проверка драйверов.

Устройства перечислены в /sys/bus/usb/devices:
  * usbN          - корневые концентраторы контроллеров (пропускаются);
  * B-P[.P...]    - устройства (B - номер шины, P - номера портов);
  * B-P:C.I       - интерфейсы устройства (C - конфигурация, I - номер).
Имена атрибутов взяты из Documentation/ABI/testing/sysfs-bus-usb и
drivers/usb/core/sysfs.c.
"""

import os
from dataclasses import dataclass, field, asdict

from . import sysio
from . import kernel_drivers as kd
from .usbids import class_name_ru

# Итоговые оценки устройства
OK = "ok"              # все интерфейсы обслуживаются драйверами ядра
INFO = "info"          # драйвер ядра не нужен или интерфейс занят программой
WARN = "warn"          # драйвер в ядре есть, но не работает
MISSING = "missing"    # для части интерфейсов драйвера в ядре нет
BLOCKED = "blocked"    # устройство не авторизовано (authorized = 0)

SPEED_NAMES = {
    "1.5": "1.5 Мбит/с (Low Speed)",
    "12": "12 Мбит/с (Full Speed)",
    "480": "480 Мбит/с (High Speed, USB 2.0)",
    "5000": "5 Гбит/с (SuperSpeed, USB 3.x)",
    "10000": "10 Гбит/с (SuperSpeed+, USB 3.x)",
    "20000": "20 Гбит/с (SuperSpeed+ 2x2, USB 3.2)",
}


@dataclass
class UsbInterface:
    name: str
    number: int
    cls: int
    subclass: int
    protocol: int
    class_ru: str
    class_db: str
    label: str
    modalias: str
    driver: dict


@dataclass
class UsbDevice:
    name: str
    busnum: int
    devnum: int
    vid: int
    pid: int
    bcd_device: str
    manufacturer: str
    product: str
    serial: str
    dev_class: int
    dev_subclass: int
    dev_protocol: int
    speed: str
    usb_version: str
    authorized: bool
    vendor_db: str
    product_db: str
    interfaces: list = field(default_factory=list)
    verdict: str = ""
    verdict_text: str = ""

    @property
    def id_text(self):
        return "%04x:%04x" % (self.vid, self.pid)

    @property
    def name_from_device(self):
        return join_names(self.manufacturer, self.product)

    @property
    def name_from_db(self):
        return join_names(self.vendor_db, self.product_db)

    @property
    def full_name(self):
        """Полное наименование: строки самого устройства, иначе база usb.ids."""
        return (self.name_from_device or self.name_from_db or
                "Неизвестное устройство %s" % self.id_text)

    @property
    def kind_text(self):
        """Тип устройства по классу (для составных - по интерфейсам)."""
        if self.dev_class not in (0x00, 0xEF):
            return class_name_ru(self.dev_class)
        kinds = []
        for intf in self.interfaces:
            if intf.class_ru not in kinds:
                kinds.append(intf.class_ru)
        return ", ".join(kinds) if kinds else class_name_ru(self.dev_class)

    def signature(self):
        """Краткий отпечаток состояния драйверов (для поиска изменений)."""
        return tuple((i.name, i.driver["status"], i.driver["driver"])
                     for i in self.interfaces)

    def to_dict(self):
        """Данные для передачи другому процессу (будущий модуль поиска)."""
        data = asdict(self)
        data["id"] = self.id_text
        data["full_name"] = self.full_name
        data["name_from_device"] = self.name_from_device
        data["name_from_db"] = self.name_from_db
        data["kind"] = self.kind_text
        return data


def join_names(vendor, product):
    vendor = (vendor or "").strip()
    product = (product or "").strip()
    if vendor and product:
        if product.lower().startswith(vendor.lower()):
            return product
        return vendor + " " + product
    return vendor or product


def is_device_name(name):
    """Имя устройства (не корневого хаба и не интерфейса)."""
    return "-" in name and ":" not in name and not name.startswith("usb")


class UsbInspector:
    """Читает устройства из sysfs и проверяет драйверы их интерфейсов."""

    def __init__(self, ids, modules, sys_root="/sys"):
        self.ids = ids
        self.modules = modules
        self.sys_root = sys_root
        self.devices_dir = os.path.join(sys_root, "bus", "usb", "devices")

    def device_names(self):
        return [n for n in sysio.list_dir(self.devices_dir) if is_device_name(n)]

    def read_device(self, name):
        path = os.path.join(self.devices_dir, name)
        vid = sysio.read_hex(os.path.join(path, "idVendor"))
        pid = sysio.read_hex(os.path.join(path, "idProduct"))
        if vid is None or pid is None:
            return None   # устройство уже отключено

        def attr(key):
            return sysio.read_attr(os.path.join(path, key))

        def number(key):
            value = attr(key)
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        dev = UsbDevice(
            name=name,
            busnum=number("busnum"),
            devnum=number("devnum"),
            vid=vid,
            pid=pid,
            bcd_device=attr("bcdDevice") or "",
            manufacturer=attr("manufacturer") or "",
            product=attr("product") or "",
            serial=attr("serial") or "",
            dev_class=sysio.read_hex(os.path.join(path, "bDeviceClass")) or 0,
            dev_subclass=sysio.read_hex(
                os.path.join(path, "bDeviceSubClass")) or 0,
            dev_protocol=sysio.read_hex(
                os.path.join(path, "bDeviceProtocol")) or 0,
            speed=attr("speed") or "",
            usb_version=attr("version") or "",
            authorized=attr("authorized") != "0",
            vendor_db=self.ids.vendor_name(vid) or "",
            product_db=self.ids.product_name(vid, pid) or "",
        )
        for entry in sysio.list_dir(path):
            if entry.startswith(name + ":"):
                intf = self._read_interface(os.path.join(path, entry), entry)
                if intf:
                    dev.interfaces.append(intf)
        dev.interfaces.sort(key=lambda i: i.number)
        dev.verdict, dev.verdict_text = make_verdict(dev)
        return dev

    def _read_interface(self, path, name):
        cls = sysio.read_hex(os.path.join(path, "bInterfaceClass"))
        if cls is None:
            return None
        subclass = sysio.read_hex(os.path.join(path, "bInterfaceSubClass")) or 0
        protocol = sysio.read_hex(os.path.join(path, "bInterfaceProtocol")) or 0
        modalias = sysio.read_attr(os.path.join(path, "modalias")) or ""
        info = kd.check_interface(self.modules, path, modalias,
                                  cls, subclass, protocol)
        return UsbInterface(
            name=name,
            number=sysio.read_hex(os.path.join(path, "bInterfaceNumber")) or 0,
            cls=cls,
            subclass=subclass,
            protocol=protocol,
            class_ru=class_name_ru(cls),
            class_db=self.ids.class_name(cls, subclass, protocol) or "",
            label=sysio.read_attr(os.path.join(path, "interface")) or "",
            modalias=modalias,
            driver=asdict(info),
        )


def make_verdict(dev):
    """Итог проверки драйверов для устройства: (оценка, текст)."""
    if not dev.authorized:
        return BLOCKED, ("Устройство не авторизовано (authorized = 0): "
                         "его блокирует политика безопасности, например "
                         "USBGuard. Драйверы к нему не привязываются.")
    if not dev.interfaces:
        return WARN, ("У устройства нет активной конфигурации, интерфейсы "
                      "не созданы. Проверка драйверов невозможна.")

    groups = {}
    for intf in dev.interfaces:
        groups.setdefault(intf.driver["status"], []).append(intf)

    def numbers(status):
        return ", ".join("№%d" % i.number for i in groups.get(status, []))

    def details(statuses):
        return "; ".join("интерфейс №%d - %s" % (i.number, i.driver["note"])
                         for s in statuses for i in groups.get(s, []))

    drivers = []
    for intf in groups.get(kd.BOUND, []):
        d = intf.driver
        if d["module_kind"] == "builtin":
            text = "%s (встроен в ядро)" % d["driver"]
        elif d["module"] == d["driver"]:
            text = "%s (модуль загружен)" % d["driver"]
        else:
            text = "%s (модуль %s загружен)" % (d["driver"], d["module"])
        if text not in drivers:
            drivers.append(text)
    working = ("Работает: " + ", ".join(drivers) + ".") if drivers else ""

    if kd.MISSING in groups:
        head = ("Драйвер в ядре не найден для интерфейса %s. "
                "Потребуется поиск внешнего драйвера." % numbers(kd.MISSING))
        return MISSING, (head + " " + working).strip()

    problems = (kd.AVAILABLE, kd.BLACKLISTED, kd.LOADED_UNBOUND, kd.UNKNOWN)
    if any(s in groups for s in problems):
        head = "Проверку выполнить не удалось: " if kd.UNKNOWN in groups \
            else "Драйвер в ядре есть, но не работает: "
        return WARN, (head + details(problems) + ". " + working).strip()

    if kd.USERSPACE in groups or kd.USBFS in groups:
        head = "Драйвер ядра не требуется: " if drivers == [] else ""
        text = head + details((kd.USERSPACE, kd.USBFS)) + "."
        return INFO, (working + " " + text[0].upper() + text[1:]).strip()
    return OK, "Драйвер есть в ядре и работает: " + ", ".join(drivers) + "."


def speed_text(speed):
    return SPEED_NAMES.get(speed, speed + " Мбит/с" if speed else "нет данных")
