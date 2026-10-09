"""Имитации для проверки подготовки модели.

* FAKE_OLLAMA - текст поддельной программы «ollama serve»: слушает адрес из
  OLLAMA_HOST, хранит «модели» файлами в OLLAMA_MODELS, отвечает на
  /api/version, /api/tags, /api/show, /api/chat и потоковый /api/pull
  в формате документации Ollama; прерванная загрузка продолжается.
* build_archive() - архив .tar.zst со структурой переносимой сборки
  (bin/ollama, lib/ollama/...), как ollama-linux-amd64.tar.zst.
* DownloadServer - раздача архива с поддержкой Range и возможностью
  оборвать первую передачу на середине.
"""

import io
import json
import os
import socket
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAKE_OLLAMA = r'''#!%(python)s
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if sys.argv[1:] != ["serve"]:
    sys.exit("usage: ollama serve")
host, port = os.environ["OLLAMA_HOST"].rsplit(":", 1)
models = os.environ["OLLAMA_MODELS"]
os.makedirs(models, exist_ok=True)
TOTAL = 50 * 1024 * 1024
STEPS = 10
DELAY = float(os.environ.get("FAKE_PULL_DELAY", "0.02"))
STALL = int(os.environ.get("FAKE_PULL_STALL", "0"))   # «зависнуть» после этого шага

def answer(req):
    """Ответ «модели»: FAKE_CHAT=good - правильный (пояснения по-английски), bad -
    неверный, slow - медленный, russian - верный, но пояснения по-русски (язык
    пояснений программа не проверяет)."""
    mode = os.environ.get("FAKE_CHAT", "good")
    with open(os.path.join(models, "chats.log"), "a") as log:
        log.write(mode + "\n")
    if mode == "slow":
        time.sleep(float(os.environ.get("FAKE_CHAT_DELAY", "1.5")))
    keys = set(req["format"]["properties"])
    prompt = req["messages"][-1]["content"]
    if keys == {"keywords"}:
        return {"keywords": ["wifi"] if mode == "bad" else ["rtl8188eus"]}
    if keys == {"items"}:
        items = []
        for line in prompt.splitlines():
            if line.startswith("{"):
                cand = json.loads(line)
                good = ("rtl8188eu" in cand["name"].lower()) != (mode == "bad")
                items.append({"id": cand["id"], "relevant": good, "score": 9 if good else 1,
                              "reason": "Подходит: драйвер этой микросхемы"
                              if mode == "russian" else "Matches the chip"})
        return {"items": items}
    fragment = prompt.split("<<<", 1)[1].split(">>>", 1)[0]
    commands, inside = [], False
    for line in fragment.splitlines():
        if line.strip().startswith("```"):
            inside = not inside
        elif inside and line.strip():
            commands.append(line.strip())
    if mode == "bad":
        commands = ["sudo ./install-everything.sh --force"]
    text = "Выполнить команду" if mode == "russian" else "Run the command"
    return {"steps": [{"description": text, "command": c} for c in commands]}

def fname(model):
    if ":" not in model:
        model += ":latest"
    return os.path.join(models, model.replace(":", "__").replace("/", "_"))

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"     # поток /api/pull - chunked, как у настоящей Ollama
    def log_message(self, *a):
        pass
    def js(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.path == "/api/version":
            return self.js({"version": "0.0.0-test"})
        if self.path == "/api/tags":
            names = [n.replace("__", ":") for n in os.listdir(models)
                     if "__" in n and not n.endswith(".partial")]
            size = int(os.environ.get("FAKE_MODEL_SIZE", "986000000"))
            return self.js({"models": [{"name": n, "model": n, "size": size}
                                       for n in names]})
        if self.path == "/api/ps":
            return self.js({"models": []})
        self.send_error(404)
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/show":
            # параметры qwen2.5:1.5b из config.json (28 слоёв, 2 головы KV по 128)
            return self.js({"capabilities": ["completion", "tools"], "model_info": {
                "general.architecture": "qwen2", "qwen2.block_count": 28,
                "qwen2.attention.head_count": 12, "qwen2.attention.head_count_kv": 2,
                "qwen2.embedding_length": 1536}})
        if self.path == "/api/chat":
            return self.js({"done": True, "message": {"role": "assistant",
                                                      "content": json.dumps(answer(req))}})
        if self.path != "/api/pull":
            return self.send_error(404)
        target = fname(req["model"])
        partial = target + ".partial"
        step = int(open(partial).read()) if os.path.exists(partial) else 0
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        def send(obj):
            data = (json.dumps(obj) + "\n").encode()
            self.wfile.write(b"%%x\r\n%%s\r\n" %% (len(data), data))
            self.wfile.flush()
        try:
            send({"status": "pulling manifest"})
            if os.environ.get("FAKE_PULL_MANIFEST_STALL"):
                # как настоящая Ollama, когда реестр не отвечает: в журнал - сбой
                # запроса, в поток - ничего
                sys.stderr.write('time=now level=INFO source=images.go msg="request '
                                 'failed: Get \\"https://registry.ollama.ai/v2/library/'
                                 'qwen2.5/manifests/1.5b\\": dial tcp: i/o timeout"\n')
                sys.stderr.flush()
                time.sleep(120)
            send({"status": "pulling abc", "digest": "sha256:abc", "total": TOTAL,
                  "completed": TOTAL * step // STEPS})
            while step < STEPS:
                time.sleep(DELAY)
                if STALL and step == STALL:
                    time.sleep(120)
                step += 1
                open(partial, "w").write(str(step))
                send({"status": "pulling abc", "digest": "sha256:abc", "total": TOTAL,
                      "completed": TOTAL * step // STEPS})
            send({"status": "pulling small", "digest": "sha256:def", "total": 1000,
                  "completed": 1000})
            for status in ("verifying sha256 digest", "writing manifest",
                           "removing any unused layers"):
                send({"status": status})
            open(target, "w").write("weights")
            os.remove(partial)
            send({"status": "success"})
            self.wfile.write(b"0\r\n\r\n")
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            pass

ThreadingHTTPServer((host, int(port)), H).serve_forever()
'''


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def zstd_compress(data):
    try:
        from compression import zstd
        return zstd.compress(data)
    except ImportError:
        import zstandard
        return zstandard.ZstdCompressor().compress(data)


