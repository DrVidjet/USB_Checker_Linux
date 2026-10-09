"""Модуль «Монитор USB» приложения «Суперапп».

Отслеживает подключение устройств USB, выводит их полное наименование
и проверяет, есть ли для них драйвер в ядре Linux.
"""

from .devices import UsbInspector
from .kernel_drivers import KernelModules
from .tracker import DeviceTracker, LogFile, default_log_path
from .usbids import UsbIds


def create_tracker(sys_root="/sys", modules_dir=None, usb_ids=None,
                   log_path=None, proc_root="/proc", modprobe_dirs=None):
    """Собирает все части модуля с путями по умолчанию."""
    ids = UsbIds.load(usb_ids)
    kwargs = {}
    if modprobe_dirs is not None:
        kwargs["modprobe_dirs"] = modprobe_dirs
    modules = KernelModules(modules_dir, sys_root=sys_root,
                            proc_root=proc_root, **kwargs).load()
    inspector = UsbInspector(ids, modules, sys_root=sys_root)
    log = LogFile(log_path or default_log_path())
    return DeviceTracker(inspector, log)


__all__ = ["create_tracker", "DeviceTracker", "KernelModules", "UsbIds",
           "UsbInspector", "LogFile"]
