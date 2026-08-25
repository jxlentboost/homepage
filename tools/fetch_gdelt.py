#!/usr/bin/env python3
"""Fetch the latest GDELT 2.0 Events slice and write gdelt.js for homepage.html.

Same contract as the other fetchers here: the homepage is opened as a file:// page
where Safari blocks fetch/XHR but still loads sibling <script> tags, so this writes
a plain JS file (window.HP_GDELT = {...}) to disk. Nothing runs in the browser and
no request leaves this machine at page-load time.

GDELT republishes a 15-minute Event slice at data.gdeltproject.org/gdeltv2/. This
reads lastupdate.txt for the current one, pulls the ~75 KB zip, aggregates it in
memory and discards it — nothing accumulates on disk. (The DOC 2.0 search API is a
separate service and was rate-limiting this network, so it is deliberately unused.)

Event file layout is the documented 61-column GDELT 2.0 Event format:
  http://data.gdeltproject.org/documentation/GDELT-Event_Codebook-V2.0.pdf

Run by hand:  python3 ~/homepage/tools/fetch_gdelt.py
"""

import csv
import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
os.makedirs(DATA, exist_ok=True)
OUT = os.path.join(DATA, "gdelt.js")

LASTUPDATE = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"
TIMEOUT = 90
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) homepage-gdelt/1.0"

TOP_EVENTS = 20
TOP_COUNTRIES = 18

# Column offsets we use, from the 61-column Event format.
C_ACTOR1, C_ACTOR2 = 6, 16
C_ROOT, C_QUAD, C_GOLDSTEIN = 28, 29, 30
C_MENTIONS, C_SOURCES, C_ARTICLES, C_TONE = 31, 32, 33, 34
C_GEO_NAME, C_GEO_CC = 52, 53
C_SOURCEURL = 60
NCOLS = 61

# CAMEO event root codes (the top level of the taxonomy).
CAMEO = {
    "01": "Make public statement", "02": "Appeal", "03": "Express intent to cooperate",
    "04": "Consult", "05": "Diplomatic cooperation", "06": "Material cooperation",
    "07": "Provide aid", "08": "Yield", "09": "Investigate", "10": "Demand",
    "11": "Disapprove", "12": "Reject", "13": "Threaten", "14": "Protest",
    "15": "Exhibit force posture", "16": "Reduce relations", "17": "Coerce",
    "18": "Assault", "19": "Fight", "20": "Unconventional mass violence",
}

QUAD = {
    "1": "Verbal cooperation", "2": "Material cooperation",
    "3": "Verbal conflict",    "4": "Material conflict",
}

