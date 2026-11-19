"""Small HTTP helpers on the standard library (no extra packages to install)."""
import json
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def fetch(url, *, data=None, headers=None, timeout=30, retries=2):
    """GET (or POST when data is given). Returns text. Raises on final failure."""
    hdrs = {"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"}
    hdrs.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 401, 403, 404):
                raise
        except Exception as e:  # timeouts, resets
            last = e
        time.sleep(2 * (attempt + 1))
    raise last


def fetch_json(url, **kw):
    return json.loads(fetch(url, **kw))
