"""Проверка наличия драйвера для интерфейса USB в ядре Linux.

Как ядро выбирает драйвер:
  * каждый интерфейс USB публикует строку modalias вида
    usb:vVVVVpPPPPdDDDDdcDCdscDSdpDPicICiscISipIPinIN
    (drivers/usb/core/sysfs.c, modalias_show);
  * драйверы объявляют поддерживаемые устройства через MODULE_DEVICE_TABLE,
    при сборке из них получаются шаблоны (scripts/mod/file2alias.c);
  * depmod записывает шаблоны загружаемых модулей в
    /lib/modules/<версия>/modules.alias в виде "alias <шаблон> <модуль>",
    а шаблоны встроенных в ядро драйверов лежат в modules.builtin.modinfo
    в виде записей "модуль.alias=шаблон\\0";
  * modprobe сравнивает modalias с шаблонами функцией fnmatch(3).

Модуль повторяет эту логику, а также учитывает чёрные списки modprobe.d.
"""

import fnmatch
import os
from dataclasses import dataclass, field

from . import sysio

MODPROBE_DIRS = (
    "/etc/modprobe.d",
    "/run/modprobe.d",
    "/usr/local/lib/modprobe.d",
    "/usr/lib/modprobe.d",
    "/lib/modprobe.d",
)

# Статусы интерфейса
BOUND = "bound"                  # драйвер ядра привязан
USBFS = "usbfs"                  # интерфейс занят программой через usbfs
LOADED_UNBOUND = "loaded_unbound"  # драйвер в ядре есть и загружен, но не привязан
AVAILABLE = "available"          # модуль есть, но не загружен
BLACKLISTED = "blacklisted"      # модуль есть, но в чёрном списке
USERSPACE = "userspace"          # класс обслуживается программами пользователя
MISSING = "missing"              # драйвера в ядре нет
UNKNOWN = "unknown"              # проверить не удалось

# Классы, для которых драйвер ядра обычно не нужен: устройство обслуживает
# программа пользовательского пространства через libusb.
# Ключ: (класс, подкласс или None, протокол или None).
USERSPACE_CLASSES = (
    ((0x06, 0x01, 0x01),
     "камеры и телефоны в режиме PTP/MTP обслуживаются программами "
     "пользователя (libgphoto2, libmtp)"),
    ((0x0B, None, None),
     "считыватели смарт-карт обслуживает служба pcscd с драйвером ccid "
     "через libusb"),
    ((0xFE, 0x01, None),
     "режим обновления прошивки (DFU) используют утилиты вроде dfu-util"),
)


STATUS_RU = {
    BOUND: "драйвер работает",
    USBFS: "занят программой",
    LOADED_UNBOUND: "драйвер не привязан",
    AVAILABLE: "модуль не загружен",
    BLACKLISTED: "модуль в чёрном списке",
    USERSPACE: "драйвер ядра не нужен",
    MISSING: "нет драйвера в ядре",
    UNKNOWN: "не проверено",
}


def normalize(module):
    """modprobe не различает '-' и '_' в именах модулей (modprobe(8))."""
    return module.replace("-", "_")


@dataclass
class DriverInfo:
    status: str
    driver: str = None          # имя драйвера, привязанного к интерфейсу
    module: str = None          # модуль, которому принадлежит драйвер
    module_kind: str = None     # "loadable" или "builtin"
    candidates: list = field(default_factory=list)  # [(модуль, вид)]
    note: str = ""


