#!/usr/bin/env python3
"""Daily diesel price collector (Google Places API New). Stdlib only.

Appends to:
  data/prices.csv  one row per station with a diesel price, every run (raw, unfiltered)
  data/runs.csv    one summary row per run (counts, fresh median, HEB median)

Usage:
  GOOGLE_MAPS_API_KEY=... python3 collect.py            # real run, writes CSVs
  GOOGLE_MAPS_API_KEY=... python3 collect.py --dry-run  # prints only, writes nothing

Changing ZIP/LAT/LNG/MILES starts a new "area": rows are tagged with zip and
radius so old history never silently mixes with a different area.
"""
import csv
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------- config ----------------
ZIP = "77840"
LAT, LNG = 30.6166, -96.3120  # zip centroid (geocoded once, hardcoded to skip that call)
MILES = 5.0
MAX_CALLS = 24    # hard ceiling on Nearby Search calls per run (cost guard)
STALE_DAYS = 3    # prices older than this are kept in the CSV but excluded from stats
DATA_DIR = Path(__file__).resolve().parent / "data"
# ----------------------------------------

RADIUS_M = min(MILES * 1609.34, 50000)
KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.location",
    "places.fuelOptions",
])
STATE = {"calls": 0, "capped_leaves": 0, "reserve": 0}


