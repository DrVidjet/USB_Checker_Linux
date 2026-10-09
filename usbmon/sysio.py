"""Низкоуровневый доступ к файловой системе через системные вызовы.

Все чтения sysfs и служебных файлов ядра выполняются функциями модуля os,
которые являются тонкими обёртками над системными вызовами Linux:
open(2), read(2), close(2), readlink(2), getdents64(2) (через listdir),
stat(2) и access(2).
"""

import os

# Атрибуты sysfs занимают не больше одной страницы памяти (4096 байт),
# поэтому одного вызова read(2) с таким буфером достаточно.
SYSFS_PAGE = 4096


def read_bytes(path, limit=None):
    """Читает файл целиком системными вызовами open/read/close."""
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if limit is not None and total >= limit:
                break
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_attr(path):
    """Читает текстовый атрибут sysfs. Возвращает None, если его нет.

    Ядро может скрыть атрибут (например, product, если у устройства нет
    строкового дескриптора), поэтому отсутствие файла - нормальная ситуация.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        data = os.read(fd, SYSFS_PAGE)
    except OSError:
        return None
    finally:
        os.close(fd)
    return data.decode("utf-8", errors="replace").strip()


def read_hex(path):
    """Читает шестнадцатеричный атрибут (idVendor, bDeviceClass и т.п.)."""
    value = read_attr(path)
    if value is None or value == "":
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def link_target_name(path):
    """Имя объекта, на который указывает символическая ссылка (readlink)."""
    try:
        return os.path.basename(os.readlink(path))
    except OSError:
        return None


def list_dir(path):
    """Содержимое каталога (системный вызов getdents64 через os.listdir)."""
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def exists(path):
    """Проверка существования пути системным вызовом access(2)."""
    return os.access(path, os.F_OK)


def is_dir(path):
    return os.path.isdir(path)
