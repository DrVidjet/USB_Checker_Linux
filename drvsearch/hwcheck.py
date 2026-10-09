"""Проверка, хватит ли компьютеру сил на языковую модель.

Модель работает на процессоре и целиком лежит в оперативной памяти.
Нужная память оценивается так:
    веса модели (размер файла модели)
  + кэш внимания (KV-кэш) на NUM_CTX токенов:
        2 (K и V) x слои x головы KV x размер головы x 2 байта
    (2 байта - тип кэша f16, он у Ollama по умолчанию: docs/faq.mdx,
    OLLAMA_KV_CACHE_TYPE);
  + 512 МБ на буферы вычислений и саму службу Ollama.
Параметры слоёв берутся из ответа /api/show (поле model_info), а для
моделей, которые ещё не скачаны, - из таблицы KNOWN (файлы config.json
моделей на Hugging Face, размеры - со страниц моделей на ollama.com).

Компьютер считается слишком слабым, если
  * всей памяти (MemTotal) меньше, чем нужно модели, плюс 1,5 ГБ на систему
    и рабочий стол;
  * логических процессоров меньше двух;
  * процессор x86_64 без набора команд AVX: Ollama тогда берёт самую
    медленную библиотеку «cpu» (docs/troubleshooting.mdx), и ответа
    пришлось бы ждать слишком долго.
Перед каждым поиском дополнительно проверяется свободная память
(MemAvailable): если её сейчас не хватает, этот поиск идёт без модели.

Проверку можно отключить переменной SUPERAPP_IGNORE_HW=1 (например, если
модель считается на видеокарте с собственной памятью).
"""

import os
import platform

NUM_CTX = 8192                    # контекст, с которым программа вызывает модель
KV_BYTES = 2                      # f16
OVERHEAD = 512 << 20              # буферы вычислений и служба Ollama
OS_RESERVE = 1536 << 20           # система, рабочий стол и окна программы
MIN_CPUS = 2

# модель: (размер файла, слоёв, голов KV, размер головы)
KNOWN = {
    "qwen2.5:1.5b": (986_000_000, 28, 2, 128),       # модель по умолчанию
    "qwen2.5:3b": (1_900_000_000, 36, 2, 128),
    "qwen3:1.7b": (1_400_000_000, 28, 8, 128),
    "qwen3:4b": (2_500_000_000, 36, 8, 128),
}


def proc_root():
    return os.environ.get("SUPERAPP_PROC_ROOT", "/proc")


def ignored():
    return os.environ.get("SUPERAPP_IGNORE_HW") == "1"


def gb(size):
    """Размер в ГБ с одним знаком после запятой: 3,9 ГБ."""
    return ("%.1f ГБ" % (size / (1 << 30))).replace(".", ",")


def meminfo():
    """MemTotal и MemAvailable из /proc/meminfo в байтах."""
    result = {}
    try:
        with open(os.path.join(proc_root(), "meminfo"), encoding="ascii") as f:
            for line in f:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    result[key] = int(parts[0]) * (1024 if parts[1:] == ["kB"] else 1)
    except OSError:
        pass
    return result


