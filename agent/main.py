"""One run of the agent: read 👍/👎 → collect → Gemini → dedup → send digest → save memory.

Run:  python -m agent.main            (normal run, needs secrets in environment)
      python -m agent.main --dry      (only collect and print counts; no keys needed)
      python -m agent.main --no-send  (collect + Gemini, print the digest instead of sending)
"""
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher

from . import collect, llm, store, tg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALMATY = timezone(timedelta(hours=5))
MAX_MAIN = 10       # main items per digest
MAX_FREE = 3        # freebies per digest
MIN_SCORE = 5       # below this nothing is sent
DROP_FLAGS = {"school_only", "rf_only"}  # the user can't take part anyway


def load_env_file():
    """Keys for running on a PC live in .env (never committed). GitHub uses its own secrets instead."""
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            key, sep, val = line.strip().partition("=")
            if sep and key and not key.startswith("#") and val.strip() and not os.environ.get(key.strip()):
                os.environ[key.strip()] = val.strip().strip('"').strip("'")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


# ------------------------------------------------------------------ dedup
def norm(title):
    t = title.lower()
    t = re.sub(r"\b20\d\d\b", " ", t)
    t = re.sub(r"[^\w]+", " ", t, flags=re.U)
    return " ".join(t.split())


def same_event(a, b):
    na, nb = norm(a["title"]), norm(b["title"])
    if not na or not nb:
        return False
    dates_ok = not a["start_date"] or not b["start_date"] or a["start_date"] == b["start_date"]
    if na == nb:
        return dates_ok
    if min(len(na), len(nb)) >= 8 and (na in nb or nb in na):
        return dates_ok
    return dates_ok and SequenceMatcher(None, na, nb).ratio() >= 0.85


def merge(events, found):
    added = 0
    for op in found:
        hit = next((e for e in events if same_event(e, op)), None)
        if hit:
            if op["source"] not in hit["sources"]:
                hit["sources"].append(op["source"])
            hit["score"] = max(hit["score"], op["score"])
            for k in ("deadline", "start_date", "end_date", "link", "prize", "who"):
                if not hit.get(k) and op.get(k):
                    hit[k] = op[k]
            continue
        eid = hashlib.sha1((norm(op["title"]) + op["start_date"]).encode()).hexdigest()[:10]
        e = {k: op[k] for k in ("title", "kind", "summary", "city", "online", "start_date", "end_date", "deadline",
                                "who", "prize", "flags", "is_freebie", "score", "why", "link", "post_url", "source")}
        e.update({"id": eid, "sources": [op["source"]], "first_seen": store.now().isoformat(),
                  "sent_at": None, "reminded_at": None, "feedback": None})
        events.append(e)
        added += 1
    return added


def is_stale(e, today):
    last = e.get("deadline") or e.get("end_date") or e.get("start_date")
    if last:
        return last[:10] < today
    return e["first_seen"] < (store.now() - timedelta(days=21)).isoformat()


# ------------------------------------------------------------------ telegram feedback
def process_updates(bot, meta, events):
    by_id = {e["id"]: e for e in events}
    try:
        ups = bot.updates(meta.get("tg_offset", 0))
    except Exception as e:
        print(f"  ! getUpdates: {e}")
        return
    votes = 0
    for u in ups:
        meta["tg_offset"] = u["update_id"] + 1
        msg = u.get("message")
        if msg:
            chat = msg["chat"]["id"]
            if not meta.get("chat_id") and msg.get("text", "").startswith("/start"):
                meta["chat_id"] = chat
                try:
                    bot.send(chat, "Привет! Я запомнил этот чат. Дайджест приходит в 9:00 и 18:00 по Алматы.\n"
                                   "Под каждой возможностью есть 👍/👎 — я учту их в следующем отборе.\n"
                                   "Нажатия я читаю при следующем запуске, поэтому «Учтено» появится не сразу.")
                except Exception as ex:
                    print(f"  ! welcome: {ex}")
            continue
        cq = u.get("callback_query")
        if not cq or cq["message"]["chat"]["id"] != meta.get("chat_id"):
            continue
        vote, _, eid = (cq.get("data") or "").partition(":")
        if vote in ("up", "down") and eid in by_id:
            by_id[eid]["feedback"] = vote
            votes += 1
            for method, params in (("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Учтено"}),
                                   ("editMessageReplyMarkup", {"chat_id": meta["chat_id"],
                                                               "message_id": cq["message"]["message_id"],
                                                               "reply_markup": {"inline_keyboard": tg.voted_buttons(vote)}})):
                try:
                    bot.call(method, **params)
                except Exception:
                    pass  # old callbacks can't be answered; that's fine
    print(f"  updates: {len(ups)}, votes: {votes}")


