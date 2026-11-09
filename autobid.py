#!/usr/bin/env python3
"""Автоотклики на Freelancehunt 2 раза в день (утро и вечер по Киеву).

Как работает:
  1. Берёт новые проекты из RSS Freelancehunt (тот же источник, что и монитор).
  2. Оставляет только проекты по вашим темам (ключевые слова из autobid.json, минус-слова).
  3. Для каждого подбирает шаблон отклика по теме и языку проекта (укр / рус / англ).
  4. Отправляет ставку через API Freelancehunt (нужен секрет FH_TOKEN) и пишет итог в Telegram.

Без ИИ и внешних библиотек. Переменные окружения:
  FH_TOKEN   - API-ключ Freelancehunt (https://freelancehunt.com/my/api2), секрет
  BOT_TOKEN  - токен Telegram-бота, CHAT_ID - ID группы (как у монитора)
  FORCE=1    - запустить вне окна расписания (ручной запуск)
  DRY_RUN=1  - ничего не отправлять, только печатать
Если FH_TOKEN не задан или в autobid.json "mode": "draft", ставки не отправляются,
а готовые тексты приходят в Telegram, чтобы вставить их вручную.
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from bot import clean, compile_words, send

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "autobid.json")
STATE = os.path.join(HERE, "autobid_state.json")
API = "https://api.freelancehunt.com/v2"
UA = "orders-bot/1.0 (+https://github.com/pavelgeniy/orders-bot)"
KYIV = ZoneInfo("Europe/Kyiv")


# ---------- state ----------

def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE, encoding="utf-8"))
    return {"done_slots": [], "bid_projects": [], "log": []}


def save_state(st):
    st["done_slots"] = st["done_slots"][-60:]
    st["bid_projects"] = st["bid_projects"][-3000:]
    st["log"] = st["log"][-300:]
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def current_slot(cfg, now):
    """Окно 'утро' или 'вечер', в которое попадает текущее время (по Киеву)."""
    for name, (start, end) in cfg["slots"].items():
        if start <= now.hour < end:
            return f"{now:%Y-%m-%d}-{name}"
    return None


# ---------- Freelancehunt ----------

def fh_request(method, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json",
        "Content-Type": "application/json", "Accept-Language": "uk", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:300]}


def api_error_text(payload):
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        parts = [err.get("title") or err.get("detail") or ""]
        for f in err.get("fields") or []:
            if isinstance(f, dict):
                parts.append(f"{f.get('field', '')}: {f.get('message', '')}")
        return "; ".join(p for p in parts if p)[:300]
    return json.dumps(payload, ensure_ascii=False)[:300]


def rss_projects():
    req = urllib.request.Request("https://freelancehunt.com/projects.rss", headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=30) as r:
        root = ET.fromstring(r.read())
    out = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        m = re.search(r"/(\d+)\.html", link)
        if not m:
            continue
        try:
            published = parsedate_to_datetime(it.findtext("pubDate") or "")
        except (TypeError, ValueError):
            published = None
        out.append({"id": int(m.group(1)), "title": clean(it.findtext("title", "")),
                    "text": clean(it.findtext("description", "")), "url": link.split("?")[0],
                    "published": published, "budget": None, "currency": None, "bid_count": None})
    return out


def enrich(p, token):
    """Бюджет, валюта и число ставок из API (если API ответил)."""
    code, data = fh_request("GET", f"/projects/{p['id']}", token)
    if code != 200:
        return
    a = (data.get("data") or {}).get("attributes") or {}
    if a.get("description"):
        p["text"] = clean(a["description"])[:3000]
    b = a.get("budget") or {}
    if b.get("amount"):
        p["budget"], p["currency"] = float(b["amount"]), b.get("currency")
    p["bid_count"] = a.get("bid_count")
    st = a.get("status") or {}
    p["status"] = st.get("name") or st.get("id")


# ---------- text ----------

def detect_lang(text):
    if re.search(r"[іїєґІЇЄҐ]", text):
        return "uk"
    if re.search(r"[а-яёА-ЯЁ]", text):
        return "ru"
    return "en"


def pick_topic(cfg, hay):
    best, best_hits = None, 0
    for name, t in cfg["topics"].items():
        rx = compile_words(t["keywords"])
        hits = len(set(m.lower() for m in rx.findall(hay))) if rx else 0
        if hits > best_hits:
            best, best_hits = name, hits
    return best


def price_for(cfg, topic, p):
    t = cfg["topics"][topic]
    floor = cfg["min_price_uah"]
    if p["budget"] and (p["currency"] or "UAH") == "UAH":
        amount = max(floor, int(p["budget"] * cfg["budget_share"] // 50 * 50))
        return min(amount, int(p["budget"])), "UAH"
    if p["budget"] and p["currency"]:
        return max(1, int(p["budget"] * cfg["budget_share"])), p["currency"]
    return t["price_uah"], "UAH"


def build_comment(cfg, topic, lang, p):
    t = cfg["topics"][topic]
    tpl = t["text"].get(lang) or t["text"]["uk"]
    about = cfg["about"].get(lang) or cfg["about"]["uk"]
    hello = {"uk": "Добрий день!", "ru": "Здравствуйте!", "en": "Hello!"}[lang]
    return f"{hello} {tpl.strip()}\n\n{about.strip()}"


# ---------- main ----------

def main():
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    st = load_state()
    now = dt.datetime.now(KYIV)
    force = os.environ.get("FORCE") == "1"
    slot = current_slot(cfg, now)
    if not force and (slot is None or slot in st["done_slots"]):
        print(f"Не время отклика ({now:%H:%M} Киев) или слот {slot} уже отработан", file=sys.stderr)
        return
    slot = slot or f"{now:%Y-%m-%d-%H%M}-manual"

    fh_token = os.environ.get("FH_TOKEN", "")
    tg_token, chat_id = os.environ.get("BOT_TOKEN", ""), os.environ.get("CHAT_ID", "")
    dry = os.environ.get("DRY_RUN") == "1"
    auto = cfg.get("mode", "auto") == "auto" and bool(fh_token) and not dry

    def tg(text):
        if dry or not tg_token or not chat_id:
            print("----- TG\n" + text)
        else:
            send(tg_token, chat_id, text)
            time.sleep(3.1)

    kw, minus = compile_words(cfg["keywords"]), compile_words(cfg["minus_words"])
    max_age = dt.timedelta(hours=cfg["max_age_hours"])
    done = set(st["bid_projects"])

    projects = rss_projects()
    print(f"RSS: {len(projects)} проектов", file=sys.stderr)
    picked = []
    for p in projects:
        if p["id"] in done:
            continue
        if p["published"] and now - p["published"] > max_age:
            continue
        hay = (p["title"] + "\n" + p["text"]).lower()
        if minus.search(hay) or not kw.search(hay):
            continue
        if fh_token:
            enrich(p, fh_token)
            if p.get("bid_count") and p["bid_count"] > cfg["skip_if_bids_over"]:
                continue
            hay = (p["title"] + "\n" + p["text"]).lower()
            if minus.search(hay):
                continue
        topic = pick_topic(cfg, hay)
        if topic:
            picked.append((p, topic))
        if len(picked) >= cfg["max_bids_per_run"]:
            break

    sent, failed, drafts = [], [], []
    for p, topic in picked:
        lang = detect_lang(p["title"] + " " + p["text"])
        amount, cur = price_for(cfg, topic, p)
        days = cfg["topics"][topic]["days"]
        comment = build_comment(cfg, topic, lang, p)
        if not auto:
            drafts.append((p, topic, amount, cur, days, comment))
            continue
        body = {"days": days, "safe_type": cfg["safe_type"], "budget": {"amount": amount, "currency": cur},
                "comment": comment, "is_hidden": cfg.get("hidden_bids", False)}
        code, resp = fh_request("POST", f"/projects/{p['id']}/bids", fh_token, body)
        if code in (200, 201):
            sent.append((p, topic, amount, cur, days))
            st["bid_projects"].append(p["id"])
        else:
            err = api_error_text(resp)
            failed.append((p, code, err))
            if code in (400, 409, 422) and re.search(r"already|уже|вже", err, re.I):
                st["bid_projects"].append(p["id"])
            if code in (401, 403, 429):
                break  # ключ не работает / лимит: дальше нет смысла
        st["log"].append({"t": now.isoformat(timespec="minutes"), "id": p["id"], "code": code,
                          "amount": amount, "cur": cur, "topic": topic})
        time.sleep(cfg.get("pause_seconds", 20))

    # ---- отчёт в Telegram ----
    label = "🌅 Утренние" if "morning" in slot else ("🌙 Вечерние" if "evening" in slot else "🖐 Ручные")
    if auto:
        lines = [f"<b>{label} автоотклики Freelancehunt</b>",
                 f"Подходящих проектов: {len(picked)}, отправлено: {len(sent)}, ошибок: {len(failed)}"]
        for p, topic, amount, cur, days in sent:
            lines.append(f'✅ <a href="{html.escape(p["url"])}">{html.escape(p["title"][:90])}</a> · {amount} {cur} · {days} дн · {topic}')
        for p, code, err in failed:
            lines.append(f'❌ <a href="{html.escape(p["url"])}">{html.escape(p["title"][:90])}</a> · {code}: {html.escape(err)}')
        tg("\n".join(lines))
    else:
        why = "нет секрета FH_TOKEN" if not fh_token else "режим draft"
        tg(f"<b>{label} отклики Freelancehunt (черновики, {why})</b>\n"
           f"Подходящих проектов: {len(drafts)}. Тексты ниже, их можно вставить вручную.")
        for p, topic, amount, cur, days, comment in drafts:
            tg(f'<a href="{html.escape(p["url"])}">{html.escape(p["title"][:120])}</a>\n'
               f"💰 {amount} {cur} · {days} дн · {topic}\n\n{html.escape(comment)[:3500]}")
            st["bid_projects"].append(p["id"])  # не присылать тот же черновик повторно

    st["done_slots"].append(slot)
    save_state(st)
    print(f"Готово: picked={len(picked)} sent={len(sent)} failed={len(failed)} drafts={len(drafts)}", file=sys.stderr)


if __name__ == "__main__":
    main()
