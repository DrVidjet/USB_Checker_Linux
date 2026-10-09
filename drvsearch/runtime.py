"""Подготовка локальной языковой модели на любом дистрибутиве Linux.

Всё, что нужно модели, программа держит в папке model в корне проекта и
ничего не ставит в систему (документация Ollama: linux.mdx, api.md, faq.mdx):
  1) проверяется мощность компьютера (hwcheck.py); если он слишком слабый,
     ничего не скачивается, поиск идёт без модели;
  2) если в model/ollama нет Ollama, скачивается переносимая сборка
     https://ollama.com/download/ollama-linux-<amd64|arm64>.tar.zst
     (с докачкой после обрыва) и распаковывается туда;
  3) программа запускает «ollama serve» на 127.0.0.1:11435 с каталогом
     моделей model/models (переменные OLLAMA_HOST и OLLAMA_MODELS); Ollama,
     установленная в системе, и её служба на порту 11434 не используются;
  4) если модели нет в списке GET /api/tags, сначала проверяется связь с
     реестром моделей registry.ollama.ai (сама Ollama ждёт его ответа без
     ограничения времени), затем модель скачивается запросом POST /api/pull;
     прогресс приходит потоком JSON, прерванная загрузка продолжается с
     места остановки;
  5) модель один раз проходит самопроверку на задачах программы
     (selftest.py); не прошла - поиск идёт без неё.
Каждый шаг записывается в отчёт (атрибут report), его показывает окно
«Сведения о модели».
"""

import http.client
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import hwcheck, selftest
from .llm import APP_PORT, DEFAULT_MODEL, Ollama
from .net import HttpError, request_json, urlopen

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOWNLOAD_BASE = "https://ollama.com/download"
# реестр, из которого Ollama скачивает модели (каталоги model/models/manifests/
# registry.ollama.ai/...); описание модели - GET /v2/<имя>/manifests/<тег>
REGISTRY = "https://registry.ollama.ai"
REGISTRY_TIMEOUT = 30     # с на проверку связи с реестром
PULL_SILENCE = 120        # с без единой строки от Ollama при загрузке модели
CHUNK = 256 * 1024
ARCHES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}

STATUS_RU = {
    "pulling manifest": "получение описания модели",
    "verifying sha256 digest": "проверка контрольной суммы",
    "writing manifest": "запись описания модели",
    "removing any unused layers": "удаление лишних файлов",
    "removing unused layers": "удаление лишних файлов",
    "success": "загрузка модели завершена",
}


class SetupError(Exception):
    pass


def describe_net_error(reason, timeout):
    """Понятное описание сетевой ошибки urllib/socket."""
    if isinstance(reason, socket.gaierror):
        return "не удалось найти адрес сервера (DNS: %s)" % (reason.strerror or reason)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "сервер не ответил за %d с" % timeout
    if isinstance(reason, ssl.SSLError):
        return "ошибка защищённого соединения (TLS: %s)" % (reason.reason or reason)
    if isinstance(reason, ConnectionRefusedError):
        return "соединение отклонено"
    if isinstance(reason, ConnectionResetError):
        return "соединение сброшено"
    return str(reason)


class Cancelled(SetupError):
    pass


class WeakHardware(SetupError):
    """Компьютер слишком слабый для модели: поиск идёт без неё."""


class SelfTestFailed(WeakHardware):
    """Модель не справилась с самопроверкой: поиск идёт без неё."""


def model_dir():
    """Папка model в корне проекта (переменная SUPERAPP_MODEL_DIR меняет её)."""
    return os.environ.get("SUPERAPP_MODEL_DIR") or os.path.join(PROJECT_ROOT, "model")


