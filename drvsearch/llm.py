"""Клиент локальной языковой модели через HTTP API Ollama.

Используемые эндпоинты (документация Ollama API):
  GET  /api/tags  - список загруженных моделей;
  POST /api/show  - сведения о модели (поле thinking и список capabilities);
  POST /api/chat  - ответ модели; параметр format с JSON-схемой заставляет
                    модель вернуть JSON, соответствующий схеме (structured
                    outputs), stream=false возвращает ответ одним объектом;
  GET  /api/ps    - модели, уже загруженные в память.
Программа обращается к своей службе Ollama на 127.0.0.1:11435 (её запускает
runtime.py из папки model проекта); адрес передаёт SUPERAPP_OLLAMA_URL.

Перед каждым поиском check() проверяет и мощность компьютера (hwcheck.py):
хватает ли памяти под модель именно сейчас.

Модель решает только узкие задачи (выделить ключевые слова, оценить
кандидатов, пересказать шаги из README). Ссылки и команды, которые она
возвращает, программа проверяет, а не принимает на веру.
"""

import json
import os
import re

from . import hwcheck
from .net import HttpError, request_json

APP_PORT = 11435            # служба Ollama, которую запускает сама программа


def default_url():
    return "http://127.0.0.1:%s" % (os.environ.get("SUPERAPP_OLLAMA_PORT") or APP_PORT)
DEFAULT_MODEL = "qwen2.5:1.5b"
SETUP_HINT = ".venv/bin/python -m drvsearch --setup-model"


class LLMError(Exception):
    """Ошибка модели. fatal - служба не ответила (сеть, тайм-аут, нехватка
    памяти у Ollama): дальнейшие вопросы в этом поиске бессмысленны."""

    def __init__(self, message, fatal=False):
        super().__init__(message)
        self.fatal = fatal


class Ollama:
    def __init__(self, url=None, model=None, timeout=180, num_ctx=hwcheck.NUM_CTX):
        self.url = (url or os.environ.get("SUPERAPP_OLLAMA_URL")
                    or default_url()).rstrip("/")
        self.model = model or os.environ.get("SUPERAPP_LLM_MODEL") or DEFAULT_MODEL
        self.timeout = timeout
        self.num_ctx = num_ctx
        self._think = "unknown"

    def check(self):
        """Возвращает (готова ли модель, пояснение для пользователя)."""
        try:
            data, _ = request_json(self.url + "/api/tags", timeout=5)
        except HttpError as exc:
            if exc.status == 0:
                return False, ("служба Ollama не отвечает по адресу %s; модель "
                               "готовит главное окно при запуске (меню «Модель») "
                               "или команда %s" % (self.url, SETUP_HINT))
            return False, "Ollama вернула ошибку: %s" % exc.message
        entry = self._find(data)
        if entry is None:
            return False, ("модель %s не скачана; её скачивает главное окно при "
                           "запуске или команда %s --model %s"
                           % (self.model, SETUP_HINT, self.model))
        verdict = self.hardware(entry.get("size"), check_available=True)
        if not verdict.ok:
            return False, verdict.text()
        note = "модель %s готова" % self.model
        return True, note + ("; " + verdict.text() if verdict.warnings else "")

    def _find(self, tags):
        wanted = {self.model, self.model + ":latest"}
        for m in tags.get("models", []):
            if {m.get("name"), m.get("model")} & wanted:
                return m
        return None

    def hardware(self, size=None, check_available=False):
        """Хватит ли компьютеру сил на модель (параметры - из /api/show)."""
        info, loaded = None, False
        try:
            shown, _ = request_json(self.url + "/api/show", method="POST",
                                    data={"model": self.model}, timeout=10)
            info = shown.get("model_info")
            if size is None:
                tags, _ = request_json(self.url + "/api/tags", timeout=10)
                size = (self._find(tags) or {}).get("size")
            running, _ = request_json(self.url + "/api/ps", timeout=5)
            loaded = self._find(running) is not None
        except HttpError:
            pass
        return hwcheck.assess(self.model, info, size, check_available, loaded)

    def _think_param(self):
        """Отключает «размышления» у моделей, которые это поддерживают.

        По документации /api/show возвращает объект thinking со списком
        допустимых значений; False в списке означает, что размышления
        можно выключить. Для поиска драйверов они только замедляют ответ.
        """
        if self._think != "unknown":
            return self._think
        self._think = None
        try:
            info, _ = request_json(self.url + "/api/show", method="POST",
                                   data={"model": self.model}, timeout=10)
        except HttpError:
            return None
        thinking = info.get("thinking")
        if isinstance(thinking, dict) and False in thinking.get("values", []):
            self._think = False
        elif "thinking" in info.get("capabilities", []):
            self._think = False
        return self._think

    def chat_json(self, system, user, schema, num_predict=1024):
        """Запрос к модели с ответом строго по JSON-схеме."""
        # схема и в тексте запроса: так советует документация Ollama
        # (structured outputs), чтобы маленькая модель лучше заполняла поля
        user = "%s\n\nRespond with JSON matching this schema: %s" % (
            user, json.dumps(schema, ensure_ascii=False))
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "format": schema,
            "options": {"temperature": 0, "num_ctx": self.num_ctx,
                        "num_predict": num_predict},
        }
        think = self._think_param()
        if think is not None:
            payload["think"] = think
        try:
            data, _ = request_json(self.url + "/api/chat", method="POST",
                                   data=payload, timeout=self.timeout)
        except HttpError as exc:
            raise LLMError("ошибка Ollama: %s" % exc.message, fatal=True) from None
        content = (data.get("message") or {}).get("content", "")
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
        try:
            result = json.loads(content)
        except ValueError:
            raise LLMError("модель вернула не JSON") from None
        if not isinstance(result, dict):
            raise LLMError("модель вернула JSON неверной структуры")
        return result
