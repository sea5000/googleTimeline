#!/usr/bin/env python3
"""Sample-only, fully offline entrypoint for the Google Timeline dashboard.

Lets the repository run end-to-end with zero real location history and zero
network:

1. Generates a synthetic Timeline.json that matches the real Google Timeline
   export schema exactly (semanticSegments / rawSignals / userLocationProfile).
   Deterministic given the SEED below, so every clone renders the same sample.
2. Patches the two path constants in app.py to ./sample_output and sets
   DISABLE_GEOCODE=1 before importing app, so nothing touches a real
   Timeline.json and the Nominatim geocode thread never starts (no network,
   no addresses.db full of real coordinates).
3. Runs the real production path: app.ensure_vendor() -> app.load_data() ->
   app.app.run(), then serves on PORT (8080 by default).

Usage:

    python3 run_sample.py              # generate + serve http://127.0.0.1:8080
    python3 run_sample.py --no-serve   # generate + load + print summary only
"""
from __future__ import annotations

import json
import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "sample_output"
TIMELINE_OUT = OUT / "Timeline.json"

SEED = 20261018               # deterministic: identical output for every clone
START = datetime(2016, 1, 4)
END = datetime(2025, 12, 31)

# Fictional coordinates: one metro (work + home + gym) plus a far-away city
# (Oslo) far enough to cluster as its own forecast region.
HOME = (48.0534, 11.4449)
WORK = (48.1385, 11.6012)
GYM = (48.0411, 11.4602)
TRIP = (59.9139, 10.7522)    # ~11.8 deg from the metro -> separate region

PLACE_HOME = "ChIJsyntheticHomeMunich0001"
PLACE_WORK = "ChIJsyntheticOfficeMunich0002"
PLACE_GYM = "ChIJsyntheticGymMunich0003"


def _ts(d: datetime) -> str:
    """ISO-8601 with explicit +00:00 offset, like real Timeline output."""
    return d.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")


def _pt(rng: random.Random, lat: float, lon: float, spread: float = 0.002) -> str:
    """A 'lat°, lon°' point string, the format app.parse_latlng expects."""
    return (f"{lat + rng.uniform(-spread, spread):.5f}°, "
            f"{lon + rng.uniform(-spread, spread):.5f}°")


