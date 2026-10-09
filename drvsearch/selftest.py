"""Самопроверка языковой модели на задачах программы.

Перед первым использованием модель решает три задания с заранее
известным ответом. Запросы и JSON-схемы те же, что в поиске (pipeline.py):
  1. выделить обозначение микросхемы из названия устройства;
  2. отличить драйвер этого устройства от постороннего репозитория;
  3. переписать команды установки из фрагмента README дословно.
Язык пояснений не проверяется: модель пишет их по-английски (см. pipeline.py).
Если модель ошиблась или отвечала дольше TIME_LIMIT секунд, программа её не
использует, и поиск идёт без модели. Результат хранится в model/selftest.json
вместе с отпечатком модели (digest из /api/tags), поэтому проверка проходит
один раз; «Модель → Повторить подготовку и проверку» проводит её заново.
"""

import json
import os
import time
from datetime import datetime

from . import textutil as tu
from .llm import LLMError
from .pipeline import (KEYWORDS_SCHEMA, RANK_SCHEMA, STEPS_SCHEMA, SYSTEM,
                       keywords_prompt, rank_prompt, steps_prompt)

TIME_LIMIT = 90           # с на одно задание (первое включает загрузку модели в память)
VERSION = 3               # меняется вместе с заданиями: старый результат не годится
                          # (в версии 2 требовались пояснения на русском)
BIGGER = "qwen2.5:3b"     # что предложить, если модель ошибается (не если медленная)

DEVICE = {"vid": 0x0BDA, "pid": 0x8179, "full_name": "Realtek 802.11n NIC",
          "name_from_device": "Realtek 802.11n NIC",
          "name_from_db": "Realtek Semiconductor Corp. RTL8188EUS 802.11n "
                          "Wireless Network Adapter"}

# правильный ответ - кандидат 1, а не первый в списке
CANDIDATES = [
    {"id": 0, "name": "someone/awesome-linux",
     "description": "A curated list of awesome Linux applications and tools",
     "stars": 950, "last_push": "2025-01-10", "archived": False, "facts": [],
     "readme": "Awesome Linux. A list of applications: browsers, editors, "
               "music players, games."},
    {"id": 1, "name": "lab/rtl8188eus-linux",
     "description": "Linux driver for Realtek RTL8188EUS USB WiFi adapters",
     "stars": 310, "last_push": "2025-06-01", "archived": False,
     "facts": ["README mentions 0bda:8179"],
     "readme": "Supported USB devices: 0bda:8179 (RTL8188EUS). Build with make "
               "and install with sudo make install."},
]

README = """## Installation
Install the build tools first:
```
sudo apt install build-essential dkms git
```
Then build and install the module:
```
make
sudo make install
sudo modprobe 8188eu
```
"""


def check_keywords(client):
    answer = client.chat_json(SYSTEM, keywords_prompt(DEVICE), KEYWORDS_SCHEMA, 200)
    raw = [str(w) for w in answer.get("keywords", [])]
    words = [tu.clean_keyword(w) or "" for w in raw]
    ok = any(w.startswith("rtl8188eu") for w in words)
    return ok, "ответ: %s (ожидалось rtl8188eus)" % (", ".join(raw) or "пусто")


def check_rank(client):
    answer = client.chat_json(SYSTEM, rank_prompt(DEVICE["full_name"], "0bda", "8179",
                                                  "6.12.0", CANDIDATES), RANK_SCHEMA, 900)
    items = {}
    for item in answer.get("items", []):
        if isinstance(item.get("id"), int):
            items[item["id"]] = item
    right, wrong = items.get(1, {}), items.get(0, {})
    score = lambda item: int(item.get("score", 0) or 0)        # noqa: E731
    reason = " ".join(str(right.get("reason", "")).split())
    ok = bool(right.get("relevant")) and score(right) > score(wrong)
    return ok, "драйвер: %s/10%s, посторонний репозиторий: %s/10; пояснение: «%s»" % (
        score(right), "" if right.get("relevant") else " (не признан)", score(wrong),
        reason[:80])


def check_steps(client):
    answer = client.chat_json(SYSTEM, steps_prompt("lab/rtl8188eus-linux", README),
                              STEPS_SCHEMA, 900)
    steps = answer.get("steps", [])
    commands = [tu.normalize_command(s.get("command", "")) for s in steps]
    commands = [c for c in commands if c]
    exact = [c for c in commands if tu.command_in_text(c, README)]
    lines = {line for c in exact for line in c.splitlines()}
    invented = len(commands) - len(exact)
    ok = {"make", "sudo make install"} <= lines and invented <= 1
    return ok, "команд дословно из README: %d, выдуманных: %d" % (len(exact), invented)


CHECKS = [("ключевые слова", check_keywords), ("оценка кандидатов", check_rank),
          ("шаги установки", check_steps)]


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def run(client, digest, cache_path, force=False, progress=None):
    """Проверяет модель client.model; возвращает словарь с полем ok."""
    say = progress or (lambda text: None)
    cache = load(cache_path)
    saved = cache.get(client.model)
    if saved and saved.get("digest") == digest and saved.get("version") == VERSION \
            and not force:
        saved["cached"] = True
        return saved
    result = {"model": client.model, "digest": digest, "version": VERSION,
              "ok": True, "checks": [],
              "date": datetime.now().isoformat(timespec="seconds")}
    started = time.monotonic()
    for name, check in CHECKS:
        say("проверка модели: %s" % name)
        t0 = time.monotonic()
        try:
            ok, detail = check(client)
        except LLMError as exc:
            ok, detail = False, str(exc)
        spent = time.monotonic() - t0
        if ok and spent > TIME_LIMIT:
            ok, detail = False, "%s; ответ занял %.0f с - слишком медленно" % (detail, spent)
        result["checks"].append({"name": name, "ok": ok, "seconds": round(spent, 1),
                                 "detail": detail})
        if not ok:
            result["ok"] = False
            break                      # дальше проверять незачем
    result["seconds"] = round(time.monotonic() - started, 1)
    cache[client.model] = result
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return result


def describe(result):
    """Строки для отчёта и окна «Сведения о модели»."""
    head = "Проверка модели %s: %s" % (
        result.get("model"), "пройдена" if result.get("ok") else "НЕ пройдена")
    if result.get("cached"):
        head += " (%s, сохранённый результат)" % result.get("date", "")[:10]
    lines = [head]
    for c in result.get("checks", []):
        lines.append("  %s %s - %s (%.1f с)" % ("+" if c["ok"] else "-", c["name"],
                                              c["detail"], c["seconds"]))
    return lines


def failure(result):
    bad = [c for c in result.get("checks", []) if not c["ok"]]
    if not bad:
        return ""
    text = "модель %s не прошла проверку («%s»: %s)" % (
        result.get("model"), bad[0]["name"], bad[0]["detail"])
    if "слишком медленно" not in bad[0]["detail"] and result.get("model") != BIGGER:
        text += "; можно попробовать модель крупнее: --model %s" % BIGGER
    return text
