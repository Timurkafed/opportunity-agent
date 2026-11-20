"""Tests without network: python -m pytest tests"""
import io
import json
import urllib.error
from datetime import datetime, timedelta

import pytest

from agent import collect, llm, main as m, store, tg


def ev(title, start="", city="Алматы", **kw):
    e = {"title": title, "kind": "meetup", "summary": "s", "city": city, "online": False, "start_date": start,
         "end_date": "", "deadline": "", "who": "", "prize": "", "flags": [], "is_freebie": False, "score": 8,
         "why": "", "link": "", "post_url": "https://t.me/x/1", "source": "@x", "sources": ["@x"],
         "id": title[:10], "first_seen": store.now().isoformat(), "sent_at": None, "reminded_at": None,
         "feedback": None}
    e.update(kw)
    return e


# ---------------------------------------------------------------- dedup
@pytest.mark.parametrize("a,b", [
    ("IT-беш от Servercore в Алматы", "Servercore’s Flagship IT-Besh in Almaty"),
    ("IT-беш от Servercore в Алматы", "Servercore-дың флагмандық IT-беші Алматыда"),
    ("Almaty Hub Pizza Pitch", "Pizza Pitch: Посещение питч-конкурса"),
    ("KBTU Open Programming Contest 2026", "KBTU OPEN FALL 2026 — бағдарламалау жарысы"),
    ("Встреча «ГЕН ЕДИНОРОГА: Почему одни компании растут в 100 раз»",
     "Almaty Hub | Почему одни компании растут в 100 раз, а другие топчутся"),
])
def test_same_event_glues_repeats(a, b):
    assert m.same_event(ev(a, "2026-11-26"), ev(b, "2026-11-26"))


@pytest.mark.parametrize("a,b", [
    ("Pizza Pitch Almaty", "Pizza Pitch New Year. New Unicorns!"),  # other date
    ("Astana Hub Demo Day", "Astana Hub Hackathon"),                  # same hub, other event
    ("Хакатон по искусственному интеллекту в медицине", "Конференция: искусственный интеллект для бизнеса"),
])
def test_same_event_keeps_different_events(a, b):
    assert not m.same_event(ev(a, "2026-11-19"), ev(b, "2026-12-22" if "Year" in b else "2026-11-19"))


def test_same_event_needs_same_city():
    assert not m.same_event(ev("Servercore IT-беш", "2026-11-26", city="Астана"),
                            ev("Флагманский митап Servercore", "2026-11-26", city="Алматы"))


def test_dedupe_queue_marks_later_repeat_and_keeps_sources():
    first = ev("IT-беш от Servercore в Алматы", "2026-11-26", sources=["@a"])
    second = ev("Флагманский IT-беш от Servercore", "2026-11-26", sources=["@b"], deadline="2026-11-25")
    other = ev("Almaty Java Community", "2026-11-26")
    events = [first, second, other]
    assert m.dedupe_queue(events) == 1
    assert second["sent_at"] == "dupe" and other["sent_at"] is None
    assert first["sources"] == ["@a", "@b"] and first["deadline"] == "2026-11-25"


def test_merge_cleans_dates():
    events = []
    op = {k: v for k, v in ev("X", "25.11.2026").items()}
    op["deadline"] = "2026-11-30T23:59"
    m.merge(events, [op])
    assert events[0]["start_date"] == "" and events[0]["deadline"] == "2026-11-30"


# ---------------------------------------------------------------- reminders
def test_reminder_not_right_after_first_show():
    today = datetime(2026, 11, 20, 9, 0, tzinfo=m.ALMATY)
    just_sent = ev("A", deadline="2026-11-22", sent_at=store.now().isoformat())
    old_sent = ev("B", deadline="2026-11-22", sent_at=(store.now() - timedelta(days=2)).isoformat())
    disliked = ev("C", deadline="2026-11-22", sent_at=(store.now() - timedelta(days=2)).isoformat(), feedback="down")
    far = ev("D", deadline="2026-12-22", sent_at=(store.now() - timedelta(days=2)).isoformat())
    picked = m.pick_reminders([just_sent, old_sent, disliked, far], today)
    assert [e["title"] for e in picked] == ["B"]


def test_recent_run_guard():
    assert m.recent_run({"last_run": (store.now() - timedelta(hours=1)).isoformat()})
    assert m.recent_run({"last_run": (store.now() - timedelta(hours=9)).isoformat()}) is None
    assert m.recent_run({}) is None


# ---------------------------------------------------------------- telegram updates
class FakeBot:
    def __init__(self, pages=(), fail_on=()):
        self.pages = list(pages)
        self.sent = []
        self.calls = []
        self.fail_on = set(fail_on)

    def updates(self, offset):
        return self.pages.pop(0) if self.pages else []

    def call(self, method, **params):
        self.calls.append(method)

    def send(self, chat, text, buttons=None, silent=False):
        n = len(self.sent)
        self.sent.append(text)
        if n in self.fail_on:
            raise RuntimeError("telegram said no")


def cb(uid, data, chat=1, with_msg=True):
    q = {"id": str(uid), "data": data}
    if with_msg:
        q["message"] = {"chat": {"id": chat}, "message_id": uid}
    return {"update_id": uid, "callback_query": q}


def test_updates_read_all_pages_and_survive_odd_updates():
    events = [ev("A", id="a1"), ev("B", id="b1")]
    page1 = [cb(i, "noop") for i in range(99)] + [cb(99, "up:a1")]
    page2 = [cb(100, "down:b1"), cb(101, "up:gone"), cb(102, "up:a1", with_msg=False), {"update_id": 103}]
    meta = {"chat_id": 1, "tg_offset": 0}
    m.process_updates(FakeBot([page1, page2]), meta, events)
    assert events[0]["feedback"] == "up" and events[1]["feedback"] == "down"
    assert meta["tg_offset"] == 104


