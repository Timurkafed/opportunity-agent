"""Collectors: turn every source into a list of raw items.

Raw item = {"uid", "source", "url", "date", "text", "links"}
  uid   — stable id, used to skip things already processed
  text  — plain text that Gemini will read
  links — external links found in the item (Gemini may pick one as the registration link)
"""
import concurrent.futures as cf
import hashlib
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from .net import fetch, fetch_json

MAX_TEXT = 1500  # characters per post sent to Gemini


def clean_html(s):
    s = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)\b.*?</\1>", " ", s or "")
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"[ \t ]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def external_links(fragment, skip=("t.me/s/", "telegram.org")):
    out = []
    for u in re.findall(r'href="(https?://[^"]+)"', fragment or ""):
        u = html.unescape(u)
        if any(x in u for x in skip) or u in out:
            continue
        out.append(u)
    return out[:5]


# ---------------------------------------------------------------- Telegram
def _tg_page(channel, before=None):
    url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
    body = fetch(url)
    posts = []
    for block in re.split(r'(?=<div class="tgme_widget_message_wrap)', body)[1:]:
        m = re.search(r'data-post="([^"/]+)/(\d+)"', block)
        if not m:
            continue
        pid = int(m.group(2))
        t = re.search(r'<time datetime="([^"]+)"', block)
        txt = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        if not txt:
            continue  # photos/stickers without text
        frag = txt.group(1)
        posts.append({
            "uid": f"tg:{channel.lower()}/{pid}",
            "source": "@" + channel,
            "url": f"https://t.me/{channel}/{pid}",
            "date": t.group(1) if t else "",
            "text": clean_html(frag)[:MAX_TEXT],
            "links": external_links(frag),
            "_pid": pid,
        })
    return posts


def telegram_channel(channel, seen, since):
    """Newest posts first page; one more page if everything there is new and recent."""
    out = []
    try:
        page = _tg_page(channel)
        out += page
        if page and all(p["uid"] not in seen for p in page):
            oldest = min(p["_pid"] for p in page)
            dates = [p["date"] for p in page if p["date"]]
            if dates and min(dates) > since:
                out += _tg_page(channel, before=oldest)
    except Exception as e:
        print(f"  ! {channel}: {e}")
    fresh = [p for p in out if p["uid"] not in seen and (p["date"] or "9") >= since and len(p["text"]) > 40]
    for p in fresh:
        p.pop("_pid", None)
    return fresh


# ---------------------------------------------------------------- sites
def astanahub(cfg, seen, since):
    items, cursor = [], None
    keep = set(cfg.get("keep_cities", []))
    for _ in range(cfg.get("pages", 3)):
        url = "https://astanahub.com/api/event/timeline/?period=upcoming" + (f"&cursor={cursor}" if cursor else "")
        j = fetch_json(url)
        for e in j.get("results", []):
            city = e.get("city") or ""
            fmt = e.get("event_format") or ""
            if city not in keep and fmt not in ("online", "hybrid"):
                continue
            uid = f"ah:{e['id']}"
            if uid in seen:
                continue
            title = (e.get("title") or {}).get("ru") or (e.get("title") or {}).get("en") or ""
            content = (e.get("content") or {}).get("ru") or (e.get("content") or {}).get("en") or ""
            text = (f"{title}\nГород: {city or '—'}; формат: {fmt}; тип: {e.get('event_type')}; "
                    f"начало: {e.get('datetime_start')}; конец: {e.get('datetime_end')}\n{clean_html(content)}")
            links = [l for l in [e.get("form_link"), e.get("conference_link")] if l]
            items.append({"uid": uid, "source": cfg["name"], "url": e.get("absolute_url") or "https://astanahub.com/ru/event/",
                          "date": e.get("created_at") or "", "text": text[:MAX_TEXT], "links": links})
        cursor = j.get("next_cursor")
        if not j.get("has_next") or not cursor:
            break
    return items


def devpost(cfg, seen, since):
    items = []
    for page in range(1, cfg.get("pages", 2) + 1):
        j = fetch_json(f"https://devpost.com/api/hackathons?status[]=open&status[]=upcoming&order_by=recently-added&page={page}")
        for h in j.get("hackathons", []):
            uid = f"dp:{h['id']}"
            if uid in seen:
                continue
            prize = clean_html(h.get("prize_amount") or "")
            themes = ", ".join(t["name"] for t in h.get("themes", []))
            text = (f"Хакатон Devpost: {h['title']}\nОрганизатор: {h.get('organization_name')}\n"
                    f"Где: {h['displayed_location'].get('location')}\nПриём работ: {h.get('submission_period_dates')} "
                    f"({h.get('time_left_to_submission')})\nПризы: {prize}\nТемы: {themes}\n"
                    f"Участников: {h.get('registrations_count')}; только по приглашению: {h.get('invite_only')}")
            items.append({"uid": uid, "source": "Devpost", "url": h["url"], "date": "", "text": text, "links": []})
    return items


