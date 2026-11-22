"""Bot memory: plain JSON files in state/, committed back to the repo after each run."""
import json
import os
from datetime import datetime, timedelta, timezone

STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")


def _path(name):
    return os.path.join(STATE_DIR, name)


def load(name, default):
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(name, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = _path(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(name))


def save_text(name, text):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = _path(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, _path(name))


def now():
    return datetime.now(timezone.utc)


def prune_seen(seen, days=45):
    cut = (now() - timedelta(days=days)).isoformat()
    return {k: v for k, v in seen.items() if v >= cut}


def prune_events(events, days=60):
    """Forget events that are long gone (keep feedback-rated ones a bit longer for learning)."""
    cut = (now() - timedelta(days=days)).isoformat()
    cut_rated = (now() - timedelta(days=180)).isoformat()
    return [e for e in events if e["first_seen"] >= (cut_rated if e.get("feedback") else cut)]