# ------------------------------------------------------------------ main
def main():
    load_env_file()
    args = set(sys.argv[1:])
    dry, no_send = "--dry" in args, "--no-send" in args
    sources = json.loads(read("config/sources.json"))
    criteria = read("criteria.md")
    seen = store.load("seen.json", {})
    events = store.load("events.json", [])
    meta = store.load("meta.json", {})
    today_dt = datetime.now(ALMATY)
    today = today_dt.date().isoformat()
    first_run = not seen

    bot = None
    if not dry and not no_send:
        bot = tg.Bot(os.environ["TELEGRAM_BOT_TOKEN"])
        if os.environ.get("TELEGRAM_CHAT_ID"):
            meta["chat_id"] = int(os.environ["TELEGRAM_CHAT_ID"])
        print("1) Reading 👍/👎 ...")
        process_updates(bot, meta, events)

    print("2) Collecting ...")
    items, stats = collect.collect_all(sources, seen, days_back=14 if first_run else 10)
    print(f"  new materials: {len(items)}  " + ", ".join(f"{k}:{v}" for k, v in sorted(stats.items()) if v))
    if dry:
        return

    gem = llm.Gemini(os.environ["GEMINI_API_KEY"])
    print("3) Gemini: extracting ...")
    found, failed = llm.extract(gem, items, criteria, events, today)
    failed_uids = {it["uid"] for it in failed}
    stamp = store.now().isoformat()
    for it in items:
        if it["uid"] not in failed_uids:
            seen[it["uid"]] = stamp
    added = merge(events, found)
    print(f"  opportunities: {len(found)}, new after dedup: {added}")

    print("4) Choosing ...")
    pending = [e for e in events if not e["sent_at"] and not is_stale(e, today)
               and e["score"] >= MIN_SCORE and not (set(e["flags"]) & DROP_FLAGS)]
    pending.sort(key=lambda e: -e["score"])
    main_c = [e for e in pending if not (e["is_freebie"] or e["kind"] == "freebie")][:40]
    free_c = [e for e in pending if e["is_freebie"] or e["kind"] == "freebie"]
    recent_cut = (store.now() - timedelta(days=30)).isoformat()
    sent_titles = [e["title"] for e in events if e["sent_at"] and e["sent_at"] != "dupe" and e["sent_at"] >= recent_cut]
    if main_c:
        try:
            dupes, order = llm.rerank(gem, main_c, sent_titles, criteria, events, today)
            for i in dupes:
                main_c[i]["sent_at"] = "dupe"
            main_c = [main_c[i] for i in order]
        except Exception as ex:
            print(f"  ! rerank failed, using scores: {ex}")
    digest, freebies = main_c[:MAX_MAIN], free_c[:MAX_FREE]
    queue_left = max(0, len(main_c) - len(digest)) + max(0, len(free_c) - len(freebies))

    soon = (today_dt.date() + timedelta(days=3)).isoformat()
    tomorrow2 = (today_dt.date() + timedelta(days=2)).isoformat()
    reminders = [e for e in events if e["sent_at"] and e["sent_at"] != "dupe" and not e["reminded_at"]
                 and e["feedback"] != "down" and not is_stale(e, today)
                 and ((e["deadline"] and today <= e["deadline"][:10] <= soon)
                      or (e["feedback"] == "up" and not e["deadline"] and e["start_date"]
                          and today < e["start_date"][:10] <= tomorrow2))][:5]

    part = "☀️ Утренний" if today_dt.hour < 14 else "🌙 Вечерний"
    if digest or freebies:
        header = f"{part} дайджест, {tg.nice_date(today)}: {len(digest)} возможностей"
        header += f" + {len(freebies)} халявы" if freebies else ""
        header += f".\nЕщё в очереди: {queue_left} — пришлю в следующий раз." if queue_left else "."
    else:
        header = f"{part} дайджест, {tg.nice_date(today)}: нового стоящего нет."

    print("5) Sending ...")
    if no_send:
        print("\n" + header)
        for e in reminders:
            print("\n[НАПОМИНАНИЕ]\n" + tg.item_text(e))
        for e in digest:
            print("\n" + tg.item_text(e) + f"\n   (оценка {e['score']}: {e['why']})")
        for e in freebies:
            print("\n[ХАЛЯВА]\n" + tg.item_text(e))
        print(f"\nGemini calls: {gem.calls}  (preview: memory not saved)")
        return

    chat = meta.get("chat_id")
    if not chat:
        print("  ! chat unknown: open the bot in Telegram and press Start. Nothing sent this time.")
    else:
        bot.send(chat, header)
        for e in reminders:
            bot.send(chat, "⏰ <b>Напоминание</b>\n" + tg.item_text(e), silent=True)
            e["reminded_at"] = stamp
        for e in digest:
            bot.send(chat, tg.item_text(e), tg.vote_buttons(e["id"]), silent=True)
            e["sent_at"] = stamp
        if freebies:
            bot.send(chat, "🎁 <b>Халява</b>", silent=True)
            for e in freebies:
                bot.send(chat, tg.item_text(e), tg.vote_buttons(e["id"]), silent=True)
                e["sent_at"] = stamp

    meta["last_run"] = stamp
    meta["gemini_calls_last_run"] = gem.calls
    store.save("seen.json", store.prune_seen(seen))
    store.save("events.json", store.prune_events(events))
    store.save("meta.json", meta)
    print(f"Done. Sent {len(digest)} + {len(freebies)} freebies + {len(reminders)} reminders. Gemini calls: {gem.calls}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
