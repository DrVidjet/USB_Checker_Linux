"""Тесты модуля «Поиск драйвера» на имитациях GitHub и Ollama.

Запуск из корня проекта:  python3 -m unittest discover -s tests -v
"""

import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fakesystem as fs                            # noqa: E402
import mockservers as ms                          # noqa: E402
from drvsearch import textutil as tu              # noqa: E402
from drvsearch.channel import Channel             # noqa: E402
from drvsearch.github import GitHub, GitHubError, RateLimited  # noqa: E402
from drvsearch.llm import Ollama                  # noqa: E402
from drvsearch.pipeline import DriverSearch       # noqa: E402

DEVICE = {"vid": 0x0bda, "pid": 0x8179, "manufacturer": "Realtek",
          "product": "802.11n NIC", "vendor_db": "Realtek Semiconductor Corp.",
          "product_db": "RTL8188EUS 802.11n Wireless Network Adapter",
          "name_from_device": "Realtek 802.11n NIC",
          "name_from_db": "Realtek Semiconductor Corp. RTL8188EUS 802.11n "
                          "Wireless Network Adapter",
          "full_name": "Realtek 802.11n NIC", "verdict": "missing", "interfaces": []}


class TestText(unittest.TestCase):
    def test_keywords(self):
        self.assertEqual(tu.chip_keywords("ASIX AX88179 Gigabit", "CH340 serial"),
                         ["ax88179", "ch340"])
        self.assertIsNone(tu.clean_keyword("bad word!"))
        self.assertIsNone(tu.clean_keyword("usb"))
        self.assertEqual(tu.clean_keyword("RTL8188EUS"), "rtl8188eus")

    def test_id_mentions(self):
        self.assertTrue(tu.mentions_id("Realtek NIC (0bda:8179)", "0bda", "8179"))
        self.assertTrue(tu.mentions_id("{USB_DEVICE(0x0BDA, 0x8179)}", "0bda", "8179"))
        self.assertFalse(tu.mentions_id("0bda:81790", "0bda", "8179"))

    def test_install_excerpt_and_commands(self):
        excerpt, headings = tu.install_excerpt(ms.GOOD_README)
        self.assertIn("Installation", headings)
        self.assertIn("make", excerpt)
        self.assertTrue(tu.command_in_text("$ sudo   modprobe 8188eu", ms.GOOD_README))
        self.assertFalse(tu.command_in_text("sudo ./install-everything.sh",
                                            ms.GOOD_README))
        self.assertEqual(len(tu.install_blocks(ms.GOOD_README)), 3)

    def test_danger(self):
        self.assertTrue(tu.danger_notes("curl -sL https://x/s.sh | sudo bash"))
        self.assertFalse(tu.danger_notes("make && sudo make install"))


class TestChannel(unittest.TestCase):
    def test_roundtrip_and_eof(self):
        r, w = os.pipe()
        writer, reader = Channel(None, w), Channel(r, None)
        big = {"type": "device", "device": {"blob": "x" * 20000}}   # больше PIPE_BUF
        self.assertTrue(writer.send({"type": "ready", "pid": 1}))
        self.assertTrue(writer.send(big))
        writer.close()
        messages = []
        while True:
            got = reader.receive()
            if got is None:
                break
            messages.extend(got)
        self.assertEqual(messages[0]["type"], "ready")
        self.assertEqual(len(messages[1]["device"]["blob"]), 20000)
        reader.close()

    def test_send_to_closed_pipe(self):
        r, w = os.pipe()
        os.close(r)
        self.assertFalse(Channel(None, w).send({"type": "x"}))


class WithMocks(unittest.TestCase):
    def setUp(self):
        self.gh = ms.GitHubMock()
        self.llm = ms.OllamaMock()
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        # «мощный компьютер», чтобы проверка памяти не зависела от машины
        os.environ["SUPERAPP_PROC_ROOT"] = fs.make_proc(os.path.join(self.tmp, "proc"))
        self.progress = []

    def tearDown(self):
        self.gh.close()
        self.llm.close()
        os.environ.pop("SUPERAPP_PROC_ROOT", None)
        shutil.rmtree(self.tmp)

    def search(self, llm=True, token=None, gh_url=None):
        github = GitHub(token=token, base=gh_url or self.gh.url)
        model = Ollama(url=self.llm.url) if llm else None
        return DriverSearch(github=github, llm=model, use_llm=llm,
                            progress=self.progress.append, kernel="7.2.7-arch1-1")


