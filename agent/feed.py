"""Web feed: every live opportunity on one page, rebuilt after each run (state/feed.html).

It is plain HTML with a little JavaScript for filters, opened from the digest via raw.githack.com
(the repository is public, so no hosting account is needed).
"""
import html
import json
from datetime import date, timedelta

from . import store, tg

FEED_URL = "https://raw.githack.com/Timurkafed/opportunity-agent/main/state/feed.html"

KIND_RU = {"hackathon": "Хакатон", "meetup": "Митап", "startup": "Стартапы", "case": "Кейс", "forum": "Форум",
           "project": "Проект", "volunteer": "Волонтёрство", "social": "Соцпроект", "contest": "Конкурс",
           "internship": "Стажировка", "freebie": "Халява", "other": "Другое"}

CSS = """
:root{--bg:#eef1ef;--card:#fff;--ink:#15302d;--muted:#5b6f6b;--line:#d4dcd9;--accent:#0f7c78;--soft:#d7ecea;
--sun:#b86e09;--sunsoft:#fbecd3}
@media (prefers-color-scheme:dark){:root{--bg:#0f1817;--card:#16211f;--ink:#e4eeeb;--muted:#93a8a3;--line:#2a3a37;
--accent:#4fc2bb;--soft:#163633;--sun:#f0b04f;--sunsoft:#3a2c14;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,
"Segoe UI",Roboto,sans-serif;padding:20px max(14px,env(safe-area-inset-left)) 40px}
.wrap{max-width:760px;margin:0 auto;display:grid;gap:18px}
h1{font-size:22px;margin:0}h2{font-size:16px;margin:8px 0 0}
.sub{color:var(--muted);font-size:13px;margin:2px 0 0}
.chips{display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;position:sticky;top:0;background:var(--bg);padding:8px 0;z-index:2}
.chips::-webkit-scrollbar{display:none}.chip{flex:none}
.chip{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:6px 12px;
font:600 13px inherit;cursor:pointer}.chip[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:var(--card)}
.list{display:grid;gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 14px;display:grid;gap:4px}
.card.star{border-color:var(--accent);box-shadow:inset 3px 0 0 var(--accent)}
.top{display:flex;justify-content:space-between;gap:10px;align-items:baseline}
.card a.t{color:var(--ink);font-weight:700;text-decoration:none}.card a.t:hover{text-decoration:underline}
.when{white-space:nowrap;color:var(--muted);font:600 12px ui-monospace,Menlo,monospace}
.meta{display:flex;gap:6px;flex-wrap:wrap;font-size:12.5px;color:var(--muted)}
.pill{font-size:11.5px;font-weight:700;padding:1px 7px;border-radius:6px;background:var(--soft);color:var(--ink)}
.pill.dl{background:var(--sunsoft);color:var(--sun)}
.sum{font-size:13.5px}.src{font-size:12px}.src a{color:var(--accent)}
.empty{color:var(--muted);padding:20px;text-align:center}
footer{color:var(--muted);font-size:12px}
"""

JS = """
const chips=[...document.querySelectorAll('.chip')],cards=[...document.querySelectorAll('.card')];
const state={};
function apply(){const on=chips.filter(c=>c.getAttribute('aria-pressed')==='true');
 const groups={};on.forEach(c=>(groups[c.dataset.g]=groups[c.dataset.g]||[]).push(c.dataset.v));
 let shown=0;cards.forEach(card=>{const ok=Object.entries(groups).every(([g,vs])=>vs.some(v=>(card.dataset[g]||'').split(' ').includes(v)));
  card.hidden=!ok;if(ok)shown++});document.getElementById('count').textContent=shown;}
chips.forEach(c=>c.addEventListener('click',()=>{c.setAttribute('aria-pressed',c.getAttribute('aria-pressed')!=='true');apply()}));
apply();
"""


def _place_tags(e):
    city = (e.get("city") or "").lower()
    tags = []
    if "алматы" in city or "almaty" in city:
        tags.append("almaty")
    if "астана" in city or "astana" in city:
        tags.append("astana")
    if e.get("online") or "онлайн" in city or "online" in city:
        tags.append("online")
    return tags or ["other"]


def _days_to(d, today):
    try:
        return (date.fromisoformat(d[:10]) - today).days
    except Exception:
        return None