# GDELT geocodes with FIPS 10-4, not ISO 3166 — CH is China, not Switzerland.
# Mapped to (name, ISO 3166-1 alpha-2) so the page can show a flag.
FIPS = {
    "AA": ("Aruba", "AW"), "AC": ("Antigua and Barbuda", "AG"), "AE": ("United Arab Emirates", "AE"),
    "AF": ("Afghanistan", "AF"), "AG": ("Algeria", "DZ"), "AJ": ("Azerbaijan", "AZ"),
    "AL": ("Albania", "AL"), "AM": ("Armenia", "AM"), "AN": ("Andorra", "AD"),
    "AO": ("Angola", "AO"), "AR": ("Argentina", "AR"), "AS": ("Australia", "AU"),
    "AU": ("Austria", "AT"), "AV": ("Anguilla", "AI"), "BA": ("Bahrain", "BH"),
    "BB": ("Barbados", "BB"), "BC": ("Botswana", "BW"), "BD": ("Bermuda", "BM"),
    "BE": ("Belgium", "BE"), "BF": ("Bahamas", "BS"), "BG": ("Bangladesh", "BD"),
    "BH": ("Belize", "BZ"), "BK": ("Bosnia and Herzegovina", "BA"), "BL": ("Bolivia", "BO"),
    "BM": ("Myanmar", "MM"), "BN": ("Benin", "BJ"), "BO": ("Belarus", "BY"),
    "BP": ("Solomon Islands", "SB"), "BR": ("Brazil", "BR"), "BT": ("Bhutan", "BT"),
    "BU": ("Bulgaria", "BG"), "BX": ("Brunei", "BN"), "BY": ("Burundi", "BI"),
    "CA": ("Canada", "CA"), "CB": ("Cambodia", "KH"), "CD": ("Chad", "TD"),
    "CE": ("Sri Lanka", "LK"), "CF": ("Congo-Brazzaville", "CG"), "CG": ("DR Congo", "CD"),
    "CH": ("China", "CN"), "CI": ("Chile", "CL"), "CJ": ("Cayman Islands", "KY"),
    "CM": ("Cameroon", "CM"), "CN": ("Comoros", "KM"), "CO": ("Colombia", "CO"),
    "CS": ("Costa Rica", "CR"), "CT": ("Central African Republic", "CF"), "CU": ("Cuba", "CU"),
    "CV": ("Cape Verde", "CV"), "CY": ("Cyprus", "CY"), "DA": ("Denmark", "DK"),
    "DJ": ("Djibouti", "DJ"), "DO": ("Dominica", "DM"), "DR": ("Dominican Republic", "DO"),
    "EC": ("Ecuador", "EC"), "EG": ("Egypt", "EG"), "EI": ("Ireland", "IE"),
    "EK": ("Equatorial Guinea", "GQ"), "EN": ("Estonia", "EE"), "ER": ("Eritrea", "ER"),
    "ES": ("El Salvador", "SV"), "ET": ("Ethiopia", "ET"), "EZ": ("Czechia", "CZ"),
    "FI": ("Finland", "FI"), "FJ": ("Fiji", "FJ"), "FM": ("Micronesia", "FM"),
    "FR": ("France", "FR"), "GA": ("Gambia", "GM"), "GB": ("Gabon", "GA"),
    "GG": ("Georgia", "GE"), "GH": ("Ghana", "GH"), "GI": ("Gibraltar", "GI"),
    "GJ": ("Grenada", "GD"), "GK": ("Guernsey", "GG"), "GL": ("Greenland", "GL"),
    "GM": ("Germany", "DE"), "GR": ("Greece", "GR"), "GT": ("Guatemala", "GT"),
    "GV": ("Guinea", "GN"), "GY": ("Guyana", "GY"), "GZ": ("Gaza Strip", "PS"),
    "HA": ("Haiti", "HT"), "HK": ("Hong Kong", "HK"), "HO": ("Honduras", "HN"),
    "HR": ("Croatia", "HR"), "HU": ("Hungary", "HU"), "IC": ("Iceland", "IS"),
    "ID": ("Indonesia", "ID"), "IN": ("India", "IN"), "IR": ("Iran", "IR"),
    "IS": ("Israel", "IL"), "IT": ("Italy", "IT"), "IV": ("Côte d'Ivoire", "CI"),
    "IZ": ("Iraq", "IQ"), "JA": ("Japan", "JP"), "JM": ("Jamaica", "JM"),
    "JO": ("Jordan", "JO"), "KE": ("Kenya", "KE"), "KG": ("Kyrgyzstan", "KG"),
    "KN": ("North Korea", "KP"), "KS": ("South Korea", "KR"), "KU": ("Kuwait", "KW"),
    "KV": ("Kosovo", "XK"), "KZ": ("Kazakhstan", "KZ"), "LA": ("Laos", "LA"),
    "LE": ("Lebanon", "LB"), "LG": ("Latvia", "LV"), "LH": ("Lithuania", "LT"),
    "LI": ("Liberia", "LR"), "LO": ("Slovakia", "SK"), "LS": ("Liechtenstein", "LI"),
    "LT": ("Lesotho", "LS"), "LU": ("Luxembourg", "LU"), "LY": ("Libya", "LY"),
    "MA": ("Madagascar", "MG"), "MC": ("Macau", "MO"), "MD": ("Moldova", "MD"),
    "MG": ("Mongolia", "MN"), "MI": ("Malawi", "MW"), "MK": ("North Macedonia", "MK"),
    "ML": ("Mali", "ML"), "MN": ("Monaco", "MC"), "MO": ("Morocco", "MA"),
    "MP": ("Mauritius", "MU"), "MR": ("Mauritania", "MR"), "MT": ("Malta", "MT"),
    "MU": ("Oman", "OM"), "MV": ("Maldives", "MV"), "MX": ("Mexico", "MX"),
    "MY": ("Malaysia", "MY"), "MZ": ("Mozambique", "MZ"), "NC": ("New Caledonia", "NC"),
    "NG": ("Niger", "NE"), "NH": ("Vanuatu", "VU"), "NI": ("Nigeria", "NG"),
    "NL": ("Netherlands", "NL"), "NO": ("Norway", "NO"), "NP": ("Nepal", "NP"),
    "NR": ("Nauru", "NR"), "NS": ("Suriname", "SR"), "NU": ("Nicaragua", "NI"),
    "NZ": ("New Zealand", "NZ"), "PA": ("Paraguay", "PY"), "PE": ("Peru", "PE"),
    "PK": ("Pakistan", "PK"), "PL": ("Poland", "PL"), "PM": ("Panama", "PA"),
    "PO": ("Portugal", "PT"), "PP": ("Papua New Guinea", "PG"), "PU": ("Guinea-Bissau", "GW"),
    "QA": ("Qatar", "QA"), "RI": ("Serbia", "RS"), "RM": ("Marshall Islands", "MH"),
    "RO": ("Romania", "RO"), "RP": ("Philippines", "PH"), "RS": ("Russia", "RU"),
    "RW": ("Rwanda", "RW"), "SA": ("Saudi Arabia", "SA"), "SE": ("Seychelles", "SC"),
    "SF": ("South Africa", "ZA"), "SG": ("Senegal", "SN"), "SI": ("Slovenia", "SI"),
    "SL": ("Sierra Leone", "SL"), "SM": ("San Marino", "SM"), "SN": ("Singapore", "SG"),
    "SO": ("Somalia", "SO"), "SP": ("Spain", "ES"), "SU": ("Sudan", "SD"),
    "SW": ("Sweden", "SE"), "SY": ("Syria", "SY"), "SZ": ("Switzerland", "CH"),
    "TD": ("Trinidad and Tobago", "TT"), "TH": ("Thailand", "TH"), "TI": ("Tajikistan", "TJ"),
    "TN": ("Tonga", "TO"), "TO": ("Togo", "TG"), "TS": ("Tunisia", "TN"),
    "TU": ("Turkey", "TR"), "TV": ("Tuvalu", "TV"), "TW": ("Taiwan", "TW"),
    "TX": ("Turkmenistan", "TM"), "TZ": ("Tanzania", "TZ"), "UG": ("Uganda", "UG"),
    "UK": ("United Kingdom", "GB"), "UP": ("Ukraine", "UA"), "US": ("United States", "US"),
    "UV": ("Burkina Faso", "BF"), "UY": ("Uruguay", "UY"), "UZ": ("Uzbekistan", "UZ"),
    "VC": ("Saint Vincent", "VC"), "VE": ("Venezuela", "VE"), "VM": ("Vietnam", "VN"),
    "WA": ("Namibia", "NA"), "WE": ("West Bank", "PS"), "WZ": ("Eswatini", "SZ"),
    "YM": ("Yemen", "YE"), "ZA": ("Zambia", "ZM"), "ZI": ("Zimbabwe", "ZW"),
}


