"""Тесты подготовки модели: переносимая Ollama, докачка, загрузка модели.

Запуск из корня проекта:  python3 -m unittest discover -s tests -v
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import namedtuple
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fakeollama as fo                            # noqa: E402
import fakesystem as fs                            # noqa: E402
import mockservers as ms                           # noqa: E402
from drvsearch import runtime as rt                # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="superapp-rt-")
        self.events = []
        self.cleanup = []
        # «мощный компьютер»: 16 ГБ ОЗУ, AVX2 - чтобы проверки не зависели от машины
        # XDG_DATA_HOME - чтобы не трогать файлы прежней версии в ~/.local/share
        env = mock.patch.dict(os.environ, {
            "SUPERAPP_PROC_ROOT": fs.make_proc(os.path.join(self.tmp, "proc")),
            "XDG_DATA_HOME": os.path.join(self.tmp, "xdg"), "FAKE_CHAT": "good"})
        env.start()
        self.cleanup.append(env.stop)

    def tearDown(self):
        for fn in reversed(self.cleanup):
            fn()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def registry(self, mode="ok"):
        """Имитация реестра моделей (registry.ollama.ai)."""
        srv = fo.DownloadServer(b"", registry=mode)
        self.cleanup.append(srv.close)
        return srv.url

    def setup_obj(self, server_url, **kw):
        kw.setdefault("app_port", fo.free_port())
        if "registry" not in kw:
            kw["registry"] = self.registry()
        setup = rt.ModelSetup(
            model="qwen2.5:1.5b", progress=lambda *e: self.events.append(e),
            base_dir=os.path.join(self.tmp, "data"), download_base=server_url, **kw)
        self.cleanup.append(setup.stop_server)
        return setup


class TestHelpers(unittest.TestCase):
    def test_human(self):
        self.assertEqual(rt.human(2_684_354_560), "2,5 ГБ")
        self.assertEqual(rt.human(870 * 1024 * 1024), "870 МБ")
        self.assertEqual(rt.human(None), "?")


class TestPortable(Base):
    def test_download_resume_after_drop(self):
        payload = fo.build_archive()
        srv = fo.DownloadServer(payload, drop_first_at=len(payload) // 3)
        self.cleanup.append(srv.close)
        setup = self.setup_obj(srv.url)
        target = os.path.join(setup.base, "ollama-linux-amd64.tar.zst")
        with self.assertRaises(rt.SetupError) as ctx:
            setup.download(srv.url + "/ollama-linux-amd64.tar.zst", target)
        self.assertIn("продолжится", str(ctx.exception))
        self.assertEqual(os.path.getsize(target + ".part"), len(payload) // 3)
        setup.download(srv.url + "/ollama-linux-amd64.tar.zst", target)
        self.assertEqual(srv.requests[1][1], "bytes=%d-" % (len(payload) // 3))
        with open(target, "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_full_setup_without_ollama(self):
        srv = fo.DownloadServer(fo.build_archive())
        self.cleanup.append(srv.close)
        setup = self.setup_obj(srv.url)
        url = setup.run()
        self.assertEqual(url, setup.app_url)
        self.assertTrue(os.access(os.path.join(setup.runtime_dir, "bin", "ollama"), os.X_OK))
        self.assertTrue(os.path.islink(os.path.join(setup.runtime_dir, "lib", "ollama",
                                                    "libfake.so.1")))
        self.assertFalse(os.path.exists(os.path.join(setup.base, "ollama-linux-amd64.tar.zst")))
        self.assertTrue(setup.has_model(url))
        self.assertTrue(os.path.exists(os.path.join(setup.models_dir, "qwen2.5__1.5b")))
        stages = [e[0] for e in self.events]
        for stage in ("runtime", "extract", "server", "model", "selftest", "ready"):
            self.assertIn(stage, stages)
        self.assertTrue(setup.test["ok"])
        last = [e for e in self.events if e[0] == "model" and e[3]][-1]
        self.assertEqual(last[2], last[3])          # прогресс дошёл до 100 %
        pid = setup.server.pid
        setup.stop_server()
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

        # повторный запуск: всё уже есть, ничего не скачивается, проверка из кэша
        before = len(srv.requests)
        self.events.clear()
        again = self.setup_obj(srv.url)
        again.run()
        self.assertEqual(len(srv.requests), before)
        self.assertTrue(again.test.get("cached"))
        self.assertNotIn("selftest", [e[0] for e in self.events])

    def test_abort_stalled_download(self):
        payload = fo.build_archive()
        srv = fo.DownloadServer(payload, stall_at=len(payload) // 2)
        self.cleanup.append(srv.close)
        setup = self.setup_obj(srv.url)
        target = os.path.join(setup.base, "ollama.tar.zst")
        threading.Timer(1.0, setup.abort).start()
        started = time.time()
        with self.assertRaises(rt.Cancelled):
            setup.download(srv.url + "/ollama.tar.zst", target)
        self.assertLess(time.time() - started, 5)     # не ждёт тайм-аута в 60 с
        self.assertEqual(os.path.getsize(target + ".part"), len(payload) // 2)

    def test_unsafe_archive_rejected(self):
        srv = fo.DownloadServer(fo.build_archive(evil=True))
        self.cleanup.append(srv.close)
        with self.assertRaises(rt.SetupError) as ctx:
            self.setup_obj(srv.url).install_portable()
        self.assertIn("недопустимый путь", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "data", "evil.sh")))

    def test_link_outside_rejected(self):
        srv = fo.DownloadServer(fo.build_archive(evil="link"))
        self.cleanup.append(srv.close)
        setup = self.setup_obj(srv.url)
        # как в Python без фильтра "data": защищает только собственная проверка
        with mock.patch.object(rt, "extract_args", return_value={}):
            with self.assertRaises(rt.SetupError) as ctx:
                setup.install_portable()
        self.assertIn("ссылка за пределы", str(ctx.exception))
        self.assertFalse(os.path.exists(setup.runtime_dir))

    def test_unsupported_arch(self):
        with mock.patch.object(rt.platform, "machine", return_value="riscv64"):
            with self.assertRaises(rt.SetupError) as ctx:
                self.setup_obj("http://unused").install_portable()
        self.assertIn("riscv64", str(ctx.exception))

    def test_not_enough_space(self):
        srv = fo.DownloadServer(fo.build_archive())
        self.cleanup.append(srv.close)
        usage = namedtuple("usage", "total used free")(10 ** 9, 10 ** 9, 1000)
        with mock.patch.object(rt.shutil, "disk_usage", return_value=usage):
            with self.assertRaises(rt.SetupError) as ctx:
                self.setup_obj(srv.url).install_portable()
        self.assertIn("недостаточно места", str(ctx.exception))


class TestModelPull(Base):
    def start_fake(self, models_dir, delay="0.02", stall="0", port=None):
        script = os.path.join(self.tmp, "ollama")
        with open(script, "w") as f:
            f.write(fo.FAKE_OLLAMA % {"python": sys.executable})
        os.chmod(script, 0o755)
        port = port or fo.free_port()
        env = dict(os.environ, OLLAMA_HOST="127.0.0.1:%d" % port,
                   OLLAMA_MODELS=models_dir, FAKE_PULL_DELAY=delay, FAKE_PULL_STALL=stall)
        proc = subprocess.Popen([script, "serve"], env=env)
        self.cleanup.append(lambda: (proc.terminate(), proc.wait()))
        url = "http://127.0.0.1:%d" % port
        probe = rt.ModelSetup()
        for _ in range(100):
            if probe.alive(url):
                return url
            threading.Event().wait(0.05)
        self.fail("поддельная служба не запустилась")

    def test_existing_service_with_model(self):
        models = os.path.join(self.tmp, "m")
        os.makedirs(models)
        open(os.path.join(models, "qwen2.5__1.5b"), "w").close()
        # своя служба, оставшаяся от прошлого запуска, используется повторно
        port = fo.free_port()
        url = self.start_fake(models, port=port)
        setup = self.setup_obj("http://unused", app_port=port)
        self.assertEqual(setup.run(), url)
        self.assertIsNone(setup.server)
        self.assertNotIn("model", [e[0] for e in self.events])

    def test_cancel_and_resume_pull(self):
        port = fo.free_port()
        url = self.start_fake(os.path.join(self.tmp, "m"), delay="0.1", port=port)
        cancel = threading.Event()

        def progress(stage, text, done, total):
            self.events.append((stage, text, done, total))
            if stage == "model" and done and done >= total * 3 // 10:
                cancel.set()

        setup = self.setup_obj("http://unused", app_port=port)
        setup.progress, setup.cancel = progress, cancel
        with self.assertRaises(rt.Cancelled):
            setup.run()
        self.assertFalse(setup.has_model(url))
        self.events.clear()
        again = self.setup_obj("http://unused", app_port=port)
        again.run()
        first = [e for e in self.events if e[0] == "model" and e[3]][0]
        self.assertGreater(first[2], 0)              # продолжилась, а не началась заново
        self.assertTrue(again.has_model(url))

    def test_abort_stalled_pull(self):
        port = fo.free_port()
        self.start_fake(os.path.join(self.tmp, "m"), stall="4", port=port)
        setup = self.setup_obj("http://unused", app_port=port)
        threading.Timer(1.5, setup.abort).start()
        started = time.time()
        with self.assertRaises(rt.Cancelled):
            setup.run()
        self.assertLess(time.time() - started, 6)
        done = [e[2] for e in self.events if e[0] == "model" and e[3]]
        self.assertEqual(done[-1], 50 * 1024 * 1024 * 4 // 10)

    def test_registry_unreachable(self):
        # реестр моделей недоступен: понятная причина, загрузка не начинается
        port = fo.free_port()
        models = os.path.join(self.tmp, "m")
        self.start_fake(models, port=port)
        setup = self.setup_obj("http://unused", app_port=port, registry=ms.closed_url())
        with self.assertRaises(rt.SetupError) as ctx:
            setup.run()
        self.assertIn("нет связи с реестром моделей", str(ctx.exception))
        self.assertIn("соединение отклонено", str(ctx.exception))
        self.assertFalse([e for e in self.events if e[0] == "model" and e[3]])
        self.assertFalse(os.path.exists(os.path.join(models, "qwen2.5__1.5b.partial")))
        self.assertTrue(any("недоступен" in line for line in setup.report))

    def test_registry_not_answering(self):
        port = fo.free_port()
        self.start_fake(os.path.join(self.tmp, "m"), port=port)
        setup = self.setup_obj("http://unused", app_port=port,
                               registry=self.registry("stall"))
        setup.registry_timeout = 1
        started = time.time()
        with self.assertRaises(rt.SetupError) as ctx:
            setup.run()
        self.assertLess(time.time() - started, 8)
        self.assertIn("сервер не ответил за 1 с", str(ctx.exception))

    def test_model_files_not_answering(self):
        port = fo.free_port()
        self.start_fake(os.path.join(self.tmp, "m"), port=port)
        registry = fo.DownloadServer(b"", registry="blob_stall")
        self.cleanup.append(registry.close)
        setup = self.setup_obj("http://unused", app_port=port, registry=registry.url)
        setup.registry_timeout = 1
        with self.assertRaises(rt.SetupError) as ctx:
            setup.run()
        self.assertIn("файлы модели", str(ctx.exception))
        self.assertIn("не ответил за 1 с", str(ctx.exception))
        self.assertEqual(registry.manifests, [
            "/v2/library/qwen2.5/manifests/1.5b", "HEAD /v2/library/qwen2.5/blobs/sha256:abc"])

    def test_unknown_model_in_registry(self):
        port = fo.free_port()
        self.start_fake(os.path.join(self.tmp, "m"), port=port)
        setup = self.setup_obj("http://unused", app_port=port,
                               registry=self.registry("missing"))
        with self.assertRaises(rt.SetupError) as ctx:
            setup.run()
        self.assertIn("нет в реестре Ollama", str(ctx.exception))

    def test_ollama_silent_while_pulling(self):
        # реестр проверку прошёл, но служба Ollama молчит после «pulling manifest»:
        # сообщение называет этап и причину из журнала Ollama
        srv = fo.DownloadServer(fo.build_archive())
        self.cleanup.append(srv.close)
        setup = self.setup_obj(srv.url)
        setup.pull_silence = 2
        started = time.time()
        with mock.patch.dict(os.environ, {"FAKE_PULL_MANIFEST_STALL": "1"}):
            with self.assertRaises(rt.SetupError) as ctx:
                setup.run()
        self.assertLess(time.time() - started, 30)
        text = str(ctx.exception)
        self.assertIn("больше 2 с не получает данных от реестра", text)
        self.assertIn("получение описания модели", text)
        self.assertIn("dial tcp: i/o timeout", text)
        self.assertIn("проверьте подключение к интернету", text)
        self.assertNotIn("продолжится", text)        # ничего ещё не скачано

    def test_locate_does_not_download(self):
        port = str(fo.free_port())
        with mock.patch.dict(os.environ, {"SUPERAPP_OLLAMA_PORT": port,
                                          "SUPERAPP_MODEL_DIR": os.path.join(self.tmp, "d")}):
            self.assertIsNone(rt.locate("qwen2.5:1.5b"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "d", "ollama")))


class TestCommandLine(Base):
    """python -m drvsearch --setup-model и консольный поиск с уже скачанной моделью."""

    def setUp(self):
        super().setUp()
        self.srv = fo.DownloadServer(fo.build_archive())
        self.cleanup.append(self.srv.close)
        self.port = fo.free_port()
        self.env = dict(os.environ, SUPERAPP_MODEL_DIR=os.path.join(self.tmp, "model"),
                        XDG_DATA_HOME=os.path.join(self.tmp, "xdg"),
                        XDG_CACHE_HOME=os.path.join(self.tmp, "cache"),
                        SUPERAPP_OLLAMA_DOWNLOAD=self.srv.url,
                        SUPERAPP_OLLAMA_REGISTRY=self.srv.url,
                        SUPERAPP_OLLAMA_PORT=str(self.port))
        for name in ("SUPERAPP_LLM_MODEL", "SUPERAPP_OLLAMA_URL"):
            self.env.pop(name, None)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "drvsearch"] + list(args),
                              cwd=ROOT, env=self.env, capture_output=True,
                              text=True, timeout=120)

    def assert_stopped(self):
        self.assertFalse(rt.ModelSetup().alive("http://127.0.0.1:%d" % self.port))

    def test_setup_then_search(self):
        out = self.cli("--setup-model")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("скачивается Ollama", out.stdout)
        self.assertIn("Проверка модели qwen2.5:1.5b: пройдена", out.stdout)
        self.assertIn("100%", out.stdout)
        self.assertIn("Модель qwen2.5:1.5b готова", out.stdout)
        models = os.path.join(self.tmp, "model", "models")
        self.assertTrue(os.path.exists(os.path.join(models, "qwen2.5__1.5b")))
        self.assert_stopped()                       # служба не остаётся после выхода

        before = len(self.srv.requests)
        again = self.cli("--setup-model")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotIn("скачивание", again.stdout)
        self.assertEqual(len(self.srv.requests), before)
        self.assert_stopped()

        gh = ms.GitHubMock()
        self.cleanup.append(gh.close)
        self.env["DRVSEARCH_GITHUB_API"] = gh.url
        found = self.cli("--vid", "0bda", "--pid", "8179", "--json")
        self.assertEqual(found.returncode, 0, found.stderr)
        result = __import__("json").loads(found.stdout)
        self.assertTrue(result["llm"]["used"], result["llm"])
        self.assertEqual(result["llm"]["model"], "qwen2.5:1.5b")
        self.assert_stopped()

    def test_search_without_model_does_not_download(self):
        gh = ms.GitHubMock()
        self.cleanup.append(gh.close)
        self.env["DRVSEARCH_GITHUB_API"] = gh.url
        found = self.cli("--vid", "0bda", "--pid", "8179", "--json")
        self.assertEqual(found.returncode, 0, found.stderr)
        result = __import__("json").loads(found.stdout)
        self.assertFalse(result["llm"]["used"])
        self.assertIn("--setup-model", result["llm"]["note"])
        self.assertEqual(self.srv.requests, [])


if __name__ == "__main__":
    unittest.main()
