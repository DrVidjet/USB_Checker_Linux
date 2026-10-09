"""Детерминированная обработка текста: ключевые слова, README, команды."""

import re

# Слова, которые не помогают искать драйвер конкретной микросхемы
STOP_WORDS = {
    "usb", "linux", "driver", "drivers", "device", "devices", "adapter",
    "wireless", "network", "networking", "controller", "interface", "corp",
    "inc", "ltd", "co", "technology", "technologies", "semiconductor",
    "electronics", "generic", "hub", "port", "card", "reader", "high", "speed",
    "nic", "lan", "wlan", "wifi", "bluetooth", "mouse", "keyboard", "the",
    "and", "for", "with", "mode", "dongle", "receiver", "unknown",
}

# Обозначения микросхем: буквы и цифры вместе (RTL8188EUS, MT7921U, AX88179,
# CH340, CP2102) или цифры с буквенным суффиксом (8812au).
CHIP_RE = re.compile(r"\b([A-Za-z]{1,6}[-_]?\d{3,5}[A-Za-z0-9]{0,8}|\d{4}[A-Za-z]{2,3})\b")

INSTALL_HEADING_RE = re.compile(
    r"install|build|compil|setup|usage|dkms|getting started|how to|"
    r"установ|сборк|компил|использ", re.IGNORECASE)
CODE_HINT_RE = re.compile(r"\b(make|dkms|modprobe|insmod|install|cmake|git clone|"
                          r"pacman|apt|apt-get|dnf|zypper|yay)\b")

DANGEROUS = [
    (re.compile(r"(curl|wget)[^|\n]*\|\s*(sudo\s+)?(ba|z|da)?sh\b"),
     "скрипт из интернета передаётся сразу в оболочку"),
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f?\s+/(\s|$)"), "удаление корневого каталога"),
    (re.compile(r"\bdd\s+if="), "прямая запись на диск (dd)"),
    (re.compile(r"\bmkfs"), "форматирование диска"),
    (re.compile(r"chmod\s+(-R\s+)?777"), "открытие доступа ко всем файлам"),
]


def chip_keywords(*texts):
    """Обозначения микросхем из названий устройства, в нижнем регистре."""
    found = []
    for text in texts:
        for match in CHIP_RE.findall(text or ""):
            word = match.lower().replace("_", "-")
            if word not in STOP_WORDS and not word.isdigit() and word not in found:
                found.append(word)
    return found


def clean_keyword(word):
    word = (word or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,30}", word):
        return None
    if word in STOP_WORDS or word.isdigit():
        return None
    return word


def id_patterns(vid, pid):
    """Регулярные выражения для упоминаний пары VID:PID в тексте и коде."""
    v, p = vid.lower(), pid.lower()
    return [
        re.compile(r"\b%s\s*[:_ -]\s*%s\b" % (v, p), re.IGNORECASE),
        re.compile(r"0x%s\s*,\s*0x%s\b" % (v, p), re.IGNORECASE),
    ]


def mentions_id(text, vid, pid):
    return any(rx.search(text or "") for rx in id_patterns(vid, pid))


def sections(markdown):
    """Разбивает Markdown на разделы: [(заголовок, текст)]."""
    result, title, buf = [], "", []
    in_code = False
    for line in markdown.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        if not in_code and re.match(r"#{1,6}\s", line):
            result.append((title, "\n".join(buf)))
            title, buf = line.lstrip("#").strip(), []
        else:
            buf.append(line)
    result.append((title, "\n".join(buf)))
    return result


def code_blocks(markdown):
    return [m.group(1).strip("\n")
            for m in re.finditer(r"```[^\n]*\n(.*?)```", markdown, re.DOTALL)]


def install_excerpt(markdown, limit=5000):
    """Разделы README об установке и сборке; если их нет - начало файла."""
    parts = [(t, body) for t, body in sections(markdown)
             if INSTALL_HEADING_RE.search(t)]
    if not parts:
        parts = [(t, body) for t, body in sections(markdown)
                 if any(CODE_HINT_RE.search(b) for b in code_blocks(body))]
    text = "\n\n".join("## %s\n%s" % (t, body.strip()) for t, body in parts)
    return (text or markdown)[:limit], [t for t, _ in parts]


def install_blocks(markdown, max_blocks=6):
    """Блоки кода из разделов об установке (без участия модели)."""
    excerpt, _ = install_excerpt(markdown, limit=20000)
    blocks = [b for b in code_blocks(excerpt) if CODE_HINT_RE.search(b)]
    return blocks[:max_blocks]


def normalize_command(cmd):
    lines = []
    for line in (cmd or "").strip().splitlines():
        line = re.sub(r"^\s*[$#]\s+", "", line)
        lines.append(" ".join(line.split()))
    return "\n".join(l for l in lines if l)


def command_in_text(cmd, text):
    """Есть ли каждая строка команды дословно в тексте README."""
    norm_text = " ".join((text or "").split())
    lines = normalize_command(cmd).splitlines()
    return bool(lines) and all(line in norm_text for line in lines)


def danger_notes(cmd):
    return [note for rx, note in DANGEROUS if rx.search(cmd or "")]
