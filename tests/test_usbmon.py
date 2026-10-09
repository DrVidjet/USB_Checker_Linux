"""Тесты модуля «Монитор USB» на поддельном дереве sysfs.

Запуск из корня проекта:  python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fakesys                                    # noqa: E402
from usbmon import create_tracker                 # noqa: E402
from usbmon import kernel_drivers as kd           # noqa: E402
from usbmon import tracker as tr                  # noqa: E402
from usbmon.devices import join_names             # noqa: E402
from usbmon.uevent import parse_uevent, Uevent    # noqa: E402
from usbmon.usbids import UsbIds                  # noqa: E402


class Env(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="usbmon-test-")
        self.paths = fakesys.build(self.root)
        self.log_path = os.path.join(self.root, "logs", "usb.log")
        self.tracker = create_tracker(
            self.paths["sys"], self.paths["modules"], self.paths["usb_ids"],
            self.log_path, self.paths["proc"], self.paths["modprobe"])

    def tearDown(self):
        shutil.rmtree(self.root)

    def device(self, name):
        return self.tracker.inspector.read_device(name)


class TestUeventParsing(unittest.TestCase):
    def test_kernel_message(self):
        raw = (b"add@/devices/pci0000:00/0000:00:14.0/usb1/1-2\0"
               b"ACTION=add\0DEVPATH=/devices/pci0000:00/0000:00:14.0/usb1/1-2\0"
               b"SUBSYSTEM=usb\0DEVTYPE=usb_device\0PRODUCT=951/1666/100\0"
               b"TYPE=0/0/0\0SEQNUM=4242\0")
        ev = parse_uevent(raw)
        self.assertEqual(ev.action, "add")
        self.assertEqual(ev.name, "1-2")
        self.assertEqual(ev.subsystem, "usb")
        self.assertEqual(ev.devtype, "usb_device")
        self.assertEqual(ev.env["PRODUCT"], "951/1666/100")

    def test_foreign_message_ignored(self):
        self.assertIsNone(parse_uevent(b"libudev\0\xfe\xed\xca\xfe"))


class TestUsbIds(Env):
    def test_names(self):
        ids = UsbIds.load(self.paths["usb_ids"])
        self.assertEqual(ids.vendor_name(0x0951), "Kingston Technology")
        self.assertEqual(ids.product_name(0x0bda, 0x8179),
                         "RTL8188EUS 802.11n Wireless Network Adapter")
        self.assertIsNone(ids.product_name(0x0bda, 0x0000))
        self.assertEqual(ids.class_name(0x08, 0x06, 0x50),
                         "Mass Storage / SCSI / Bulk-Only")
        # раздел AT после классов не должен ломать разбор
        self.assertNotIn(0x0100, ids.vendors)

    def test_join_names(self):
        self.assertEqual(join_names("Kingston", "DataTraveler"),
                         "Kingston DataTraveler")
        self.assertEqual(join_names("Logitech", "Logitech USB Receiver"),
                         "Logitech USB Receiver")
        self.assertEqual(join_names("", "Mouse"), "Mouse")
        self.assertEqual(join_names("", ""), "")


class TestAliasMatching(Env):
    def test_patterns(self):
        mods = self.tracker.inspector.modules
        self.assertTrue(mods.available)
        m = mods.match("usb:v0BDAp8179d0000dc00dsc00dp00icFFiscFFipFFin00")
        self.assertEqual(m, [("r8188eu", "loadable")])
        m = mods.match("usb:v1234p5678d0100dc00dsc00dp00ic08isc06ip50in00")
        self.assertEqual(m, [("usb_storage", "loadable")])
        m = mods.match("usb:v1234p5678d0100dc00dsc00dp00ic03isc01ip01in00")
        self.assertEqual(m, [("usbhid", "builtin")])
        # диапазон bcdDevice [0-1] из file2alias.c
        self.assertTrue(mods.match(
            "usb:v0781p5406d0100dc00dsc00dp00ic00isc00ip00in00"))
        self.assertFalse(mods.match(
            "usb:v0781p5406d0200dc00dsc00dp00ic00isc00ip00in00"))
        self.assertIn("dvb_usb_rtl28xxu", mods.blacklist)

    def test_missing_modules_dir(self):
        mods = kd.KernelModules(os.path.join(self.root, "nope")).load()
        self.assertFalse(mods.available)
        self.assertIn("перезагрузите", mods.error)

    def test_cmdline_blacklist(self):
        fakesys.write(os.path.join(self.paths["proc"], "cmdline"),
                      "quiet modprobe.blacklist=r8188eu,nouveau\n")
        mods = kd.KernelModules(self.paths["modules"], self.paths["sys"],
                                self.paths["proc"], []).load()
        self.assertEqual(mods.blacklist["r8188eu"], "командная строка ядра")


class TestDevices(Env):
    def test_enumeration_skips_root_hub(self):
        names = self.tracker.inspector.device_names()
        self.assertNotIn("usb1", names)
        self.assertEqual(names, ["1-1", "1-2", "1-3", "1-4", "1-5", "1-6",
                                 "1-7", "1-8"])

    def test_bound_loadable(self):
        dev = self.device("1-1")
        self.assertEqual(dev.full_name, "Kingston DataTraveler 3.0")
        self.assertEqual(dev.name_from_db,
                         "Kingston Technology DataTraveler 100 G3/G4/SE9 G2/50 Kyson")
        self.assertEqual(dev.serial, "ABC123")
        d = dev.interfaces[0].driver
        self.assertEqual((d["status"], d["driver"], d["module"], d["module_kind"]),
                         (kd.BOUND, "usb-storage", "usb_storage", "loadable"))
        self.assertEqual(dev.verdict, "ok")

    def test_available_not_loaded(self):
        dev = self.device("1-2")
        self.assertEqual(dev.interfaces[0].driver["status"], kd.AVAILABLE)
        self.assertEqual(dev.verdict, "warn")
        self.assertIn("не загружен", dev.verdict_text)

    def test_missing_driver_and_db_name(self):
        dev = self.device("1-3")
        self.assertEqual(dev.full_name, "OpenMoko, Inc.")
        self.assertEqual(dev.interfaces[0].driver["status"], kd.MISSING)
        self.assertEqual(dev.verdict, "missing")
        self.assertIn("Потребуется поиск", dev.verdict_text)

    def test_userspace_class(self):
        dev = self.device("1-4")
        self.assertEqual(dev.full_name,
                         "SCM Microsystems, Inc. SCR331-LC1 / SCR3310 SmartCard Reader")
        self.assertEqual(dev.interfaces[0].driver["status"], kd.USERSPACE)
        self.assertEqual(dev.verdict, "info")

    def test_builtin_driver(self):
        dev = self.device("1-5")
        self.assertEqual(len(dev.interfaces), 2)
        self.assertTrue(all(i.driver["module_kind"] == "builtin"
                            for i in dev.interfaces))
        self.assertEqual(dev.verdict, "ok")
        self.assertIn("встроен в ядро", dev.verdict_text)
        self.assertEqual(dev.kind_text, "Устройство ввода (HID)")

    def test_usbfs(self):
        dev = self.device("1-6")
        self.assertEqual(dev.interfaces[0].driver["status"], kd.USBFS)
        self.assertEqual(dev.verdict, "info")

    def test_blacklisted(self):
        dev = self.device("1-7")
        self.assertEqual(dev.interfaces[0].driver["status"], kd.BLACKLISTED)
        self.assertIn("rtlsdr.conf", dev.interfaces[0].driver["note"])
        self.assertEqual(dev.verdict, "warn")

    def test_unauthorized(self):
        self.assertEqual(self.device("1-8").verdict, "blocked")

    def test_json_for_other_process(self):
        data = json.loads(json.dumps(self.device("1-1").to_dict()))
        self.assertEqual(data["id"], "0951:1666")
        self.assertEqual(data["interfaces"][0]["driver"]["module"], "usb_storage")


class TestTracker(Env):
    def event(self, action, devpath, devtype, **env):
        env.update(SUBSYSTEM="usb", DEVTYPE=devtype)
        return Uevent(action, devpath, env)

    def test_hotplug_cycle(self):
        t = self.tracker
        notices = t.scan()
        self.assertEqual(len(notices), 8)
        self.assertTrue(all(n.kind == tr.PRESENT for n in notices))

        # подключение нового устройства
        fakesys.make_device(self.paths["sys"], "2-1", 0x0bda, 0x8179,
                            interfaces=[(0, 0xff, 0xff, 0xff, None)])
        names, notices = t.handle(self.event(
            "add", "/devices/pci0000:00/usb2/2-1", "usb_device"))
        self.assertEqual((names, notices), (["2-1"], []))
        names, _ = t.handle(self.event(
            "add", "/devices/pci0000:00/usb2/2-1/2-1:1.0", "usb_interface"))
        self.assertEqual(names, ["2-1"])
        notices = t.inspect("2-1")
        self.assertEqual(notices[0].kind, tr.CONNECTED)
        self.assertEqual(notices[0].device.full_name,
                         "Realtek Semiconductor Corp. RTL8188EUS 802.11n "
                         "Wireless Network Adapter")
        self.assertEqual(t.inspect("2-1"), [])   # без изменений - тишина

        # привязка драйвера меняет оценку
        drv = os.path.join(self.paths["sys"], "bus", "usb", "drivers", "r8188eu")
        os.makedirs(drv)
        os.symlink(drv, os.path.join(self.paths["sys"], "devices", "pci0000:00",
                                     "usb1", "2-1", "2-1:1.0", "driver"))
        fakesys.bind_module(self.paths["sys"], "r8188eu", "r8188eu", True)
        names, notices = t.handle(self.event(
            "bind", "/devices/pci0000:00/usb2/2-1/2-1:1.0", "usb_interface",
            DRIVER="r8188eu"))
        self.assertEqual(notices[0].kind, tr.BIND)
        notices = t.inspect("2-1")
        self.assertEqual(notices[0].kind, tr.CHANGED)
        self.assertEqual(notices[0].device.verdict, "ok")

        # отключение
        names, notices = t.handle(self.event(
            "remove", "/devices/pci0000:00/usb2/2-1", "usb_device"))
        self.assertEqual(notices[0].kind, tr.DISCONNECTED)
        self.assertNotIn("2-1", t.devices)

        # события других подсистем и корневых хабов игнорируются
        self.assertEqual(t.handle(Uevent("add", "/devices/x/sda",
                                         {"SUBSYSTEM": "block"})), ([], []))
        self.assertEqual(t.handle(self.event(
            "add", "/devices/pci0000:00/usb3", "usb_device")), ([], []))

        with open(self.log_path, encoding="utf-8") as f:
            log = f.read()
        for title in ("ОБНАРУЖЕНО ПРИ ЗАПУСКЕ", "ПОДКЛЮЧЕНО", "ДРАЙВЕР ПРИВЯЗАН",
                      "ИЗМЕНЕНИЕ ДРАЙВЕРОВ", "ОТКЛЮЧЕНО"):
            self.assertIn(title, log)

    def test_resync_after_overflow(self):
        t = self.tracker
        t.scan()
        shutil.rmtree(os.path.join(self.paths["sys"], "devices", "pci0000:00",
                                   "usb1", "1-3"))
        os.unlink(os.path.join(self.paths["sys"], "bus", "usb", "devices", "1-3"))
        names, notices = t.resync()
        self.assertEqual(names, [])
        self.assertEqual([n.name for n in notices], ["1-3"])


class TestQtTranslation(unittest.TestCase):
    """Стандартные кнопки Qt подписаны по-русски (перевод qtbase_ru.qm)."""

    def test_message_box_buttons(self):
        code = (
            "from PyQt5.QtWidgets import QApplication, QMessageBox, QPushButton\n"
            "import qtenv\n"
            "app = QApplication([])\n"
            "assert qtenv.translate(app) is not None\n"
            "box = QMessageBox(QMessageBox.Information, 't', 't', QMessageBox.Close)\n"
            "box.setDetailedText('d')\n"
            "print('|'.join(b.text() for b in box.findChildren(QPushButton)))\n")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=root)
        out = subprocess.run([sys.executable, "-c", code], env=env, cwd=root,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Закрыть", out.stdout)
        self.assertIn("Показать подробности", out.stdout)


if __name__ == "__main__":
    unittest.main()
