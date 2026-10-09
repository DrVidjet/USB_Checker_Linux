"""Папка model, самопроверка модели и защита от слабого компьютера.

Ollama, модель и «мощность компьютера» - имитации (fakeollama.py,
fakesystem.py), настоящие модели не скачиваются.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fakeollama as fo                            # noqa: E402
import fakesystem as fs                            # noqa: E402
import mockservers as ms                           # noqa: E402
from drvsearch import hwcheck, selftest            # noqa: E402
from drvsearch import runtime as rt                # noqa: E402
from drvsearch.llm import Ollama                   # noqa: E402
from drvsearch.pipeline import DriverSearch        # noqa: E402
from drvsearch.github import GitHub                # noqa: E402


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="superapp-inst-")
        self.cleanup = []
        self.events = []
        self.proc = fs.make_proc(os.path.join(self.tmp, "proc"))
        self.env({"SUPERAPP_PROC_ROOT": self.proc, "HOME": os.path.join(self.tmp, "home"),
                  "XDG_DATA_HOME": os.path.join(self.tmp, "xdg"), "FAKE_CHAT": "good"})
        self.env({}, clear=("SUPERAPP_IGNORE_HW", "SUPERAPP_LLM_MODEL", "SUPERAPP_OLLAMA_URL"))

    def tearDown(self):
        for fn in reversed(self.cleanup):
            fn()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, values, clear=()):
        patch = mock.patch.dict(os.environ, values)
        patch.start()
        self.cleanup.append(patch.stop)
        for name in clear:
            os.environ.pop(name, None)


class TestModelFolder(Env):
    def setup_obj(self, srv=None, **kw):
        setup = rt.ModelSetup(
            "qwen2.5:1.5b", progress=lambda *e: self.events.append(e),
            app_port=fo.free_port(), base_dir=os.path.join(self.tmp, "project", "model"),
            download_base=srv.url if srv else "http://127.0.0.1:9",
            registry=srv.url if srv else "http://127.0.0.1:9", **kw)
        self.cleanup.append(setup.stop_server)
        return setup

    def server(self, **kw):
        srv = fo.DownloadServer(fo.build_archive(), **kw)
        self.cleanup.append(srv.close)
        return srv

    def chats(self, setup):
        path = os.path.join(setup.models_dir, "chats.log")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return f.read().split()

    def test_ollama_in_path_is_not_used(self):
        # даже если ollama есть в системе, всё берётся из папки model
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "ollama"), "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(os.path.join(bindir, "ollama"), 0o755)
        self.env({"PATH": bindir + os.pathsep + os.environ.get("PATH", "")})
        srv = self.server()
        setup = self.setup_obj(srv)
        self.assertEqual(setup.run(), setup.app_url)
        self.assertEqual(len(srv.requests), 1)
        self.assertEqual(setup.binary, os.path.join(setup.base, "ollama", "bin", "ollama"))
        self.assertTrue(os.path.exists(os.path.join(setup.base, "models", "qwen2.5__1.5b")))

    def test_previous_version_files_are_moved(self):
        payload = fo.build_archive()
        legacy = os.path.join(os.environ["XDG_DATA_HOME"], "superapp")
        os.makedirs(legacy)
        with open(os.path.join(legacy, "ollama-linux-amd64.tar.zst.part"), "wb") as f:
            f.write(payload[:1000])                     # недокачанный архив прежней версии
        srv = self.server()
        setup = self.setup_obj(srv)
        with mock.patch.object(rt.platform, "machine", return_value="x86_64"):
            setup.run()
        self.assertEqual(srv.requests[0][1], "bytes=1000-")      # докачка, а не заново
        self.assertFalse(os.path.exists(os.path.join(legacy, "ollama-linux-amd64.tar.zst.part")))

        # прежняя версия успела распаковать Ollama - она переносится целиком
        setup.stop_server()
        other = os.path.join(self.tmp, "other")
        shutil.move(setup.runtime_dir, os.path.join(legacy, "ollama"))
        again = rt.ModelSetup("qwen2.5:1.5b", app_port=fo.free_port(), base_dir=other,
                              download_base=srv.url, registry=srv.url)
        self.cleanup.append(again.stop_server)
        again.run()
        self.assertEqual(len(srv.requests), 1)
        self.assertTrue(os.access(os.path.join(other, "ollama", "bin", "ollama"), os.X_OK))

    def test_selftest_passes_once(self):
        setup = self.setup_obj(self.server())
        setup.run()
        self.assertTrue(setup.test["ok"])
        self.assertEqual([c["name"] for c in setup.test["checks"]],
                         ["ключевые слова", "оценка кандидатов", "шаги установки"])
        self.assertEqual(len(self.chats(setup)), 3)
        setup.stop_server()
        again = self.setup_obj()
        again.run()
        self.assertTrue(again.test["cached"])
        self.assertEqual(len(self.chats(setup)), 3)          # модели вопросов не задавали
        again.stop_server()
        forced = self.setup_obj(retest=True)
        forced.run()
        self.assertEqual(len(self.chats(setup)), 6)

    def test_model_that_fails_is_not_used(self):
        self.env({"FAKE_CHAT": "bad"})
        setup = self.setup_obj(self.server())
        with self.assertRaises(rt.SelfTestFailed) as ctx:
            setup.run()
        self.assertIn("ключевые слова", str(ctx.exception))
        self.assertIsNone(setup.server)                       # служба остановлена сразу
        self.assertTrue(any("НЕ пройдена" in line for line in setup.report))
        saved = selftest.load(setup.selftest_path)["qwen2.5:1.5b"]
        self.assertFalse(saved["ok"])

    def test_answer_language_is_not_checked(self):
        # пояснения на другом языке - не причина отключать модель
        self.env({"FAKE_CHAT": "russian"})
        setup = self.setup_obj(self.server())
        setup.run()
        self.assertTrue(setup.test["ok"])
        self.assertTrue(selftest.load(setup.selftest_path)["qwen2.5:1.5b"]["ok"])
        setup.stop_server()

    def test_old_failed_selftest_is_repeated(self):
        # провал версии 2 (тогда требовались пояснения на русском) не учитывается
        self.env({"FAKE_CHAT": "good"})
        setup = self.setup_obj(self.server())
        setup.run()
        setup.stop_server()
        cache = selftest.load(setup.selftest_path)
        cache["qwen2.5:1.5b"].update(version=2, ok=False)
        with open(setup.selftest_path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        again = self.setup_obj(self.server())
        again.run()
        self.assertTrue(again.test["ok"])
        self.assertFalse(again.test.get("cached"))
        again.stop_server()

    def test_too_slow_model_is_not_used(self):
        self.env({"FAKE_CHAT": "slow", "FAKE_CHAT_DELAY": "0.6"})
        setup = self.setup_obj(self.server())
        with mock.patch.object(selftest, "TIME_LIMIT", 0.3):
            with self.assertRaises(rt.SelfTestFailed) as ctx:
                setup.run()
        self.assertIn("слишком медленно", str(ctx.exception))

    def test_weak_computer_downloads_nothing(self):
        fs.make_proc(self.proc, total_gib=2, avail_gib=1)
        srv = self.server()
        setup = self.setup_obj(srv)
        with self.assertRaises(rt.WeakHardware) as ctx:
            setup.run()
        self.assertIn("всего 2,0 ГБ ОЗУ", str(ctx.exception))
        self.assertEqual(srv.requests, [])
        self.assertFalse(os.path.exists(setup.runtime_dir))

    def test_local_service_reachable_with_proxy_variable(self):
        setup = self.setup_obj(self.server())
        setup.run()
        self.env({"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"},
                 clear=("no_proxy", "NO_PROXY"))
        self.assertTrue(setup.alive(setup.app_url))


class TestHardware(Env):
    def test_requirements(self):
        need = hwcheck.estimate("qwen2.5:1.5b")
        self.assertEqual(need, 986_000_000 + 8192 * 28 * 2 * 256 * 2 + (512 << 20))
        self.assertTrue(hwcheck.assess("qwen2.5:1.5b").ok)
        fs.make_proc(self.proc, total_gib=4, avail_gib=3)
        self.assertTrue(hwcheck.assess("qwen2.5:1.5b").ok)
        v = hwcheck.assess("qwen2.5:3b")
        self.assertFalse(v.ok)
        self.assertIn("--model qwen2.5:1.5b", v.text())        # подсказка: модель поменьше
        fs.make_proc(self.proc, total_gib=3, avail_gib=2)
        self.assertFalse(hwcheck.assess("qwen2.5:1.5b").ok)

    def test_cpu(self):
        fs.make_proc(self.proc, flags="fpu sse sse2 sse4_2")
        with mock.patch.object(hwcheck.platform, "machine", return_value="x86_64"):
            v = hwcheck.assess("qwen2.5:1.5b")
            self.assertFalse(v.ok)
            self.assertIn("AVX", v.text())
            fs.make_proc(self.proc, flags="fpu sse sse2 avx")
            v = hwcheck.assess("qwen2.5:1.5b")
            self.assertTrue(v.ok)
            self.assertIn("AVX2", v.text())
        with mock.patch.object(hwcheck, "cpu_count", return_value=1):
            self.assertIn("процессоров 1", hwcheck.assess("qwen2.5:1.5b").text())

    def test_available_memory_before_search(self):
        fs.make_proc(self.proc, total_gib=8, avail_gib=1)
        self.assertTrue(hwcheck.assess("qwen2.5:1.5b").ok)
        v = hwcheck.assess("qwen2.5:1.5b", check_available=True)
        self.assertFalse(v.ok)
        self.assertIn("сейчас свободно 1,0 ГБ", v.text())
        self.assertTrue(hwcheck.assess("qwen2.5:1.5b", check_available=True, loaded=True).ok)

    def test_model_info_and_override(self):
        info = {"general.architecture": "llama", "llama.block_count": 32,
                "llama.attention.head_count": 32, "llama.attention.head_count_kv": 8,
                "llama.embedding_length": 4096}
        self.assertEqual(hwcheck.from_model_info(info, 4_000_000_000),
                         4_000_000_000 + 8192 * 32 * 8 * 256 * 2 + (512 << 20))
        fs.make_proc(self.proc, total_gib=2, avail_gib=1)
        os.environ["SUPERAPP_IGNORE_HW"] = "1"
        self.assertTrue(hwcheck.assess("qwen2.5:1.5b").ok)


class TestSearchProtection(Env):
    DEVICE = {"vid": 0x0BDA, "pid": 0x8179, "full_name": "Realtek 802.11n NIC",
              "product_db": "RTL8188EUS 802.11n Wireless Network Adapter",
              "vendor_db": "Realtek Semiconductor Corp.", "verdict": "missing"}

    def search(self, llm):
        gh = ms.GitHubMock()
        self.cleanup.append(gh.close)
        self.cleanup.append(llm.close)
        self.env({"XDG_CACHE_HOME": os.path.join(self.tmp, "cache")})
        return DriverSearch(github=GitHub(base=gh.url), llm=Ollama(url=llm.url),
                            progress=self.events.append, kernel="7.2.7-arch1-1")

    def test_low_free_memory_means_no_model(self):
        fs.make_proc(self.proc, total_gib=8, avail_gib=1)
        llm = ms.OllamaMock()
        result = self.search(llm).run(self.DEVICE)
        self.assertFalse(result["llm"]["used"])
        self.assertIn("сейчас свободно", result["llm"]["note"])
        self.assertEqual(llm.chats, [])
        self.assertEqual(result["results"][0]["full_name"], "example-lab/rtl8188eus-linux")

    def test_loaded_model_needs_no_free_memory(self):
        fs.make_proc(self.proc, total_gib=8, avail_gib=1)
        result = self.search(ms.OllamaMock(loaded=True)).run(self.DEVICE)
        self.assertTrue(result["llm"]["used"])

    def test_slow_model_is_dropped(self):
        llm = ms.OllamaMock(delay=0.5)
        search = self.search(llm)
        search.slow_limit = 0.3
        result = search.run(self.DEVICE)
        self.assertEqual(len(llm.chats), 1)                 # дальше вопросов не было
        self.assertTrue(any("слишком медленно" in n for n in result["notes"]))
        self.assertEqual(result["results"][0]["full_name"], "example-lab/rtl8188eus-linux")

    def test_model_explanations_are_shown(self):
        result = self.search(ms.OllamaMock()).run(self.DEVICE)
        best = result["results"][0]
        self.assertEqual(best["reason"], "supports 0bda:8179")
        self.assertEqual([st["description"] for st in best["steps"]],
                         ["Install the dependencies", "Build the module", "Load the driver"])

    def test_prompts_are_english(self):
        llm = ms.OllamaMock()
        self.search(llm).run(self.DEVICE)
        self.assertEqual(len(llm.chats), 4)       # ключевые слова, оценка, шаги ×2
        for chat in llm.chats:
            system, user = chat["messages"][0]["content"], chat["messages"][1]["content"]
            self.assertTrue(system.startswith("You help"))
            self.assertIn("in English", system)
            self.assertIn("Respond with JSON matching this schema", user)
            text = system + user + json.dumps(chat["format"], ensure_ascii=False)
            self.assertFalse([c for c in text if "\u0400" <= c <= "\u04ff"], user[:300])
        rank = [c for c in llm.chats if "items" in c["format"]["properties"]][0]
        self.assertIn("one-sentence reason in English", rank["messages"][1]["content"])

    def test_ollama_memory_error_stops_questions(self):
        llm = ms.OllamaMock(fail_chat=True)
        result = self.search(llm).run(self.DEVICE)
        self.assertEqual(len(llm.chats), 1)
        self.assertTrue(any("more system memory" in n for n in result["notes"]))


if __name__ == "__main__":
    unittest.main()
