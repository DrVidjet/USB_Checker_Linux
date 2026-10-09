"""Клиент GitHub REST API для поиска драйверов.

Используемые эндпоинты (документация GitHub REST API):
  GET /search/repositories  - поиск репозиториев; без токена 10 запросов
                              в минуту, с токеном 30;
  GET /search/code          - поиск по коду; только с токеном, 10 в минуту;
  GET /repos/{owner}/{repo}/readme - README репозитория (содержимое в base64).
Без токена общий лимит - 60 запросов в час. Каждый запрос обязан содержать
заголовок User-Agent. Заголовок X-GitHub-Api-Version не передаётся: в этом
случае GitHub использует версию API по умолчанию.
"""

import base64
import os
import time
import urllib.parse

from .net import HttpError, request_json

DEFAULT_API = "https://api.github.com"
USER_AGENT = "superapp-drvsearch"


class GitHubError(Exception):
    pass


class RateLimited(GitHubError):
    def __init__(self, message, wait_seconds=None):
        super().__init__(message)
        self.wait_seconds = wait_seconds


def load_token():
    """Токен из переменной GITHUB_TOKEN или файла ~/.config/superapp/github_token."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    path = os.path.join(os.environ.get("XDG_CONFIG_HOME",
                                       os.path.expanduser("~/.config")),
                        "superapp", "github_token")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


class GitHub:
    def __init__(self, token=None, base=None, timeout=20):
        self.token = token
        self.base = (base or os.environ.get("DRVSEARCH_GITHUB_API")
                     or DEFAULT_API).rstrip("/")
        self.timeout = timeout
        self.requests_made = 0
        self.remaining = None

    def _get(self, path, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/vnd.github+json",
                   "User-Agent": USER_AGENT}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        self.requests_made += 1
        try:
            data, resp_headers = request_json(url, headers=headers,
                                              timeout=self.timeout)
        except HttpError as exc:
            raise self._convert(exc) from None
        self.remaining = resp_headers.get("x-ratelimit-remaining")
        return data

    def _convert(self, exc):
        if exc.status == 0:
            return GitHubError("нет соединения с GitHub: %s" % exc.message)
        if exc.status in (403, 429):
            retry_after = exc.header("retry-after")
            remaining = exc.header("x-ratelimit-remaining")
            reset = exc.header("x-ratelimit-reset")
            if retry_after or remaining == "0" or "rate limit" in exc.message.lower():
                wait = None
                if retry_after and retry_after.isdigit():
                    wait = int(retry_after)
                elif reset and reset.isdigit():
                    wait = max(0, int(reset) - int(time.time()))
                text = "исчерпан лимит запросов к GitHub"
                if wait is not None:
                    text += ", повторить можно через %d с" % wait
                if not self.token:
                    text += " (без токена - 10 поисковых запросов в минуту)"
                return RateLimited(text, wait)
        if exc.status == 401:
            return GitHubError("токен GitHub недействителен")
        if exc.status == 422:
            return GitHubError("GitHub отклонил запрос: %s" % exc.message)
        return GitHubError("GitHub ответил %d: %s" % (exc.status, exc.message))

    def search_repositories(self, query, per_page=10):
        data = self._get("/search/repositories",
                         {"q": query, "per_page": per_page})
        return data.get("items", [])

    def search_code(self, query, per_page=20):
        if not self.token:
            raise GitHubError("поиск по коду GitHub доступен только с токеном")
        data = self._get("/search/code", {"q": query, "per_page": per_page})
        return data.get("items", [])

    def readme(self, full_name):
        """Текст README репозитория или None, если README нет."""
        owner, _, repo = full_name.partition("/")
        path = "/repos/%s/%s/readme" % (urllib.parse.quote(owner),
                                        urllib.parse.quote(repo))
        try:
            data = self._get(path)
        except GitHubError as exc:
            if " 404:" in str(exc):
                return None
            raise
        content = data.get("content", "")
        if data.get("encoding") == "base64":
            raw = base64.b64decode(content)
            return {"text": raw.decode("utf-8", errors="replace"),
                    "name": data.get("name", "README"),
                    "url": data.get("html_url", "")}
        return {"text": content, "name": data.get("name", "README"),
                "url": data.get("html_url", "")}
