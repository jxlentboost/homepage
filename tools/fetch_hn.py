#!/usr/bin/env python3
"""Fetch Hacker News via the official Firebase API and write hn.js for homepage.html.

Same contract as the other fetchers here: the homepage is opened as a file:// page
where Safari blocks fetch/XHR but still loads sibling <script> tags, so this writes
a plain JS file (window.HP_HN = {...}) to disk. Nothing runs in the browser and no
request leaves this machine at page-load time.

The API (https://github.com/HackerNews/API) gives one id list per feed, then one
request per item — so item lookups are pooled and cached across feeds, since a
story usually appears in several of them.

Run by hand:  python3 ~/homepage/tools/fetch_hn.py
"""

import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
os.makedirs(DATA, exist_ok=True)
OUT = os.path.join(DATA, "hn.js")

BASE = "https://hacker-news.firebaseio.com/v0"
ITEM_URL = "https://news.ycombinator.com/item?id={}"
TIMEOUT = 15
WORKERS = 12
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) homepage-hn/1.0"

# (tab label, API list, how many to keep). Reorder or retune freely.
FEEDS = [
    ("Top",  "topstories",  24),
    ("Best", "beststories", 24),
    ("New",  "newstories",  18),
    ("Ask",  "askstories",  12),
    ("Show", "showstories", 12),
]


def get(path):
    req = urllib.request.Request(f"{BASE}/{path}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)


def domain(url):
    """news.ycombinator.com style short source label."""
    if not url:
        return ""
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def shape(item):
    """One API item -> the flat shape the page renders."""
    url = item.get("url") or ""
    return {
        "id": item["id"],
        "title": item.get("title", "(untitled)"),
        # Self-posts (most Ask HN) have no url — point them at the thread.
        "url": url or ITEM_URL.format(item["id"]),
        "comments": ITEM_URL.format(item["id"]),
        "domain": domain(url) or "news.ycombinator.com",
        "by": item.get("by", ""),
        "score": int(item.get("score", 0) or 0),
        "kids": int(item.get("descendants", 0) or 0),
        "ts": int(item.get("time", 0) or 0),
        "self": not url,
    }


def main():
    errors = []
    cache = {}

    # One id list per feed, fetched in parallel.
    def ids_for(feed):
        label, listname, keep = feed
        try:
            return label, (get(f"{listname}.json") or [])[:keep], None
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as err:
            return label, [], f"{label}: {err}"

    with ThreadPoolExecutor(max_workers=len(FEEDS)) as pool:
        listings = list(pool.map(ids_for, FEEDS))

    wanted = []
    for _label, ids, err in listings:
        if err:
            errors.append(err)
        for i in ids:
            if i not in cache:
                cache[i] = None
                wanted.append(i)

    # Then one request per unique item, shared across feeds.
    def fetch_item(item_id):
        try:
            return item_id, get(f"item/{item_id}.json")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
            return item_id, None

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for item_id, item in pool.map(fetch_item, wanted):
            # Dead or deleted items come back null.
            if item and not item.get("deleted") and not item.get("dead"):
                cache[item_id] = shape(item)

    groups = []
    for label, ids, _err in listings:
        items = [cache[i] for i in ids if cache.get(i)]
        if items:
            groups.append({"name": label, "items": items})

    missing = sum(1 for i in wanted if not cache.get(i))
    if missing:
        errors.append(f"{missing} item(s) unavailable")

    if not groups:
        print("no Hacker News data could be fetched; leaving hn.js untouched", file=sys.stderr)
        for err in errors:
            print("  ", err, file=sys.stderr)
        return 1

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "groups": groups,
        "errors": errors,
    }

    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("window.HP_HN = ")
        json.dump(payload, fh, ensure_ascii=False)
        fh.write(";\n")
    os.replace(tmp, OUT)  # atomic, so the page never loads a half-written file

    total = sum(len(g["items"]) for g in groups)
    print(f"wrote {total} HN stories across {len(groups)} feeds "
          f"({len(wanted)} items fetched) -> {OUT}")
    for err in errors:
        print("  warning:", err, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
