"""Локальные имитации GitHub REST API и Ollama API для тестов.

Форматы ответов повторяют документацию: поля элементов поиска
(full_name, html_url, stargazers_count, pushed_at, archived, fork, license),
README в base64, заголовки лимитов, ответ /api/chat с полем message.content.
Репозитории вымышлены.
"""

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

GOOD_README = """# rtl8188eus-linux

Linux driver for Realtek RTL8188EUS USB Wi-Fi adapters.

## Supported devices

* TP-Link TL-WN722N v2/v3 (2357:010c)
* Realtek 802.11n NIC (0bda:8179)

## Installation

Install the build dependencies first:

```
sudo pacman -S --needed base-devel linux-headers
```

Then build and install the module:

```
git clone https://github.com/example-lab/rtl8188eus-linux.git
cd rtl8188eus-linux
make
sudo make install
```

Load the driver:

```
sudo modprobe 8188eu
```
"""

RISKY_README = """# rtl8188eus-dkms

DKMS package for rtl8188eus.

## Install

```
curl -sL https://example.org/setup.sh | sudo bash
```
"""

LIST_README = "# Awesome wifi\n\nA list of links about wifi.\n"


def repo(name, desc, stars, pushed, archived=False, fork=False, lic="GPL-2.0"):
    return {"full_name": name, "html_url": "https://github.com/" + name,
            "description": desc, "stargazers_count": stars, "pushed_at": pushed,
            "archived": archived, "fork": fork,
            "license": {"spdx_id": lic} if lic else None}


REPOS = {
    "example-lab/rtl8188eus-linux": (
        repo("example-lab/rtl8188eus-linux", "Realtek RTL8188EUS driver for Linux",
             850, "2026-08-01T10:00:00Z"), GOOD_README),
    "someone/rtl8188eus-dkms": (
        repo("someone/rtl8188eus-dkms", "rtl8188eus dkms", 3, "2019-03-01T10:00:00Z",
             fork=True, lic=None), RISKY_README),
    "foo/awesome-wifi": (
        repo("foo/awesome-wifi", "Awesome list of wifi tools", 2200,
             "2026-01-01T00:00:00Z"), LIST_README),
    "old/rtl8188eu-archive": (
        repo("old/rtl8188eu-archive", "Old rtl8188eu linux driver", 120,
             "2016-05-01T00:00:00Z", archived=True), "# old\n\nmake\n"),
}


class GitHubMock:
    def __init__(self):
        self.rate_limited = False
        self.requests = []
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, code, data, headers=None):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("x-ratelimit-remaining", "0" if code == 403 else "9")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urlparse(self.path)
                q = parse_qs(url.query).get("q", [""])[0]
                mock.requests.append((url.path, q, dict(self.headers)))
                if not self.headers.get("User-Agent"):
                    return self.send_json(403, {"message": "User-Agent required"})
                if mock.rate_limited and url.path.startswith("/search"):
                    return self.send_json(403, {"message": "API rate limit exceeded"},
                                          {"x-ratelimit-reset": "9999999999"})
                if url.path == "/search/repositories":
                    names = []
                    if "rtl8188eus" in q:
                        names = list(REPOS)
                    elif q.startswith("0bda 8179"):
                        names = ["example-lab/rtl8188eus-linux", "someone/rtl8188eus-dkms"]
                    elif q.startswith("repo:"):
                        names = [q[5:]] if q[5:] in REPOS else []
                    return self.send_json(200, {"total_count": len(names),
                                                "incomplete_results": False,
                                                "items": [REPOS[n][0] for n in names]})
                if url.path == "/search/code":
                    if not self.headers.get("Authorization"):
                        return self.send_json(401, {"message": "Requires authentication"})
                    item = {"name": "usb_intf.c", "path": "os_dep/linux/usb_intf.c",
                            "repository": {"full_name": "example-lab/rtl8188eus-linux"}}
                    return self.send_json(200, {"total_count": 1, "items": [item]})
                parts = url.path.strip("/").split("/")
                if len(parts) == 4 and parts[0] == "repos" and parts[3] == "readme":
                    name = parts[1] + "/" + parts[2]
                    if name not in REPOS:
                        return self.send_json(404, {"message": "Not Found"})
                    text = REPOS[name][1]
                    return self.send_json(200, {
                        "name": "README.md", "encoding": "base64",
                        "content": base64.b64encode(text.encode()).decode(),
                        "html_url": "https://github.com/%s/blob/main/README.md" % name})
                return self.send_json(404, {"message": "Not Found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class OllamaMock:
    """Имитация модели, которая иногда ошибается - для проверки защиты."""

    def __init__(self, model="qwen2.5:1.5b", delay=0, fail_chat=False, loaded=False):
        self.model = model
        self.chats = []
        self.delay = delay            # задержка ответа - «слабый компьютер»
        self.fail_chat = fail_chat    # 500, как при нехватке памяти у Ollama
        self.loaded = loaded          # модель уже в памяти (/api/ps)
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, code, data):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/tags":
                    return self.send_json(200, {"models": [
                        {"name": mock.model, "model": mock.model, "size": 986000000}]})
                if self.path == "/api/ps":
                    return self.send_json(200, {"models": [
                        {"name": mock.model, "model": mock.model}] if mock.loaded else []})
                self.send_json(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length))
                if self.path == "/api/show":
                    return self.send_json(200, {
                        "capabilities": ["completion", "tools", "thinking"],
                        "thinking": {"values": [True, False], "default": True},
                        "model_info": {"general.architecture": "qwen2",
                                       "qwen2.block_count": 28,
                                       "qwen2.attention.head_count": 12,
                                       "qwen2.attention.head_count_kv": 2,
                                       "qwen2.embedding_length": 1536}})
                if self.path != "/api/chat":
                    return self.send_json(404, {"error": "not found"})
                mock.chats.append(payload)
                if mock.delay:
                    time.sleep(mock.delay)
                if mock.fail_chat:
                    return self.send_json(500, {"error": "model requires more system "
                                                "memory (4.0 GiB) than is available (1.2 GiB)"})
                keys = set(payload["format"]["properties"])
                prompt = payload["messages"][-1]["content"]
                if keys == {"keywords"}:
                    answer = {"keywords": ["RTL8188EUS", "usb", "bad word!"]}
                elif keys == {"items"}:
                    items = []
                    for line in prompt.splitlines():
                        if line.startswith("{"):
                            cand = json.loads(line)
                            good = "rtl8188eus-linux" in cand["name"]
                            reason = ("supports 0bda:8179" if good
                                      else "not a driver for this device")
                            items.append({"id": cand["id"], "relevant": good,
                                          "score": 9 if good else 2, "reason": reason})
                    items.append({"id": 99, "relevant": True, "score": 10,
                                  "reason": "a candidate that does not exist"})
                    answer = {"items": items}
                else:
                    answer = {"steps": [
                        {"description": "Install the dependencies",
                         "command": "sudo pacman -S --needed base-devel linux-headers"},
                        {"description": "Build the module", "command": "make"},
                        {"description": "Install (made up by the model)",
                         "command": "sudo ./install-everything.sh --force"},
                        {"description": "Load the driver",
                         "command": "sudo modprobe 8188eu"}]}
                self.send_json(200, {"model": mock.model, "done": True,
                                     "message": {"role": "assistant",
                                                 "content": json.dumps(answer)}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def closed_url():
    """Адрес, на котором гарантированно никто не слушает."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "http://127.0.0.1:%d" % port
