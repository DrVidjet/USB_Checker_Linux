"""Текстовое и HTML-представление результата поиска."""

import html


def esc(text):
    return html.escape(str(text)) if text else ""


def to_text(result):
    lines = ["Устройство: %s [%s], ядро %s" % (
        result["device"]["name"], result["device"]["id"], result["kernel"])]
    if result.get("from_cache"):
        lines.append("(результат из кэша от %s)" % result.get("created", ""))
    llm = result.get("llm", {})
    lines.append("Модель: %s" % (llm.get("model") if llm.get("used")
                                  else "не использовалась (%s)" % llm.get("note", "")))
    lines.append("Запросы: %s" % "; ".join(result.get("queries", [])))
    if not result["results"]:
        lines.append("Подходящие репозитории не найдены.")
    for n, r in enumerate(result["results"], 1):
        lines.append("")
        lines.append("%d. %s  (★ %d, обновлён %s%s)" % (
            n, r["url"], r["stars"], r["pushed_at"] or "?",
            ", " + r["license"] if r["license"] else ""))
        if r["description"]:
            lines.append("   " + r["description"])
        for e in r["evidence"]:
            lines.append("   + " + e)
        if r["reason"]:
            lines.append("   Оценка модели: " + r["reason"])
        if r["steps"]:
            lines.append("   Шаги установки (источник: %s):" % r["steps_source"])
            for i, s in enumerate(r["steps"], 1):
                lines.append("     %d) %s" % (i, s["description"]))
                for cmd in s["command"].splitlines():
                    lines.append("        $ " + cmd)
        for w in r["warnings"]:
            lines.append("   ! " + w)
    for w in result.get("warnings", []):
        lines.append("")
        lines.append("Внимание: " + w)
    return "\n".join(lines)


def to_html(result, mono="monospace", header=True):
    parts = []
    dev = result["device"]
    if header:
        parts.append("<h2 style='margin:0'>%s</h2><p style='color:gray; margin:2px 0 8px 0'>"
                     "%s, ядро %s</p>" % (esc(dev["name"]), esc(dev["id"]),
                                          esc(result["kernel"])))
    if result.get("from_cache"):
        parts.append("<p style='color:#8A4E05'>GitHub недоступен, показан сохранённый "
                     "результат от %s.</p>" % esc(result.get("created")))
    if not result["results"]:
        parts.append("<p>Подходящие репозитории не найдены.</p>")
    for n, r in enumerate(result["results"], 1):
        meta = "★ %d, обновлён %s" % (r["stars"], r["pushed_at"] or "?")
        if r["license"]:
            meta += ", лицензия " + r["license"]
        parts.append("<h3 style='margin:14px 0 2px 0'>%d. <a href='%s'>%s</a></h3>"
                     "<p style='color:gray; margin:0'>%s</p>"
                     % (n, esc(r["url"]), esc(r["full_name"]), esc(meta)))
        if r["description"]:
            parts.append("<p style='margin:4px 0'>%s</p>" % esc(r["description"]))
        facts = list(r["evidence"])
        if r["reason"]:
            facts.append("оценка модели: " + r["reason"])
        if facts:
            parts.append("<ul style='margin:2px 0'>%s</ul>"
                         % "".join("<li>%s</li>" % esc(f) for f in facts))
        if r["steps"]:
            parts.append("<p style='margin:6px 0 2px 0'><b>Шаги установки</b> "
                         "<span style='color:gray'>(источник: %s)</span></p><ol style='margin:0'>"
                         % esc(r["steps_source"]))
            for s in r["steps"]:
                cmd = ""
                if s["command"]:
                    cmd = ("<pre style=\"font-family:'%s'; margin:2px 0\">%s</pre>"
                           % (mono, esc(s["command"])))
                parts.append("<li>%s%s</li>" % (esc(s["description"]), cmd))
            parts.append("</ol>")
        if r["warnings"]:
            parts.append("<p style='color:#8A4E05; margin:4px 0'>%s</p>"
                         % "<br>".join("⚠ " + esc(w) for w in r["warnings"]))
    for w in result.get("warnings", []):
        parts.append("<p style='color:#8A4E05; margin-top:12px'>%s</p>" % esc(w))
    return "".join(parts)
