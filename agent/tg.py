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


# ---------------------------------------------------------------- one-message digest
MONTHS_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
FLAG_SHORT = {"needs_project": "💡 нужен свой проект", "age_limit": "🔞 есть ограничение по возрасту",
              "sales_hr": "📎 продажи/HR", "paid": "💳 платно", "women_only": "👩 только для девушек"}
LIMIT = 3900  # Telegram allows 4096 characters per message


def short_date(s):
    try:
        d = date.fromisoformat(s[:10])
        return f"{d.day} {MONTHS_SHORT[d.month - 1]}"
    except Exception:
        return ""


def short_when(e):
    a, b = short_date(e.get("start_date", "")), short_date(e.get("end_date", ""))
    return f"{a}–{b}" if a and b and a != b else (a or b)


def place(e):
    p = (e.get("city") or "").strip()
    if e.get("online") and "онлайн" not in p.lower() and "online" not in p.lower():
        p = f"{p} или онлайн" if p else "онлайн"
    return p.replace("Online", "онлайн").replace("Онлайн", "онлайн")


def deadline_text(e, today):
    dl = e.get("deadline") or ""
    if not dl:
        return ""
    try:
        days = (date.fromisoformat(dl[:10]) - date.fromisoformat(today)).days
    except Exception:
        return ""
    when = {0: "сегодня", 1: "завтра"}.get(days, f"через {days} дн." if 0 < days <= 7 else "")
    return f"подать до {short_date(dl)}" + (f" ({when})" if when else "")


def link_of(e):
    return e.get("link") or e.get("post_url") or ""


def title_link(e, limit=80):
    t = e["title"] if len(e["title"]) <= limit else e["title"][:limit - 1] + "…"
    url = link_of(e)
    return f'<a href="{attr(url)}">{h(t)}</a>' if url else h(t)


def top_block(n, e, hook, today):
    lines = [f"<b>{n}. {KIND_ICON.get(e['kind'], '📌')} {title_link(e)}</b>"]
    meta = [x for x in (place(e), short_when(e)) if x]
    if e.get("prize"):
        meta.append(f"🏆 {h(e['prize'][:60])}")
    if meta:
        lines.append(" · ".join(h(x) if not x.startswith("🏆") else x for x in meta))
    dl = deadline_text(e, today)
    if dl:
        lines.append(f"⏰ <b>{dl}</b>")
    lines.append(f"<i>{h(hook or e.get('summary', ''))}</i>")
    flags = [FLAG_SHORT[f] for f in e.get("flags", []) if f in FLAG_SHORT]
    if flags:
        lines.append(" · ".join(flags))
    return "\n".join(lines)


def short_line(n, e, today, show_place=True):
    bits = [x for x in (short_when(e), place(e) if show_place else "") if x]
    dl = e.get("deadline")
    if dl and short_date(dl) and not short_when(e).endswith(short_date(dl)):
        bits.append(f"подать до {short_date(dl)}")
    tail = f" — {h(', '.join(bits))}" if bits else ""
    mark = " 💡" if "needs_project" in e.get("flags", []) else ""
    return f"{n}. {KIND_ICON.get(e['kind'], '📌')} {title_link(e, 70)}{tail}{mark}"


def digest_message(today_dt, top, more, women, free, reminders, queue_left, evening=False, watch=()):
    """top: [(event, hook)], others: [event]. Returns (html text, numbered events in order)."""
    today = today_dt.date().isoformat()
    numbered, parts = [], []
    if evening:
        parts.append("🌙 <b>Вечером: это стоит успеть</b>")
    else:
        day = f"{WEEKDAYS[today_dt.weekday()].capitalize()}, {today_dt.day} {MONTHS[today_dt.month - 1]}"
        counts = [f"{len(top)} главн{'ое' if len(top) == 1 else 'ых'}" if top else "",
                  f"{len(more)} можно глянуть" if more else "", f"{len(free)} халява" if free else ""]
        counts = " · ".join(c for c in counts if c) or "нового стоящего нет"
        parts.append(f"☀️ <b>{day}</b>\n{counts}")

    if top:
        blocks = []
        for e, hook in top:
            numbered.append(e)
            blocks.append(top_block(len(numbered), e, hook, today))
        parts.append(("🔥 <b>Стоит подать</b>\n\n" if not evening else "") + "\n\n".join(blocks))

    def section(title, items, show_place=True):
        lines = []
        for e in items:
            numbered.append(e)
            lines.append(short_line(len(numbered), e, today, show_place))
        if lines:
            parts.append(f"{title}\n" + "\n".join(lines))

    section("👀 <b>Можно глянуть</b>", more)
    section("👩 <b>Только для девушек</b> <i>(для интереса)</i>", women)
    section("🎁 <b>Халява</b>", free)
    if watch:
        parts.append("⭐ <b>Отмеченное</b>\n" + "\n".join(
            f"• {title_link(e, 60)} — {deadline_text(e, today) or short_when(e)}" for e in watch))
    if reminders:
        parts.append("⏰ <b>Скоро дедлайн</b>\n" + "\n".join(
            f"• {title_link(e, 60)} — {deadline_text(e, today)}" for e in reminders))
    foot = []
    if numbered:
        foot.append("Нажми ⭐ с номером: сохраню в «Отмеченное», напомню о дедлайне и учту в подборках.")
    if queue_left:
        foot.append(f"В очереди ещё {queue_left}.")
    if foot:
        parts.append("<i>" + " ".join(foot) + "</i>")

    text = "\n\n".join(parts)
    while len(text) > LIMIT and more:  # too long: drop the least valuable lines
        more = more[:-1]
        return digest_message(today_dt, top, more, women, free, reminders, queue_left + 1, evening, watch)
    return text, numbered


def number_buttons(events, per_row=6):
    btns = [{"text": f"⭐{i}", "callback_data": f"up:{e['id']}"} for i, e in enumerate(events, 1)]
    return [btns[i:i + per_row] for i in range(0, len(btns), per_row)]


def mark_voted(keyboard, data):
    """Keyboard of a list digest after a press: the pressed number becomes ✅N."""
    return [[{"text": "✅" + b["text"].lstrip("⭐"), "callback_data": "noop"} if b.get("callback_data") == data else b
             for b in row] for row in keyboard]
