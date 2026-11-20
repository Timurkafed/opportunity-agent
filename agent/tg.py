"""Telegram Bot API: send digest items with 👍/👎 buttons, read button presses with getUpdates."""
import html
import json
import re
import time
import urllib.error
from datetime import date

from .net import fetch

KIND_ICON = {"hackathon": "💻", "meetup": "☕", "startup": "🚀", "case": "📊", "forum": "🎤", "project": "✨",
             "volunteer": "🙋", "social": "🌱", "contest": "🏁", "internship": "🧑‍💼", "freebie": "🎁", "other": "📌"}
FLAG_TEXT = {"needs_project": "💡 Нужен свой проект", "women_only": "👩 Только для девушек",
             "school_only": "🎓 Только школьники", "rf_only": "🇷🇺 Только граждане РФ",
             "age_limit": "🔞 Есть ограничение по возрасту", "sales_hr": "📎 Тема: продажи/HR", "paid": "💳 Участие платное"}
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


class Bot:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, **params):
        for attempt in range(2):
            try:
                r = json.loads(fetch(self.base + method, data=params, timeout=40, retries=1))
                break
            except urllib.error.HTTPError as e:
                body = _body(e)
                if e.code == 429 and attempt == 0:  # flood limit: wait as Telegram asks
                    m = re.search(r'"retry_after":\s*(\d+)', body)
                    time.sleep(min(int(m.group(1)) if m else 5, 60) + 1)
                    continue
                raise TelegramError(e.code, f"{method}: {e.code} {body[:200]}") from None
        if not r.get("ok"):
            raise TelegramError(None, f"{method}: {r}")
        time.sleep(0.4)  # stay far below Telegram flood limits
        return r["result"]

    def send(self, chat_id, text, buttons=None, silent=False):
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True, "disable_notification": silent}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        try:
            return self.call("sendMessage", **params)
        except TelegramError as e:
            if e.code != 400 or "parse" not in str(e).lower():
                raise
            # broken HTML in a title or link: send the same text without formatting
            params.pop("parse_mode")
            params["text"] = html.unescape(re.sub(r"<[^>]+>", "", text))
            return self.call("sendMessage", **params)

    def updates(self, offset):
        return self.call("getUpdates", offset=offset, timeout=0, allowed_updates=["message", "callback_query"])


class TelegramError(RuntimeError):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _body(e):
    try:
        return e.read().decode("utf-8", "replace")
    except Exception:
        return ""


def h(s):
    return html.escape(s or "", quote=False)


def attr(s):
    """Value inside href="...": quotes must be escaped too."""
    return html.escape(s or "", quote=True)


def nice_date(s):
    try:
        d = date.fromisoformat(s[:10])
        return f"{d.day} {MONTHS[d.month - 1]}"
    except Exception:
        return s or ""


def when_text(e):
    a, b = nice_date(e.get("start_date", "")), nice_date(e.get("end_date", ""))
    if a and b and a != b:
        return f"{a} – {b}"
    return a or b


def item_text(e):
    lines = [f"{KIND_ICON.get(e['kind'], '📌')} <b>{h(e['title'])}</b>", h(e["summary"])]
    place = e.get("city") or ""
    if e.get("online") and "онлайн" not in place.lower():
        place = (place + ", можно онлайн") if place else "Онлайн"
    meta = [x for x in [f"📍 {h(place)}" if place else "", f"📅 {when_text(e)}" if when_text(e) else "",
                        f"⏰ Дедлайн: {nice_date(e['deadline'])}" if e.get("deadline") else ""] if x]
    if meta:
        lines.append(" · ".join(meta))
    if e.get("who"):
        lines.append(f"👥 Кому: {h(e['who'])}")
    if e.get("prize"):
        lines.append(f"🏆 {h(e['prize'])}")
    lines += [FLAG_TEXT[f] for f in e.get("flags", []) if f in FLAG_TEXT]
    links = []
    if e.get("link"):
        links.append(f'<a href="{attr(e["link"])}">Регистрация / подробности</a>')
    links.append(f'<a href="{attr(e["post_url"])}">Источник: {h(e["source"])}</a>')
    if len(e.get("sources", [])) > 1:
        links.append(f"(ещё в {len(e['sources']) - 1} ист.)")
    lines.append(" · ".join(links))
    return "\n".join(lines)


def vote_buttons(eid):
    return [[{"text": "👍", "callback_data": f"up:{eid}"}, {"text": "👎", "callback_data": f"down:{eid}"}]]


def voted_buttons(vote):
    label = "✅ Учтено: 👍" if vote == "up" else "✅ Учтено: 👎"
    return [[{"text": label, "callback_data": "noop"}]]
