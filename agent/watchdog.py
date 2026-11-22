"""Watchdog, run once a day by a separate cloud task: if the bot has not run for too long, say so in Telegram.

python -m agent.watchdog            (reads state/meta.json of the newest main from GitHub)
The bot itself can't report that it never started, so this check lives outside its scheduled session.
"""
import sys
from datetime import datetime, timedelta

from . import gitstate, store
from .main import load_env_file, notify_failure

MAX_SILENCE_HOURS = 30


def main():
    load_env_file()
    gitstate.refresh()
    meta = store.load("meta.json", {})
    last = meta.get("last_run")
    try:
        ago = store.now() - datetime.fromisoformat(last) if last else None
    except ValueError:
        ago = None
    if ago is not None and ago < timedelta(hours=MAX_SILENCE_HOURS):
        print(f"WATCHDOG OK: last run {int(ago.total_seconds() // 3600)} h ago ({meta.get('runner') or '?'})")
        return 0
    hours = f"{int(ago.total_seconds() // 3600)} ч" if ago else "неизвестно сколько"
    notify_failure(meta, f"бот не запускался уже {hours}. Скорее всего остановились облачные запуски. "
                         "Напиши Claude в сессии про бота, он починит. Запасной вариант: запусти run_digest.ps1 на ноутбуке.")
    print(f"WATCHDOG ALERT: no run for {hours}")
    return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
