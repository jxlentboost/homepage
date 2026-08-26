#!/usr/bin/env python3
"""Fetch the feeds listed in sports.conf and write sports.js for index.html.

Same contract as the other fetchers: the page makes no network request of its
own, so headlines and photos are written to disk here.

This reuses fetch_news.py's RSS reader and image cache rather than repeating
them. Images go in their own bucket, so pruning here can never touch the news
section's cache.

Run by hand:  python3 ~/homepage/tools/fetch_sports.py
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
os.makedirs(DATA, exist_ok=True)

sys.path.insert(0, HERE)
import fetch_news

CONF = os.path.join(HERE, "sports.conf")
OUT = os.path.join(DATA, "sports.js")
BUCKET = "sports"

PER_FEED = 8      # most recent items taken from each feed
PER_TAB = 12      # headlines kept per tab after merging


def read_conf():
    feeds = []
    with open(CONF, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) != 3:
                print(f"skipping malformed line: {line}", file=sys.stderr)
                continue
            feeds.append(tuple(parts))
    return feeds


def main():
    feeds = read_conf()
    if not feeds:
        print("no sports feeds configured", file=sys.stderr)
        return 1

    fetch_news.PER_FEED = PER_FEED
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch_news.fetch, feeds))

    order, buckets = [], {}
    for tab, _name, _url in feeds:
        if tab not in buckets:
            order.append(tab)
            buckets[tab] = []

    errors = []
    for tab, _source, items, err in results:
        if err:
            errors.append(err)
        buckets[tab].extend(items)

    groups = []
    for tab in order:
        items = sorted(buckets[tab], key=lambda i: i["ts"], reverse=True)
        # Interleave sources so one prolific feed can't own a whole tab.
        spread, seen = [], {}
        for item in items:
            rank = seen.get(item["source"], 0)
            seen[item["source"]] = rank + 1
            spread.append((rank, item))
        spread.sort(key=lambda pair: (pair[0], -pair[1]["ts"]))
        groups.append({"name": tab, "items": [i for _r, i in spread[:PER_TAB]]})

    # Yahoo and ESPN publish no media tags, so those items need their article's
    # og:image; CBS and the motorsport outlets carry images in the feed itself.
    shown = [item for g in groups for item in g["items"]]
    kept = fetch_news.attach_images(shown, bucket=BUCKET, scrape=True)
    dropped = fetch_news.prune(kept, bucket=BUCKET)
    with_image = sum(1 for i in shown if i["img"])

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "groups": groups,
        "errors": errors,
    }

    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("window.HP_SPORTS = ")
        json.dump(payload, fh, ensure_ascii=False)
        fh.write(";\n")
    os.replace(tmp, OUT)  # atomic, so the page never loads a half-written file

    print(f"wrote {len(shown)} sports headlines across {len(groups)} tabs, "
          f"{with_image} with images ({dropped} pruned) -> {OUT}")
    for err in errors:
        print("  warning:", err, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