def flag(iso):
    if not iso or len(iso) != 2 or not iso.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso.upper())


def country(code):
    name, iso = FIPS.get(code, (code or "Unknown", ""))
    return {"cc": code, "name": name, "flag": flag(iso)}


def domain(url):
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def num(text, default=0.0):
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def slice_url():
    """lastupdate.txt lists the current export / mentions / gkg triple."""
    text = fetch(LASTUPDATE).decode("utf-8", "replace")
    for line in text.strip().splitlines():
        parts = line.split()
        if len(parts) >= 3 and ".export.CSV.zip" in parts[2]:
            return parts[2]
    raise ValueError("no export slice listed in lastupdate.txt")


def read_rows(url):
    archive = zipfile.ZipFile(io.BytesIO(fetch(url)))
    name = archive.namelist()[0]
    text = archive.read(name).decode("utf-8", "replace")
    stamp = "".join(ch for ch in name if ch.isdigit())[:14]
    rows = [r for r in csv.reader(io.StringIO(text), delimiter="\t") if len(r) == NCOLS]
    return stamp, rows


def label_for(row):
    """A short human phrase for the event, since actor fields are often blank."""
    action = CAMEO.get(row[C_ROOT], "Event")
    a1 = (row[C_ACTOR1] or "").strip().title()
    a2 = (row[C_ACTOR2] or "").strip().title()
    if a1 and a2:
        return f"{a1} → {a2}: {action.lower()}"
    if a1:
        return f"{a1}: {action.lower()}"
    if a2:
        return f"{action} — {a2}"
    return action


