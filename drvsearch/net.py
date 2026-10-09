"""HTTP-запросы с JSON на стандартной библиотеке (urllib).

urllib сам учитывает переменные окружения HTTPS_PROXY и NO_PROXY,
поэтому модуль работает и за прокси-сервером. Но если задан http_proxy,
а NO_PROXY нет, urllib отправляет через прокси даже запросы к 127.0.0.1,
и локальная служба Ollama выглядит недоступной. Поэтому запросы к своему
компьютеру (localhost, 127.0.0.0/8, ::1) всегда идут напрямую.
"""

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request

_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def is_local(url):
    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def urlopen(req, timeout):
    """urllib.request.urlopen, но без прокси для адресов своего компьютера."""
    url = req.full_url if isinstance(req, urllib.request.Request) else req
    if is_local(url):
        return _DIRECT.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


class HttpError(Exception):
    def __init__(self, status, message, headers=None, payload=None):
        super().__init__(message)
        self.status = status          # 0 - сетевая ошибка без ответа сервера
        self.message = message
        self.headers = headers
        self.payload = payload or {}

    def header(self, name):
        return self.headers.get(name) if self.headers is not None else None


def request_json(url, method="GET", headers=None, data=None, timeout=30):
    """Выполняет запрос и возвращает (разобранный JSON, заголовки ответа)."""
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, method=method,
                                 headers=dict(headers or {}))
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            return payload, resp.headers
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b""
        exc.close()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        message = payload.get("message") or payload.get("error") or str(exc.reason)
        raise HttpError(exc.code, message, exc.headers, payload) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise HttpError(0, str(reason)) from None
    except ValueError as exc:
        raise HttpError(0, "ответ сервера не является JSON: %s" % exc) from None
