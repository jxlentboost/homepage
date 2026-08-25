#!/usr/bin/env python3
"""Fetch the RSS feeds listed in feeds.conf and write news.js for homepage.html.

Output is a plain JS file (window.HP_NEWS = {...}) rather than JSON because the
homepage is opened as a file:// page, where Safari blocks fetch/XHR but still
loads sibling <script> tags. Nothing here runs in the browser and no request
leaves this machine at page-load time — the page only reads what this script
already wrote to disk.

That last point is why images are cached locally rather than hotlinked. Remote
images do load on a file:// page, but some CDNs refuse them: the Guardian's
i.guim.co.uk serves fine to this script and rejects the browser's file:// origin.
Downloading here keeps the page self-contained, working offline, and free of
per-source hotlink surprises.

Images come from the feed's own media tags where present, upgraded to the largest
resolution each source offers. Feeds that publish no media (NPR, Al Jazeera, The
Verge, TechCrunch, ESPN, Yahoo) get one extra request per story to read the
article's og:image.

Run by hand:  python3 ~/homepage/tools/fetch_news.py
"""

import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
os.makedirs(DATA, exist_ok=True)
CONF = os.path.join(HERE, "feeds.conf")
OUT = os.path.join(DATA, "news.js")
IMG_DIR = os.path.join(DATA, "img")
# Path the page uses, relative to index.html rather than to this script.
IMG_HREF = "data/img"
# Each caller owns a subdirectory. Pruning is scoped to one bucket, so the F1
# fetcher reusing this module can never delete the news section's images.
NEWS_BUCKET = "news"

PER_FEED = 6          # most recent items taken from each feed
PER_CATEGORY = 12     # headlines kept per category after merging
TIMEOUT = 15
SCRAPE_TIMEOUT = 12   # per article page, when hunting for an og:image
IMG_TIMEOUT = 25
WORKERS = 8
MAX_IMG_BYTES = 6_000_000
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 homepage-rss/1.0"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

IMG_EXT = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
           "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif"}


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


def strip_ns(tag):
    return tag.split("}", 1)[-1]


def parse_when(text):
    """RSS uses RFC 822 dates, Atom uses ISO 8601. Accept either."""
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def clean(text):
    """Feed titles arrive with entities and the occasional stray tag."""
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).strip()


def upgrade(url):
    """Ask each source for the biggest version it will serve.

    BBC encodes the width in the path, so it can simply be raised. The Guardian
    signs every width separately (&s=<hash>), so its URLs must be used exactly as
    published — the feed's own largest variant is the ceiling there.
    """
    if "ichef.bbci.co.uk" in url:
        return re.sub(r"/(?:standard|news|ace/standard)/\d+/", "/ace/standard/1024/", url)
    return url


def pick_image(entry):
    """Largest usable image declared by the item's media tags."""
    best, best_w = "", -1
    for child in entry.iter():
        tag = strip_ns(child.tag)
        if tag not in ("thumbnail", "content", "enclosure", "image"):
            continue
        url = (child.get("url") or child.get("href") or "").strip()
        if not url:
            continue
        mime = (child.get("type") or "").lower()
        looks_image = mime.startswith("image/") or re.search(r"\.(jpe?g|png|webp|avif)\b", url, re.I)
        if not looks_image:
            continue
        try:
            width = int(child.get("width") or 0)
        except ValueError:
            width = 0
        if width > best_w:
            best, best_w = url, width
    return upgrade(best) if best else ""


def extract(entry):
    title = link = when = None
    for child in entry:
        tag = strip_ns(child.tag)
        if tag == "title" and title is None:
            title = clean("".join(child.itertext()))
        elif tag == "link" and not link:
            # RSS puts the URL in the text, Atom in an href attribute.
            href = child.get("href")
            rel = child.get("rel", "alternate")
            if href:
                if rel == "alternate":
                    link = href.strip()
            elif child.text:
                link = child.text.strip()
        elif tag in ("pubDate", "published", "updated", "date") and when is None:
            when = parse_when("".join(child.itertext()))
    return title, link, when, pick_image(entry)


def fetch(feed):
    """One feed -> its most recent items. Deliberately does no image I/O, so
    fetch_f1.py can reuse this without paying for downloads."""
    category, source, url = feed
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as err:
        return category, source, [], f"{source}: {err}"

    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as err:
        return category, source, [], f"{source}: unparseable feed ({err})"

    items = []
    for entry in root.iter():
        if strip_ns(entry.tag) not in ("item", "entry"):
            continue
        title, link, when, img = extract(entry)
        if not title or not link:
            continue
        items.append({
            "title": title,
            "url": link,
            "source": source,
            "ts": when.timestamp() if when else 0,
            "img": img,
        })
        if len(items) >= PER_FEED:
            break

    if not items:
        return category, source, [], f"{source}: no items found"
    return category, source, items, None