class TestClients(WithMocks):
    def test_github(self):
        gh = GitHub(base=self.gh.url)
        items = gh.search_repositories("rtl8188eus linux driver")
        self.assertEqual(len(items), 4)
        readme = gh.readme("example-lab/rtl8188eus-linux")
        self.assertIn("0bda:8179", readme["text"])
        self.assertIsNone(gh.readme("nobody/nothing"))
        self.assertTrue(self.gh.requests[0][2].get("User-Agent"))
        with self.assertRaises(GitHubError):
            gh.search_code("0x0bda 0x8179")            # без токена
        self.gh.rate_limited = True
        with self.assertRaises(RateLimited) as ctx:
            gh.search_repositories("rtl8188eus")
        self.assertIn("лимит", str(ctx.exception))

    def test_ollama(self):
        ok, note = Ollama(url=self.llm.url).check()
        self.assertTrue(ok, note)
        ok, note = Ollama(url=self.llm.url, model="nope:1b").check()
        self.assertFalse(ok)
        self.assertIn("не скачана", note)
        self.assertIn("--setup-model --model nope:1b", note)
        ok, note = Ollama(url=ms.closed_url()).check()
        self.assertFalse(ok)
        self.assertIn("не отвечает", note)


class TestPipeline(WithMocks):
    def test_full_search_with_model(self):
        result = self.search().run(DEVICE)
        self.assertTrue(result["llm"]["used"])
        best = result["results"][0]
        self.assertEqual(best["full_name"], "example-lab/rtl8188eus-linux")
        self.assertIn("README упоминает 0bda:8179", best["evidence"])
        self.assertEqual(best["reason"], "supports 0bda:8179")
        # все ссылки взяты из ответов GitHub
        self.assertTrue(all(r["url"].startswith("https://github.com/")
                            and r["full_name"] in ms.REPOS for r in result["results"]))
        # выдуманная моделью команда отброшена, остальные есть в README дословно
        commands = [s["command"] for s in best["steps"]]
        self.assertNotIn("sudo ./install-everything.sh --force", commands)
        self.assertIn("sudo modprobe 8188eu", commands)
        self.assertTrue(all(tu.command_in_text(c, ms.GOOD_README) for c in commands if c))
        self.assertIn("отброшено команд, которых нет в README: 1", best["steps_source"])
        # список-«awesome» не выше настоящего драйвера, форк с curl | bash помечен
        names = [r["full_name"] for r in result["results"]]
        risky = next(r for r in result["results"] if r["full_name"] == "someone/rtl8188eus-dkms")
        self.assertTrue(any("опасная команда" in w for w in risky["warnings"]))
        self.assertTrue(any("форк" in w for w in risky["warnings"]))
        self.assertNotIn("foo/awesome-wifi", names)       # нет совпадений с устройством
        self.assertNotIn("old/rtl8188eu-archive", names)
        # модель вызывалась с JSON-схемой, температурой 0 и без размышлений
        chat = self.llm.chats[0]
        self.assertIn("properties", chat["format"])
        self.assertEqual(chat["options"]["temperature"], 0)
        self.assertIs(chat["think"], False)
        self.assertFalse(chat["stream"])
        self.assertTrue(any("linux-headers" in w for w in result["warnings"]))

    def test_without_model(self):
        result = self.search(llm=False).run(DEVICE)
        self.assertFalse(result["llm"]["used"])
        best = result["results"][0]
        self.assertEqual(best["full_name"], "example-lab/rtl8188eus-linux")
        self.assertEqual(len(best["steps"]), 3)          # блоки кода из README
        self.assertEqual(self.llm.chats, [])

    def test_model_service_down(self):
        search = self.search()
        search.llm = Ollama(url=ms.closed_url())
        result = search.run(DEVICE)
        self.assertFalse(result["llm"]["used"])
        self.assertIn("не отвечает", result["llm"]["note"])
        self.assertEqual(result["results"][0]["full_name"], "example-lab/rtl8188eus-linux")

    def test_code_search_with_token(self):
        result = self.search(token="test-token").run(DEVICE)
        best = result["results"][0]
        self.assertTrue(any("таблица устройств" in e for e in best["evidence"]))
        auth = [h.get("Authorization") for p, q, h in self.gh.requests]
        self.assertIn("Bearer test-token", auth)

    def test_rate_limit_and_cache(self):
        first = self.search().run(DEVICE)
        self.assertFalse(first["from_cache"])
        self.gh.rate_limited = True
        second = self.search().run(DEVICE)
        self.assertTrue(second["from_cache"])
        self.assertEqual(second["results"][0]["url"], first["results"][0]["url"])
        down = self.search(gh_url=ms.closed_url())
        shutil.rmtree(os.path.join(self.tmp, "superapp"))
        third = down.run(DEVICE)
        self.assertEqual(third["results"], [])
        self.assertTrue(any("не завершён" in w for w in third["warnings"]))


