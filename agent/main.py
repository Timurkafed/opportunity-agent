"""One run of the agent: read 👍/👎 → collect → Gemini → dedup → send digest → save memory.

Run:  python -m agent.main            (normal run, needs secrets in environment)
      python -m agent.main --force    (normal run even if another run was less than 4 hours ago)
      python -m agent.main --dry      (only collect and print counts; no keys needed)
      python -m agent.main --no-send  (collect + Gemini, print the digest instead of sending)

Exit code: 0 = done or skipped, 1 = failed (a short note is sent to Telegram when possible).
Saving state/ to the repository is a separate step: python -m agent.gitstate <runner>.
"""
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher

from . import collect, gitstate, llm, store, tg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALMATY = timezone(timedelta(hours=5))
MAX_MAIN = 10       # main items per digest
MAX_FREE = 3        # freebies per digest
MIN_SCORE = 5       # below this nothing is sent
DROP_FLAGS = {"school_only", "rf_only"}  # the user can't take part anyway
SKIP_HOURS = 4      # another run finished less than this ago -> do nothing (laptop + cloud overlap)


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


# words that say nothing about WHICH event it is
STOP = set("""
hackathon hackaton хакатон хакатона хакатоне meetup митап митапа forum форум форума summit саммит conference
конференция конференции startup стартап стартапов open fall winter spring summer week неделя cup camp
challenge contest конкурс конкурса program programming программа программы almaty алматы алматыда astana
астана астане kazakhstan казахстан казахстана online онлайн edition global international community event
talks demo night city school школа club клуб день days fest festival фестиваль бизнес бизнеса business
искусственный интеллект интеллекта agents агенты data данных tech техно встреча встречи практикум воркшоп
workshop вебинар webinar курс course hub хаб центр center года 2025 2026 2027 новый new the and for with
""".split())


def key_tokens(title):
    """Distinctive words of a title: brands and names (latin from 4 letters, others from 5)."""
    out = set()
    for t in norm(title).split():
        if t in STOP or t.isdigit():
            continue
        if (t.isascii() and len(t) >= 4) or len(t) >= 5:
            out.add(t)
    return out


def norm_city(e):
    c = (e.get("city") or "").lower().strip()
    if not c or "онлайн" in c or "online" in c:
        return "online" if e.get("online") or c else ""
    return {"almaty": "алматы", "astana": "астана"}.get(c, c)


def same_event(a, b):
    na, nb = norm(a["title"]), norm(b["title"])
    if not na or not nb:
        return False
    dates_ok = not a["start_date"] or not b["start_date"] or a["start_date"] == b["start_date"]
    if na == nb:
        return dates_ok
    if min(len(na), len(nb)) >= 8 and (na in nb or nb in na):
        return dates_ok
    if dates_ok and SequenceMatcher(None, na, nb).ratio() >= 0.85:
        return True
    # the same event written in another language or with other words around the name:
    # same start date, same city and a shared distinctive word (brand, name)
    if not a["start_date"] or a["start_date"] != b["start_date"]:
        return False
    ca, cb = norm_city(a), norm_city(b)
    if not ca or ca != cb:
        return False
    ta, tb = key_tokens(a["title"]), key_tokens(b["title"])
    common = ta & tb
    if any(t.isascii() for t in common):
        return True
    return len(common) >= 2 and len(common) >= 0.5 * min(len(ta), len(tb))


def clean_date(s):
    m = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", s or "")
    if not m:
        return ""
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return ""


def merge(events, found):
    added = 0
    for op in found:
        for k in ("start_date", "end_date", "deadline"):
            op[k] = clean_date(op.get(k))
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


