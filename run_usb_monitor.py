#!/usr/bin/env python3
"""Запуск окна «Монитор USB» приложения «Суперапп».

    .venv/bin/python run_usb_monitor.py
или, после source .venv/bin/activate:
    python run_usb_monitor.py

При запуске окно в фоне готовит языковую модель для поиска драйверов:
скачивает Ollama и модель в папку model проекта и один раз проверяет
модель на задачах программы (см. drvsearch/runtime.py, selftest.py):
    --model qwen2.5:3b   другая модель (по умолчанию qwen2.5:1.5b)
    --no-model           не проверять и не скачивать модель
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from PyQt5.QtWidgets import QApplication    # noqa: E402
except ImportError:
    sys.exit("Не найдена библиотека PyQt5 для интерпретатора %s.\n"
             "Создайте окружение и установите зависимости (из корня проекта):\n"
             "  python3 -m venv .venv\n"
             "  .venv/bin/pip install -r requirements.txt\n"
             "и запускайте программу интерпретатором окружения:\n"
             "  .venv/bin/python run_usb_monitor.py" % sys.executable)

import qtenv                                      # noqa: E402
from usbmon import create_tracker                 # noqa: E402
from usbmon.gui import UsbMonitorWindow           # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Монитор USB")
    parser.add_argument("--usb-ids", help="путь к файлу usb.ids")
    parser.add_argument("--log", help="путь к лог-файлу")
    parser.add_argument("--model", help="языковая модель Ollama для поиска драйверов "
                        "(по умолчанию qwen2.5:1.5b; точнее, но больше: qwen2.5:3b)")
    parser.add_argument("--no-model", action="store_true",
                        help="не проверять и не скачивать языковую модель")
    # пути для проверки на тестовом дереве sysfs
    parser.add_argument("--sys-root", default="/sys", help=argparse.SUPPRESS)
    parser.add_argument("--proc-root", default="/proc", help=argparse.SUPPRESS)
    parser.add_argument("--modules-dir", help=argparse.SUPPRESS)
    parser.add_argument("--modprobe-dir", action="append",
                        help=argparse.SUPPRESS)
    args, qt_args = parser.parse_known_args()

    qtenv.prepare()       # обход ошибки PyQt5 с кириллицей в пути (см. qtenv.py)
    app = QApplication([sys.argv[0]] + qt_args)
    qtenv.translate(app)  # русские надписи стандартных кнопок Qt
    tracker = create_tracker(args.sys_root, args.modules_dir, args.usb_ids,
                             args.log, args.proc_root, args.modprobe_dir)
    window = UsbMonitorWindow(tracker, model_setup=not args.no_model, model=args.model)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