def post(url, body, attempts=3):
    data = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": KEY,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    for i in range(attempts):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if (e.code >= 500 or e.code == 429) and i < attempts - 1:
                time.sleep(2 * (i + 1))
                continue
            detail = e.read().decode(errors="replace")[:300]
            raise SystemExit(f"Places API HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            if i < attempts - 1:
                time.sleep(2 * (i + 1))
                continue
            raise SystemExit(f"Network error: {e}")


def miles_between(lat1, lng1, lat2, lng2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearby(lat, lng, radius_m):
    body = {
        "includedTypes": ["gas_station"],
        "maxResultCount": 20,
        "rankPreference": "DISTANCE",
        "locationRestriction": {
            "circle": {"center": {"latitude": lat, "longitude": lng}, "radius": radius_m}
        },
    }
    return post("https://places.googleapis.com/v1/places:searchNearby", body).get("places", [])


def heb_search(lat, lng):
    body = {
        "textQuery": "H-E-B fuel",
        "maxResultCount": 20,
        "locationBias": {
            "circle": {"center": {"latitude": lat, "longitude": lng}, "radius": RADIUS_M}
        },
    }
    return post("https://places.googleapis.com/v1/places:searchText", body).get("places", [])


def search_area(lat, lng, r_m, cos_lat, depth=0):
    """Search one circle; if it hits the 20 cap, split into 4 circles that cover it
    (max 2 levels). Splits never spend calls reserved for top-level circles."""
    limit = MAX_CALLS if depth == 0 else MAX_CALLS - STATE["reserve"]
    if STATE["calls"] >= limit:
        return []
    STATE["calls"] += 1
    got = nearby(lat, lng, r_m)
    if len(got) < 20:
        return got
    if depth >= 2:
        STATE["capped_leaves"] += 1
        return got
    out = list(got)
    half_mi = (r_m / 1609.34) / 2
    for sy in (1, -1):
        for sx in (1, -1):
            out += search_area(
                lat + sy * half_mi / 69.0,
                lng + sx * half_mi / (69.0 * cos_lat),
                r_m * 0.72, cos_lat, depth + 1,
            )
    return out


def money(p):
    return int(p.get("units", 0) or 0) + p.get("nanos", 0) / 1e9


def diesel_of(place):
    """(price, updateTime, fuel_type) for the station's diesel entry, or None."""
    prices = (place.get("fuelOptions") or {}).get("fuelPrices", [])
    diesel = [fp for fp in prices if "DIESEL" in fp.get("type", "")]
    if not diesel:
        return None
    diesel.sort(key=lambda fp: fp.get("type") != "DIESEL")  # prefer plain DIESEL
    fp = diesel[0]
    return money(fp["price"]), fp.get("updateTime"), fp["type"]


def age_days(ts, now):
    if not ts:
        return None
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (now - t).total_seconds() / 86400


def is_heb(place):
    name = place.get("displayName", {}).get("text", "").lower()
    return "h-e-b" in name or "heb" in name.replace("-", "")


def append_csv(path, fields, rows):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerows(rows)


PRICE_FIELDS = ["run_ts", "zip", "radius_mi", "place_id", "name", "is_heb", "address",
                "lat", "lng", "price", "fuel_type", "price_updated"]
RUN_FIELDS = ["run_ts", "zip", "radius_mi", "search_calls", "capped_leaves", "budget_hit",
              "places_found", "with_diesel", "fresh_diesel", "fresh_median", "fresh_mean",
              "heb_fresh", "heb_fresh_median"]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    dry = "--dry-run" in argv
    if not KEY:
        sys.exit("Set GOOGLE_MAPS_API_KEY first.")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    run_ts = now.isoformat()
    cos_lat = math.cos(math.radians(LAT))
    STATE.update(calls=0, capped_leaves=0, reserve=0)

    # hex pattern: center + ring of 6, each circle wide enough to cover the area together
    ring_d, sub_r = 0.68 * MILES, 0.56 * RADIUS_M
    points = [(LAT, LNG)] + [
        (LAT + ring_d * math.cos(math.radians(60 * k)) / 69.0,
         LNG + ring_d * math.sin(math.radians(60 * k)) / (69.0 * cos_lat))
        for k in range(6)
    ]
    found = []
    for i, (plat, plng) in enumerate(points):
        STATE["reserve"] = len(points) - i - 1
        found += search_area(plat, plng, sub_r, cos_lat)
    found += heb_search(LAT, LNG)

    places = {}
    for p in found:
        loc = p.get("location", {})
        if "latitude" not in loc:
            continue
        if miles_between(LAT, LNG, loc["latitude"], loc["longitude"]) > MILES:
            continue  # Text Search only biases location, so enforce the radius here
        places[p["id"]] = p
    if not places:
        sys.exit("No stations returned; not writing anything.")

    price_rows, fresh, heb_fresh = [], [], []
    for p in places.values():
        d = diesel_of(p)
        if not d:
            continue
        price, ts, ftype = d
        heb = is_heb(p)
        price_rows.append({
            "run_ts": run_ts, "zip": ZIP, "radius_mi": MILES, "place_id": p["id"],
            "name": p["displayName"]["text"], "is_heb": int(heb),
            "address": p.get("formattedAddress", ""),
            "lat": p["location"]["latitude"], "lng": p["location"]["longitude"],
            "price": f"{price:.3f}", "fuel_type": ftype, "price_updated": ts or "",
        })
        a = age_days(ts, now)
        if a is not None and a <= STALE_DAYS:
            fresh.append(price)
            if heb:
                heb_fresh.append(price)

    budget_hit = int(STATE["calls"] >= MAX_CALLS)
    run_row = {
        "run_ts": run_ts, "zip": ZIP, "radius_mi": MILES,
        "search_calls": STATE["calls"], "capped_leaves": STATE["capped_leaves"],
        "budget_hit": budget_hit, "places_found": len(places),
        "with_diesel": len(price_rows), "fresh_diesel": len(fresh),
        "fresh_median": f"{statistics.median(fresh):.3f}" if fresh else "",
        "fresh_mean": f"{statistics.mean(fresh):.3f}" if fresh else "",
        "heb_fresh": len(heb_fresh),
        "heb_fresh_median": f"{statistics.median(heb_fresh):.3f}" if heb_fresh else "",
    }
    if not price_rows:
        sys.exit("Stations found but zero diesel prices; not writing (likely an API problem).")

    print(json.dumps(run_row, indent=2))
    if dry:
        print("(dry run: nothing written)")
        return
    append_csv(DATA_DIR / "prices.csv", PRICE_FIELDS, price_rows)
    append_csv(DATA_DIR / "runs.csv", RUN_FIELDS, [run_row])
    print(f"Wrote {len(price_rows)} price rows and 1 run row to {DATA_DIR}")


if __name__ == "__main__":
    main()
