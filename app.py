#!/usr/bin/env python3
"""Live Google Timeline dashboard backend.

Reads Timeline.json directly, aggregates on demand, and serves the data over
REST. Place addresses are reverse-geocoded in the background (one request/sec
against the free OpenStreetMap Nominatim service) and cached in addresses.db.
Map tiles are keyless (OpenStreetMap / CARTO / OpenTopoMap / Esri imagery).

Run:  python3 app.py          (then open http://127.0.0.1:5000)
"""
import json
import os
import re
import sys
import time
import json as _json
import sqlite3
import threading
import urllib.request
import urllib.parse
import urllib.error
from collections import defaultdict
from datetime import datetime

from flask import Flask, jsonify, request, Response, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
TIMELINE_FILE = os.path.join(BASE, "Timeline.json")
DB_PATH = os.path.join(BASE, "addresses.db")
VENDOR_DIR = os.path.join(BASE, "static", "vendor")
INDEX_TEMPLATE = os.path.join(BASE, "index.html")

LNG_RE = re.compile(r"([-\d.]+)°,\s*([-\d.]+)°")
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "google-timeline-dashboard/1.0 (personal analytics)"
GEOCODE_DELAY = float(os.environ.get("GEOCODE_DELAY", "1.05"))  # seconds, keep >=1
AUTO_GEOCODE = os.environ.get("DISABLE_GEOCODE", "") != "1"
PORT = int(os.environ.get("PORT", "5000"))
HOST = os.environ.get("HOST", "127.0.0.1")

VENDOR_FILES = {
    "leaflet.css": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
    "leaflet.js": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
    "leaflet-heat.js": "https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js",
    "chart.umd.min.js": "https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js",
    "maplibre-gl.css": "https://unpkg.com/maplibre-gl@5/dist/maplibre-gl.css",
    "maplibre-gl.js": "https://unpkg.com/maplibre-gl@5/dist/maplibre-gl.js",
    "leaflet-maplibre-gl.js": "https://unpkg.com/@maplibre/maplibre-gl-leaflet/leaflet-maplibre-gl.js",
}

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False


# ---------------------------------------------------------------- data load
_load_lock = threading.Lock()
DATA = {"ready": False}


def parse_ts(s):
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def parse_latlng(s):
    if not s:
        return None
    m = LNG_RE.search(s)
    if not m:
        return None
    return (float(m.group(1)), float(m.group(2)))


def localize(dt, offset_min):
    from datetime import timezone, timedelta
    if offset_min is None:
        return dt
    return dt.astimezone(timezone.utc) + timedelta(minutes=offset_min)


# ============================================ forecast model
# Lightweight Bayesian schedule model.
#   * Region level: coarse geographic area (clustered from coords + tz).
#   * Place level: a specific placeId (home / work / named place).
# Tables are sparse (dow,hour)-keyed count dicts; probabilities are smoothed
# with Laplace + shrinkage to the region marginal so sparse cells stay sane.
GEO_DEG = 111.0
SMOOTH_ALPHA = 1.0
SHRINK_CELLS = 5.0  # blend weight reaches 0.5 when a cell has this many observations

ACTIVITY_LABELS = [
    "WALKING", "IN_PASSENGER_VEHICLE", "IN_VEHICLE", "IN_BUS", "IN_SUBWAY",
    "IN_TRAM", "IN_TRAIN", "IN_FERRY", "FLYING", "CYCLING", "RUNNING",
    "MOTORCYCLING", "SAILING", "BOATING", "SKIING",
]


def _dist_deg(lat1, lon1, lat2, lon2):
    dlat = lat1 - lat2
    dlon = lon1 - lon2
    return (dlat * dlat + dlon * dlon) ** 0.5


def _cluster_regions(visit_rows, activity_rows):
    """Weighted 1-degree grid cells merged into coarse regions (continents/areas)."""
    pts = {}
    for r in visit_rows:
        c = (round(r[2], 0), round(r[3], 0))
        pts[c] = pts.get(c, 0) + 1
    for r in activity_rows:
        c = (round(r[2], 0), round(r[3], 0))
        pts[c] = pts.get(c, 0) + 1
    regions = []
    for c, cnt in sorted(pts.items(), key=lambda kv: -kv[1]):
        lat, lon = c[0] + 0.5, c[1] + 0.5
        best_i, best_d = None, None
        for i, rg in enumerate(regions):
            d = _dist_deg(lat, lon, rg["lat"], rg["lon"])
            if best_d is None or d < best_d:
                best_d, best_i = d, i
        if best_i is not None and best_d <= 5.0:
            rg = regions[best_i]
            n = rg["n"]
            rg["lat"] = (rg["lat"] * n + lat * cnt) / (n + cnt)
            rg["lon"] = (rg["lon"] * n + lon * cnt) / (n + cnt)
            rg["n"] = n + cnt
            rg["count"] += cnt
            rg["minLat"] = min(rg["minLat"], lat); rg["maxLat"] = max(rg["maxLat"], lat)
            rg["minLon"] = min(rg["minLon"], lon); rg["maxLon"] = max(rg["maxLon"], lon)
        else:
            regions.append({
                "id": len(regions), "name": None, "address": None,
                "lat": lat, "lon": lon, "n": cnt, "count": cnt,
                "minLat": lat, "maxLat": lat, "minLon": lon, "maxLon": lon,
            })
    return regions