OG_RE = re.compile(
    r'<meta[^>]+(?:property|name)\s*=\s*["\'](?:og:image(?::secure_url|:url)?|twitter:image(?::src)?)["\'][^>]*>',
    re.I)
CONTENT_RE = re.compile(r'content\s*=\s*["\']([^"\']+)["\']', re.I)


def scrape_og(url):
    """One article page -> its social preview image, if it advertises one."""
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=SCRAPE_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "html" not in ctype:
                return ""
            head = resp.read(200_000).decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return ""
    for tag in OG_RE.findall(head):
        found = CONTENT_RE.search(tag)
        if found:
            candidate = html.unescape(found.group(1)).strip()
            if candidate.startswith("//"):
                candidate = "https:" + candidate
            if candidate.startswith("http"):
                return candidate
    return ""


def bucket_dir(bucket):
    path = os.path.join(IMG_DIR, bucket)
    os.makedirs(path, exist_ok=True)
    return path


def download(url, referer="", bucket=NEWS_BUCKET):
    """Cache one image under img/<bucket>/, named by a hash of its URL. Returns
    the page-relative path, or "" if it could not be stored."""
    folder = bucket_dir(bucket)
    name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    for existing in os.listdir(folder):
        if existing.startswith(name + "."):
            return f"{IMG_HREF}/{bucket}/{existing}", existing

    headers = {"User-Agent": BROWSER_UA, "Accept": "image/*,*/*"}
    if referer:
        headers["Referer"] = referer
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=IMG_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not ctype.startswith("image/"):
                return "", ""
            data = resp.read(MAX_IMG_BYTES + 1)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return "", ""
    if not data or len(data) > MAX_IMG_BYTES:
        return "", ""

    filename = name + IMG_EXT.get(ctype, ".jpg")
    path = os.path.join(folder, filename)
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except OSError:
        return "", ""
    return f"{IMG_HREF}/{bucket}/{filename}", filename


def attach_images(items, bucket=NEWS_BUCKET, scrape=True):
    """Fill in each item's local image, scraping og:image where the feed had none."""
    bucket_dir(bucket)

    needs_scrape = [i for i in items if not i["img"]] if scrape else []
    if needs_scrape:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for item, found in zip(needs_scrape, pool.map(lambda i: scrape_og(i["url"]), needs_scrape)):
                item["img"] = found

    have = [i for i in items if i["img"]]
    kept = set()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = pool.map(lambda i: download(i["img"], referer=i["url"], bucket=bucket), have)
        for item, (href, filename) in zip(have, results):
            item["img"] = href
            if filename:
                kept.add(filename)
    for item in items:
        if not item["img"].startswith(IMG_HREF):
            item["img"] = ""
    return kept


def prune(keep, bucket=NEWS_BUCKET):
    """Drop cached images in this bucket no longer referenced by current items."""
    folder = bucket_dir(bucket)
    removed = 0
    for name in os.listdir(folder):
        if name in keep:
            continue
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed


def main():
    feeds = read_conf()
    if not feeds:
        print("no feeds configured", file=sys.stderr)
        return 1

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(fetch, feeds))

    # Preserve the category order given in feeds.conf.
    order, buckets = [], {}
    for category, _, _ in feeds:
        if category not in buckets:
            order.append(category)
            buckets[category] = []

    errors = []
    for category, _source, items, err in results:
        if err:
            errors.append(err)
        buckets[category].extend(items)

    groups = []
    for category in order:
        items = sorted(buckets[category], key=lambda i: i["ts"], reverse=True)
        # Interleave sources so one prolific feed can't own the whole column.
        spread, seen_counts = [], {}
        for item in items:
            rank = seen_counts.get(item["source"], 0)
            seen_counts[item["source"]] = rank + 1
            spread.append((rank, item))
        spread.sort(key=lambda pair: (pair[0], -pair[1]["ts"]))
        groups.append({
            "name": category,
            "items": [item for _rank, item in spread[:PER_CATEGORY]],
        })

    shown = [item for g in groups for item in g["items"]]
    kept = attach_images(shown)
    dropped = prune(kept)
    with_image = sum(1 for i in shown if i["img"])

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "groups": groups,
        "errors": errors,
    }

    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("window.HP_NEWS = ")
        json.dump(payload, fh, ensure_ascii=False)
        fh.write(";\n")
    os.replace(tmp, OUT)  # atomic, so the page never loads a half-written file

    total = len(shown)
    print(f"wrote {total} headlines across {len(groups)} categories, "
          f"{with_image} with images ({dropped} cached images pruned) -> {OUT}")
    for err in errors:
        print("  warning:", err, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
