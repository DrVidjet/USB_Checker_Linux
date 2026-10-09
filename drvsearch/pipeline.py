"""Конвейер поиска внешнего драйвера для устройства USB.

Этапы:
  1) ключевые слова: обозначения микросхем из названий устройства
     (регулярное выражение) и, если доступна, подсказки языковой модели;
  2) поиск репозиториев на GitHub по ключевым словам и по паре VID:PID
     в README; при наличии токена - поиск таблиц устройств в коде на C;
  3) предварительная оценка кандидатов по детерминированным признакам;
  4) чтение README лучших кандидатов, проверка упоминания VID:PID;
  5) оценка релевантности моделью (если доступна);
  6) шаги установки - только из README найденного репозитория: команды,
     которых нет в README дословно, отбрасываются;
  7) предупреждения о рисках.
Ссылки берутся только из ответов GitHub API; модель ссылается на кандидатов
по номеру и не может добавить свою ссылку.
"""

import json
import math
import os
import re
import time
from datetime import datetime, timezone

from . import textutil as tu
from .github import GitHub, GitHubError, RateLimited, load_token
from .llm import LLMError, Ollama

MAX_REPO_QUERIES = 4      # без токена GitHub разрешает 10 поисков в минуту
MAX_CANDIDATES = 6        # README читается только у лучших кандидатов
MAX_RESULTS = 4
STEPS_FOR = 3             # шаги установки - для трёх лучших результатов
SLOW_LIMIT = 120          # с; если один ответ модели дольше, дальше без неё

# Системный промт, задания и описания полей схем - на английском, и пояснения
# модель тоже пишет по-английски. Попытка получать пояснения на русском
# (русские промты и проверка языка ответа) на настоящих qwen2.5:1.5b и
# qwen2.5:3b не удалась: ответы не проходили проверку языка, и самопроверка
# отключала модель совсем. Интерфейс программы при этом остаётся русским.
SYSTEM = ("You help a Linux user find an out-of-tree driver for a USB device "
          "on GitHub. Answer strictly with JSON matching the schema. Write all "
          "human-readable text (the reason and description fields) in English. "
          "Never change repository names, chip identifiers, VID:PID pairs or "
          "commands.")

KEYWORDS_SCHEMA = {
    "type": "object",
    "properties": {"keywords": {"type": "array", "items": {"type": "string"},
                                "maxItems": 4}},
    "required": ["keywords"],
}
RANK_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"},
                       "relevant": {"type": "boolean"},
                       "score": {"type": "integer", "minimum": 0, "maximum": 10},
                       "reason": {"type": "string",
                                  "description": "one sentence in English"}},
        "required": ["id", "relevant", "score", "reason"]}}},
    "required": ["items"],
}
STEPS_SCHEMA = {
    "type": "object",
    "properties": {"steps": {"type": "array", "maxItems": 8, "items": {
        "type": "object",
        "properties": {"description": {"type": "string",
                                       "description": "short explanation in English"},
                       "command": {"type": "string",
                                   "description": "command copied exactly from the "
                                                  "fragment, or an empty string"}},
        "required": ["description", "command"]}}},
    "required": ["steps"],
}


def keywords_prompt(device):
    return ("USB device:\n"
            "- strings reported by the device: %s\n"
            "- name in the usb.ids database: %s\n"
            "- VID:PID: %04x:%04x\n"
            "Extract chipset or model identifiers that Linux driver "
            "repositories would use in their names, for example rtl8188eus, "
            "mt7921u, ax88179. Return at most 4 lowercase identifiers "
            "without spaces. Use only identifiers supported by the names "
            "above; if there are none, return an empty list."
            % (device.get("name_from_device") or "none",
               device.get("name_from_db") or "none",
               device["vid"], device["pid"]))


def rank_prompt(name, vid, pid, kernel, candidates):
    """candidates - словари с полями id, name, description, stars, ..."""
    lines = [json.dumps(c, ensure_ascii=False) for c in candidates]
    return ("Device: %s, VID:PID %s:%s, Linux kernel %s.\nCandidates (one "
            "JSON per line):\n%s\n\nFor every candidate decide whether it "
            "is a Linux driver that supports THIS device (match by VID:PID "
            "or chipset). Give a score from 0 to 10 and a one-sentence reason "
            "in English. Use only the ids listed above."
            % (name, vid, pid, kernel, "\n".join(lines)))


