"""Подготовка окружения Qt для PyQt5, установленного через pip.

PyQt5 5.15 сообщает Qt, где лежит его копия Qt, через встроенный файл
qt.conf и при этом переводит путь в кодировку Latin-1
(qpy/QtCore/qpycore_qt_conf.cpp: qt_dir_name.toLatin1()). Если в пути
к проекту есть кириллица (например, «~/Документы/Курсовая»), русские
буквы превращаются в «?», Qt ищет плагины в несуществующем каталоге и
окно не запускается с ошибкой
    Could not find the Qt platform plugin "xcb" in ""

Переменные окружения QT_QPA_PLATFORM_PLUGIN_PATH (каталог платформенных
плагинов) и QT_PLUGIN_PATH (дополнительные каталоги остальных плагинов)
Qt читает в локальной кодировке, то есть в UTF-8 (qguiapplication.cpp,
qcoreapplication.cpp), поэтому правильный путь передаётся через них.
Функцию prepare() нужно вызвать до создания QApplication. Дочерние
процессы (окно поиска драйвера) наследуют переменные окружения.
"""

import os


def prepare():
    """Возвращает каталог плагинов, если пришлось его указать, иначе None."""
    import PyQt5
    from PyQt5.QtCore import QLibraryInfo

    plugins = os.path.join(os.path.dirname(os.path.abspath(PyQt5.__file__)),
                           "Qt5", "plugins")
    if not os.path.isdir(plugins):
        return None             # PyQt5 из системного пакета: Qt находит плагины сам
    if os.path.isdir(QLibraryInfo.location(QLibraryInfo.PluginsPath)):
        return None             # путь из встроенного qt.conf корректен
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = os.path.join(plugins, "platforms")
    old = os.environ.get("QT_PLUGIN_PATH")
    os.environ["QT_PLUGIN_PATH"] = plugins + (os.pathsep + old if old else "")
    return plugins


def translate(app):
    """Русские надписи стандартных кнопок и окон Qt («Закрыть», «Показать
    подробности...»). Перевод qtbase_ru.qm поставляется вместе с PyQt5-Qt5;
    при кириллице в пути каталог из QLibraryInfo неверен (см. выше), поэтому
    он ищется и рядом с модулем PyQt5. Возвращает объект перевода или None."""
    import PyQt5
    from PyQt5.QtCore import QLibraryInfo, QTranslator

    dirs = [QLibraryInfo.location(QLibraryInfo.TranslationsPath),
            os.path.join(os.path.dirname(os.path.abspath(PyQt5.__file__)),
                         "Qt5", "translations")]
    for path in dirs:
        translator = QTranslator(app)
        if os.path.isdir(path) and translator.load("qtbase_ru", path):
            app.installTranslator(translator)
            return translator
    return None
