#!/usr/bin/env python3
"""Мониторинг заказов: биржи + Telegram-каналы -> ваша Telegram-группа.

Без ИИ и без внешних библиотек (только стандартный Python 3.9+).
Переменные окружения:
  BOT_TOKEN  - токен бота от @BotFather (секрет)
  CHAT_ID    - ID группы, куда слать заказы
  DRY_RUN=1  - ничего не отправлять, только печатать (для проверки)
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
SEEN = os.path.join(HERE, "seen.json")
KNOWN = os.path.join(HERE, "sources_seen.json")  # источники, которые уже прошли первый (тихий) запуск
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SEEN_LIMIT = 20000


# ---------- helpers ----------

def fetch(url, timeout=25, headers=None):
    h = {"User-Agent": UA, "Accept-Language": "uk,ru;q=0.9,en;q=0.8,pl;q=0.7"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")


def clean(text):
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def compile_words(words):
    # Совпадение по началу слова: "seo" найдёт "SEO-аудит", но не "museo".
    pats = [r"(?<![0-9a-zа-яёіїєґ])" + re.escape(w.lower().strip()) for w in words if w.strip()]
    return re.compile("|".join(pats), re.I) if pats else None


def item(source, title, url, text="", budget=""):
    return {"source": source, "title": clean(title)[:200], "url": url.strip(),
            "text": clean(text)[:1500], "budget": clean(budget)[:80]}


# ---------- sources ----------

def src_rss(s):
    xml = fetch(s["url"])
    root = ET.fromstring(xml.encode("utf-8"))
    out = []
    ns_atom = "{http://www.w3.org/2005/Atom}"
    for it in root.iter("item"):
        out.append(item(s["name"], it.findtext("title", ""), it.findtext("link", ""), it.findtext("description", "")))
    for e in root.iter(ns_atom + "entry"):
        link = e.find(ns_atom + "link")
        href = link.get("href") if link is not None else ""
        body = e.findtext(ns_atom + "content", "") or e.findtext(ns_atom + "summary", "")
        out.append(item(s["name"], e.findtext(ns_atom + "title", ""), href, body))
    return out


def src_html(s):
    page = fetch(s["url"])
    rx = re.compile(r'<a[^>]+href="((?:https?://[^"]*)?' + s["link"] + r')"[^>]*>(.*?)</a>', re.S | re.I)
    out, seen = [], set()
    for href, inner in rx.findall(page):
        url = urllib.parse.urljoin(s["base"], html.unescape(href))
        title = clean(inner)
        if url in seen or len(title) < 8:
            continue
        seen.add(url)
        out.append(item(s["name"], title, url))
    return out


def src_freelancer(s):
    out = []
    for q in s["queries"]:
        u = ("https://www.freelancer.com/api/projects/0.1/projects/active/?compact=true&limit=30"
             "&full_description=true&sort_field=time_updated&query=" + urllib.parse.quote(q))
        data = json.loads(fetch(u))
        for p in data.get("result", {}).get("projects", []):
            b = p.get("budget") or {}
            cur = (p.get("currency") or {}).get("code", "")
            budget = f"{b.get('minimum', '')}–{b.get('maximum', '')} {cur}".strip("– ")
            out.append(item(s["name"], p.get("title", ""), "https://www.freelancer.com/projects/" + p.get("seo_url", str(p.get("id"))),
                            p.get("description") or p.get("preview_description", ""), budget))
        time.sleep(1)
    return out


def src_remotive(s):
    data = json.loads(fetch(s["url"]))
    return [item(s["name"], j.get("title", "") + " · " + j.get("company_name", ""), j.get("url", ""),
                 j.get("description", ""), j.get("salary", "")) for j in data.get("jobs", [])]


def src_remoteok(s):
    data = json.loads(fetch(s["url"]))
    out = []
    for j in data:
        if not isinstance(j, dict) or not j.get("position"):
            continue
        sal = f"${j['salary_min']}–{j['salary_max']}" if j.get("salary_min") else ""
        out.append(item(s["name"], j["position"] + " · " + j.get("company", ""), j.get("url", ""), j.get("description", ""), sal))
    return out


def src_himalayas(s):
    data = json.loads(fetch(s["url"]))
    out = []
    for j in data.get("jobs", []):
        sal = f"{j.get('minSalary') or ''}–{j.get('maxSalary') or ''} {j.get('currency') or ''}".strip("– ")
        out.append(item(s["name"], j.get("title", "") + " · " + j.get("companyName", ""),
                        j.get("applicationLink") or j.get("guid", ""), j.get("excerpt") or j.get("description", ""), sal))
    return out


def src_telegram(channel):
    page = fetch(f"https://t.me/s/{channel}")
    out = []
    for block in re.split(r'<div class="tgme_widget_message_wrap', page)[1:]:
        m = re.search(r'data-post="([^"]+)"', block)
        t = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        if not m or not t:
            continue
        text = clean(t.group(1))
        first = text.split("\n", 1)[0]
        out.append(item("TG @" + channel, first[:120], "https://t.me/" + m.group(1), text))
    return out


JSON_TYPES = {"freelancer": src_freelancer, "remotive": src_remotive, "remoteok": src_remoteok, "himalayas": src_himalayas}


def collect(cfg):
    jobs = []  # (label, func, arg, filter, require)
    S = cfg["sources"]
    for s in S.get("rss", []):
        jobs.append((s["name"], src_rss, s, s.get("filter", True), s.get("require")))
    for s in S.get("html", []):
        jobs.append((s["name"], src_html, s, s.get("filter", True), s.get("require")))
    for s in S.get("json", []):
        jobs.append((s["name"], JSON_TYPES[s["type"]], s, s.get("filter", True), s.get("require")))
    for ch in S.get("telegram", []):
        jobs.append(("TG @" + ch, src_telegram, ch, True, None))

    def run(job):
        label, fn, arg, flt, req = job
        try:
            return job, fn(arg), None
        except Exception as e:  # один сломанный источник не мешает остальным
            return job, [], f"{type(e).__name__}: {e}"

    results, errors = [], []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for job, items, err in ex.map(run, jobs):
            label = job[0]
            if err:
                errors.append(f"{label}: {err[:150]}")
                print(f"[ERR] {label}: {err[:200]}", file=sys.stderr)
            else:
                print(f"[ok]  {label}: {len(items)}", file=sys.stderr)
            for it in items:
                if it["url"]:
                    results.append((it, job[3], job[4]))
    return results, errors


# ---------- filter & send ----------

def matches(it, use_filter, require, kw, minus):
    hay = (it["title"] + "\n" + it["text"]).lower()
    if minus and minus.search(hay):
        return None
    if require and not any(r.lower() in hay for r in require):
        return None
    if not use_filter:
        return "категория"
    m = kw.findall(hay) if kw else []
    if not m:
        return None
    uniq = []
    for w in m:
        w = w.strip().lower()
        if w not in uniq:
            uniq.append(w)
    return ", ".join(uniq[:5])


def fmt(it, why):
    snippet = it["text"]
    if snippet.startswith(it["title"]):
        snippet = snippet[len(it["title"]):].strip()
    snippet = snippet[:400] + ("…" if len(snippet) > 400 else "")
    lines = [f"<b>{html.escape(it['title'])}</b>"]
    if it["budget"]:
        lines.append(f"💰 {html.escape(it['budget'])}")
    if snippet:
        lines.append(html.escape(snippet))
    lines.append(f"📍 {html.escape(it['source'])} · 🔎 {html.escape(why)}")
    lines.append(f'<a href="{html.escape(it["url"], quote=True)}">Открыть заказ</a>')
    return "\n".join(lines)


def send(token, chat_id, text):
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 429:
                wait = json.loads(body).get("parameters", {}).get("retry_after", 5)
                time.sleep(wait + 1)
                continue
            raise RuntimeError(f"Telegram {e.code}: {body}")
    raise RuntimeError("Telegram: too many retries")


def main():
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    token, chat_id = os.environ.get("BOT_TOKEN", ""), os.environ.get("CHAT_ID", "")
    dry = os.environ.get("DRY_RUN") == "1"
    if not dry and (not token or not chat_id):
        sys.exit("ОШИБКА: не заданы секреты BOT_TOKEN и/или CHAT_ID. Ничего не отправлено.")
    kw, minus = compile_words(cfg["keywords"]), compile_words(cfg.get("minus_words", []))

    first_run = not os.path.exists(SEEN)
    seen = [] if first_run else json.load(open(SEEN, encoding="utf-8"))
    seen_set = set(seen)

    results, errors = collect(cfg)
    # Новый источник в первый раз только запоминается, чтобы не прислать сразу всю его ленту.
    known = set(json.load(open(KNOWN, encoding="utf-8"))) if os.path.exists(KNOWN) else None
    answered = {it["source"] for it, _, _ in results}
    new = []
    for it, flt, req in results:
        key = it["url"]
        if key in seen_set:
            continue
        seen_set.add(key)
        seen.append(key)
        if known is not None and it["source"] not in known:
            continue
        why = matches(it, flt, req, kw, minus)
        if why:
            new.append((it, why))
    known = (known or set()) | answered

    print(f"Всего записей: {len(results)}, новых подходящих: {len(new)}, ошибок источников: {len(errors)}", file=sys.stderr)

    if first_run:
        # Первый запуск: запоминаем всё, что уже есть, чтобы не завалить группу старыми заказами.
        msg = (f"✅ Бот запущен. Источников с ответом: {len(set(r[0]['source'] for r in results))}, "
               f"с ошибкой: {len(errors)}. Старые заказы пропущены, дальше буду присылать только новые.")
        print(msg)
        if not dry:
            send(token, chat_id, msg)
    else:
        limit = int(cfg.get("max_messages_per_run", 25))
        for it, why in new[:limit]:
            text = fmt(it, why)
            if dry:
                print("-----\n" + text)
            else:
                send(token, chat_id, text)
                time.sleep(3.1)  # лимит Telegram для групп: ~20 сообщений в минуту
        if len(new) > limit and not dry:
            send(token, chat_id, f"…и ещё {len(new) - limit} заказов в этой проверке не поместились.")

    json.dump(seen[-SEEN_LIMIT:], open(SEEN, "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(sorted(known), open(KNOWN, "w", encoding="utf-8"), ensure_ascii=False, indent=0)


if __name__ == "__main__":
    main()