def _build_forecast_tables(visit_rows, activity_rows, regions):
    def nearest_region(lat, lon):
        best_i, best_d = 0, None
        for i, rg in enumerate(regions):
            d = _dist_deg(lat, lon, rg["lat"], rg["lon"])
            if best_d is None or d < best_d:
                best_d, best_i = d, i
        return best_i

    model = {
        "region_act": {}, "region_act_total": {}, "region_act_marg": {}, "region_act_dist": {},
        "region_visit_type": {}, "region_visit_type_total": {},
        "region_top_place": {}, "region_top_place_total": {},
        "region_visit_marg": {}, "region_place_marg": {},
        "place_visit": {}, "place_total_visits": {}, "place_type_marg": {}, "place_region": {},
    }

    def cget2d(d, k):
        # d[k][subkey] += 1  -> returns the inner dict for k, creating as needed
        return d.setdefault(k, {})

    for r in activity_rows:
        dow, hour, lat, lon, atype, dist = r
        rid = nearest_region(lat, lon)
        k = (dow, hour)
        cell = cget2d(model["region_act"], rid)
        row = cell.setdefault(k, {})
        row[atype] = row.get(atype, 0) + 1
        t = model["region_act_total"].setdefault(rid, {})
        t[k] = t.get(k, 0) + 1
        m = model["region_act_marg"].setdefault(rid, {})
        m[atype] = m.get(atype, 0) + 1
        d = model["region_act_dist"].setdefault(rid, {}).setdefault(atype, {"count": 0, "km": 0.0})
        d["count"] += 1
        d["km"] += (dist or 0) / 1000.0

    for r in visit_rows:
        dow, hour, lat, lon, pid, stype = r
        rid = nearest_region(lat, lon)
        k = (dow, hour)
        vtc = cget2d(model["region_visit_type"], rid)
        vrow = vtc.setdefault(k, {})
        vrow[stype] = vrow.get(stype, 0) + 1
        vtt = model["region_visit_type_total"].setdefault(rid, {})
        vtt[k] = vtt.get(k, 0) + 1
        tpc = cget2d(model["region_top_place"], rid)
        prow = tpc.setdefault(k, {})
        prow[pid] = prow.get(pid, 0) + 1
        tpt = model["region_top_place_total"].setdefault(rid, {})
        tpt[k] = tpt.get(k, 0) + 1
        vm = model["region_visit_marg"].setdefault(rid, {})
        vm[stype] = vm.get(stype, 0) + 1
        pm = model["region_place_marg"].setdefault(rid, {})
        pm[pid] = pm.get(pid, 0) + 1
        pv = model["place_visit"].setdefault(pid, {})
        pv[k] = pv.get(k, 0) + 1
        model["place_total_visits"][pid] = model["place_total_visits"].get(pid, 0) + 1
        model["place_region"][pid] = rid
        ptm = model["place_type_marg"].setdefault(pid, {})
        ptm[stype] = ptm.get(stype, 0) + 1

    return model


def build_forecast(visit_rows, activity_rows):
    regions = _cluster_regions(visit_rows, activity_rows)
    model = _build_forecast_tables(visit_rows, activity_rows, regions)
    return {"regions": regions, "model": model}


def _dows_for(dow):
    """Map a dow selector to a set of weekday indexes (0=Mon)."""
    if dow in ("all", None, "any"):
        return set(range(7))
    if dow == "weekend":
        return {5, 6}  # Sat, Sun
    if dow == "weekday":
        return {0, 1, 2, 3, 4}
    try:
        d = int(dow)
        return {d % 7}
    except (TypeError, ValueError):
        return set(range(7))


def _smooth_dist(counts, total, marg_counts, label_space):
    """Blend cell distribution (with Laplace smoothing) toward the marginal."""
    # cell probs with Laplace
    alpha = SMOOTH_ALPHA
    cell_total = float(total) if total else 0.0
    k = float(max(1, len(label_space)))
    cell = {lab: (counts.get(lab, 0) + alpha) / (cell_total + alpha * k) for lab in label_space}
    # marginal probs
    marg_total = float(sum(marg_counts.values())) or 1.0
    marg = {lab: marg_counts.get(lab, 0) / marg_total for lab in label_space}
    # shrink toward marginal when cell is sparse
    w = 1.0 if cell_total == 0 else SHRINK_CELLS / (SHRINK_CELLS + cell_total)
    merged = {lab: (1 - w) * cell[lab] + w * marg[lab] for lab in label_space}
    return merged


def _label_space_from(marg_counts, preferred):
    # union of preferred (fixed) labels and those actually seen
    return list(dict.fromkeys(list(preferred) + list(marg_counts.keys())))


def _top(dist, n=5):
    return sorted(dist.items(), key=lambda kv: -kv[1])[:n]


