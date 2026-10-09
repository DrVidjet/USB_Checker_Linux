"""Построение поддельного дерева sysfs, каталога модулей и usb.ids для тестов.

Структура повторяет реальную: /sys/bus/usb/devices/<имя>, ссылки driver на
/sys/bus/usb/drivers/<драйвер>, ссылки module на /sys/module/<модуль>.
"""

import os

USB_IDS = """\
#
#\tList of USB ID's
#
0451  Texas Instruments, Inc.
046d  Logitech, Inc.
\tc31c  Keyboard K120
0951  Kingston Technology
\t1666  DataTraveler 100 G3/G4/SE9 G2/50 Kyson
0bda  Realtek Semiconductor Corp.
\t2838  RTL2838 DVB-T
\t8179  RTL8188EUS 802.11n Wireless Network Adapter
\t\t00  some interface
04e6  SCM Microsystems, Inc.
\t5116  SCR331-LC1 / SCR3310 SmartCard Reader
1d50  OpenMoko, Inc.

# List of known device classes, subclasses and protocols
C 00  (Defined at Interface level)
C 03  Human Interface Device
\t01  Boot Interface Subclass
\t\t01  Keyboard
C 08  Mass Storage
\t06  SCSI
\t\t50  Bulk-Only
C 0b  Chip/SmartCard
C ff  Vendor Specific Class
\tff  Vendor Specific Subclass
\t\tff  Vendor Specific Protocol

# List of Audio Class Terminal Types
AT 0100  USB Undefined
"""

MODULES_ALIAS = """\
# Aliases extracted from modules themselves.
alias usb:v0BDAp8179d*dc*dsc*dp*ic*isc*ip*in* r8188eu
alias usb:v0BDAp2838d*dc*dsc*dp*ic*isc*ip*in* dvb_usb_rtl28xxu
alias usb:v*p*d*dc*dsc*dp*ic08isc06ip50in* usb_storage
alias usb:v0781p5406d0[0-1]*dc*dsc*dp*ic*isc*ip*in* range_test
alias pci:v00008086d*sv*sd*bc*sc*i* not_usb
"""

BUILTIN_MODINFO = (b"usbhid.alias=usb:v*p*d*dc*dsc*dp*ic03isc*ip*in*\0"
                   b"usbhid.license=GPL\0"
                   b"usbcore.license=GPL\0")

BUILTIN = "kernel/drivers/hid/usbhid/usbhid.ko\nkernel/drivers/usb/core/usbcore.ko\n"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(text, bytes) else "w"
    with open(path, mode) as f:
        f.write(text)


def make_device(sysroot, name, vid, pid, cls=0, strings=None, speed="480",
                authorized="1", interfaces=()):
    """interfaces: [(номер, класс, подкласс, протокол, драйвер или None)]."""
    devices = os.path.join(sysroot, "bus", "usb", "devices")
    real = os.path.join(sysroot, "devices", "pci0000:00", "usb1", name)
    os.makedirs(real, exist_ok=True)
    attrs = {"idVendor": "%04x" % vid, "idProduct": "%04x" % pid,
             "bcdDevice": "0100", "bDeviceClass": "%02x" % cls,
             "bDeviceSubClass": "00", "bDeviceProtocol": "00",
             "busnum": name.split("-")[0], "devnum": "5", "speed": speed,
             "version": " 2.00", "authorized": authorized}
    attrs.update(strings or {})
    for key, value in attrs.items():
        write(os.path.join(real, key), value + "\n")
    os.makedirs(devices, exist_ok=True)
    link = os.path.join(devices, name)
    if not os.path.lexists(link):
        os.symlink(real, link)

    for number, icls, isub, iproto, driver in interfaces:
        iname = "%s:1.%d" % (name, number)
        ipath = os.path.join(real, iname)
        for key, value in {"bInterfaceNumber": "%02x" % number,
                           "bInterfaceClass": "%02x" % icls,
                           "bInterfaceSubClass": "%02x" % isub,
                           "bInterfaceProtocol": "%02x" % iproto}.items():
            write(os.path.join(ipath, key), value + "\n")
        modalias = ("usb:v%04Xp%04Xd0100dc%02Xdsc00dp00ic%02Xisc%02Xip%02Xin%02X"
                    % (vid, pid, cls, icls, isub, iproto, number))
        write(os.path.join(ipath, "modalias"), modalias + "\n")
        ilink = os.path.join(devices, iname)
        if not os.path.lexists(ilink):
            os.symlink(ipath, ilink)
        if driver:
            ddir = os.path.join(sysroot, "bus", "usb", "drivers", driver)
            os.makedirs(ddir, exist_ok=True)
            os.symlink(ddir, os.path.join(ipath, "driver"))
    return real