def unstop(cfg, seen, since):
    items = []
    for kind in cfg.get("kinds", ["hackathons"]):
        for page in range(1, cfg.get("pages", 1) + 1):
            j = fetch_json(f"https://unstop.com/api/public/opportunity/search-result?opportunity={kind}"
                           f"&oppstatus=open&per_page=30&page={page}")["data"]
            for e in j.get("data", []):
                if e.get("region") != "online":
                    continue
                uid = f"un:{e['id']}"
                if uid in seen:
                    continue
                prizes = "; ".join(f"{p.get('rank')}: {p.get('cash')} {p.get('currencyCode')}" for p in (e.get("prizes") or [])[:3])
                req = e.get("regnRequirements") or {}
                elig = ", ".join(f.get("name", "") for f in (e.get("filters") or []) if f.get("type") == "eligible")
                text = (f"Unstop ({e.get('subtype')}): {e['title']}\nОрганизатор: {(e.get('organisation') or {}).get('name')}\n"
                        f"Онлайн. Регистрация до: {req.get('end_regn_dt')}\nКоманда: {req.get('min_team_size')}–{req.get('max_team_size')}\n"
                        f"Кому: {elig}\nПризы: {prizes}\n{clean_html(e.get('details') or '')[:600]}")
                items.append({"uid": uid, "source": "Unstop", "url": "https://unstop.com/" + e["public_url"],
                              "date": e.get("updated_at") or "", "text": text[:MAX_TEXT], "links": []})
    return items


def bevy(cfg, seen, since):
    items = []
    now = datetime.now(timezone.utc).isoformat()
    for e in fetch_json(cfg["url"]).get("results", []):
        if (e.get("start_date") or "") < now:
            continue
        uid = f"bevy:{e['id']}"
        if uid in seen:
            continue
        url = e.get("static_url") or e.get("url") or cfg["base"]
        text = (f"{cfg['name']}: {e.get('title')}\nНачало: {e.get('start_date')}; формат: {e.get('audience_type')}\n"
                f"{clean_html(e.get('description_short') or e.get('description') or '')[:800]}")
        items.append({"uid": uid, "source": cfg["name"], "url": url, "date": "", "text": text, "links": []})
    return items


def rss(cfg, seen, since):
    root = ET.fromstring(fetch(cfg["url"]).encode("utf-8"))
    items = []
    flt = (cfg.get("title_filter") or "").lower()
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if flt and flt not in title.lower():
            continue
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link).strip()
        uid = "rss:" + hashlib.sha1(guid.encode()).hexdigest()[:16]
        if uid in seen:
            continue
        body = it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or it.findtext("description") or ""
        limit = 6000 if flt else MAX_TEXT  # weekly digests are long and hold many events
        items.append({"uid": uid, "source": cfg["name"], "url": link, "date": it.findtext("pubDate") or "",
                      "text": (title + "\n" + clean_html(body))[:limit], "links": external_links(body)})
        if len(items) >= cfg.get("limit", 15):
            break
    return items


def page(cfg, seen, since):
    """Whole page as one item; re-read only when its text changed."""
    raw = fetch(cfg["url"])
    text = clean_html(raw)
    if len(text) < 1500:  # pages rendered by JavaScript: pull names out of embedded JSON
        names = list(dict.fromkeys(re.findall(r'"(?:name|title)":"([^"\\]{6,90})"', raw)))
        text += "\n" + "\n".join(names[:80])
    text = text[:8000]
    uid = "page:" + hashlib.sha1((cfg["url"] + text).encode()).hexdigest()[:16]
    if uid in seen:
        return []
    return [{"uid": uid, "source": cfg["name"], "url": cfg["url"], "date": "", "text": text, "links": [], "multi": True}]


SITE_TYPES = {"astanahub": astanahub, "devpost": devpost, "unstop": unstop, "bevy": bevy, "rss": rss, "page": page}


def collect_all(sources, seen, days_back):
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    items, stats = [], {}
    with cf.ThreadPoolExecutor(5) as ex:
        futs = {ex.submit(telegram_channel, ch, seen, since): "@" + ch for ch in sources["telegram"]}
        for cfg in sources["sites"]:
            futs[ex.submit(_safe_site, cfg, seen, since)] = cfg["name"]
        for f in cf.as_completed(futs):
            got = f.result()
            stats[futs[f]] = len(got)
            items += got
    return items, stats


def _safe_site(cfg, seen, since):
    try:
        return SITE_TYPES[cfg["type"]](cfg, seen, since)
    except Exception as e:
        print(f"  ! {cfg['name']}: {e}")
        return []