def forecast_for(model, regions, entity, dow_sel):
    """Compute an hourly schedule for a region or place entity + day selector."""
    dows = _dows_for(dow_sel)
    if entity.startswith("place:"):
        pid = entity[len("place:"):]
        rid = model["place_region"].get(pid)
        is_place = True
    elif entity.startswith("region:"):
        rid = int(entity[len("region:"):])
        pid = None
        is_place = False
    else:
        return None
    if rid is None or rid not in {r["id"] for r in regions}:
        return None

    ract = model["region_act"].get(rid, {})
    ract_marg = model["region_act_marg"].get(rid, {})
    ract_tot = model["region_act_total"].get(rid, {})
    rdist = model["region_act_dist"].get(rid, {})
    rtop = model["region_top_place"].get(rid, {})
    rtop_tot = model["region_top_place_total"].get(rid, {})
    rvis_marg = model["region_visit_marg"].get(rid, {})
    rplace_marg = model["region_place_marg"].get(rid, {})
    place_vis = model["place_visit"].get(pid, {}) if pid else {}
    place_type_marg = model["place_type_marg"].get(pid, {}) if pid else {}

    act_space = _label_space_from(ract_marg, ACTIVITY_LABELS)
    visit_type_space = _label_space_from(rvis_marg, ["HOME", "WORK", "UNKNOWN", "SEARCHED_ADDRESS",
                                                     "INFERRED_HOME", "INFERRED_WORK", "ALIASED_LOCATION"])
    place_space = _label_space_from(rplace_marg, rplace_marg.keys())

    hours = []
    act_marg_total = sum(ract_marg.values()) or 1
    for h in range(24):
        # aggregate day counts into this hour
        act_counts = {}
        place_counts = {}
        visit_type_counts = {}
        for d in dows:
            k = (d, h)
            for lab, c in ract.get(k, {}).items():
                act_counts[lab] = act_counts.get(lab, 0) + c
            for lab, c in rtop.get(k, {}).items():
                place_counts[lab] = place_counts.get(lab, 0) + c
            for lab, c in model["region_visit_type"].get(rid, {}).get(k, {}).items():
                visit_type_counts[lab] = visit_type_counts.get(lab, 0) + c
        cell_total = sum(act_counts.values())
        act_dist = _smooth_dist(act_counts, cell_total, ract_marg, act_space)
        visit_type_dist = _smooth_dist(visit_type_counts, sum(visit_type_counts.values()),
                                       rvis_marg, visit_type_space)
        place_dist = _smooth_dist(place_counts, sum(place_counts.values()), rplace_marg, place_space)

        # place presence: for a specific place, how engaged at this hour?
        if is_place:
            p_hour = sum(place_vis.get((d, h), 0) for d in dows)
        else:
            p_hour = sum(place_counts.get(lab, 0) for lab in place_counts) + sum(act_counts.values())
        total_hour_obs = p_hour

        top_act = _top(act_dist, 3)
        top_places = _top(place_dist, 3)
        hours.append({
            "hour": h,
            "total": total_hour_obs,
            "atPlace": 1.0 if not is_place else min(1.0, total_hour_obs / max(1, SHRINK_CELLS)),
            "topActivities": [{"type": a, "prob": round(p, 3)} for a, p in top_act],
            "topPlaces": [{"name": str(pl), "prob": round(p, 3)} for pl, p in top_places],
            "topTypes": [{"type": a, "prob": round(p, 3)} for a, p in _top(visit_type_dist, 2)],
        })

    # day summary: aggregate per-hour cells restricted to the selected days
    day_activity = {}   # label -> total event count across selected days
    day_places = {}
    day_type = {}
    day_tot = 0         # total activity events across selected days
    day_visit_tot = 0   # total visit events across selected days
    dist_km = {}
    for d in dows:
        for h in range(24):
            k = (d, h)
            n_act = ract_tot.get(k, 0)
            day_tot += n_act
            for lab, c in ract.get(k, {}).items():
                day_activity[lab] = day_activity.get(lab, 0) + c
            for lab, c in rtop.get(k, {}).items():
                day_places[lab] = day_places.get(lab, 0) + c
            for lab, c in model["region_visit_type"].get(rid, {}).get(k, {}).items():
                day_type[lab] = day_type.get(lab, 0) + c
                day_visit_tot += c
        # distance marginals are global; use overall mean per activity as a proxy count
        for atype, dd in rdist.items():
            mean = dd["km"] / max(1, dd["count"])
            dist_km[atype] = dist_km.get(atype, 0) + mean * (day_activity.get(atype, 0) /
                                                             max(1, day_tot or 1))
    day_dist_km = sum(dist_km.values())
    day_act_norm = {lab: c / max(1, day_tot) for lab, c in day_activity.items()}
    day_act_top = sorted(day_act_norm.items(), key=lambda kv: -kv[1])[:6]
    day_pl_top = sorted(day_places.items(), key=lambda kv: -kv[1])[:8]
    day_type_top = sorted(day_type.items(), key=lambda kv: -kv[1])[:4]
    day_places_total = sum(day_places.values())
    day_type_total = sum(day_type.values())

    region = next((r for r in regions if r["id"] == rid), None)
    rname = (region or {}).get("name")
    entity_name = rname if not is_place else pid

    narrative = _build_narrative(hours, is_place)

    span_days = "Day " + ("/".join(str(d) for d in sorted(dows)))

    return {
        "entityType": "place" if is_place else "region",
        "entityName": entity_name,
        "regionName": rname,
        "regionId": rid,
        "dow": span_days,
        "hours": hours,
        "summary": {
            "activityTop": [{"type": a, "prob": round(p, 3)} for a, p in day_act_top],
            "placesTop": [{"place": str(pl), "prob": round(c / max(1, day_places_total), 3)}
                          for pl, c in day_pl_top],
            "typesTop": [{"type": a, "prob": round(c / max(1, day_type_total), 3)}
                         for a, c in day_type_top],
            "distanceKm": round(day_dist_km, 1),
            "activityCount": day_tot,
            "visitCount": day_visit_tot,
            "engagement": round(min(1.0, day_visit_tot / max(1, len(dows) * 1.0)) , 2),
            "regionName": rname,
        },
        "narrative": narrative,
    }