def steps_prompt(repo_name, excerpt):
    return ("README fragment of repository %s:\n<<<\n%s\n>>>\n\nList the "
            "installation steps for Linux described in this fragment, in "
            "order. 'command' must be copied exactly from the fragment "
            "(empty string if the step has no command). 'description' is a "
            "short explanation in English. Do not add anything that is not "
            "in the fragment." % (repo_name, excerpt))


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    return os.path.join(base, "superapp", "drvsearch")


def years_since(iso):
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - when).days / 365.25


class DriverSearch:
    def __init__(self, github=None, llm=None, use_llm=True, progress=None,
                 kernel=None, llm_off=None):
        self.github = github or GitHub(load_token())
        self.llm = llm if llm is not None else (Ollama() if use_llm else None)
        self.progress = progress or (lambda text: None)
        self.kernel = kernel or os.uname().release
        self.notes = []
        self.slow_limit = SLOW_LIMIT
        self.llm_off = llm_off        # причина, по которой модель отключена

    # --- вспомогательное ---------------------------------------------------
    def say(self, text):
        self.progress(text)

    def _ask(self, prompt, schema, num_predict=1024):
        started = time.monotonic()
        try:
            return self.llm.chat_json(SYSTEM, prompt, schema, num_predict)
        except LLMError as exc:
            self.notes.append("модель: %s" % exc)
            if exc.fatal:
                self._drop_llm("модель не ответила (%s)" % exc)
            else:
                self.say("Модель не ответила: %s; продолжаю без неё" % exc)
            return None
        finally:
            spent = time.monotonic() - started
            if self.llm is not None and spent > self.slow_limit:
                # защита слабого компьютера: следующие вопросы ждать не будем
                self._drop_llm("модель отвечает слишком медленно (%.0f с на ответ)"
                               % spent)

    def _drop_llm(self, reason):
        self.llm = None
        self.notes.append(reason)
        self.say("%s; дальше поиск идёт без неё" % (reason[:1].upper() + reason[1:]))

    # --- основной метод ----------------------------------------------------
    def run(self, device):
        vid, pid = "%04x" % device["vid"], "%04x" % device["pid"]
        result = {
            "device": {"id": "%s:%s" % (vid, pid),
                       "name": device.get("full_name", ""),
                       "verdict": device.get("verdict", "")},
            "kernel": self.kernel, "created": datetime.now().isoformat(timespec="seconds"),
            "queries": [], "results": [], "warnings": [], "from_cache": False,
            "llm": {"used": False, "model": None, "note": ""},
            "github": {"token": bool(self.github.token), "note": ""},
        }
        if device.get("verdict") == "ok":
            result["warnings"].append(
                "Драйвер для этого устройства уже есть в ядре и работает; "
                "внешний драйвер обычно не нужен.")

        if self.llm is not None:
            ok, note = self.llm.check()
            result["llm"]["note"] = note
            if ok:
                result["llm"].update(used=True, model=self.llm.model)
                self.say("Языковая модель: " + note)
            else:
                self.say("Языковая модель недоступна: %s. Поиск продолжится "
                         "без неё" % note)
                self.llm = None
        else:
            result["llm"]["note"] = self.llm_off or "модель отключена"

        try:
            keywords = self.keywords(device)
            candidates = self.search(device, vid, pid, keywords, result)
            if not candidates:
                raise GitHubError("подходящих репозиториев не найдено")
            ranked = self.rank(device, vid, pid, keywords, candidates)
            result["results"] = self.finish(ranked, vid, pid)
        except GitHubError as exc:
            result["github"]["note"] = str(exc)
            cached = self.load_cache(vid, pid)
            if cached and not result["results"]:
                self.say("GitHub недоступен (%s); показан сохранённый результат"
                         % exc)
                cached["from_cache"] = True
                cached["github"]["note"] = str(exc)
                return cached
            self.say("Поиск остановлен: %s" % exc)
            result["warnings"].append("Поиск не завершён: %s." % exc)
            return result

        result["warnings"].append(
            "Сторонний драйвер работает в ядре с полными правами. Приложение "
            "ничего не устанавливает: перед установкой проверьте исходный код "
            "и историю репозитория. Для сборки модулей нужны заголовки "
            "работающего ядра (в Arch - пакет linux-headers для ядра linux), "
            "для DKMS - пакет dkms.")
        result["notes"] = self.notes
        if result["results"]:
            self.save_cache(vid, pid, result)
        return result

    # --- 1. ключевые слова --------------------------------------------------
    def keywords(self, device):
        names = [device.get("product_db"), device.get("vendor_db"),
                 device.get("product"), device.get("manufacturer")]
        words = tu.chip_keywords(*names)
        if self.llm is not None:
            self.say("Модель выделяет обозначения микросхем из названий")
            answer = self._ask(keywords_prompt(device), KEYWORDS_SCHEMA, num_predict=200)
            for word in (answer or {}).get("keywords", []):
                word = tu.clean_keyword(word)
                if word and word not in words:
                    words.append(word)
        self.say("Ключевые слова: %s" % (", ".join(words) or "нет"))
        return words[:4]

    # --- 2. поиск на GitHub --------------------------------------------------
    def search(self, device, vid, pid, keywords, result):
        queries = ["%s linux driver" % kw for kw in keywords[:3]]
        queries.append("%s %s in:readme" % (vid, pid))
        if not keywords and device.get("name_from_db"):
            words = [w for w in re.findall(r"[A-Za-z0-9]+", device["name_from_db"])
                     if w.lower() not in tu.STOP_WORDS][:3]
            if words:
                queries.insert(0, " ".join(words) + " linux driver")
        queries = list(dict.fromkeys(queries))[:MAX_REPO_QUERIES]
        result["queries"] = list(queries)

        candidates = {}

        def add(repo, query):
            name = repo.get("full_name")
            if not name or not repo.get("html_url"):
                return
            entry = candidates.setdefault(name, {"repo": repo, "queries": [],
                                                 "code_paths": [], "readme": None,
                                                 "evidence": [], "facts": []})
            if query not in entry["queries"]:
                entry["queries"].append(query)

        for query in queries:
            self.say("Поиск на GitHub: %s" % query)
            try:
                items = self.github.search_repositories(query, per_page=10)
            except RateLimited as exc:
                self.notes.append(str(exc))
                self.say("Лимит GitHub: %s" % exc)
                if candidates:
                    break
                raise
            for repo in items:
                add(repo, query)

        if self.github.token:
            query = "0x%s 0x%s language:C" % (vid, pid)
            self.say("Поиск таблиц устройств в коде: %s" % query)
            result["queries"].append(query + " (код)")
            try:
                for item in self.github.search_code(query, per_page=30):
                    repo = item.get("repository") or {}
                    full = repo.get("full_name")
                    if full not in candidates:
                        try:
                            found = self.github.search_repositories(
                                "repo:%s" % full, per_page=1)
                        except GitHubError:
                            found = []
                        if not found:
                            continue
                        add(found[0], query)
                    candidates[full]["code_paths"].append(item.get("path", ""))
            except GitHubError as exc:
                self.notes.append("поиск по коду: %s" % exc)
                self.say("Поиск по коду не выполнен: %s" % exc)
        self.say("Найдено репозиториев-кандидатов: %d" % len(candidates))
        return list(candidates.values())

    # --- 3-5. оценка ---------------------------------------------------------
    def heuristic(self, cand, keywords, vid, pid):
        repo = cand["repo"]
        short = repo["full_name"].split("/")[-1].lower()
        desc = (repo.get("description") or "").lower()
        score = 0.0

        def ev(ru, en):
            """Признак: по-русски - для отчёта, по-английски - для модели."""
            cand["evidence"].append(ru)
            cand["facts"].append(en)

        if cand["code_paths"]:
            score += 6
            ev("в коде есть таблица устройств с %s:%s (%s)" % (vid, pid, cand["code_paths"][0]),
               "source code has a device table with %s:%s (%s)" % (vid, pid, cand["code_paths"][0]))
        for kw in keywords:
            if kw in short or (len(short) >= 6 and short in kw):
                score += 4
                ev("название репозитория соответствует %s" % kw,
                   "repository name matches %s" % kw)
                break
        for kw in keywords:
            if kw in desc:
                score += 2
                ev("описание упоминает %s" % kw, "description mentions %s" % kw)
                break
        if tu.mentions_id(desc, vid, pid):
            score += 3
            ev("описание упоминает %s:%s" % (vid, pid), "description mentions %s:%s" % (vid, pid))
        if re.search(r"driver|kernel|module|dkms|драйвер", short + " " + desc):
            score += 2
        else:
            score -= 3
        if "linux" in short + " " + desc:
            score += 1
        score += min(4.0, math.log10(repo.get("stargazers_count", 0) + 1) * 1.5)
        if repo.get("archived"):
            score -= 3
        if repo.get("fork"):
            score -= 1
        age = years_since(repo.get("pushed_at"))
        if age is not None:
            score -= 2 if age > 6 else 1 if age > 3 else 0
        if len(cand["queries"]) > 1:
            score += 1
        return score

    def rank(self, device, vid, pid, keywords, candidates):
        for cand in candidates:
            cand["score"] = self.heuristic(cand, keywords, vid, pid)
        candidates.sort(key=lambda c: c["score"], reverse=True)
        top = candidates[:MAX_CANDIDATES]

        for cand in top:
            name = cand["repo"]["full_name"]
            self.say("Чтение README: %s" % name)
            try:
                cand["readme"] = self.github.readme(name)
            except RateLimited as exc:
                self.notes.append(str(exc))
                self.say("Лимит GitHub, README остальных кандидатов не прочитаны")
                break
            except GitHubError as exc:
                self.notes.append("README %s: %s" % (name, exc))
                continue
            text = (cand["readme"] or {}).get("text", "")
            if tu.mentions_id(text, vid, pid):
                cand["score"] += 5
                cand["evidence"].append("README упоминает %s:%s" % (vid, pid))
                cand["facts"].append("README mentions %s:%s" % (vid, pid))
            else:
                for kw in keywords:
                    if kw in text.lower():
                        cand["score"] += 2
                        cand["evidence"].append("README упоминает %s" % kw)
                        cand["facts"].append("README mentions %s" % kw)
                        break

        if self.llm is not None and top:
            self.say("Модель оценивает %d кандидатов" % len(top))
            items = []
            for i, cand in enumerate(top):
                repo = cand["repo"]
                text = (cand["readme"] or {}).get("text", "")
                items.append({
                    "id": i, "name": repo["full_name"],
                    "description": repo.get("description") or "",
                    "stars": repo.get("stargazers_count", 0),
                    "last_push": (repo.get("pushed_at") or "")[:10],
                    "archived": bool(repo.get("archived")),
                    "facts": cand["facts"], "readme": " ".join(text.split())[:700],
                })
            prompt = rank_prompt(device.get("full_name"), vid, pid, self.kernel, items)
            answer = self._ask(prompt, RANK_SCHEMA, num_predict=900)
            for item in (answer or {}).get("items", []):
                idx = item.get("id")
                if not isinstance(idx, int) or not 0 <= idx < len(top):
                    continue          # модель сослалась на несуществующего кандидата
                cand = top[idx]
                llm_score = max(0, min(10, int(item.get("score", 0))))
                reason = " ".join(str(item.get("reason", "")).split())[:300]
                cand["llm"] = {"relevant": bool(item.get("relevant")),
                               "score": llm_score, "reason": reason}
                cand["score"] += llm_score * 0.6
                if not item.get("relevant") and not any(
                        "%s:%s" % (vid, pid) in e for e in cand["evidence"]):
                    cand["score"] -= 4
        top.sort(key=lambda c: c["score"], reverse=True)
        # Отсеиваются кандидаты без единого совпадения с устройством, которых
        # модель (если она есть) тоже не признала подходящими.
        kept = [c for c in top
                if c["evidence"] or (c.get("llm") or {}).get("relevant")]
        if not kept:
            self.notes.append("прямых совпадений с устройством не найдено")
            self.say("Прямых совпадений нет, показаны лучшие по оценке кандидаты")
            kept = top[:2]
        dropped = len(top) - len(kept)
        if dropped:
            self.say("Отсеяно кандидатов без совпадений: %d" % dropped)
        return kept

    # --- 6-7. шаги установки и предупреждения ---------------------------------
    def steps(self, cand):
        readme = cand.get("readme") or {}
        text = readme.get("text", "")
        if not text:
            return [], "README не получен"
        excerpt, headings = tu.install_excerpt(text)
        steps, dropped = [], 0
        if self.llm is not None:
            self.say("Модель пересказывает шаги установки: %s"
                     % cand["repo"]["full_name"])
            answer = self._ask(steps_prompt(cand["repo"]["full_name"], excerpt),
                               STEPS_SCHEMA, num_predict=900)
            for step in (answer or {}).get("steps", []):
                cmd = tu.normalize_command(step.get("command", ""))
                if cmd and not tu.command_in_text(cmd, text):
                    dropped += 1        # команды нет в README - не показываем
                    continue
                description = " ".join(str(step.get("description", "")).split())[:300]
                if not description:
                    if not cmd:
                        continue        # пустой шаг
                    description = "Команда из README"
                steps.append({"description": description, "command": cmd})
        if not steps:
            for block in tu.install_blocks(text):
                steps.append({"description": "Команды из README", "command":
                              tu.normalize_command(block)})
        source = "%s (%s)" % (readme.get("name", "README"),
                              ", ".join(h for h in headings if h) or "весь файл")
        if dropped:
            source += "; отброшено команд, которых нет в README: %d" % dropped
        return steps, source

    def finish(self, ranked, vid, pid):
        results = []
        for i, cand in enumerate(ranked[:MAX_RESULTS]):
            repo = cand["repo"]
            warnings = []
            if repo.get("archived"):
                warnings.append("репозиторий в архиве, автор его больше не развивает")
            age = years_since(repo.get("pushed_at"))
            if age is not None and age > 2:
                warnings.append("последнее изменение %.0f г. назад: драйвер может "
                                "не собраться с ядром %s" % (age, self.kernel))
            if repo.get("stargazers_count", 0) < 5:
                warnings.append("мало звёзд: проект малоизвестен")
            if not repo.get("license"):
                warnings.append("лицензия не указана")
            if repo.get("fork"):
                warnings.append("это форк, сравните его с исходным репозиторием")
            steps, source = (self.steps(cand) if i < STEPS_FOR else ([], ""))
            readme_text = (cand.get("readme") or {}).get("text", "")
            commands = [s["command"] for s in steps] + tu.code_blocks(readme_text)
            for note in dict.fromkeys(n for c in commands for n in tu.danger_notes(c)):
                warnings.append("опасная команда в README: %s" % note)
            llm = cand.get("llm") or {}
            results.append({
                "full_name": repo["full_name"],
                "url": repo["html_url"],
                "description": repo.get("description") or "",
                "stars": repo.get("stargazers_count", 0),
                "pushed_at": (repo.get("pushed_at") or "")[:10],
                "license": (repo.get("license") or {}).get("spdx_id") or "",
                "archived": bool(repo.get("archived")),
                "score": round(cand["score"], 1),
                "evidence": cand["evidence"],
                "reason": llm.get("reason", ""),
                "llm_relevant": llm.get("relevant"),
                "readme_url": (cand.get("readme") or {}).get("url", ""),
                "steps": steps, "steps_source": source,
                "warnings": warnings,
            })
        allowed = {c["repo"]["html_url"] for c in ranked}
        if not results:
            return results
        assert all(r["url"] in allowed for r in results)
        return results

    # --- кэш для работы без сети --------------------------------------------
    def _cache_path(self, vid, pid):
        return os.path.join(cache_dir(), "%s_%s.json" % (vid, pid))

    def save_cache(self, vid, pid, result):
        try:
            os.makedirs(cache_dir(), exist_ok=True)
            with open(self._cache_path(vid, pid), "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def load_cache(self, vid, pid):
        try:
            with open(self._cache_path(vid, pid), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