class TestPopupProcess(WithMocks):
    """Окно поиска как отдельный процесс: обмен по каналам и завершение."""

    def read_until(self, channel, wanted, timeout=60):
        got = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            ready, _, _ = select.select([channel.in_fd], [], [], 0.5)
            if not ready:
                continue
            batch = channel.receive()
            if batch is None:
                return got, True
            got.extend(batch)
            if any(m.get("type") == wanted for m in batch):
                return got, False
        self.fail("нет сообщения %s" % wanted)

    def test_window_process(self):
        to_r, to_w = os.pipe()
        from_r, from_w = os.pipe()
        # модель ещё скачивается главным окном: служба по адресу окна не отвечает
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
                   DRVSEARCH_GITHUB_API=self.gh.url, SUPERAPP_OLLAMA_URL=ms.closed_url())
        proc = subprocess.Popen(
            [sys.executable, "-m", "drvsearch", "--window",
             "--in-fd", str(to_r), "--out-fd", str(from_w)],
            pass_fds=(to_r, from_w), cwd=ROOT, env=env,
            stderr=subprocess.DEVNULL)
        os.close(to_r)
        os.close(from_w)
        writer, reader = Channel(None, to_w), Channel(from_r, None)
        got, _ = self.read_until(reader, "ready")
        self.assertEqual(got[0]["pid"], proc.pid)
        writer.send({"type": "model", "state": "not_ready", "model": "qwen2.5:1.5b",
                     "text": "qwen2.5:1.5b ещё не готова (загрузка qwen2.5:1.5b: 40%)"})
        writer.send({"type": "device", "device": DEVICE})
        got, _ = self.read_until(reader, "result")
        result = [m for m in got if m["type"] == "result"][0]
        self.assertEqual(result["top"][0]["name"], "example-lab/rtl8188eus-linux")
        self.assertIsNone(result["llm"])
        self.assertTrue(any(m["type"] == "progress" for m in got))
        # главное окно сообщает по каналу, что модель готова; повторный поиск идёт с ней
        writer.send({"type": "model", "state": "ready", "url": self.llm.url,
                     "model": "qwen2.5:1.5b"})
        writer.send({"type": "device", "device": DEVICE})
        got, _ = self.read_until(reader, "result")
        result = [m for m in got if m["type"] == "result"][0]
        self.assertEqual(result["llm"], "qwen2.5:1.5b")
        self.assertTrue(self.llm.chats)
        # главное окно «закрылось»: окно поиска получает конец файла и завершается
        writer.close()
        self.assertEqual(proc.wait(timeout=15), 0)
        reader.close()

    def test_window_without_llm(self):
        """Кнопка «Найти драйвер без ИИ»: окно с --no-llm не трогает модель,
        даже если главное окно сообщает, что она готова."""
        to_r, to_w = os.pipe()
        from_r, from_w = os.pipe()
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
                   DRVSEARCH_GITHUB_API=self.gh.url, SUPERAPP_OLLAMA_URL=self.llm.url)
        proc = subprocess.Popen(
            [sys.executable, "-m", "drvsearch", "--window",
             "--in-fd", str(to_r), "--out-fd", str(from_w), "--no-llm"],
            pass_fds=(to_r, from_w), cwd=ROOT, env=env, stderr=subprocess.DEVNULL)
        os.close(to_r)
        os.close(from_w)
        writer, reader = Channel(None, to_w), Channel(from_r, None)
        self.read_until(reader, "ready")
        writer.send({"type": "model", "state": "ready", "url": self.llm.url,
                     "model": "qwen2.5:1.5b"})
        writer.send({"type": "device", "device": DEVICE})
        got, _ = self.read_until(reader, "result")
        result = [m for m in got if m["type"] == "result"][0]
        self.assertIsNone(result["llm"])
        self.assertEqual(result["top"][0]["name"], "example-lab/rtl8188eus-linux")
        self.assertEqual(self.llm.chats, [])
        writer.close()
        self.assertEqual(proc.wait(timeout=15), 0)
        reader.close()


if __name__ == "__main__":
    unittest.main()