def main():
    errors = []
    try:
        url = slice_url()
        stamp, rows = read_rows(url)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            zipfile.BadZipFile, ValueError, IndexError) as err:
        print(f"could not fetch GDELT slice: {err}; leaving gdelt.js untouched", file=sys.stderr)
        return 1

    if not rows:
        print("GDELT slice was empty; leaving gdelt.js untouched", file=sys.stderr)
        return 1

    try:
        sliced = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        slice_iso = sliced.isoformat(timespec="seconds")
    except ValueError:
        slice_iso = None
        errors.append(f"unparseable slice stamp {stamp!r}")

    # --- headline numbers -------------------------------------------------
    tones = [num(r[C_TONE]) for r in rows]
    quad_counts = defaultdict(int)
    for r in rows:
        quad_counts[r[C_QUAD]] += 1
    coop = quad_counts["1"] + quad_counts["2"]
    conflict = quad_counts["3"] + quad_counts["4"]

    stats = {
        "events": len(rows),
        "sources": len({domain(r[C_SOURCEURL]) for r in rows if r[C_SOURCEURL]}),
        "articles": int(sum(num(r[C_ARTICLES]) for r in rows)),
        "tone": round(sum(tones) / len(tones), 2) if tones else 0.0,
        "coop": coop,
        "conflict": conflict,
        # Share of coded events that are cooperative, for the split bar.
        "coopPct": round(coop / (coop + conflict) * 100, 1) if (coop + conflict) else 0.0,
        "quads": [
            {"code": k, "label": QUAD[k], "count": quad_counts.get(k, 0)}
            for k in ("1", "2", "3", "4")
        ],
    }

    # --- most-covered events ---------------------------------------------
    # One article can yield several coded events; keep the best-covered per URL.
    best = {}
    for r in rows:
        src = r[C_SOURCEURL]
        if not src:
            continue
        if src not in best or num(r[C_ARTICLES]) > num(best[src][C_ARTICLES]):
            best[src] = r

    top = sorted(best.values(), key=lambda r: -num(r[C_ARTICLES]))[:TOP_EVENTS]
    events = [{
        "label": label_for(r),
        "action": CAMEO.get(r[C_ROOT], "Event"),
        "quad": r[C_QUAD],
        "quadLabel": QUAD.get(r[C_QUAD], ""),
        "place": r[C_GEO_NAME] or "",
        "country": country(r[C_GEO_CC]),
        "tone": round(num(r[C_TONE]), 1),
        "goldstein": round(num(r[C_GOLDSTEIN]), 1),
        "articles": int(num(r[C_ARTICLES])),
        "sources": int(num(r[C_SOURCES])),
        "url": r[C_SOURCEURL],
        "domain": domain(r[C_SOURCEURL]),
    } for r in top]

    # --- where the coverage is -------------------------------------------
    by_cc = defaultdict(lambda: {"n": 0, "tone": 0.0, "conflict": 0})
    for r in rows:
        cc = r[C_GEO_CC]
        if not cc:
            continue
        bucket = by_cc[cc]
        bucket["n"] += 1
        bucket["tone"] += num(r[C_TONE])
        if r[C_QUAD] in ("3", "4"):
            bucket["conflict"] += 1

    countries = sorted(
        ({**country(cc), "events": b["n"],
          "tone": round(b["tone"] / b["n"], 1),
          "conflict": b["conflict"]}
         for cc, b in by_cc.items()),
        key=lambda c: -c["events"],
    )[:TOP_COUNTRIES]

    # --- what kind of events ---------------------------------------------
    by_root = defaultdict(lambda: {"n": 0, "tone": 0.0})
    for r in rows:
        root = r[C_ROOT]
        if root not in CAMEO:
            continue
        by_root[root]["n"] += 1
        by_root[root]["tone"] += num(r[C_TONE])

    types = sorted(
        ({"code": root, "label": CAMEO[root], "count": b["n"],
          "tone": round(b["tone"] / b["n"], 1)}
         for root, b in by_root.items()),
        key=lambda t: -t["count"],
    )

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "slice": slice_iso,
        "sliceUrl": url,
        "stats": stats,
        "events": events,
        "countries": countries,
        "types": types,
        "errors": errors,
    }

    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("window.HP_GDELT = ")
        json.dump(payload, fh, ensure_ascii=False)
        fh.write(";\n")
    os.replace(tmp, OUT)  # atomic, so the page never loads a half-written file

    print(f"wrote GDELT slice {stamp}: {len(rows)} events, {stats['sources']} sources, "
          f"{len(countries)} countries, tone {stats['tone']} -> {OUT}")
    for err in errors:
        print("  warning:", err, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
