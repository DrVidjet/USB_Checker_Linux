"""Фоновая подготовка языковой модели для главного окна.

Скачивание может длиться часами (Ollama 1,34 ГБ и модель qwen2.5:1.5b
986 МБ), поэтому оно выполняется в отдельном потоке QThread, а окно
остаётся рабочим. Ход загрузки передаётся сигналами Qt.
"""

import os
import sys
import threading

from PyQt5.QtCore import QThread, pyqtSignal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from drvsearch.runtime import (Cancelled, ModelSetup, SelfTestFailed,   # noqa: E402
                               SetupError, WeakHardware)


class ModelSetupThread(QThread):
    progress = pyqtSignal(str, str, object, object)   # этап, текст, сделано, всего
    ready = pyqtSignal(str, str)                       # адрес Ollama, модель
    failed = pyqtSignal(str)
    cancelled = pyqtSignal(str)
    weak = pyqtSignal(str, str)        # модель отключена: причина, надпись

    def __init__(self, model=None, server=None, retest=False, parent=None):
        super().__init__(parent)
        self.cancel_event = threading.Event()
        self.setup = ModelSetup(model, progress=self.progress.emit,
                                cancel=self.cancel_event, retest=retest)
        # служба, запущенная предыдущей попыткой, остаётся на попечении окна
        self.setup.server = server

    def run(self):
        try:
            url = self.setup.run()
        except Cancelled as exc:
            self.cancelled.emit(str(exc))
        except SelfTestFailed as exc:
            self.weak.emit(str(exc), "ИИ отключён: модель не прошла проверку")
        except WeakHardware as exc:
            self.weak.emit(str(exc), "ИИ отключён: слабый компьютер")
        except SetupError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:          # непредвиденная ошибка не должна ронять окно
            self.failed.emit("%s: %s" % (type(exc).__name__, exc))
        else:
            self.ready.emit(url, self.setup.model)

    def stop(self):
        """Просит поток остановиться; чтение из сети прерывается сразу."""
        self.setup.abort()