def build_archive(evil=False):
    """Архив .tar.zst со структурой переносимой сборки Ollama.

    evil="path" (или True) - файл ../evil.sh, evil="link" - ссылка на /etc.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        def add(name, data, mode=0o644):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))
        for d in ("bin", "lib", "lib/ollama"):
            info = tarfile.TarInfo(d)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
        script = FAKE_OLLAMA % {"python": sys.executable}
        add("bin/ollama", script.encode(), 0o755)
        add("lib/ollama/libfake.so", os.urandom(300 * 1024))
        link = tarfile.TarInfo("lib/ollama/libfake.so.1")
        link.type = tarfile.SYMTYPE
        link.linkname = "libfake.so"
        tar.addfile(link)
        if evil == "link":
            bad = tarfile.TarInfo("lib/etc")
            bad.type = tarfile.SYMTYPE
            bad.linkname = "../../etc"
            tar.addfile(bad)
        elif evil:
            add("../evil.sh", b"rm -rf ~")
    return zstd_compress(buf.getvalue())


class DownloadServer:
    """Сервер загрузки архива Ollama и заодно реестр моделей (GET /v2/...):
    registry="ok" - описание модели отдаётся, "missing" - 404, "error" - 500,
    "stall" - соединение принято, но ответ «зависает», "blob_stall" - описание
    отдаётся, а запрос файла модели (HEAD .../blobs/...) «зависает»."""

    def __init__(self, payload, drop_first_at=None, stall_at=None, registry="ok"):
        self.payload = payload
        self.registry = registry
        self.manifests = []               # запросы к реестру (отдельно от загрузок)
        self.drop_first_at = drop_first_at
        self.stall_at = stall_at          # отдать столько байт и «зависнуть»
        self.release = threading.Event()
        self.requests = []
        srv = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_GET(self):
                rng = self.headers.get("Range")
                if self.path.startswith("/v2/"):
                    srv.manifests.append(self.path)
                    return self.manifest()
                srv.requests.append((self.path, rng))
                if not self.path.endswith(".tar.zst"):
                    return self.send_error(404)
                data, start = srv.payload, 0
                if rng:
                    start = int(rng.split("=")[1].rstrip("-"))
                    if start >= len(data):
                        return self.send_error(416)
                    self.send_response(206)
                    self.send_header("Content-Range", "bytes %d-%d/%d"
                                     % (start, len(data) - 1, len(data)))
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(len(data) - start))
                self.end_headers()
                body = data[start:]
                if srv.stall_at is not None:
                    self.wfile.write(body[:srv.stall_at])
                    self.wfile.flush()
                    srv.release.wait(120)
                    return
                if srv.drop_first_at is not None and len(srv.requests) == 1:
                    self.wfile.write(body[:srv.drop_first_at])
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                self.wfile.write(body)

            def do_HEAD(self):
                if "/blobs/" not in self.path:
                    return self.send_error(404)
                srv.manifests.append("HEAD " + self.path)
                if srv.registry == "blob_stall":
                    srv.release.wait(120)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(50 * 1024 * 1024))
                self.end_headers()

            def manifest(self):
                if srv.registry == "missing":
                    return self.send_error(404)
                if srv.registry == "error":
                    return self.send_error(500)
                body = json.dumps({"schemaVersion": 2, "layers": [
                    {"digest": "sha256:abc", "size": 50 * 1024 * 1024}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type",
                                 "application/vnd.docker.distribution.manifest.v2+json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if srv.registry == "stall":
                    self.wfile.write(body[:10])
                    self.wfile.flush()
                    srv.release.wait(120)
                    return
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