class KernelModules:
    """Индекс шаблонов modalias для USB из файлов depmod."""

    def __init__(self, modules_dir=None, sys_root="/sys", proc_root="/proc",
                 modprobe_dirs=MODPROBE_DIRS):
        self.release = os.uname().release
        self.modules_dir = modules_dir or "/lib/modules/" + self.release
        self.sys_root = sys_root
        self.proc_root = proc_root
        self.modprobe_dirs = modprobe_dirs
        self.available = False
        self.error = ""
        self.builtin = set()          # имена встроенных модулей
        self.by_vendor = {}           # "0BDA" -> [(шаблон, модуль, вид)]
        self.generic = []             # шаблоны без конкретного производителя
        self.blacklist = {}           # модуль -> источник записи
        self.alias_count = 0

    def load(self):
        alias_file = os.path.join(self.modules_dir, "modules.alias")
        if not sysio.exists(alias_file):
            self.error = (
                "не найден файл %s. Если ядро обновлено пакетным менеджером, "
                "а система не перезагружена, каталог модулей работающего ядра "
                "мог быть удалён: перезагрузите компьютер." % alias_file)
            return self
        self._load_aliases(alias_file)
        self._load_builtin()
        self._load_blacklist()
        self.available = True
        return self

    def _add_alias(self, pattern, module, kind):
        entry = (pattern, normalize(module), kind)
        vendor = pattern[5:9]
        if pattern.startswith("usb:v") and len(vendor) == 4 and \
                all(c in "0123456789ABCDEF" for c in vendor):
            self.by_vendor.setdefault(vendor, []).append(entry)
        else:
            self.generic.append(entry)
        self.alias_count += 1

    def _load_aliases(self, alias_file):
        text = sysio.read_bytes(alias_file).decode("utf-8", errors="replace")
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[0] == "alias" and \
                    parts[1].startswith("usb:"):
                self._add_alias(parts[1], parts[2], "loadable")

    def _load_builtin(self):
        # modules.builtin: пути встроенных модулей, по одному в строке
        path = os.path.join(self.modules_dir, "modules.builtin")
        if sysio.exists(path):
            for line in sysio.read_bytes(path).decode().splitlines():
                name = os.path.basename(line.strip())
                if name.endswith(".ko"):
                    self.builtin.add(normalize(name[:-3]))
        # modules.builtin.modinfo: записи "модуль.ключ=значение\0"
        path = os.path.join(self.modules_dir, "modules.builtin.modinfo")
        if sysio.exists(path):
            for record in sysio.read_bytes(path).split(b"\0"):
                if b".alias=usb:" not in record:
                    continue
                left, value = record.decode(errors="replace").split("=", 1)
                module, key = left.split(".", 1)
                if key == "alias":
                    self._add_alias(value, module, "builtin")
                    self.builtin.add(normalize(module))

    def _load_blacklist(self):
        # Файл с одинаковым именем берётся из каталога с большим приоритетом,
        # затем все файлы читаются в лексикографическом порядке (modprobe.d(5)).
        chosen = {}
        for directory in self.modprobe_dirs:
            for name in sysio.list_dir(directory):
                if name.endswith(".conf") and name not in chosen:
                    chosen[name] = os.path.join(directory, name)
        for name in sorted(chosen):
            try:
                text = sysio.read_bytes(chosen[name]).decode(errors="replace")
            except OSError:
                continue
            text = text.replace("\\\n", " ")
            for line in text.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "blacklist":
                    self.blacklist.setdefault(normalize(parts[1]), chosen[name])
        # Параметр командной строки ядра modprobe.blacklist=a,b (modprobe(8))
        cmdline = sysio.read_attr(os.path.join(self.proc_root, "cmdline")) or ""
        for arg in cmdline.split():
            if arg.startswith("modprobe.blacklist="):
                for module in arg.split("=", 1)[1].split(","):
                    if module:
                        self.blacklist.setdefault(normalize(module),
                                                  "командная строка ядра")

    def match(self, modalias):
        """Модули, шаблоны которых подходят к modalias: [(модуль, вид)]."""
        if not modalias or not modalias.startswith("usb:v"):
            return []
        entries = self.by_vendor.get(modalias[5:9], []) + self.generic
        result = []
        for pattern, module, kind in entries:
            if fnmatch.fnmatchcase(modalias, pattern) and \
                    (module, kind) not in result:
                result.append((module, kind))
        return result

    def module_kind(self, module):
        """'loadable', если модуль загружен как .ko, иначе 'builtin'.

        Атрибут initstate есть в /sys/module/<имя> только у загружаемых
        модулей (kernel/module/main.c, modinfo_initstate).
        """
        if sysio.exists(os.path.join(self.sys_root, "module",
                                     normalize(module), "initstate")):
            return "loadable"
        return "builtin"

    def is_active(self, module, kind):
        """Код модуля сейчас находится в ядре (встроен или загружен)."""
        if kind == "builtin":
            return True
        return sysio.exists(os.path.join(self.sys_root, "module",
                                         normalize(module), "initstate"))


def userspace_hint(cls, subclass, protocol):
    for (c, s, p), text in USERSPACE_CLASSES:
        if c == cls and (s is None or s == subclass) and \
                (p is None or p == protocol):
            return text
    return None


def check_interface(modules, intf_path, modalias, cls, subclass, protocol):
    """Определяет, есть ли в ядре драйвер для интерфейса."""
    driver = sysio.link_target_name(os.path.join(intf_path, "driver"))
    if driver == "usbfs":
        # drivers/usb/core/devio.c: интерфейс, захваченный программой
        # через /dev/bus/usb (libusb), привязан к драйверу "usbfs".
        return DriverInfo(USBFS, driver=driver,
                          note="интерфейс занят программой пользователя "
                               "через usbfs (libusb)")
    if driver:
        module = sysio.link_target_name(
            os.path.join(intf_path, "driver", "module"))
        kind = modules.module_kind(module) if module else "builtin"
        return DriverInfo(BOUND, driver=driver, module=module or driver,
                          module_kind=kind)

    if not modules.available:
        return DriverInfo(UNKNOWN, note=modules.error)

    candidates = modules.match(modalias)
    if candidates:
        active = [c for c in candidates if modules.is_active(*c)]
        allowed = [c for c in candidates if c[0] not in modules.blacklist]
        if active:
            names = ", ".join(m for m, _ in active)
            return DriverInfo(
                LOADED_UNBOUND, candidates=candidates,
                note="подходящий драйвер (%s) уже в ядре, но к интерфейсу "
                     "не привязан: привязка ещё идёт, устройство отклонено "
                     "драйвером или драйвер отвязан вручную" % names)
        if allowed:
            return DriverInfo(
                AVAILABLE, candidates=candidates,
                note="модуль %s есть в ядре, но не загружен"
                     % ", ".join(m for m, _ in allowed))
        module = candidates[0][0]
        return DriverInfo(
            BLACKLISTED, candidates=candidates,
            note="модуль %s есть в ядре, но занесён в чёрный список (%s)"
                 % (module, modules.blacklist[module]))

    hint = userspace_hint(cls, subclass, protocol)
    if hint:
        return DriverInfo(USERSPACE, note=hint)
    return DriverInfo(MISSING, note="подходящего драйвера в ядре %s нет"
                      % modules.release)