def _build_narrative(hours, is_place):
    """Compress the hourly schedule into a human-readable day timeline based on
    the dominant place type (region) or presence (place)."""
    merged = []
    for h in range(24):
        entry = hours[h]
        if is_place:
            act = entry["topActivities"][0] if entry["topActivities"] else None
            at = entry["atPlace"] or 0
            if at > 0.6:
                label = "HERE"
            elif act and act["prob"] > 0.15:
                label = act["type"].replace("_", " ").title()
            else:
                label = None
            merged.append((h, label))
        else:
            tt = entry["topTypes"]
            style = tt[0]["type"] if tt else None
            p = tt[0]["prob"] if tt else 0
            merged.append((h, style if p > 0.4 else None))

    def pretty(t):
        if not t:
            return None
        if t == "HERE":
            return "at this place"
        if t in ("UNKNOWN", "SEARCHED_ADDRESS", "ALIASED_LOCATION", "OTHER"):
            return "out & about (unlabeled spot)"
        return "at " + t.replace("_", " ").title()

    blocks = []
    cur = None
    start = 0
    for h, state in merged:
        if state != cur:
            if cur is not None:
                blocks.append((start, h - 1, cur))
            cur = state
            start = h
    if cur is not None:
        blocks.append((start, 23, cur))

    # drop a long leading sleep/quiet block
    out = []
    started = False
    for s, e, state in blocks:
        if not started and state is None and e < 8:
            continue
        started = True
        if state is None:
            out.append(f"{s:02d}:00 to {e:02d}:00 \u2192 no strong signal")
        else:
            out.append(f"{s:02d}:00 to {e:02d}:00 \u2192 {pretty(state)}")
    return out or ["No strong activity signal for this selection."]


# ---------------------------------------------------------------- geo helpers
REGION_DB_TABLE = "regions"


def region_names_from_db():
    conn = db_conn()
    try:
        rows = conn.execute("SELECT id, name, address FROM regions").fetchall()
        return {int(r[0]): (r[1], r[2]) for r in rows if r[1]}
    finally:
        conn.close()


def cache_region_name(rid, name, address):
    conn = db_conn()
    try:
        conn.execute("INSERT OR REPLACE INTO regions(id, name, address) VALUES(?,?,?)",
                     (int(rid), name, address))
        conn.commit()
    finally:
        conn.close()


def geocode_region_names():
    """Background: reverse-geocode one centroid per unnamed region and cache it."""
    conn = db_conn()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS regions(id INTEGER PRIMARY KEY, name TEXT, address TEXT)")
        conn.commit()
    finally:
        conn.close()
    regions = DATA.get("regions", [])
    cache = region_names_from_db()
    for rg in regions:
        if rg["name"]:
            continue
        if rg["id"] in cache:
            continue
        status, res = nominatim_reverse(rg["lat"], rg["lon"])
        if status == "ok":
            addr = res.get("address") or {}
            state = (addr.get("state") or addr.get("region")
                     or addr.get("country") or addr.get("city")
                     or addr.get("town"))
            name = state or short_label(res)
        elif status == "retry":
            break
        else:
            name = f"Region {rg['id']}"
        rg["name"] = name
        rg["address"] = res.get("display_name") if status == "ok" else None
        cache_region_name(rg["id"], name, rg["address"])
        time.sleep(GEOCODE_DELAY)


