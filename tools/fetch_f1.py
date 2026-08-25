#!/usr/bin/env python3
"""Fetch the current Formula 1 season and write f1.js for homepage.html.

Same contract as fetch_news.py: the homepage is opened as a file:// page where
Safari blocks fetch/XHR but still loads sibling <script> tags, so this writes a
plain JS file (window.HP_F1 = {...}) to disk. Nothing here runs in the browser
and no request leaves this machine at page-load time.

Data comes from Jolpica-F1 (api.jolpi.ca), the community-run successor to the
Ergast API, which kept Ergast's URL shapes. Four requests per run:
  driver standings, constructor standings, full season schedule, last results,
plus one for the season's race winners (used to annotate finished rounds).

Run by hand:  python3 ~/homepage/tools/fetch_f1.py
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
os.makedirs(DATA, exist_ok=True)
OUT = os.path.join(DATA, "f1.js")

BASE = "https://api.jolpi.ca/ergast/f1"
TIMEOUT = 20
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) homepage-f1/1.0"

# Headlines for the section's News tab. Parsed by fetch_news.py's RSS reader.
F1_FEEDS = [
    ("F1", "Autosport", "https://www.autosport.com/rss/f1/news/"),
    ("F1", "Motorsport.com", "https://www.motorsport.com/rss/f1/news/"),
]
NEWS_KEEP = 14

# Demonyms (drivers/teams) and country names (circuits) share one table.
ISO = {
    "Argentine": "AR", "Argentina": "AR", "Australian": "AU", "Australia": "AU",
    "Austrian": "AT", "Austria": "AT", "Azerbaijan": "AZ", "Azerbaijani": "AZ",
    "Bahrain": "BH", "Bahraini": "BH", "Belgian": "BE", "Belgium": "BE",
    "Brazilian": "BR", "Brazil": "BR", "British": "GB", "UK": "GB", "England": "GB",
    "Canadian": "CA", "Canada": "CA", "Chinese": "CN", "China": "CN",
    "Colombian": "CO", "Colombia": "CO", "Czech": "CZ", "Danish": "DK", "Denmark": "DK",
    "Dutch": "NL", "Netherlands": "NL", "Estonian": "EE", "Finnish": "FI", "Finland": "FI",
    "French": "FR", "France": "FR", "German": "DE", "Germany": "DE",
    "Hungarian": "HU", "Hungary": "HU", "Indian": "IN", "India": "IN",
    "Indonesian": "ID", "Indonesia": "ID", "Irish": "IE", "Ireland": "IE",
    "Italian": "IT", "Italy": "IT", "Japanese": "JP", "Japan": "JP",
    "Korea": "KR", "South Korea": "KR", "Malaysian": "MY", "Malaysia": "MY",
    "Mexican": "MX", "Mexico": "MX", "Monegasque": "MC", "Monaco": "MC",
    "Moroccan": "MA", "Morocco": "MA", "New Zealander": "NZ", "New Zealand": "NZ",
    "Polish": "PL", "Poland": "PL", "Portuguese": "PT", "Portugal": "PT",
    "Qatar": "QA", "Qatari": "QA", "Russian": "RU", "Russia": "RU",
    "Saudi Arabia": "SA", "Saudi Arabian": "SA", "Singapore": "SG", "Singaporean": "SG",
    "South African": "ZA", "South Africa": "ZA", "Spanish": "ES", "Spain": "ES",
    "Swedish": "SE", "Sweden": "SE", "Swiss": "CH", "Switzerland": "CH",
    "Thai": "TH", "Thailand": "TH", "Turkish": "TR", "Turkey": "TR",
    "UAE": "AE", "United Arab Emirates": "AE", "American": "US", "USA": "US",
    "United States": "US", "Venezuelan": "VE", "Venezuela": "VE",
}


def flag(name):
    """Country name or demonym -> regional-indicator emoji, or '' if unknown."""
    code = ISO.get((name or "").strip())
    if not code:
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code)


def get(path):
    url = f"{BASE}/{path}"
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(
        url + sep + "format=json",
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)["MRData"]


def iso(date, time):
    """Ergast splits date and time; the page wants one parseable instant."""
    if not date:
        return None
    if not time:
        return date
    return f"{date}T{time.replace('Z', '')}Z" if not time.endswith("Z") else f"{date}T{time}"


SESSION_KEYS = [
    ("FirstPractice", "FP1"),
    ("SecondPractice", "FP2"),
    ("ThirdPractice", "FP3"),
    ("SprintQualifying", "Sprint Quali"),
    ("SprintShootout", "Sprint Shootout"),
    ("Sprint", "Sprint"),
    ("Qualifying", "Quali"),
]


def build_schedule(data, winners):
    races = []
    for r in data["RaceTable"]["Races"]:
        loc = r["Circuit"]["Location"]
        sessions = []
        for key, label in SESSION_KEYS:
            s = r.get(key)
            if s:
                sessions.append({"label": label, "start": iso(s.get("date"), s.get("time"))})
        sessions.append({"label": "Race", "start": iso(r.get("date"), r.get("time"))})
        rnd = int(r["round"])
        races.append({
            "round": rnd,
            "name": r["raceName"],
            "circuit": r["Circuit"]["circuitName"],
            "locality": loc.get("locality", ""),
            "country": loc.get("country", ""),
            "flag": flag(loc.get("country", "")),
            "start": iso(r.get("date"), r.get("time")),
            "date": r.get("date"),
            "url": r.get("url", ""),
            "sprint": any(s["label"].startswith("Sprint") for s in sessions),
            "sessions": sessions,
            "winner": winners.get(rnd),
        })
    return races


def build_winners(data):
    out = {}
    for r in data["RaceTable"]["Races"]:
        res = r.get("Results") or []
        if not res:
            continue
        d, c = res[0]["Driver"], res[0]["Constructor"]
        out[int(r["round"])] = {
            "code": d.get("code") or d["familyName"][:3].upper(),
            "name": f"{d['givenName']} {d['familyName']}",
            "teamId": c["constructorId"],
            "team": c["name"],
        }
    return out


def build_drivers(data):
    lists = data["StandingsTable"]["StandingsLists"]
    if not lists:
        return []
    out = []
    for s in lists[0]["DriverStandings"]:
        d = s["Driver"]
        team = (s.get("Constructors") or [{}])[-1]
        out.append({
            "pos": int(s["position"]),
            "code": d.get("code") or d["familyName"][:3].upper(),
            "number": d.get("permanentNumber", ""),
            "given": d["givenName"],
            "family": d["familyName"],
            "flag": flag(d.get("nationality", "")),
            "team": team.get("name", ""),
            "teamId": team.get("constructorId", ""),
            "points": float(s["points"]),
            "wins": int(s["wins"]),
            "url": d.get("url", ""),
        })
    return out


def build_constructors(data):
    lists = data["StandingsTable"]["StandingsLists"]
    if not lists:
        return []
    out = []
    for s in lists[0]["ConstructorStandings"]:
        c = s["Constructor"]
        out.append({
            "pos": int(s["position"]),
            "name": c["name"],
            "teamId": c["constructorId"],
            "flag": flag(c.get("nationality", "")),
            "points": float(s["points"]),
            "wins": int(s["wins"]),
            "url": c.get("url", ""),
        })
    return out


def build_last(data):
    races = data["RaceTable"]["Races"]
    if not races:
        return None
    r = races[0]
    loc = r["Circuit"]["Location"]
    results = []
    for x in r.get("Results", []):
        d, c = x["Driver"], x["Constructor"]
        gained = None
        try:
            gained = int(x["grid"]) - int(x["position"])
        except (KeyError, ValueError):
            pass
        results.append({
            "pos": x.get("positionText", x.get("position")),
            "code": d.get("code") or d["familyName"][:3].upper(),
            "name": f"{d['givenName']} {d['familyName']}",
            "flag": flag(d.get("nationality", "")),
            "team": c["name"],
            "teamId": c["constructorId"],
            "points": float(x.get("points", 0)),
            "status": x.get("status", ""),
            "time": (x.get("Time") or {}).get("time", ""),
            "grid": x.get("grid", ""),
            "gained": gained,
            "fastest": (x.get("FastestLap") or {}).get("rank") == "1",
        })
    return {
        "round": int(r["round"]),
        "name": r["raceName"],
        "circuit": r["Circuit"]["circuitName"],
        "locality": loc.get("locality", ""),
        "country": loc.get("country", ""),
        "flag": flag(loc.get("country", "")),
        "date": r.get("date"),
        "start": iso(r.get("date"), r.get("time")),
        "url": r.get("url", ""),
        "results": results,
    }


def build_news(errors):
    """F1 headlines, via the RSS reader that already ships next door."""
    sys.path.insert(0, HERE)
    try:
        import fetch_news
    except ImportError as err:
        errors["news"] = f"news: {err}"
        return []

    fetch_news.PER_FEED = 8  # a little deeper than the homepage's other feeds
    items = []
    for feed in F1_FEEDS:
        _cat, _source, got, err = fetch_news.fetch(feed)
        if err:
            errors.setdefault("news", err)
        items.extend(got)

    # Interleave sources so one prolific feed can't own the whole tab.
    items.sort(key=lambda i: i["ts"], reverse=True)
    ranked, seen = [], {}
    for item in items:
        rank = seen.get(item["source"], 0)
        seen[item["source"]] = rank + 1
        ranked.append((rank, item))
    ranked.sort(key=lambda pair: (pair[0], -pair[1]["ts"]))
    picked = [item for _rank, item in ranked[:NEWS_KEEP]]

    # Cache thumbnails into this section's own bucket. scrape=False keeps the run
    # quick: both F1 feeds already publish media tags, so there is no gap to fill.
    try:
        kept = fetch_news.attach_images(picked, bucket="f1", scrape=False)
        fetch_news.prune(kept, bucket="f1")
    except OSError as err:
        errors.setdefault("images", f"images: {err}")
    return picked


def main():
    errors = {}

    def safe(name, fn, path, *args):
        try:
            return fn(get(path), *args)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                KeyError, ValueError, TypeError) as err:
            errors[name] = f"{name}: {err}"
            return None

    winners_raw = safe("winners", build_winners, "current/results/1/?limit=100")
    winners = winners_raw or {}

    schedule = safe("schedule", build_schedule, "current/?limit=100", winners) or []
    drivers = safe("drivers", build_drivers, "current/driverstandings/?limit=100") or []
    constructors = safe("constructors", build_constructors, "current/constructorstandings/?limit=100") or []
    last = safe("results", build_last, "current/last/results/?limit=100")
    news = build_news(errors)

    if not (schedule or drivers or constructors):
        print("no F1 data could be fetched; leaving f1.js untouched", file=sys.stderr)
        for err in errors.values():
            print("  ", err, file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    season = ""
    if schedule:
        season = schedule[0]["start"][:4]
    elif drivers:
        season = str(now.year)

    # The next round is the first whose race start is still ahead of us.
    next_round = None
    for race in schedule:
        try:
            when = datetime.fromisoformat(race["start"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if when > now:
            next_round = race["round"]
            break

    payload = {
        "updated": now.isoformat(timespec="seconds"),
        "season": season,
        "round": last["round"] if last else 0,
        "totalRounds": len(schedule),
        "nextRound": next_round,
        "drivers": drivers,
        "constructors": constructors,
        "schedule": schedule,
        "last": last,
        "news": news,
        "errors": sorted(errors.values()),
    }

    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("window.HP_F1 = ")
        json.dump(payload, fh, ensure_ascii=False)
        fh.write(";\n")
    os.replace(tmp, OUT)  # atomic, so the page never loads a half-written file

    print(f"wrote {season} F1: {len(drivers)} drivers, {len(constructors)} teams, "
          f"{len(schedule)} rounds, {len(news)} headlines, next=R{next_round} -> {OUT}")
    for err in errors.values():
        print("  warning:", err, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