def bind_module(sysroot, driver, module, loadable):
    """Ссылка drivers/<driver>/module и каталог /sys/module/<module>."""
    mdir = os.path.join(sysroot, "module", module)
    os.makedirs(mdir, exist_ok=True)
    if loadable:
        write(os.path.join(mdir, "initstate"), "live\n")
    ddir = os.path.join(sysroot, "bus", "usb", "drivers", driver)
    os.makedirs(ddir, exist_ok=True)
    link = os.path.join(ddir, "module")
    if not os.path.lexists(link):
        os.symlink(mdir, link)


def build(root):
    """Создаёт полное тестовое окружение, возвращает словарь путей."""
    sysroot = os.path.join(root, "sys")
    modules_dir = os.path.join(root, "lib", "modules", "6.0.0-test")
    modprobe_dir = os.path.join(root, "etc", "modprobe.d")
    proc = os.path.join(root, "proc")
    ids = os.path.join(root, "usb.ids")

    write(ids, USB_IDS)
    write(os.path.join(modules_dir, "modules.alias"), MODULES_ALIAS)
    write(os.path.join(modules_dir, "modules.builtin.modinfo"), BUILTIN_MODINFO)
    write(os.path.join(modules_dir, "modules.builtin"), BUILTIN)
    write(os.path.join(modprobe_dir, "rtlsdr.conf"),
          "# для SDR-приёмников\nblacklist dvb_usb_rtl28xxu\n")
    write(os.path.join(proc, "cmdline"), "root=/dev/sda1 quiet\n")

    # корневой хаб - должен быть пропущен
    make_device(sysroot, "usb1", 0x1d6b, 0x0002, cls=9,
                interfaces=[(0, 9, 0, 0, "hub")])
    # флешка: драйвер usb-storage из загружаемого модуля usb_storage
    make_device(sysroot, "1-1", 0x0951, 0x1666,
                strings={"manufacturer": "Kingston",
                         "product": "DataTraveler 3.0", "serial": "ABC123"},
                interfaces=[(0, 0x08, 0x06, 0x50, "usb-storage")])
    bind_module(sysroot, "usb-storage", "usb_storage", loadable=True)
    # Wi-Fi адаптер: модуль есть, но не загружен
    make_device(sysroot, "1-2", 0x0bda, 0x8179,
                strings={"manufacturer": "Realtek", "product": "802.11n NIC"},
                interfaces=[(0, 0xff, 0xff, 0xff, None)])
    # устройство производителя без драйвера в ядре и без строк
    make_device(sysroot, "1-3", 0x1d50, 0x6089, cls=0xff,
                interfaces=[(0, 0xff, 0x00, 0x00, None)])
    # считыватель смарт-карт: обслуживается pcscd
    make_device(sysroot, "1-4", 0x04e6, 0x5116,
                interfaces=[(0, 0x0b, 0x00, 0x00, None)])
    # клавиатура: встроенный драйвер usbhid
    make_device(sysroot, "1-5", 0x046d, 0xc31c,
                strings={"manufacturer": "Logitech",
                         "product": "USB Keyboard"},
                interfaces=[(0, 0x03, 0x01, 0x01, "usbhid"),
                            (1, 0x03, 0x00, 0x00, "usbhid")])
    bind_module(sysroot, "usbhid", "usbhid", loadable=False)
    # интерфейс, захваченный программой через libusb
    make_device(sysroot, "1-6", 0x0451, 0xe008,
                interfaces=[(0, 0xff, 0x00, 0x00, "usbfs")])
    # SDR-приёмник: модуль в чёрном списке
    make_device(sysroot, "1-7", 0x0bda, 0x2838,
                strings={"manufacturer": "Realtek", "product": "RTL2838UHIDIR"},
                interfaces=[(0, 0xff, 0xff, 0xff, None)])
    # заблокированное USBGuard устройство
    make_device(sysroot, "1-8", 0x0951, 0x1666, authorized="0",
                strings={"manufacturer": "Kingston",
                         "product": "DataTraveler 3.0"})
    return {"sys": sysroot, "modules": modules_dir, "usb_ids": ids,
            "modprobe": [modprobe_dir], "proc": proc}