def make_timeline() -> dict:
    rng = random.Random(SEED)
    segs: list[dict] = []

    def visit(pid: str | None, stype: str, loc, start: datetime, end: datetime) -> None:
        lat, lon = loc
        cand: dict = {"semanticType": stype,
                      "placeLocation": {"latLng": _pt(rng, lat, lon)}}
        if pid:
            cand["placeId"] = pid
        segs.append({
            "startTime": _ts(start), "endTime": _ts(end),
            "startTimeTimezoneUtcOffsetMinutes": 0,
            "visit": {"topCandidate": cand,
                      "accuracyMeters": int(rng.uniform(8, 40))},
        })

    def activity(atype: str, loc, dist: float, start: datetime, end: datetime) -> None:
        lat, lon = loc
        segs.append({
            "startTime": _ts(start), "endTime": _ts(end),
            "startTimeTimezoneUtcOffsetMinutes": 0,
            "activity": {
                "start": {"latLng": _pt(rng, lat, lon)},
                "end": {"latLng": _pt(rng, lat, lon, 0.01)},
                "distanceMeters": dist,
                "topCandidate": {"type": atype},
            },
        })

    def route(start: datetime, end: datetime, a, b) -> None:
        """A GPS track (timelinePath) interpolated from a to b."""
        la, lo = a
        lb, lo2 = b
        steps = max(4, int((end - start).total_seconds() // 60) or 4)
        pts = []
        for i in range(steps + 1):
            f = i / steps
            t = start + (end - start) * (i / steps)
            pts.append({"point": _pt(rng, la + (lb - la) * f, lo + (lo2 - lo) * f),
                       "time": _ts(t)})
        segs.append({
            "startTime": _ts(start), "endTime": _ts(end),
            "startTimeTimezoneUtcOffsetMinutes": 0,
            "timelinePath": pts,
        })

    total_days = (END - START).days
    for idx in range(total_days + 1):
        day0 = START + timedelta(days=idx)
        dow = day0.weekday()
        if dow <= 4:  # Monday to Friday: commute + work block + home
            c0 = day0 + timedelta(hours=8, minutes=5)
            c1 = day0 + timedelta(hours=8, minutes=35)
            activity("IN_VEHICLE", HOME, rng.uniform(4000, 9000), c0, c1)
            route(c0, c1, HOME, WORK)
            visit(PLACE_WORK, "WORK", WORK, c1 + timedelta(minutes=5),
                  day0 + timedelta(hours=16, minutes=45))
            r0 = day0 + timedelta(hours=16, minutes=50)
            r1 = day0 + timedelta(hours=17, minutes=20)
            activity("IN_VEHICLE", WORK, rng.uniform(4000, 9000), r0, r1)
            route(r0, r1, WORK, HOME)
            if rng.random() < 0.2:
                g0 = day0 + timedelta(hours=20)
                visit(PLACE_GYM, "GYM", GYM, g0, g0 + timedelta(minutes=55))
            visit(PLACE_HOME, "HOME", HOME, day0 + timedelta(hours=17, minutes=25),
                  day0 + timedelta(hours=23, minutes=30))
        elif dow == 5:  # Saturday
            visit(PLACE_HOME, "HOME", HOME, day0 + timedelta(hours=8),
                  day0 + timedelta(hours=23, minutes=45))
            if rng.random() < 0.5:
                s0 = day0 + timedelta(hours=10)
                activity("CYCLING", HOME, rng.uniform(3000, 8000),
                         s0, s0 + timedelta(minutes=40))
                route(s0, s0 + timedelta(minutes=40), HOME, HOME)
        else:  # Sunday
            visit(PLACE_HOME, "HOME", HOME, day0 + timedelta(hours=8, minutes=30),
                  day0 + timedelta(hours=23, minutes=45))

        # Twice a year: a four-day trip to the far city (flight + stays).
        if idx % 182 == 30:
            f0 = day0 + timedelta(hours=9)
            f1 = day0 + timedelta(hours=13, minutes=30)
            activity("FLYING", HOME, 1500000, f0, f1)
            visit(None, "UNKNOWN", TRIP, f1 + timedelta(minutes=30),
                  day0 + timedelta(hours=23, minutes=45))
            for i in (1, 2, 3):
                dd = day0 + timedelta(days=i)
                a0 = dd + timedelta(hours=9)
                activity("IN_VEHICLE", TRIP, rng.uniform(2000, 12000),
                         a0, dd + timedelta(hours=16))
                visit(None, "UNKNOWN", TRIP, dd + timedelta(hours=16),
                      dd + timedelta(hours=23, minutes=45))

    # Recent raw GPS pings (the parser only heat-maps these).
    raw = []
    for i in range(24):
        ts0 = END - timedelta(hours=3 * i)
        la, lo = WORK if 8 <= ts0.hour < 18 else HOME
        loc = (la, lo)
        raw.append({
            "position": {
                "LatLng": _pt(rng, *loc),
                "timestamp": _ts(ts0),
                "accuracyMeters": int(rng.uniform(4, 25)),
                "speedMetersPerSecond": round(rng.uniform(0, 18), 2),
            },
        })

    return {
        "semanticSegments": segs,
        "rawSignals": raw,
        "userLocationProfile": {
            "frequentPlaces": [
                {"placeId": PLACE_HOME, "label": "Home",
                 "placeLocation": f"{HOME[0]:.5f}°, {HOME[1]:.5f}°"},
                {"placeId": PLACE_WORK, "label": "Office",
                 "placeLocation": f"{WORK[0]:.5f}°, {WORK[1]:.5f}°"},
                {"placeId": PLACE_GYM, "label": "Gym",
                 "placeLocation": f"{GYM[0]:.5f}°, {GYM[1]:.5f}°"},
            ],
            "persona": {
                "travelModeAffinities": [
                    {"mode": "IN_PASSENGER_VEHICLE", "affinity": 0.9},
                    {"mode": "CYCLING", "affinity": 0.06},
                    {"mode": "WALKING", "affinity": 0.04},
                ],
            },
        },
    }


def write_sample() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    doc = make_timeline()
    TIMELINE_OUT.write_text(json.dumps(doc, separators=(",", ":")))
    return len(doc["semanticSegments"])


def main() -> int:
    os.environ["DISABLE_GEOCODE"] = "1"
    os.environ["PORT"] = os.environ.get("PORT", "8080")
    os.environ["HOST"] = os.environ.get("HOST", "127.0.0.1")

    n = write_sample()
    print(f"sample Timeline: {n} segments -> {TIMELINE_OUT}")

    import app  # imported after env is set, so PORT/HOST come from the above
    app.TIMELINE_FILE = str(TIMELINE_OUT)
    app.DB_PATH = str(OUT / "addresses.db")

    app.ensure_vendor()
    D = app.load_data()
    s = D["summary"]
    print("sample summary (all figures synthetic):")
    print(f"  segments={s['segments']}  activities={s['totalActivities']}")
    print(f"  distance={s['totalDistanceKm']} km  places={s['uniquePlaces']}")
    print(f"  years={D['years']}  regions={len(D['regions'])}")

    if "--no-serve" in sys.argv:
        print("(--no-serve) skipping app.run()")
        return 0
    print(f"Serving on http://{app.HOST}:{app.PORT}")
    app.app.run(host=app.HOST, port=app.PORT, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