def dedupe_queue(events):
    """Glue together not-yet-sent events that repeat an earlier one (sent or not). Returns how many."""
    glued = 0
    for i, e in enumerate(events):
        if e["sent_at"]:
            continue
        for prev in events[:i]:
            if prev["sent_at"] == "dupe" or not same_event(prev, e):
                continue
            e["sent_at"] = "dupe"
            for src in e["sources"]:
                if src not in prev["sources"]:
                    prev["sources"].append(src)
            if not prev["sent_at"]:
                prev["score"] = max(prev["score"], e["score"])
                for k in ("deadline", "start_date", "end_date", "link", "prize", "who"):
                    if not prev.get(k) and e.get(k):
                        prev[k] = e[k]
            glued += 1
            break
    return glued


def is_stale(e, today):
    last = e.get("deadline") or e.get("end_date") or e.get("start_date")
    if last:
        return last[:10] < today
    return e["first_seen"] < (store.now() - timedelta(days=21)).isoformat()


# ------------------------------------------------------------------ telegram feedback
def process_updates(bot, meta, events, pages=10):
    by_id = {e["id"]: e for e in events}
    total = votes = 0
    for _ in range(pages):  # Telegram gives at most 100 updates per call
        try:
            ups = bot.updates(meta.get("tg_offset", 0))
        except Exception as e:
            print(f"  ! getUpdates: {e}")
            break
        if not ups:
            break
        total += len(ups)
        for u in ups:
            meta["tg_offset"] = u["update_id"] + 1
            votes += _handle_update(bot, meta, by_id, u)
        if len(ups) < 100:
            break
    print(f"  updates: {total}, votes: {votes}")


def _handle_update(bot, meta, by_id, u):
    msg = u.get("message")
    if msg:
        chat = (msg.get("chat") or {}).get("id")
        if chat and not meta.get("chat_id") and (msg.get("text") or "").startswith("/start"):
            meta["chat_id"] = chat
            try:
                bot.send(chat, "Привет! Я запомнил этот чат. Дайджест приходит в 9:00 и 18:00 по Алматы.\n"
                               "Под каждой возможностью есть 👍/👎 — я учту их в следующем отборе.\n"
                               "Нажатия я читаю при следующем запуске, поэтому «Учтено» появится не сразу.")
            except Exception as ex:
                print(f"  ! welcome: {ex}")
        return 0
    cq = u.get("callback_query") or {}
    cmsg = cq.get("message") or {}
    if not cq or (cmsg.get("chat") or {}).get("id") != meta.get("chat_id"):
        return 0
    vote, _, eid = (cq.get("data") or "").partition(":")
    if vote not in ("up", "down"):
        return 0
    if eid not in by_id:
        print(f"  ! vote for an event that is not in memory: {eid}")
        return 0
    by_id[eid]["feedback"] = vote
    for method, params in (("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": "Учтено"}),
                           ("editMessageReplyMarkup", {"chat_id": meta["chat_id"],
                                                       "message_id": cmsg.get("message_id"),
                                                       "reply_markup": {"inline_keyboard": tg.voted_buttons(vote)}})):
        try:
            bot.call(method, **params)
        except Exception:
            pass  # old callbacks can't be answered; that's fine
    return 1