def build(events, today_dt, blocked=lambda e: False, is_stale=lambda e, t: False):
    today = today_dt.date()
    t_iso = today.isoformat()
    live = [e for e in events if e["sent_at"] != "dupe" and not blocked(e) and not is_stale(e, t_iso)
            and (e["score"] >= 5 or e["feedback"] == "up")]

    def sort_key(e):  # starred first, then the nearest future date (deadline, start or end)
        future = [e[k][:10] for k in ("deadline", "start_date", "end_date") if e.get(k) and e[k][:10] >= t_iso]
        return (e["feedback"] != "up", future[0] if future else "9999", -e["score"])
    live.sort(key=sort_key)
    new_cut = (store.now() - timedelta(hours=26)).isoformat()
    cards = []
    for e in live:
        dl_days = _days_to(e.get("deadline") or "", today)
        soon = dl_days is not None and 0 <= dl_days <= 7
        tags_place = _place_tags(e)
        kind = e["kind"]
        pills = []
        if e["feedback"] == "up":
            pills.append('<span class="pill">⭐ отмечено</span>')
        if e["first_seen"] >= new_cut:
            pills.append('<span class="pill">новое</span>')
        if e.get("deadline"):
            left = {0: " (сегодня)", 1: " (завтра)"}.get(dl_days, f" ({dl_days} дн.)" if soon else "")
            pills.append(f'<span class="pill dl">подать до {tg.short_date(e["deadline"])}{left}</span>')
        if e.get("prize"):
            pills.append(f"<span>🏆 {html.escape(e['prize'][:70])}</span>")
        flags = [tg.FLAG_SHORT[f] for f in e.get("flags", []) if f in tg.FLAG_SHORT]
        link = e.get("link") or e.get("post_url") or ""
        extra = f" и ещё {len(e['sources']) - 1}" if len(e.get("sources", [])) > 1 else ""
        cards.append(
            f'<article class="card{" star" if e["feedback"] == "up" else ""}" data-place="{" ".join(tags_place)}" '
            f'data-kind="{kind}" data-when="{"soon" if soon else "later"}{" star" if e["feedback"] == "up" else ""}">'
            f'<div class="top"><a class="t" href="{tg.attr(link)}">{tg.KIND_ICON.get(kind, "📌")} {html.escape(e["title"])}</a>'
            f'<span class="when">{html.escape(tg.short_when(e))}</span></div>'
            f'<div class="meta"><span>{html.escape(tg.place(e) or "—")}</span><span>· {KIND_RU.get(kind, kind)}</span>'
            f'{"".join(pills)}</div>'
            f'<div class="sum">{html.escape(e.get("summary", ""))}</div>'
            + (f'<div class="meta">{html.escape(" · ".join(flags))}</div>' if flags else "")
            + f'<div class="src"><a href="{tg.attr(e.get("post_url") or link)}">Источник: {html.escape(e.get("source", ""))}</a>'
            f'{extra}</div></article>')

    def chip(group, value, label, on=False):
        return f'<button class="chip" data-g="{group}" data-v="{value}" aria-pressed="{str(on).lower()}">{label}</button>'
    chips = "".join([
        chip("when", "star", "⭐ Отмеченное"), chip("when", "soon", "⏰ Дедлайн ≤ 7 дней"),
        chip("place", "almaty", "Алматы"), chip("place", "astana", "Астана"), chip("place", "online", "Онлайн"),
        chip("kind", "hackathon", "Хакатоны"), chip("kind", "startup", "Стартапы"), chip("kind", "case", "Кейсы"),
        chip("kind", "contest", "Конкурсы"), chip("kind", "meetup", "Митапы"), chip("kind", "forum", "Форумы"),
        chip("kind", "freebie", "Халява")])
    stamp = f"{today_dt.day} {tg.MONTHS[today_dt.month - 1]}, {today_dt:%H:%M}"
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Лента возможностей</title><style>{CSS}</style></head><body><div class="wrap">
<header><h1>Лента возможностей</h1><p class="sub">Всё живое, что нашёл бот: <b id="count">{len(cards)}</b>.
Обновлено {stamp} (Алматы). Сверху отмеченное ⭐, дальше по ближайшему дедлайну.</p></header>
<nav class="chips" aria-label="Фильтры">{chips}</nav>
<section class="list">{''.join(cards) or '<p class="empty">Пока пусто.</p>'}</section>
<footer>Фильтры внутри одной группы складываются («Алматы» + «Онлайн»), между группами сужают.
Отметить ⭐ можно кнопкой с номером под утренним дайджестом.</footer>
</div><script>{JS}</script></body></html>"""


def write(events, today_dt, blocked, is_stale):
    page = build(events, today_dt, blocked, is_stale)
    store.save_text("feed.html", page)
    return page
