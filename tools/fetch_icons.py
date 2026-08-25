#!/usr/bin/env python3
"""Cache each shortcut site's logo and write icons.js for index.html.

Same contract as the other fetchers: the page makes no network request of its
own, so logos are downloaded here and referenced from disk.

Finding a genuinely high-resolution logo is the whole problem. Scraping a site's
own <link rel="icon"> works for some and fails badly for others — Gmail, Calendar
and Drive all redirect to the Google accounts login page, which advertises no
product icon, and claude.ai returns 403 to a script. The generic favicon services
answer for every domain but often only at 32x32, which is soft on a retina tile
(rendered at 34pt, so ~68px of real pixels).

So: a small table of hand-checked sources for sites worth getting right, then a
generic chain for everything else. Each candidate is tried in turn and the first
that returns a real image wins, so a stale entry degrades instead of breaking.

Run by hand:  python3 ~/homepage/tools/fetch_icons.py
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urljoin, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
os.makedirs(DATA, exist_ok=True)

sys.path.insert(0, HERE)
import fetch_news  # download(), bucket_dir(), prune() — same cache machinery

CONF = os.path.join(HERE, "shortcuts.conf")
OUT = os.path.join(DATA, "icons.js")
BUCKET = "icons"
TIMEOUT = 20
WORKERS = 7
UA = fetch_news.BROWSER_UA

# Hand-checked, in preference order. Verified resolutions in the comments.
KNOWN = {
    "github.com": [
        "https://github.githubassets.com/favicons/favicon.svg",          # vector
        "https://github.com/fluidicon.png",                              # 512px
    ],
    "mail.google.com":     ["https://www.gstatic.com/images/branding/product/2x/gmail_2020q4_48dp.png"],     # 96px
    "calendar.google.com": ["https://www.gstatic.com/images/branding/product/2x/calendar_2020q4_48dp.png"],  # 96px
    "drive.google.com":    ["https://www.gstatic.com/images/branding/product/2x/drive_2020q4_48dp.png"],     # 96px
    "maps.google.com":     ["https://www.gstatic.com/images/branding/product/2x/maps_96dp.png"],             # 192px
    "youtube.com":         ["https://www.youtube.com/s/desktop/8a76fb04/img/favicon_144x144.png"],           # 144px
    "claude.ai":           ["https://www.google.com/s2/favicons?domain=claude.ai&sz=256",                    # 248px
                            "https://claude.ai/apple-touch-icon.png"],                                       # 180px
}

LINK = re.compile(r"<link[^>]+>", re.I)
REL = re.compile(r'rel\s*=\s*["\']([^"\']+)["\']', re.I)
HREF = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.I)
SIZES = re.compile(r'sizes\s*=\s*["\'](\d+)x\d+["\']', re.I)


def host_of(url):
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def read_conf():
    out = []
    with open(CONF, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) != 2:
                print(f"skipping malformed line: {line}", file=sys.stderr)
                continue
            out.append({"name": parts[0], "url": parts[1]})
    return out


def scraped(site_url):
    """Largest <link rel=icon> the site itself advertises."""
    try:
        req = urllib.request.Request(site_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            final = resp.geturl()
            body = resp.read(400_000).decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return []

    found = []
    for tag in LINK.findall(body):
        rel = REL.search(tag)
        href = HREF.search(tag)
        if not rel or not href or "icon" not in rel.group(1).lower():
            continue
        size = SIZES.search(tag)
        px = int(size.group(1)) if size else 0
        if "apple-touch" in rel.group(1).lower():
            px = max(px, 180)
        found.append((px, urljoin(final, unescape(href.group(1)))))
    found.sort(reverse=True)
    return [u for _px, u in found]


def candidates(site_url):
    host = host_of(site_url)
    root = f"{urlsplit(site_url).scheme}://{urlsplit(site_url).netloc}"
    return (
        KNOWN.get(host, [])
        + [f"{root}/apple-touch-icon.png"]
        + scraped(site_url)
        + [f"https://www.google.com/s2/favicons?domain={host}&sz=256"]
    )


def resolve(entry):
    """First candidate that actually downloads wins."""
    host = host_of(entry["url"])
    for url in candidates(entry["url"]):
        href, filename = fetch_news.download(url, referer=entry["url"], bucket=BUCKET)
        if href:
            return host, href, filename, url
    return host, "", "", ""


def main():
    sites = read_conf()
    if not sites:
        print("no shortcuts configured", file=sys.stderr)
        return 1

    fetch_news.bucket_dir(BUCKET)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(resolve, sites))

    icons, kept, misses = {}, set(), []
    for entry, (host, href, filename, source) in zip(sites, results):
        if href:
            icons[host] = href
            kept.add(filename)
            print(f"  {entry['name']:9} {host:22} <- {source[:64]}")
        else:
            misses.append(entry["name"])

    dropped = fetch_news.prune(kept, bucket=BUCKET)

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # The page uses this as its default tiles, so shortcuts.conf stays the
        # single source of truth rather than being duplicated in the HTML.
        "defaults": sites,
        "icons": icons,
    }

    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("window.HP_ICONS = ")
        json.dump(payload, fh, ensure_ascii=False)
        fh.write(";\n")
    os.replace(tmp, OUT)  # atomic, so the page never loads a half-written file

    print(f"wrote {len(icons)}/{len(sites)} shortcut logos ({dropped} pruned) -> {OUT}")
    if misses:
        print("  no logo found for:", ", ".join(misses), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