def legacy_dir():
    """Где хранила файлы прежняя версия программы."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "superapp")


def human(size):
    """Размер в байтах в удобном виде: 850 МБ, 2,5 ГБ."""
    if size is None:
        return "?"
    for unit, factor in (("ГБ", 1 << 30), ("МБ", 1 << 20), ("КБ", 1 << 10)):
        if size >= factor:
            value = size / factor
            text = ("%.1f" % value) if value < 10 else ("%.0f" % value)
            return text.replace(".", ",") + " " + unit
    return "%d Б" % size


def zstd_reader(raw):
    """Поток распаковки zstd: модуль compression.zstd (Python 3.14+)
    или пакет zstandard (для Python 3.10-3.13, есть в requirements.txt)."""
    try:
        from compression import zstd
        return zstd.ZstdFile(raw, "rb")
    except ImportError:
        pass
    try:
        import zstandard
    except ImportError:
        raise SetupError("для распаковки архива Ollama нужен пакет zstandard: "
                         "выполните .venv/bin/pip install -r requirements.txt") from None
    return zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True)


def inside(path):
    """Путь из архива не выходит за каталог распаковки."""
    name = os.path.normpath(path)
    return not (os.path.isabs(path) or name == ".." or name.startswith("../"))


def extract_args():
    """Фильтр "data" запрещает файлы вне каталога, устройства и setuid
    (Python 3.12+, 3.11.4+, 3.10.12+); в более старых версиях его нет."""
    return {"filter": "data"} if hasattr(tarfile, "data_filter") else {}


class ModelSetup:
    def __init__(self, model=None, progress=None, cancel=None, allow_download=True,
                 app_port=None, base_dir=None, download_base=None, retest=False,
                 registry=None):
        self.model = model or os.environ.get("SUPERAPP_LLM_MODEL") or DEFAULT_MODEL
        self.progress = progress or (lambda stage, text, done, total: None)
        self.cancel = cancel or threading.Event()
        self.allow_download = allow_download
        self.retest = retest              # провести самопроверку заново
        app_port = app_port or int(os.environ.get("SUPERAPP_OLLAMA_PORT") or APP_PORT)
        self.app_url = "http://127.0.0.1:%d" % app_port
        self.base = base_dir or model_dir()
        self.runtime_dir = os.path.join(self.base, "ollama")
        self.binary = os.path.join(self.runtime_dir, "bin", "ollama")
        self.models_dir = os.path.join(self.base, "models")
        self.log_path = os.path.join(self.base, "ollama-serve.log")
        self.selftest_path = os.path.join(self.base, "selftest.json")
        self.download_base = (download_base or os.environ.get("SUPERAPP_OLLAMA_DOWNLOAD")
                              or DOWNLOAD_BASE).rstrip("/")
        self.registry = (registry or os.environ.get("SUPERAPP_OLLAMA_REGISTRY")
                         or REGISTRY).rstrip("/")
        self.registry_timeout = REGISTRY_TIMEOUT
        self.pull_silence = PULL_SILENCE
        self.server = None        # процесс ollama serve, если его запустила программа
        self.url = None
        self.hw = None            # hwcheck.Verdict
        self.test = None          # результат самопроверки
        self.report = []          # что проверено и что найдено
        self.active = None        # ответ HTTP, который сейчас читается

    # --- общее --------------------------------------------------------------
    def say(self, stage, text, done=None, total=None):
        self.progress(stage, text, done, total)

    def note(self, text, stage=None):
        """Запись в отчёт (и в журнал окна, если указан этап)."""
        self.report.append(text)
        if stage:
            self.say(stage, text)

    def abort(self):
        """Прерывает подготовку из другого потока.

        Кроме флага отмены выполняется shutdown(2) сокета текущей загрузки:
        иначе поток мог бы ждать данных от сервера до истечения тайм-аута.
        """
        self.cancel.set()
        resp = self.active
        sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled("подготовка модели остановлена; загрузка продолжится "
                            "с того же места при следующем запуске")

    def probe(self, url):
        """(отвечает ли служба Ollama, причина)."""
        try:
            data, _ = request_json(url + "/api/version", timeout=3)
            return True, "версия %s" % data.get("version", "?")
        except HttpError as exc:
            return False, exc.message

    def alive(self, url):
        return self.probe(url)[0]

    def ensure_space(self, need):
        os.makedirs(self.base, exist_ok=True)
        free = shutil.disk_usage(self.base).free
        if need and free < need:
            raise SetupError("недостаточно места на диске: нужно около %s, свободно %s "
                             "(каталог %s)" % (human(need), human(free), self.base))

    # --- основной метод -----------------------------------------------------
    def run(self):
        """Готовит модель и возвращает адрес службы Ollama."""
        self.report = []
        self.check_hardware()
        url = self.find_service()
        if url is None:
            self.adopt_legacy()
            if not os.access(self.binary, os.X_OK):
                if not self.allow_download:
                    raise SetupError("Ollama ещё не скачана в %s" % self.runtime_dir)
                self.install_portable()
            else:
                self.note("Ollama: %s" % self.binary)
            url = self.start_server(self.binary)
        self.url = url
        try:
            if not self.has_model(url):
                if not self.allow_download:
                    raise SetupError("модель %s ещё не скачана" % self.model)
                self.check_registry()
                self.pull(url)
            self.check_hardware(url)
            self.run_selftest(url)
        except WeakHardware:
            self.stop_server()        # модель не нужна - память освобождается сразу
            raise
        self.say("ready", "модель %s готова" % self.model)
        return url

    def check_hardware(self, url=None):
        """Слабый компьютер - исключение WeakHardware до любых загрузок.

        Без url оценка идёт по таблице известных моделей, с url - по точным
        параметрам скачанной модели из /api/show.
        """
        self.hw = Ollama(url, self.model).hardware() if url else hwcheck.assess(self.model)
        if not self.hw.ok:
            self.note("Мощность компьютера: " + self.hw.text())
            raise WeakHardware(self.hw.text())
        if url is None:
            self.note("Мощность компьютера: достаточно для %s%s" % (
                self.model, " (" + self.hw.text() + ")" if self.hw.warnings else ""))

    def find_service(self):
        """Своя служба, оставшаяся от прошлого запуска (например, после сбоя)."""
        ok, why = self.probe(self.app_url)
        self.note("Служба Ollama программы %s: %s" % (
            self.app_url, why if ok else "не запущена"))
        return self.app_url if ok else None

    def run_selftest(self, url):
        digest = None
        try:
            tags, _ = request_json(url + "/api/tags", timeout=10)
            for m in tags.get("models", []):
                if self.model in (m.get("name"), m.get("model")):
                    digest = m.get("digest")
        except HttpError:
            pass
        self.test = selftest.run(Ollama(url, self.model), digest, self.selftest_path,
                                 force=self.retest,
                                 progress=lambda text: self.say("selftest", text))
        for line in selftest.describe(self.test):
            self.note(line)
        if not self.test["ok"]:
            raise SelfTestFailed(selftest.failure(self.test))

    # --- переносимая сборка Ollama в папке model --------------------------------
    def adopt_legacy(self):
        """Файлы прежней версии программы (~/.local/share/superapp) переносятся
        в папку model, чтобы не скачивать их заново."""
        old = legacy_dir()
        if not os.path.isdir(old) or os.path.abspath(old) == os.path.abspath(self.base):
            return
        os.makedirs(self.base, exist_ok=True)
        for name in sorted(os.listdir(old)):
            if name in ("ollama", "models") or name.endswith(".tar.zst.part"):
                src, dst = os.path.join(old, name), os.path.join(self.base, name)
                if os.path.exists(dst):
                    continue
                try:
                    shutil.move(src, dst)
                    self.note("Перенесено из %s: %s" % (old, name), "check")
                except OSError as exc:
                    self.note("Не удалось перенести %s: %s" % (src, exc))

    def install_portable(self):
        arch = ARCHES.get(platform.machine().lower())
        if not arch:
            raise SetupError("переносимая сборка Ollama выпускается только для x86_64 "
                             "и ARM64, а у этого компьютера %s" % platform.machine())
        name = "ollama-linux-%s.tar.zst" % arch
        archive = os.path.join(self.base, name)
        # .tar.zst - обычный tar-архив, сжатый zstd (как .tar.gz), из раздела
        # «Manual install» документации Ollama (linux.mdx); это не пакет
        # дистрибутива, он распаковывается модулем tarfile без пакетного менеджера
        self.say("runtime", "скачивается Ollama - программа, которая запускает модель "
                 "(официальный архив %s для любого Linux) в %s" % (name, self.base))
        self.note("Ollama: %s/%s -> %s" % (self.download_base, name, self.runtime_dir))
        self.download(self.download_base + "/" + name, archive)
        self.extract(archive)
        os.remove(archive)
        if not os.access(self.binary, os.X_OK):
            raise SetupError("в архиве Ollama нет исполняемого файла bin/ollama")
        return self.binary

    def download(self, url, path):
        """Скачивание с докачкой: недокачанный файл хранится как *.part."""
        os.makedirs(self.base, exist_ok=True)
        part = path + ".part"
        have = os.path.getsize(part) if os.path.exists(part) else 0
        headers = {"User-Agent": "superapp"}
        if have:
            headers["Range"] = "bytes=%d-" % have
        try:
            resp = urlopen(urllib.request.Request(url, headers=headers), timeout=60)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and have:      # файл уже скачан целиком
                os.replace(part, path)
                return
            raise SetupError("сервер загрузки Ollama ответил %d" % exc.code) from None
        except (urllib.error.URLError, OSError) as exc:
            raise SetupError("не удалось скачать Ollama: %s"
                             % getattr(exc, "reason", exc)) from None
        self.active = resp
        with resp:
            if resp.status == 206:
                total = int(resp.headers.get("Content-Range", "/0").rsplit("/", 1)[1] or 0)
                mode, done = "ab", have
            else:
                total = int(resp.headers.get("Content-Length") or 0)
                mode, done = "wb", 0
            total = total or None
            if total:
                # архив + распакованные файлы (примерно втрое больше архива)
                self.ensure_space(total - done + total * 3)
            self.say("runtime", "скачивание Ollama", done, total)
            with open(part, mode) as f:
                while True:
                    self.check_cancel()
                    try:
                        chunk = resp.read(CHUNK)
                    except (OSError, http.client.HTTPException) as exc:
                        self.check_cancel()
                        raise SetupError("соединение прервано (%s); загрузка продолжится "
                                         "с %s при следующем запуске" % (exc, human(done))) from None
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    self.say("runtime", "скачивание Ollama", done, total)
        self.active = None
        self.check_cancel()
        if total and done < total:
            raise SetupError("загрузка Ollama прервана на %s из %s; она продолжится при "
                             "следующем запуске" % (human(done), human(total)))
        os.replace(part, path)

    def extract(self, archive):
        """Распаковка .tar.zst потоком, с защитой от путей вне каталога."""
        self.say("extract", "распаковка Ollama в %s" % self.runtime_dir)
        tmp = self.runtime_dir + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        safe = extract_args()
        with open(archive, "rb") as raw:
            stream = zstd_reader(raw)
            with tarfile.open(fileobj=stream, mode="r|") as tar:
                for member in tar:
                    self.check_cancel()
                    if not inside(member.name):
                        raise SetupError("в архиве Ollama недопустимый путь %s" % member.name)
                    # ссылки проверяются и без фильтра "data" (Python до 3.10.12)
                    if member.issym() and not inside(os.path.join(
                            os.path.dirname(member.name), member.linkname)):
                        raise SetupError("в архиве Ollama ссылка за пределы каталога: %s -> %s"
                                         % (member.name, member.linkname))
                    if member.islnk() and not inside(member.linkname):
                        raise SetupError("в архиве Ollama недопустимая ссылка %s" % member.name)
                    tar.extract(member, tmp, **safe)
        shutil.rmtree(self.runtime_dir, ignore_errors=True)
        os.replace(tmp, self.runtime_dir)

    # --- служба Ollama, запущенная программой -----------------------------------
    def start_server(self, binary):
        os.makedirs(self.models_dir, exist_ok=True)
        env = dict(os.environ, OLLAMA_HOST=self.app_url.split("://", 1)[1],
                   OLLAMA_MODELS=self.models_dir)
        self.say("server", "запуск службы Ollama: %s serve" % binary)
        with open(self.log_path, "ab") as log:
            self.server = subprocess.Popen([binary, "serve"], env=env, stdout=log,
                                           stderr=subprocess.STDOUT,
                                           stdin=subprocess.DEVNULL)
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.cancel.is_set():
                self.stop_server()
                self.check_cancel()
            code = self.server.poll()
            if code is not None:
                self.server = None
                raise SetupError("служба Ollama завершилась с кодом %d, подробности "
                                 "в файле %s" % (code, self.log_path))
            if self.alive(self.app_url):
                self.say("server", "служба Ollama запущена (PID %d)" % self.server.pid)
                self.note("Служба Ollama программы: %s, модели в %s"
                          % (self.app_url, self.models_dir))
                return self.app_url
            time.sleep(0.5)
        self.stop_server()
        raise SetupError("служба Ollama не ответила за 60 с, подробности в %s"
                         % self.log_path)

    def stop_server(self):
        """Останавливает службу, если её запустила программа. Возвращает код."""
        proc, self.server = self.server, None
        if proc is None or proc.poll() is not None:
            return None if proc is None else proc.returncode
        proc.terminate()
        try:
            return proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.wait()

    # --- модель -------------------------------------------------------------
    def has_model(self, url):
        try:
            data, _ = request_json(url + "/api/tags", timeout=10)
        except HttpError as exc:
            raise SetupError("Ollama не ответила на запрос списка моделей: %s"
                             % exc.message) from None
        names = set()
        for m in data.get("models", []):
            names.update({m.get("name"), m.get("model")})
        found = bool(names & {self.model, self.model + ":latest"})
        where = self.models_dir if self.server else "службе %s" % url
        self.note("Модель %s в %s: %s" % (self.model, where,
                                          "есть" if found else "нет, будет скачана"))
        if not found:
            self.say("model", "модели %s нет в %s, она будет скачана" % (self.model, where))
        return found

    def registry_name(self):
        """Имя модели в реестре: qwen2.5:1.5b -> ("library/qwen2.5", "1.5b")."""
        name, _, tag = self.model.partition(":")
        if "/" not in name:
            name = "library/" + name
        return name, tag or "latest"

    def manifest_url(self):
        return "%s/v2/%s/manifests/%s" % ((self.registry,) + self.registry_name())

    def check_registry(self):
        """Проверяет связь с реестром моделей теми же запросами, с которых
        служба Ollama начинает загрузку: описание модели (GET .../manifests/<тег>)
        и самый большой файл модели (HEAD .../blobs/<digest>, реестр
        перенаправляет его на сервер хранения). Эти запросы Ollama выполняет
        без ограничения времени, поэтому при плохой связи загрузка «висела» бы
        молча; здесь ожидание ограничено registry_timeout."""
        url = self.manifest_url()
        host = urllib.parse.urlsplit(url).hostname
        self.say("model", "проверка связи с реестром моделей %s" % host)
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.docker.distribution.manifest.v2+json",
            "User-Agent": "superapp"})
        try:
            with urlopen(req, timeout=self.registry_timeout) as resp:
                manifest = resp.read(1 << 20)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 404:
                raise SetupError("модели %s нет в реестре Ollama (%s); проверьте имя "
                                 "в параметре --model" % (self.model, host)) from None
            if exc.code < 500:
                # сервер ответил (например, просит авторизацию) - связь есть,
                # остальное решит сама Ollama
                self.note("Реестр моделей %s: ответ HTTP %d" % (host, exc.code))
                return
            reason = "ошибка сервера, HTTP %d" % exc.code
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            reason = describe_net_error(getattr(exc, "reason", exc), self.registry_timeout)
        else:
            self.note("Реестр моделей %s: доступен" % host)
            self.check_blob(manifest, host)
            return
        self.note("Реестр моделей %s: недоступен (%s)" % (host, reason))
        raise SetupError(
            "нет связи с реестром моделей Ollama %s (%s), а модель скачивается "
            "оттуда. Проверьте подключение к интернету; если ollama.com у вас "
            "открывается только через VPN или прокси, включите его" % (host, reason))

    def check_blob(self, manifest, host):
        """HEAD самого большого файла модели с переходом по перенаправлению."""
        try:
            layers = json.loads(manifest.decode("utf-8")).get("layers") or []
            digest = max(layers, key=lambda l: l.get("size", 0))["digest"]
        except (ValueError, AttributeError, KeyError, TypeError, UnicodeDecodeError):
            return                    # формат описания неизвестен - решит сама Ollama
        url = "%s/v2/%s/blobs/%s" % (self.registry, self.registry_name()[0], digest)
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "superapp"})
        try:
            with urlopen(req, timeout=self.registry_timeout) as resp:
                where = urllib.parse.urlsplit(resp.geturl()).hostname
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code < 500:
                self.note("Файлы модели: ответ HTTP %d" % exc.code)
                return
            reason = "ошибка сервера, HTTP %d" % exc.code
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            reason = describe_net_error(getattr(exc, "reason", exc), self.registry_timeout)
        else:
            self.note("Файлы модели: сервер %s доступен" % where)
            return
        self.note("Файлы модели: недоступны (%s)" % reason)
        raise SetupError(
            "реестр моделей Ollama %s отвечает, но файлы модели, на которые он "
            "перенаправляет, не загружаются (%s). Проверьте подключение к "
            "интернету; если ollama.com у вас открывается только через VPN или "
            "прокси, включите его" % (host, reason))

    def log_mark(self):
        try:
            return os.path.getsize(self.log_path)
        except OSError:
            return 0

    def log_hint(self, since=0):
        """Последнее сообщение Ollama о сбое из model/ollama-serve.log,
        записанное после позиции since (то есть во время этой загрузки)."""
        try:
            with open(self.log_path, "rb") as f:
                f.seek(max(since, self.log_mark() - 65536))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return ""
        keys = ("error", "failed", "stalled", "timeout", "timed out", "refused",
                "reset", "tls", "x509", "dial tcp", "no such host")
        for line in reversed(lines[-200:]):
            if any(k in line.lower() for k in keys):
                m = re.search(r'msg="((?:[^"\\]|\\.)*)"', line)
                text = m.group(1).replace('\\"', '"') if m else line.strip()
                return text[:300]
        return ""

    def pull(self, url):
        self.say("model", "скачивание модели %s" % self.model)
        body = json.dumps({"model": self.model, "stream": True}).encode("utf-8")
        req = urllib.request.Request(url + "/api/pull", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        mark = self.log_mark()
        try:
            resp = urlopen(req, timeout=self.pull_silence)
        except urllib.error.HTTPError as exc:
            raise SetupError("Ollama отказала в загрузке модели: %d" % exc.code) from None
        except (urllib.error.URLError, OSError) as exc:
            raise SetupError("нет связи с Ollama: %s" % getattr(exc, "reason", exc)) from None
        layers, checked, last = {}, False, ""
        self.active = resp
        with resp:
            try:
                for raw in resp:
                    self.check_cancel()
                    line = raw.strip()
                    if not line:
                        continue
                    msg = json.loads(line)
                    if "error" in msg:
                        raise SetupError("Ollama: %s" % msg["error"])
                    last = msg.get("status", "")
                    if msg.get("digest") and msg.get("total"):
                        layers[msg["digest"]] = (msg.get("completed", 0), msg["total"])
                        done = sum(c for c, _ in layers.values())
                        total = sum(t for _, t in layers.values())
                        if not checked:
                            self.ensure_space(int((total - done) * 1.1))
                            checked = True
                        self.say("model", "скачивание модели %s" % self.model, done, total)
                    elif last:
                        self.say("model", STATUS_RU.get(last, last))
            except (OSError, http.client.HTTPException) as exc:
                self.check_cancel()
                if isinstance(exc, (TimeoutError, socket.timeout)):
                    what = ("служба Ollama больше %d с не получает данных от реестра "
                            "моделей (этап: %s)" % (self.pull_silence,
                                                     STATUS_RU.get(last, last) or "начало"))
                else:
                    what = "загрузка модели прервана (%s)" % exc
                hint = self.log_hint(mark)
                if hint:
                    what += "; Ollama сообщает: %s" % hint
                self.note("Загрузка модели: " + what)
                tail = ("скачанная часть сохранена, загрузка продолжится при следующем "
                        "запуске" if layers else
                        "проверьте подключение к интернету (VPN, прокси)")
                raise SetupError("%s; %s" % (what, tail)) from None
        self.active = None
        self.check_cancel()
        if last != "success":
            raise SetupError("загрузка модели не завершена (последнее состояние: %s); "
                             "при следующем запуске она продолжится" % (last or "нет"))


def locate(model=None, progress=None):
    """Находит или запускает Ollama из папки model с уже скачанной и
    проверенной моделью, ничего не скачивая.

    Возвращает объект ModelSetup (его службу нужно остановить методом
    stop_server) или None, если модель недоступна.
    """
    setup = ModelSetup(model, progress, allow_download=False)
    try:
        url = setup.run()
    except SetupError:
        setup.stop_server()
        return None
    os.environ["SUPERAPP_OLLAMA_URL"] = url
    os.environ["SUPERAPP_LLM_MODEL"] = setup.model
    return setup
