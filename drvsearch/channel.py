"""Обмен сообщениями по безымянным каналам (pipe) в формате JSON Lines.

Каждое сообщение - один объект JSON в одной строке, оканчивающейся «\\n».
Канал - поток байтов без границ сообщений, поэтому принимающая сторона
накапливает данные в буфере и разбирает их по символу перевода строки.
Запись выполняется системным вызовом write(2) в цикле: при записи больше
PIPE_BUF байт (4096 в Linux) ядро может принять данные частями.
"""

import json
import os


class Channel:
    def __init__(self, in_fd=None, out_fd=None):
        self.in_fd = in_fd
        self.out_fd = out_fd
        self.buffer = b""

    def send(self, message):
        """Отправляет сообщение. False - если другая сторона закрыла канал."""
        if self.out_fd is None:
            return False
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            while data:
                written = os.write(self.out_fd, data)
                data = data[written:]
        except (BrokenPipeError, OSError):
            return False
        return True

    def receive(self):
        """Читает доступные данные: список сообщений или None при конце файла."""
        chunk = os.read(self.in_fd, 65536)
        if not chunk:
            return None             # другая сторона закрыла свой конец канала
        self.buffer += chunk
        messages = []
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if line.strip():
                try:
                    messages.append(json.loads(line.decode("utf-8")))
                except ValueError:
                    continue
        return messages

    def close(self):
        for fd in (self.in_fd, self.out_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.in_fd = self.out_fd = None