def test_href_quotes_are_escaped():
    e = ev("A", link='https://x.kz/?a="b"')
    assert 'href="https://x.kz/?a=&quot;b&quot;"' in tg.item_text(e)


# ---------------------------------------------------------------- gemini failover
def http_error(code, body=""):
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(body.encode()))


def ok_answer(obj):
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}, "finishReason": "STOP"}]}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setattr(llm, "PAUSE", 0)


def gem_with(script, models=("m-a", "m-b", "m-c"), budget=300):
    g = llm.Gemini("k", budget=budget, models=list(models))
    log = []

    def post(model, body, timeout):
        log.append(model)
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step
    g._post = post
    return g, log


def test_gemini_drops_missing_model_and_daily_quota():
    g, log = gem_with([http_error(404), http_error(429, '"quotaId": "GenerateRequestsPerDayPerProjectPerModel"'),
                       ok_answer({"x": 1})])
    assert g.ask("s", "u", {}) == {"x": 1}
    assert log == ["m-a", "m-b", "m-c"] and g.models == ["m-c"]


def test_gemini_overloaded_goes_to_the_end_and_minute_limit_waits_once():
    g, log = gem_with([http_error(503), http_error(429, '"retryDelay": "7s"'), ok_answer({"ok": True})])
    assert g.ask("s", "u", {}) == {"ok": True}
    assert log == ["m-a", "m-b", "m-b"] and g.models == ["m-b", "m-c", "m-a"]


def test_gemini_timeout_moves_on_without_retrying_same_model():
    g, log = gem_with([TimeoutError("read timed out"), ok_answer({})])
    g.ask("s", "u", {})
    assert log == ["m-a", "m-b"]


def test_gemini_thinking_config_rejected_retries_without_it():
    g, log = gem_with([http_error(400, "thinking not supported"), ok_answer({})], models=("gemini-2.5-flash",))
    g.ask("s", "u", {})
    assert log == ["gemini-2.5-flash", "gemini-2.5-flash"] and g.models == ["gemini-2.5-flash"]


def test_gemini_budget_stops_work(monkeypatch):
    g, log = gem_with([], budget=10)
    with pytest.raises(llm.OutOfTime):
        g.ask("s", "u", {})
    assert log == []


def item(i, text="x" * 100):
    return {"uid": f"tg:x/{i}", "source": "@x", "url": f"https://t.me/x/{i}", "date": "", "text": text, "links": []}


def test_extract_splits_truncated_batch_and_returns_unprocessed():
    items = [item(i) for i in range(4)]
    truncated = {"candidates": [{"content": {"parts": [{"text": "{\"items\": ["}]}, "finishReason": "MAX_TOKENS"}]}
    g, log = gem_with([truncated, ok_answer({"items": []}), http_error(404), http_error(404), http_error(404)])
    found, failed = llm.extract(g, items, "crit", [], "2026-11-20")
    assert found == [] and [it["uid"] for it in failed] == ["tg:x/2", "tg:x/3"]


# ---------------------------------------------------------------- whole run with fakes
@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STATE_DIR", str(tmp_path))
    store.save("seen.json", {"tg:old/1": store.now().isoformat()})
    return tmp_path


def fake_found(title, kind="hackathon", is_freebie=False, score=9, uid="tg:x/1"):
    op = ev(title, "2026-11-28", kind=kind, is_freebie=is_freebie, score=score)
    op.update({"src": 0, "uid": uid})
    return op


def run_with_fakes(monkeypatch, found, bot):
    monkeypatch.setattr(m.collect, "collect_all", lambda *a, **k: ([item(1)], {"@x": 1}, {"@y": "HTTP 404"}))
    monkeypatch.setattr(m.llm, "Gemini", lambda key: type("G", (), {"calls": 1})())
    monkeypatch.setattr(m.llm, "extract", lambda *a: (found, []))
    monkeypatch.setattr(m.llm, "rerank", lambda g, c, *a: (set(), list(range(len(c)))))
    monkeypatch.setattr(m.tg, "Bot", lambda token: bot)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    meta = {"chat_id": 1}
    m.run(meta, dry=False, no_send=False)
    return meta


def test_free_hackathon_goes_to_main_block_and_course_to_freebies(tmp_state, monkeypatch):
    bot = FakeBot()
    found = [fake_found("Free Hackathon", kind="hackathon", is_freebie=True),
             fake_found("Бесплатный курс Figma", kind="freebie", is_freebie=True)]
    run_with_fakes(monkeypatch, found, bot)
    text = "\n".join(bot.sent)
    assert text.index("Free Hackathon") < text.index("🎁 <b>Халява</b>") < text.index("Бесплатный курс Figma")


def test_broken_message_does_not_lose_the_rest(tmp_state, monkeypatch):
    bot = FakeBot(fail_on={2})  # 0 = header, 1 = first item, 2 = second item fails
    names = ["Decentrathon", "HackNU", "Junction Almaty"]
    found = [fake_found(t, score=10 - n) for n, t in enumerate(names)]
    meta = run_with_fakes(monkeypatch, found, bot)
    events = store.load("events.json", [])
    sent = {e["title"]: bool(e["sent_at"]) for e in events}
    assert sent == {"Decentrathon": True, "HackNU": False, "Junction Almaty": True}
    assert meta["last_run"] and meta["sources"]["@y"]["errors_in_row"] == 1
    assert store.load("meta.json", {})["saved_at"]