def cpu_flags():
    try:
        with open(os.path.join(proc_root(), "cpuinfo"), encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                key, _, value = line.partition(":")
                if key.strip() in ("flags", "Features"):
                    return set(value.split())
    except OSError:
        pass
    return set()


def cpu_model():
    try:
        with open(os.path.join(proc_root(), "cpuinfo"), encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                key, _, value = line.partition(":")
                if key.strip() == "model name":
                    return value.strip()
    except OSError:
        pass
    return ""


def cpu_count():
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def kv_cache(layers, kv_heads, key_len, value_len, ctx=NUM_CTX):
    """Размер KV-кэша; kv_heads может быть списком (своё число на каждый слой)."""
    if isinstance(kv_heads, list):
        heads = sum(int(h) for h in kv_heads)
    else:
        heads = int(kv_heads) * int(layers)
    return ctx * heads * (int(key_len) + int(value_len)) * KV_BYTES


def from_model_info(info, size):
    """Нужная память по полю model_info из /api/show и размеру модели."""
    arch = info.get("general.architecture")
    if not arch or not size:
        return None
    get = lambda key: info.get("%s.%s" % (arch, key))      # noqa: E731
    layers = get("block_count")
    kv_heads = get("attention.head_count_kv") or get("attention.head_count")
    key_len = get("attention.key_length")
    if not key_len and get("embedding_length") and get("attention.head_count"):
        key_len = int(get("embedding_length")) // int(get("attention.head_count"))
    value_len = get("attention.value_length") or key_len
    if not (layers and kv_heads and key_len):
        return int(size) + OVERHEAD
    return int(size) + kv_cache(layers, kv_heads, key_len, value_len) + OVERHEAD


def estimate(model, model_info=None, size=None):
    """Сколько памяти нужно модели, байт; None - неизвестно."""
    if model_info and size:
        need = from_model_info(model_info, size)
        if need:
            return need
    name = model if ":" in model else model + ":latest"
    if name in KNOWN:
        file_size, layers, kv_heads, head = KNOWN[name]
        return (size or file_size) + kv_cache(layers, kv_heads, head, head) + OVERHEAD
    return int(size) + OVERHEAD if size else None


class Verdict:
    def __init__(self, model):
        self.model = model
        self.ok = True
        self.reasons = []         # почему модель запускать нельзя
        self.warnings = []        # можно, но будет медленно
        self.need = None
        self.total = None
        self.available = None
        self.cpus = None
        self.smaller = None       # модель поменьше, на которую памяти хватит

    def fail(self, text):
        self.ok = False
        self.reasons.append(text)

    def text(self):
        if self.ok:
            return "; ".join(self.warnings)
        text = "компьютер слишком слабый для модели %s: %s" % (
            self.model, "; ".join(self.reasons))
        if self.smaller:
            text += "; памяти хватит на модель %s (параметр --model %s)" % (
                self.smaller, self.smaller)
        return text

    def summary(self):
        """Строки для окна «Сведения о модели»."""
        lines = ["Процессор: %s, логических процессоров: %s"
                 % (cpu_model() or platform.machine(), self.cpus)]
        if self.total:
            lines.append("Память: всего %s, свободно %s" % (gb(self.total),
                                                           gb(self.available or 0)))
        if self.need:
            lines.append("Модели %s нужно около %s свободной памяти, всего ОЗУ "
                         "не меньше %s" % (self.model, gb(self.need),
                                           gb(self.need + OS_RESERVE)))
        for text in self.reasons + self.warnings:
            lines.append("  - " + text)
        return lines


def assess(model, model_info=None, size=None, check_available=False, loaded=False):
    """Оценивает, потянет ли компьютер модель.

    check_available - сравнить ещё и со свободной сейчас памятью (перед
    поиском); loaded - модель уже в памяти Ollama, свободная память для неё
    не нужна.
    """
    v = Verdict(model)
    mem = meminfo()
    v.total, v.available = mem.get("MemTotal"), mem.get("MemAvailable")
    v.cpus = cpu_count()
    v.need = estimate(model, model_info, size)
    if ignored():
        v.warnings.append("проверка мощности отключена (SUPERAPP_IGNORE_HW=1)")
        return v
    if v.cpus < MIN_CPUS:
        v.fail("логических процессоров %d, нужно не меньше %d" % (v.cpus, MIN_CPUS))
    machine = platform.machine().lower()
    flags = cpu_flags()
    if machine in ("x86_64", "amd64") and flags:
        if "avx" not in flags:
            v.fail("процессор без набора команд AVX, модель считалась бы очень медленно")
        elif "avx2" not in flags:
            v.warnings.append("процессор без AVX2: ответы модели будут медленнее")
    if v.need and v.total:
        if v.total < v.need + OS_RESERVE:
            v.fail("всего %s ОЗУ, а нужно не меньше %s (модель %s и %s на систему)"
                   % (gb(v.total), gb(v.need + OS_RESERVE), gb(v.need), gb(OS_RESERVE)))
            if len(v.reasons) == 1:           # дело только в памяти
                fits = [m for m in KNOWN if estimate(m) + OS_RESERVE <= v.total
                        and estimate(m) < v.need]
                v.smaller = max(fits, key=estimate) if fits else None
        elif check_available and not loaded and v.available is not None \
                and v.available < v.need:
            v.fail("сейчас свободно %s ОЗУ, а модели нужно около %s; закройте "
                   "лишние программы и повторите поиск" % (gb(v.available), gb(v.need)))
    return v