def notify_failure(meta, text):
    """Short note to the owner's chat. Never raises."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or meta.get("chat_id")
    if not token or not chat:
        print("  ! cannot report the failure to Telegram: no token or chat")
        return
    runner = os.environ.get("OPP_RUNNER", "")
    try:
        tg.Bot(token).send(int(chat), f"⚠️ <b>Бот{' (' + tg.h(runner) + ')' if runner else ''}:</b> {tg.h(text)}")
    except Exception as ex:
        print(f"  ! failure note not sent: {ex}")


def recent_run(meta, hours=SKIP_HOURS):
    last = meta.get("last_run")
    if not last:
        return None
    try:
        ago = store.now() - datetime.fromisoformat(last)
    except ValueError:
        return None
    return ago if ago < timedelta(hours=hours) else None


def pick_reminders(events, today_dt):
    today = today_dt.date().isoformat()
    soon = (today_dt.date() + timedelta(days=3)).isoformat()
    in2 = (today_dt.date() + timedelta(days=2)).isoformat()
    shown_before = (store.now() - timedelta(hours=20)).isoformat()  # not right after the first show
    return [e for e in events if e["sent_at"] and e["sent_at"] != "dupe" and e["sent_at"] < shown_before
            and not e["reminded_at"] and e["feedback"] != "down" and not is_stale(e, today)
            and ((e["deadline"] and today <= e["deadline"][:10] <= soon)
                 or (e["feedback"] == "up" and not e["deadline"] and e["start_date"]
                     and today < e["start_date"][:10] <= in2))][:5]


def update_source_health(meta, stats, errors):
    health = meta.setdefault("sources", {})
    stamp = store.now().isoformat()[:16]
    for name in set(stats) | set(errors):
        n = stats.get(name, 0)
        h = health.setdefault(name, {})
        if name in errors:
            h["errors_in_row"] = h.get("errors_in_row", 0) + 1
            h["last_error"] = errors[name]
        else:
            h["errors_in_row"] = 0
            h.pop("last_error", None)
            if n:
                h["last_new"] = stamp


# ------------------------------------------------------------------ main
def save_all(seen, events, meta):
    meta["saved_at"] = store.now().isoformat()
    store.save("seen.json", store.prune_seen(seen))
    store.save("events.json", store.prune_events(events))
    store.save("meta.json", meta)


def main():
    load_env_file()
    args = set(sys.argv[1:])
    dry, no_send, force = "--dry" in args, "--no-send" in args, "--force" in args
    real = not dry and not no_send
    if real:
        print("0) Memory ...")
        gitstate.refresh()  # another runner (cloud / laptop) may have run since our copy
    meta = store.load("meta.json", {})
    if real and not force:
        ago = recent_run(meta)
        if ago is not None:
            print(f"SKIP: another run finished {int(ago.total_seconds() // 60)} min ago "
                  f"({meta.get('runner') or '?'}). Use --force to run anyway.")
            return 0
    try:
        run(meta, dry, no_send)
        return 0
    except Exception as ex:
        if real:
            notify_failure(meta, f"запуск упал: {type(ex).__name__}: {str(ex)[:300]}. "
                                 "То, что не успел, пришлю в следующий раз.")
        raise


def run(meta, dry, no_send):
    real = not dry and not no_send
    sources = json.loads(read("config/sources.json"))
    criteria = read("criteria.md")
    seen = store.load("seen.json", {})
    events = store.load("events.json", [])
    today_dt = datetime.now(ALMATY)
    today = today_dt.date().isoformat()
    first_run = not seen

    bot = None
    if real:
        bot = tg.Bot(os.environ["TELEGRAM_BOT_TOKEN"])
        if os.environ.get("TELEGRAM_CHAT_ID"):
            meta["chat_id"] = int(os.environ["TELEGRAM_CHAT_ID"])
        print("1) Reading 👍/👎 ...")
        process_updates(bot, meta, events)

    print("2) Collecting ...")
    items, stats, errors = collect.collect_all(sources, seen, days_back=14 if first_run else 10)
    print(f"  new materials: {len(items)}  " + ", ".join(f"{k}:{v}" for k, v in sorted(stats.items()) if v))
    if errors:
        print(f"  ! sources with errors: {', '.join(sorted(errors))}")
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
    glued = dedupe_queue(events)
    print(f"  opportunities: {len(found)}, new after dedup: {added}, glued repeats in queue: {glued}")
    if real:
        update_source_health(meta, stats, errors)
        save_all(seen, events, meta)  # what Gemini has read is not lost if the run breaks later

    print("4) Choosing ...")
    pending = [e for e in events if not e["sent_at"] and not is_stale(e, today)
               and e["score"] >= MIN_SCORE and not (set(e["flags"]) & DROP_FLAGS)]
    pending.sort(key=lambda e: -e["score"])
    main_c = [e for e in pending if e["kind"] != "freebie"][:40]
    free_c = [e for e in pending if e["kind"] == "freebie"][:10]  # a free hackathon is still a hackathon
    recent_cut = (store.now() - timedelta(days=30)).isoformat()
    sent_titles = [e["title"] for e in events if e["sent_at"] and e["sent_at"] != "dupe" and e["sent_at"] >= recent_cut]
    cands = main_c + free_c
    if cands:
        try:
            dupes, order = llm.rerank(gem, cands, sent_titles, criteria, events, today)
            for i in dupes:
                cands[i]["sent_at"] = "dupe"
            ranked = [cands[i] for i in order]
            main_c = [e for e in ranked if e["kind"] != "freebie"]
            free_c = [e for e in ranked if e["kind"] == "freebie"]
        except Exception as ex:
            print(f"  ! rerank failed, using scores: {ex}")
    digest, freebies = main_c[:MAX_MAIN], free_c[:MAX_FREE]
    queue_left = max(0, len(main_c) - len(digest)) + max(0, len(free_c) - len(freebies))
    reminders = pick_reminders(events, today_dt)

    part = "☀️ Утренний" if today_dt.hour < 14 else "🌙 Вечерний"
    if digest or freebies:
        header = f"{part} дайджест, {tg.nice_date(today)}: {len(digest)} возможностей"
        header += f" + {len(freebies)} халявы" if freebies else ""
        header += f".\nЕщё в очереди: {queue_left} — пришлю в следующий раз." if queue_left else "."
    else:
        header = f"{part} дайджест, {tg.nice_date(today)}: нового стоящего нет."
    if failed:
        header += (f"\n⚠️ Gemini не ответил: {len(failed)} из {len(items)} новых материалов разберу в следующий раз."
                   if len(failed) < len(items) else
                   "\n⚠️ Gemini сейчас не ответил, новые материалы разберу в следующий раз.")

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

    if not meta.get("chat_id"):  # maybe Start was pressed while we were collecting
        process_updates(bot, meta, events)
    chat = meta.get("chat_id")
    sent = errors_send = 0
    if not chat:
        print("  ! chat unknown: open the bot in Telegram and press Start. Nothing sent this time.")
        digest, freebies, reminders = [], [], []
    else:
        bot.send(chat, header)  # if even this fails, the run fails and nothing is marked as sent

        def send_item(e, text, buttons, field):
            nonlocal sent, errors_send
            try:
                bot.send(chat, text, buttons, silent=True)
            except Exception as ex:  # one broken item must not stop the digest; it goes next time
                errors_send += 1
                print(f"  ! not sent: {e['title'][:60]}: {ex}")
                return
            e[field] = store.now().isoformat()
            sent += 1
            save_all(seen, events, meta)  # a run killed in the middle won't resend what already went

        for e in reminders:
            send_item(e, "⏰ <b>Напоминание</b>\n" + tg.item_text(e), None, "reminded_at")
        for e in digest:
            send_item(e, tg.item_text(e), tg.vote_buttons(e["id"]), "sent_at")
        if freebies:
            try:
                bot.send(chat, "🎁 <b>Халява</b>", silent=True)
            except Exception as ex:
                print(f"  ! freebies header: {ex}")
            for e in freebies:
                send_item(e, tg.item_text(e), tg.vote_buttons(e["id"]), "sent_at")
        if errors_send and errors_send >= sent:
            raise RuntimeError(f"Telegram refused {errors_send} of {errors_send + sent} messages")

    meta["last_run"] = store.now().isoformat()
    meta["runner"] = os.environ.get("OPP_RUNNER", "")
    meta["gemini_calls_last_run"] = gem.calls
    save_all(seen, events, meta)
    print(f"Done. Sent {sent} messages ({len(digest)} + {len(freebies)} freebies + {len(reminders)} reminders"
          f"{f', {errors_send} failed' if errors_send else ''}). Gemini calls: {gem.calls}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