def load_data():
    """Parses Timeline.json once and precomputes per-year structures."""
    if DATA.get("ready"):
        return DATA
    with _load_lock:
        if DATA.get("ready"):
            return DATA
        print(f"Loading {TIMELINE_FILE} ...")
        t0 = time.time()
        with open(TIMELINE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        print(f"  parsed in {time.time() - t0:.1f}s")

        segments = raw["semanticSegments"]

        heat_by_year = defaultdict(lambda: defaultdict(int))  # year -> {(round4lat, round4lon): count}
        modes_all = defaultdict(lambda: {"count": 0, "distanceKm": 0.0, "durationH": 0.0})
        modes_by_year = defaultdict(lambda: defaultdict(lambda: {"count": 0, "distanceKm": 0.0, "durationH": 0.0}))
        by_month = defaultdict(lambda: {"distanceKm": 0.0, "count": 0})   # "YYYY-MM"
        weekhour_by_year = defaultdict(lambda: [[0] * 24 for _ in range(7)])
        hour_by_year = defaultdict(lambda: [0] * 24)
        places = defaultdict(lambda: {
            "placeId": None, "type": None, "lat": None, "lon": None,
            "byYear": defaultdict(int), "yearDur": defaultdict(float),
            "count": 0, "durationH": 0.0, "lastSeen": None,
        })
        starts, ends = [], []
        bounds = {"minLat": 90, "maxLat": -90, "minLon": 180, "maxLon": -180}
        visit_rows = []      # (weekday, hour, lat, lon, place_id, semantic_type)
        activity_rows = []   # (weekday, hour, lat, lon, activity_type, distance_m)

        def add_heat(yr, lat, lon):
            if lat is None or lon is None:
                return
            heat_by_year[yr][(round(lat, 4), round(lon, 4))] += 1
            bounds["minLat"] = min(bounds["minLat"], lat)
            bounds["maxLat"] = max(bounds["maxLat"], lat)
            bounds["minLon"] = min(bounds["minLon"], lon)
            bounds["maxLon"] = max(bounds["maxLon"], lon)

        for seg in segments:
            st = parse_ts(seg.get("startTime"))
            et = parse_ts(seg.get("endTime"))
            if st:
                starts.append(st)
            if et:
                ends.append(et)

            iso_off = seg.get("startTimeTimezoneUtcOffsetMinutes")
            if iso_off is None and st and st.tzinfo is not None:
                iso_off = int(st.utcoffset().total_seconds() // 60)
            local = localize(st, iso_off) if st else None
            yr = local.year if local else None

            if "timelinePath" in seg:
                for pt in seg["timelinePath"]:
                    ll = parse_latlng(pt.get("point"))
                    p_yr = yr
                    pt_time = parse_ts(pt.get("time"))
                    if pt_time and pt_time.tzinfo is not None:
                        p_yr = localize(pt_time, int(pt_time.utcoffset().total_seconds() // 60)).year
                    elif pt_time:
                        p_yr = pt_time.year
                    add_heat(p_yr, ll[0], ll[1])

            if "activity" in seg:
                a = seg["activity"]
                start_ll = parse_latlng(a.get("start", {}).get("latLng"))
                end_ll = parse_latlng(a.get("end", {}).get("latLng"))
                for key in ("start", "end"):
                    ll = parse_latlng(a.get(key, {}).get("latLng"))
                    if ll:
                        add_heat(yr, ll[0], ll[1])
                mode = a.get("topCandidate", {}).get("type", "UNKNOWN_ACTIVITY_TYPE")
                dist = a.get("distanceMeters") or 0.0
                if local is not None and start_ll:
                    activity_rows.append((local.weekday(), local.hour, start_ll[0], start_ll[1],
                                          mode, dist))
                dur_h = ((et - st).total_seconds() / 3600.0) if (st and et) else 0.0
                modes_all[mode]["count"] += 1
                modes_all[mode]["distanceKm"] += dist / 1000.0
                modes_all[mode]["durationH"] += dur_h
                if yr:
                    m = modes_by_year[yr][mode]
                    m["count"] += 1
                    m["distanceKm"] += dist / 1000.0
                    m["durationH"] += dur_h
                    ym = f"{yr:04d}-{local.month:02d}"
                    by_month[ym]["distanceKm"] += dist / 1000.0
                    by_month[ym]["count"] += 1
                    hour_by_year[yr][local.hour] += 1

            if "visit" in seg:
                v = seg["visit"].get("topCandidate", {})
                ll = parse_latlng(v.get("placeLocation", {}).get("latLng"))
                if ll:
                    add_heat(yr, ll[0], ll[1])
                if local is not None and ll:
                    pid = v.get("placeId") or f"loc,{round(ll[0],4)},{round(ll[1],4)}"
                    visit_rows.append((local.weekday(), local.hour, ll[0], ll[1],
                                       pid, v.get("semanticType") or "UNKNOWN"))
                pid = v.get("placeId")
                if pid:
                    rec = places[pid]
                    rec["placeId"] = pid
                    rec["type"] = v.get("semanticType") or "UNKNOWN"
                    if ll:
                        rec["lat"], rec["lon"] = ll
                    rec["count"] += 1
                    if st and et:
                        dur_h = (et - st).total_seconds() / 3600.0
                        rec["durationH"] += dur_h
                        if yr:
                            rec["yearDur"][yr] += dur_h
                    if yr:
                        rec["byYear"][yr] += 1
                        weekhour_by_year[yr][local.weekday()][local.hour] += 1
                    if rec["lastSeen"] is None or (st and st > rec["lastSeen"]):
                        rec["lastSeen"] = st

        # recent raw GPS pings
        raw_count = len(raw.get("rawSignals", []))
        for r in raw.get("rawSignals", []):
            pos = r.get("position") or {}
            ll = parse_latlng(pos.get("LatLng"))
            ts = parse_ts(pos.get("timestamp"))
            if ll and ts:
                off = None
                if ts.tzinfo is not None:
                    off = int(ts.utcoffset().total_seconds() // 60)
                add_heat(localize(ts, off).year, ll[0], ll[1])

        # dict -> lists (each point: [lat, lon, count])
        heat = {str(y): [[lat, lon, cnt] for (lat, lon), cnt in sorted(pts.items())]
                for y, pts in sorted(heat_by_year.items())}
        years_all = {int(y) for y in heat} | {int(k[:4]) for k in by_month}

        freep = raw.get("userLocationProfile", {}).get("frequentPlaces", [])
        frequent = []
        for fp in freep:
            ll = parse_latlng(fp.get("placeLocation"))
            if ll:
                frequent.append({"label": fp.get("label") or "PLACE", "lat": ll[0], "lon": ll[1]})

        home = None
        for rec in places.values():
            if rec["type"] == "HOME" and rec["lat"] is not None:
                if home is None or rec["count"] > home[2]:
                    home = (rec["lat"], rec["lon"], rec["count"])
        home_loc = {"lat": home[0], "lon": home[1]} if home else None
        if not home_loc and heat:
            lats = [p[0] for p in next(iter(heat.values()))]
            lons = [p[1] for p in next(iter(heat.values()))]
            home_loc = {"lat": sum(lats) / len(lats), "lon": sum(lons) / len(lons)}

        total_dist = sum(m["distanceKm"] for m in modes_all.values())
        total_act = sum(m["count"] for m in modes_all.values())
        months = sorted(by_month.items())

        place_list = []
        for pid, rec in places.items():
            if rec["lat"] is None:
                continue
            place_list.append({
                "placeId": pid,
                "type": rec["type"],
                "lat": rec["lat"],
                "lon": rec["lon"],
                "visits": rec["count"],
                "durationH": round(rec["durationH"], 2),
                "lastSeen": rec["lastSeen"].isoformat() if rec["lastSeen"] else None,
                "byYear": {str(y): int(v) for y, v in sorted(rec["byYear"].items())},
                "yearDur": {str(y): round(h, 2) for y, h in sorted(rec["yearDur"].items())},
            })

        months = sorted(by_month.items())
        forecast = build_forecast(visit_rows, activity_rows)
        DATA.update({
            "ready": True,
            "generated": datetime.now().isoformat(timespec="seconds"),
            "years": sorted(years_all),
            "heat": heat,
            "bounds": bounds,
            "home": home_loc,
            "modesAll": dict(modes_all),
            "modesByYear": {str(y): dict(m) for y, m in sorted(modes_by_year.items())},
            "byMonth": {"years": [k for k, _ in months], "distanceKm": [round(v["distanceKm"], 1) for _, v in months], "count": [v["count"] for _, v in months]},
            "weekhour": {str(y): m for y, m in sorted(weekhour_by_year.items())},
            "hourActivity": {str(y): h for y, h in sorted(hour_by_year.items())},
            "places": place_list,
            "frequent": frequent,
            "regions": forecast["regions"],
            "forecast": forecast["model"],
            "summary": {
                "startDate": min(starts).date().isoformat() if starts else None,
                "endDate": max(ends or starts).date().isoformat() if (ends or starts) else None,
                "segments": len(segments),
                "totalDistanceKm": round(total_dist, 1),
                "totalActivities": total_act,
                "uniquePlaces": len(place_list),
                "rawSignals": raw_count,
            },
        })
        seed_address_db(place_list)
        print(f"ready in {time.time() - t0:.1f}s | heat points={sum(len(v) for v in heat.values())} "
              f"| places={len(place_list)} | years={DATA['years']}"
              f" | regions={len(forecast['regions'])}")
        return DATA


# ------------------------------------------------------- address cache + worker
def db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def seed_address_db(place_list):
    conn = db_conn()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS places(
            place_id TEXT PRIMARY KEY,
            lat REAL, lon REAL, visits INT,
            name TEXT, address TEXT, display TEXT,
            attempts INT DEFAULT 0)""")
        rows = [(p["placeId"], p["lat"], p["lon"], p["visits"]) for p in place_list]
        conn.executemany(
            """INSERT INTO places(place_id, lat, lon, visits) VALUES(?,?,?,?)
               ON CONFLICT(place_id) DO UPDATE SET
                 lat=excluded.lat, lon=excluded.lon, visits=excluded.visits""",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


WORKER = {"stop": False, "running": False, "thread": None, "done_cache": -1}


def short_label(n):
    addr = n.get("address", {}) or {}
    city = (addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("municipality") or addr.get("suburb"))
    name = (n.get("name") or addr.get("amenity") or addr.get("shop")
            or addr.get("tourism") or addr.get("leisure") or addr.get("building")
            or addr.get("road"))
    label = ", ".join(x for x in (name, city) if x)
    return label or n.get("display_name")


def nominatim_reverse(lat, lon):
    query = urllib.parse.urlencode({"lat": lat, "lon": lon, "format": "jsonv2"})
    req = urllib.request.Request(f"{NOMINATIM_URL}?{query}",
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return "ok", json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (429, 403, 500, 502, 503, 504):
            return "retry", None
        return "fail", None
    except Exception:
        return "fail", None


def address_done():
    conn = db_conn()
    try:
        cur = conn.execute("SELECT COUNT(*) FROM places WHERE name IS NOT NULL AND address IS NOT NULL")
        return cur.fetchone()[0]
    finally:
        conn.close()


def geocode_worker():
    conn = db_conn()
    try:
        while True:
            if WORKER["stop"]:
                break
            row = conn.execute(
                """SELECT place_id, lat, lon FROM places
                   WHERE (name IS NULL OR address IS NULL) AND attempts < 4
                   ORDER BY visits DESC LIMIT 1"""
            ).fetchone()
            if not row:
                break
            time.sleep(GEOCODE_DELAY)
            pid, lat, lon = row
            status, res = nominatim_reverse(lat, lon)
            if status == "ok":
                conn.execute(
                    "UPDATE places SET name=?, address=?, display=?, attempts=attempts+1 WHERE place_id=?",
                    (short_label(res), res.get("display_name"), res.get("display_name"), pid),
                )
            else:
                conn.execute("UPDATE places SET attempts=attempts+1 WHERE place_id=?", (pid,))
            conn.commit()
            if status == "retry":
                time.sleep(3.0)
    except Exception as e:
        print("geocode worker error:", e)
    finally:
        WORKER["stop"] = False
        WORKER["running"] = False
        conn.close()


def ensure_worker():
    if (not AUTO_GEOCODE) or WORKER["running"]:
        return
    with threading.Lock():
        if WORKER["running"]:
            return
        conn = db_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM places WHERE (name IS NULL OR address IS NULL) AND attempts < 4"
            ).fetchone()
        finally:
            conn.close()
        if not row or row[0] == 0:
            return
        WORKER["stop"] = False
        WORKER["running"] = True
        t = threading.Thread(target=geocode_worker, daemon=True)
        WORKER["thread"] = t
        t.start()


REGION_WORKER = {"started": False}


def ensure_region_names():
    if REGION_WORKER["started"]:
        return
    if (not AUTO_GEOCODE) or not DATA.get("regions"):
        return
    REGION_WORKER["started"] = True
    t = threading.Thread(target=geocode_region_names, daemon=True)
    t.start()


# ------------------------------------------------------------------- routes
@app.route("/")
def index():
    vendor = all(os.path.isfile(os.path.join(VENDOR_DIR, f)) for f in VENDOR_FILES)
    sub = {
        "__LEAFLET_CSS__": f"/static/vendor/leaflet.css" if vendor else VENDOR_FILES["leaflet.css"],
        "__LEAFLET_JS__": f"/static/vendor/leaflet.js" if vendor else VENDOR_FILES["leaflet.js"],
        "__HEAT_JS__": f"/static/vendor/leaflet-heat.js" if vendor else VENDOR_FILES["leaflet-heat.js"],
        "__CHART_JS__": f"/static/vendor/chart.umd.min.js" if vendor else VENDOR_FILES["chart.umd.min.js"],
        "__MAPLIBRE_CSS__": f"/static/vendor/maplibre-gl.css" if vendor else VENDOR_FILES["maplibre-gl.css"],
        "__MAPLIBRE_JS__": f"/static/vendor/maplibre-gl.js" if vendor else VENDOR_FILES["maplibre-gl.js"],
        "__MLGL_LEAFLET_JS__": f"/static/vendor/leaflet-maplibre-gl.js" if vendor else VENDOR_FILES["leaflet-maplibre-gl.js"],
    }
    with open(INDEX_TEMPLATE, "r", encoding="utf-8") as f:
        html = f.read()
    for k, v in sub.items():
        html = html.replace(k, v)
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(os.path.join(BASE, "static"), filename)


def _get_places_with_addresses():
    D = load_data()
    conn = db_conn()
    try:
        cache = {r[0]: (r[1], r[2], r[3]) for r in conn.execute("SELECT place_id, name, address, display FROM places")}
    finally:
        conn.close()
    out = []
    for p in D["places"]:
        name, addr, disp = cache.get(p["placeId"], (None, None, None))
        p = dict(p)
        p["name"] = name or (p["type"].replace("_", " ").title() if p["type"] else None)
        p["address"] = disp or addr
        out.append(p)
    return out


@app.route("/api/summary")
def api_summary():
    D = load_data()
    ensure_worker()
    ensure_region_names()
    return jsonify({
        "generated": D["generated"],
        "years": D["years"],
        "bounds": D["bounds"],
        "home": D["home"],
        "summary": D["summary"],
        "frequentPlaces": D["frequent"],
        "geocode": {
            "total": len(D["places"]),
            "done": address_done(),
            "running": WORKER["running"],
            "auto": AUTO_GEOCODE,
        },
    })


@app.route("/api/heat")
def api_heat():
    return jsonify({"byYear": load_data()["heat"]})


@app.route("/api/places")
def api_places():
    year = request.args.get("year")
    places = _get_places_with_addresses()
    if year and year != "all":
        y = str(year)
        places = [p for p in places if int(p["byYear"].get(y, 0)) > 0]
    return jsonify(places)


@app.route("/api/modes")
def api_modes():
    D = load_data()
    year = request.args.get("year", "all")
    src = D["modesAll"] if year in (None, "", "all") else D["modesByYear"].get(str(year), {})
    labels = sorted(src.keys())
    return jsonify({
        "year": year,
        "labels": labels,
        "distanceKm": [round(src[k]["distanceKm"], 1) for k in labels],
        "count": [src[k]["count"] for k in labels],
        "durationH": [round(src[k]["durationH"], 1) for k in labels],
    })


@app.route("/api/monthly")
def api_monthly():
    return jsonify(load_data()["byMonth"])


@app.route("/api/rhythm")
def api_rhythm():
    D = load_data()
    year = request.args.get("year", "all")
    if year in (None, "", "all"):
        wk = [[0] * 24 for _ in range(7)]
        hr = [0] * 24
        for m in D["weekhour"].values():
            for d in range(7):
                for h in range(24):
                    wk[d][h] += m[d][h]
        for h in D["hourActivity"].values():
            for i in range(24):
                hr[i] += h[i]
    else:
        wk = D["weekhour"].get(str(year), [[0] * 24 for _ in range(7)])
        hr = D["hourActivity"].get(str(year), [0] * 24)
    return jsonify({"weekday": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                    "matrix": wk, "hourActivity": hr})


@app.route("/api/geocode")
def api_geocode():
    D = load_data()
    return jsonify({
        "total": len(D["places"]),
        "done": address_done(),
        "running": WORKER["running"],
        "auto": AUTO_GEOCODE,
        "delay": GEOCODE_DELAY,
    })


@app.route("/api/forecast/entities")
def api_forecast_entities():
    D = load_data()
    MING = 40  # only expose regions with at least this many events (reliable forecasts)
    regions = [{
        "id": r["id"], "name": r["name"] or f"Region {r['id']}",
        "lat": round(r["lat"], 4), "lon": round(r["lon"], 4),
        "count": r["count"],
    } for r in D["regions"] if r["count"] >= MING]
    places = []
    for p in sorted(_get_places_with_addresses(), key=lambda x: -x["visits"])[:40]:
        places.append({
            "id": p["placeId"], "name": p["name"] or p["type"].replace("_", " ").title(),
            "address": p["address"], "type": p["type"],
            "lat": round(p["lat"], 6), "lon": round(p["lon"], 6), "visits": p["visits"],
        })
    return jsonify({"regions": regions, "places": places})


def _place_label_map():
    """place_id -> friendly display label (name or type title)."""
    out = {}
    for p in _get_places_with_addresses():
        name = p.get("name") or p["type"].replace("_", " ").title() or "place"
        out[p["placeId"]] = name
    return out


def _entity_name(entity, regions, labels):
    """Return (entityType, displayName) for a 'region:<id>' / 'place:<placeId>' entity."""
    if entity.startswith("place:"):
        pid = entity[len("place:"):]
        if pid.startswith("loc,"):
            return "place", "Unnamed place"
        return "place", labels.get(pid) or pid.replace("_", " ").title()
    try:
        rid = int(entity[len("region:"):])
    except (ValueError, TypeError):
        rid = 0
    for r in regions or []:
        if r.get("id") == rid:
            return "region", r.get("name") or f"Region {rid}"
    return "region", f"Region {rid}"


def _decorate_forecast(fc, labels, entity, regions):
    def label(pid):
        if pid.startswith("loc,"):
            return "Unnamed place"
        return labels.get(pid, pid)

    for h in fc.get("hours", []):
        for tp in h.get("topPlaces", []):
            tp["name"] = label(tp["name"])
            tp["id"] = tp["name"]
    for tp in fc.get("summary", {}).get("placesTop", []):
        tp["place"] = label(tp["place"])
    fc["entityType"], fc["entityName"] = _entity_name(entity, regions, labels)
    return fc


@app.route("/api/forecast")
def api_forecast():
    D = load_data()
    entity = request.args.get("entity", "")
    dow = request.args.get("dow", "all")
    if not entity:
        return jsonify({"error": "entity required (region:<id> or place:<placeId>)"}), 400
    fc = forecast_for(D["forecast"], D["regions"], entity, dow)
    if fc is None:
        return jsonify({"error": "unknown entity"}), 404
    labels = _place_label_map()
    return jsonify(_decorate_forecast(fc, labels, entity, D["regions"]))


@app.route("/api/geocode/start", methods=["POST"])
def api_geocode_start():
    ensure_worker()
    return jsonify({"running": WORKER["running"]})


@app.route("/api/geocode/pause", methods=["POST"])
def api_geocode_pause():
    WORKER["stop"] = True
    return jsonify({"running": WORKER["running"]})


# ---------------------------------------------------------------- vendor deps
def ensure_vendor():
    os.makedirs(VENDOR_DIR, exist_ok=True)
    for fname, url in VENDOR_FILES.items():
        dest = os.path.join(VENDOR_DIR, fname)
        if os.path.isfile(dest):
            continue
        try:
            print(f"downloading {fname} ...")
            req = urllib.request.Request(url, headers={"User-Agent": "timeline-dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=90) as r, open(dest, "wb") as out:
                out.write(r.read())
        except Exception as e:
            print(f"  vendor download failed for {fname}: {e}")
            try:
                os.remove(dest)
            except OSError:
                pass


if __name__ == "__main__":
    ensure_vendor()
    load_data()
    ensure_worker()
    ensure_region_names()
    print(f"Serving on http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)