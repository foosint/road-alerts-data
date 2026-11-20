"""Keep data/roads.geojson in sync with the roads.json alert feed.

Each run:
  1. downloads roads.json (or uses the local copy with --offline)
  2. resolves city coordinates: overrides.json > cache/cities.json > Nominatim geocoding
  3. routes a segment (public OSRM server, OpenStreetMap data) only when it is new or
     its inputs changed (endpoint coordinates or an entry in overrides.json);
     every other route is reused unchanged from cache/routes.json
  4. writes data/roads.geojson (status, updatedAt, colour per segment and
     the feed's source.updatedAt) and data/cities.geojson - but only if a segment was
     added/removed, its status, updatedAt or route changed, or source.updatedAt changed
  5. merges each road's latestActivations (a rolling window in the feed) and every
     status change into data/alerts.json, a permanent log that only grows
  6. fire signals: downloads the NASA FIRMS 24h hotspot files (at most every
     FIRMS_EVERY_MIN minutes) and writes data/fires.geojson with the hotspots of the
     last 24 hours within FIRMS_MAX_KM of a road segment
  7. if PUBLISH=1: commits data/ in this repo and pushes it (only when something
     changed); the road-alerts site reads the files from this repo

Settings come from environment variables or a .env file next to this script
(see .env.example).

Usage:
  uv run build_geojson.py --url https://.../roads.json   (or set ROADS_URL)
  uv run build_geojson.py --offline                      (use the local roads.json)
  uv run build_geojson.py --no-publish                   (never push)
"""

import argparse
import csv
import io
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
ROADS_JSON = HERE / "roads.json"
OVERRIDES = HERE / "overrides.json"
CITY_CACHE = HERE / "cache" / "cities.json"
ROUTE_CACHE = HERE / "cache" / "routes.json"
DATA_DIR = HERE / "data"  # published: committed and pushed on every change
OUT_ROADS = DATA_DIR / "roads.geojson"
OUT_CITIES = DATA_DIR / "cities.geojson"
OUT_ALERTS = DATA_DIR / "alerts.json"  # permanent log of alert activations
OUT_FIRES = DATA_DIR / "fires.geojson"  # FIRMS hotspots near the roads, last 24 h
FIRMS_CACHE = HERE / "cache" / "firms.json"

# NASA FIRMS near-real-time 24h files, no API key needed. The Russia_Asia region starts
# at 26 deg E and covers all roads; the Europe region stops at about 35 deg E.
FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov/data/active_fire/"
FIRMS_SOURCES = {  # file -> satellite name shown on the map
    "suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_Russia_Asia_24h.csv": "Suomi NPP (VIIRS)",
    "noaa-20-viirs-c2/csv/J1_VIIRS_C2_Russia_Asia_24h.csv": "NOAA-20 (VIIRS)",
    "noaa-21-viirs-c2/csv/J2_VIIRS_C2_Russia_Asia_24h.csv": "NOAA-21 (VIIRS)",
    "modis-c6.1/csv/MODIS_C6_1_Russia_Asia_24h.csv": "MODIS",
}
FIRMS_HOURS = 24

OSRM = "https://router.project-osrm.org/route/v1/driving/{coords}?overview=full&geometries=geojson"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
GEOCODE_VIEWBOX = "32.0,50.0,41.0,44.2"  # lon/lat box around Crimea, south Ukraine, Donbas, Rostov
GEOCODE_MAX_KM = 150  # a geocoded town must be this close to the other end of its road
UA = {"User-Agent": "road-geojson-builder/1.0"}

# Line colour per status (simplestyle "stroke"). Unknown statuses get UNKNOWN_COLOR and a warning.
STATUS_COLORS = {
    "neutral": "#2f9e44",
    "busy": "#e03131",
}
UNKNOWN_COLOR = "#ae3ec9"


# ---------------------------------------------------------------- helpers

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, data, indent=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
    tmp.replace(path)


def http_get(url, retries=4):
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"  retry {url[:60]}... ({e})")
            time.sleep(3 * (attempt + 1))


def haversine_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def latlon(v):
    """Accept [lat, lon] or {"lat":..,"lon":..}; return (lat, lon) rounded."""
    if isinstance(v, dict):
        v = (v["lat"], v["lon"])
    return (round(float(v[0]), 6), round(float(v[1]), 6))


# ---------------------------------------------------------------- feed

def load_feed(url, offline):
    if offline or not url:
        if not offline:
            print("No --url / ROADS_URL given, using local roads.json")
        return json.loads(ROADS_JSON.read_text(encoding="utf-8"))
    print(f"Downloading {url}")
    raw = http_get(url)
    data = json.loads(raw)  # validate before overwriting the local copy
    if "roads" not in data:
        raise SystemExit("Downloaded file has no 'roads' key - aborting, nothing changed.")
    ROADS_JSON.write_bytes(raw)
    return data


# ---------------------------------------------------------------- cities

def geocode(name, near=None):
    q = urllib.parse.urlencode({
        "q": name, "format": "jsonv2", "limit": 10, "viewbox": GEOCODE_VIEWBOX,
        "bounded": 1, "accept-language": "ru",
    })
    hits = json.loads(http_get(f"{NOMINATIM}?{q}"))
    time.sleep(1.1)  # Nominatim usage policy: max 1 request/second
    hits = [h for h in hits if h["category"] in ("place", "boundary")] or hits
    if not hits:
        return None, []
    cands = [((float(h["lat"]), float(h["lon"])), h["display_name"]) for h in hits]
    if near:
        # Road endpoints are close to each other: drop namesakes in other oblasts, then keep
        # Nominatim's ranking (a real town outranks a same-named village that is merely closer).
        close = [c for c in cands if haversine_km(c[0], near) <= GEOCODE_MAX_KM]
        cands = close + [c for c in cands if c not in close]
    return cands[0], cands[1:]


def resolve_cities(roads, overrides, city_cache):
    names, neighbours = {}, {}
    for r in roads:
        a, b = r["fromCityId"], r["toCityId"]
        names[a], names[b] = r["from"], r["to"]
        neighbours.setdefault(a, set()).add(b)
        neighbours.setdefault(b, set()).add(a)

    coords = {cid: latlon(c) for cid, c in city_cache.items()}
    for cid, c in overrides.get("cities", {}).items():
        coords[cid] = latlon(c)

    missing = [cid for cid in names if cid not in coords]
    while missing:
        # geocode cities with an already-known neighbour first, so the neighbour can disambiguate
        missing.sort(key=lambda c: not any(n in coords for n in neighbours[c]))
        cid = missing.pop(0)
        near = next((coords[n] for n in neighbours[cid] if n in coords), None)
        best, others = geocode(names[cid], near)
        if not best:
            print(f"!! could not geocode {cid} ({names[cid]}) - add it to overrides.json 'cities'")
            continue
        coords[cid] = latlon(best[0])
        city_cache[cid] = {"name": names[cid], "lat": coords[cid][0], "lon": coords[cid][1],
                           "source": "nominatim", "match": best[1], "geocodedAt": now_iso()}
        print(f"++ geocoded {cid} ({names[cid]}) -> {coords[cid]}  {best[1][:80]}")
        for o in others[:3]:
            print(f"     alternative {latlon(o[0])}  {o[1][:80]}")
    return coords, names


# ---------------------------------------------------------------- routes

def route_key(inputs):
    return hashlib.sha1(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def osrm_route(points):
    coords = ";".join(f"{lon},{lat}" for lat, lon in points)
    data = json.loads(http_get(OSRM.format(coords=coords)))
    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM: {data.get('code')} {data.get('message')}")
    time.sleep(1)
    rt = data["routes"][0]
    return rt["geometry"], round(rt["distance"] / 1000, 1)


def line_length_km(geometry):
    c = geometry["coordinates"]
    return round(sum(haversine_km((a[1], a[0]), (b[1], b[0])) for a, b in zip(c, c[1:])), 1)


def build_route(road, coords, overrides, route_cache):
    """Return the cache entry for a road; re-route only if the inputs changed."""
    rid = road["id"]
    ov = overrides.get("roads", {}).get(rid, {})
    inputs = {
        "from": latlon(ov["from"]) if "from" in ov else coords[road["fromCityId"]],
        "to": latlon(ov["to"]) if "to" in ov else coords[road["toCityId"]],
        "via": [latlon(v) for v in ov.get("via", [])],
        "geometry": ov.get("geometry"),
    }
    key = route_key(inputs)
    cached = route_cache.get(rid)
    if cached and cached["key"] == key:
        return cached, False

    if inputs["geometry"]:
        geometry, km, how = inputs["geometry"], line_length_km(inputs["geometry"]), "manual geometry"
    else:
        geometry, km = osrm_route([inputs["from"], *inputs["via"], inputs["to"]])
        how = "OSRM" + (f" via {len(inputs['via'])} point(s)" if inputs["via"] else "")
    straight = haversine_km(inputs["from"], inputs["to"])
    ratio = km / straight if straight else 0
    flag = "  <-- check, long detour" if ratio > 1.6 else ""
    print(f"{'~~ re-routed' if cached else '++ routed'} {rid}: {km} km ({how}, x{ratio:.2f} straight line){flag}")
    entry = {"key": key, "inputs": inputs, "geometry": geometry, "distance_km": km, "routedAt": now_iso()}
    route_cache[rid] = entry
    return entry, True


# ---------------------------------------------------------------- change detection

def describe_changes(old, new):
    """List what differs between two roads FeatureCollections.

    A new source.updatedAt counts too, so the map can show when the feed was last
    updated. generatedAt and the other source fields are ignored.
    """
    if not old:
        return ["initial build"]
    o = {f["properties"]["id"]: f for f in old.get("features", [])}
    n = {f["properties"]["id"]: f for f in new["features"]}
    both = o.keys() & n.keys()
    counts = {
        "new": len(n.keys() - o.keys()),
        "removed": len(o.keys() - n.keys()),
        "status": sum(o[i]["properties"].get("status") != n[i]["properties"]["status"] for i in both),
        "updatedAt": sum(o[i]["properties"].get("updatedAt") != n[i]["properties"]["updatedAt"] for i in both),
        "route": sum(o[i]["geometry"] != n[i]["geometry"] for i in both),
    }
    changes = [f"{v} {k}" for k, v in counts.items() if v]
    if not changes and [f["properties"] for f in old.get("features", [])] != [f["properties"] for f in new["features"]]:
        changes.append("properties")  # e.g. activeAlert, order, colour mapping
    if (old.get("source") or {}).get("updatedAt") != (new.get("source") or {}).get("updatedAt"):
        changes.append("source updatedAt")
    return changes


# ---------------------------------------------------------------- alert log

def merge_alerts(roads, log, old_status):
    """Merge the feed into the alert log; return (log, new activation count, new status changes).

    events: the feed's latestActivations (threat / clear). The feed only keeps the last
        few per road, so the log is the only lasting record. One activation can cover
        several roads; it is stored once, with all its road ids.
    changes: every status change of a road (e.g. busy -> neutral) seen between two runs,
        timed by the road's updatedAt. Most threats end without a "clear" activation;
        the road simply turns neutral again, so this is how the end of a threat is known.
    statusSince: from when on status changes are recorded.

    Nothing is ever removed. Road names are kept for roads that leave the feed.
    """
    events = {e["id"]: dict(e, roads=list(e["roads"])) for e in log.get("events", [])}
    names = dict(log.get("roads", {}))
    status_changes = list(log.get("changes", []))
    if "statusSince" in log:
        since = log["statusSince"]
    else:  # first run with status tracking: recover past changes from the data commits
        since, recovered = status_history()
        status_changes += recovered
        if recovered:
            print(f"alert log: recovered {len(recovered)} status changes from git history since {since}")
    known = {(c["road"], c["at"], c["status"]) for c in status_changes}

    new = new_changes = 0
    for r in roads:
        names[r["id"]] = {"from": r["from"], "to": r["to"]}
        for a in r.get("latestActivations") or []:
            if not a.get("id") or not a.get("activatedAt"):
                continue
            e = events.get(a["id"])
            if e is None:
                e = events[a["id"]] = {"id": a["id"], "intent": a.get("intent"),
                                       "at": a["activatedAt"], "roads": []}
                new += 1
            if r["id"] not in e["roads"]:
                e["roads"] = sorted([*e["roads"], r["id"]])
        prev = old_status.get(r["id"])
        if prev is not None and prev != r.get("status"):
            c = {"road": r["id"], "status": r.get("status"), "from": prev, "at": r.get("updatedAt") or now_iso()}
            if (c["road"], c["at"], c["status"]) not in known:
                status_changes.append(c)
                known.add((c["road"], c["at"], c["status"]))
                new_changes += 1
    merged = {
        "statusSince": since,
        "roads": dict(sorted(names.items())),
        "events": sorted(events.values(), key=lambda e: (e["at"], e["id"])),
        "changes": sorted(status_changes, key=lambda c: (c["at"], c["road"])),
    }
    return merged, new, new_changes


def status_history():
    """Status changes found in the git history of data/roads.geojson; returns (since, changes)."""
    rel = OUT_ROADS.relative_to(HERE).as_posix()
    try:
        commits = git(HERE, "log", "--reverse", "--format=%H %cI", "--", rel).splitlines()
    except RuntimeError:
        commits = []
    since, prev, found = None, None, []
    for line in commits:
        sha, when = line.split()
        try:
            data = json.loads(git(HERE, "show", f"{sha}:{rel}"))
        except (RuntimeError, ValueError):
            continue
        cur = {f["properties"]["id"]: f["properties"] for f in data.get("features", [])}
        if since is None:
            since = datetime.fromisoformat(when).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        for rid, props in cur.items():
            if prev and rid in prev and prev[rid].get("status") != props.get("status"):
                found.append({"road": rid, "status": props.get("status"), "from": prev[rid].get("status"),
                              "at": props.get("updatedAt") or since})
        prev = cur
    return since or now_iso(), found


def save_alerts(path, log):
    """Write the log with one entry per line: small git diffs, still valid JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    dump = lambda v: json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    lines = ['{"statusSince":' + dump(log["statusSince"]) + ',"roads":{',
             ",\n".join(f"{dump(k)}:{dump(v)}" for k, v in log["roads"].items()),
             '},"events":[',
             ",\n".join(dump(e) for e in log["events"]),
             '],"changes":[',
             ",\n".join(dump(c) for c in log["changes"]),
             "]}\n"]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- FIRMS fire signals

def point_line_km(p, lines):
    """Shortest distance (km) from p = (lon, lat) to polylines [[lon, lat], ...].

    Uses a flat projection around p, accurate to well under 1 % at these distances.
    """
    kx = 111.32 * math.cos(math.radians(p[1]))
    ky = 110.57
    best = math.inf
    for line in lines:
        for a, b in zip(line, line[1:]):
            ax, ay = (a[0] - p[0]) * kx, (a[1] - p[1]) * ky
            bx, by = (b[0] - p[0]) * kx, (b[1] - p[1]) * ky
            dx, dy = bx - ax, by - ay
            seg = dx * dx + dy * dy
            t = 0 if seg == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg))
            best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def road_lines(feature):
    g = feature["geometry"]
    return g["coordinates"] if g["type"] == "MultiLineString" else [g["coordinates"]]


def firms_confidence(v):
    """VIIRS gives low/nominal/high (or l/n/h), MODIS a percentage; all become low/nominal/high."""
    v = (v or "").strip().lower()
    words = {"l": "low", "n": "nominal", "h": "high"}
    if v[:1] in words and not v[:1].isdigit():
        return words[v[:1]]
    try:
        n = float(v)
    except ValueError:
        return None
    return "low" if n < 30 else "nominal" if n < 80 else "high"


def fetch_firms(bbox, offline):
    """Hotspots inside bbox (lon_min, lat_min, lon_max, lat_max), per FIRMS source file.

    Downloads at most every FIRMS_EVERY_MIN minutes and keeps the result in
    cache/firms.json; a source that fails keeps its previous points.
    """
    cache = load_json(FIRMS_CACHE, {"fetchedAt": None, "points": {}})
    every = float(os.environ.get("FIRMS_EVERY_MIN", "15"))
    last = cache.get("fetchedAt")
    age_min = (time.time() - parse_iso(last)) / 60 if last else math.inf
    if offline or age_min < every:
        return cache["points"], False
    w, s, e, n = bbox
    for path in FIRMS_SOURCES:
        try:
            text = http_get(FIRMS_BASE + path, retries=2).decode("utf-8", "replace")
        except Exception as ex:
            print(f"!! FIRMS {path.rsplit('/', 1)[-1]}: {ex} - keeping the previous points")
            continue
        pts = []
        for r in csv.DictReader(io.StringIO(text)):
            try:
                lat, lon = float(r["latitude"]), float(r["longitude"])
            except (KeyError, ValueError):
                continue
            if not (w <= lon <= e and s <= lat <= n):
                continue
            hhmm = r.get("acq_time", "").zfill(4)
            try:
                frp = float(r["frp"])
            except (KeyError, ValueError):
                frp = None
            pts.append({
                "lon": round(lon, 5), "lat": round(lat, 5),
                "t": f"{r['acq_date']}T{hhmm[:2]}:{hhmm[2:]}:00Z",
                "conf": firms_confidence(r.get("confidence")),
                "frp": frp,
                "day": r.get("daynight") == "D",
            })
        cache["points"][path] = pts
    cache["fetchedAt"] = now_iso()
    save_json(FIRMS_CACHE, cache, indent=None)
    return cache["points"], True


def parse_iso(v):
    return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()


def build_fires(features, offline):
    """(FeatureCollection of the hotspots of the last FIRMS_HOURS hours near a road, downloaded?)."""
    max_km = float(os.environ.get("FIRMS_MAX_KM", "3"))
    lines = {f["properties"]["id"]: road_lines(f) for f in features}
    coords = [c for ls in lines.values() for line in ls for c in line]
    if not coords:
        return None
    dlat = max_km / 110.57
    dlon = max_km / (111.32 * math.cos(math.radians(max(abs(c[1]) for c in coords))))
    bbox = (min(c[0] for c in coords) - dlon, min(c[1] for c in coords) - dlat,
            max(c[0] for c in coords) + dlon, max(c[1] for c in coords) + dlat)
    sources, fetched = fetch_firms(bbox, offline)
    # per road: its bounding box grown by max_km, a quick reject before the exact distance
    boxes = {}
    for rid, ls in lines.items():
        xs = [c[0] for line in ls for c in line]
        ys = [c[1] for line in ls for c in line]
        boxes[rid] = (min(xs) - dlon, min(ys) - dlat, max(xs) + dlon, max(ys) + dlat)
    cutoff = time.time() - FIRMS_HOURS * 3600
    out, seen = [], set()
    for path, pts in sources.items():
        sat = FIRMS_SOURCES.get(path, path)
        for p in pts:
            if parse_iso(p["t"]) < cutoff:
                continue
            key = (sat, p["t"], p["lon"], p["lat"])
            if key in seen:
                continue
            seen.add(key)
            best = None
            for rid, (x0, y0, x1, y1) in boxes.items():
                if x0 <= p["lon"] <= x1 and y0 <= p["lat"] <= y1:
                    d = point_line_km((p["lon"], p["lat"]), lines[rid])
                    if d <= max_km and (best is None or d < best[1]):
                        best = (rid, d)
            if best:
                out.append({
                    "type": "Feature",
                    "properties": {
                        "id": hashlib.sha1(repr(key).encode()).hexdigest()[:12],
                        "t": p["t"], "sat": sat, "conf": p["conf"], "frp": p["frp"], "day": p["day"],
                        "road": best[0], "km": round(best[1], 2),
                    },
                    "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
                })
    out.sort(key=lambda f: (f["properties"]["t"], f["properties"]["id"]))
    return {"type": "FeatureCollection", "maxKm": max_km, "hours": FIRMS_HOURS, "features": out}, fetched


# ---------------------------------------------------------------- publish

def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def publish(message):
    """Commit data/ in this repo if it changed, and push if the clone is ahead of origin.

    Runs on every invocation, so a push that failed earlier (no network) is retried
    on the next run even when nothing new changed.
    """
    if key := os.environ.get("PUBLISH_SSH_KEY"):
        os.environ["GIT_SSH_COMMAND"] = (
            f'ssh -i "{Path(key).expanduser()}" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new')
    name = os.environ.get("PUBLISH_GIT_NAME", "road-alerts-data")
    email = os.environ.get("PUBLISH_GIT_EMAIL", "road-alerts-data@bot.invalid")
    for var in ("AUTHOR", "COMMITTER"):
        os.environ.setdefault(f"GIT_{var}_NAME", name)
        os.environ.setdefault(f"GIT_{var}_EMAIL", email)

    data_dir = DATA_DIR.relative_to(HERE).as_posix()
    try:
        if git(HERE, "status", "--porcelain", "--", data_dir):
            git(HERE, "add", "--", data_dir)
            git(HERE, "commit", "--quiet", "-m", message, "--", data_dir)  # data only, never other edits
            print(f"publish: committed '{message}'")
        if int(git(HERE, "rev-list", "--count", "@{u}..HEAD")):
            git(HERE, "pull", "--rebase", "--autostash", "--quiet")  # keep local edits (e.g. overrides.json)
            git(HERE, "push", "--quiet")
            print("publish: pushed")
    except RuntimeError as e:
        print(f"!! publish failed, will retry next run: {e}")
        return 1


def load_dotenv(path):
    """Minimal .env loader: KEY=VALUE lines, # comments; real env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- main

def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Cyrillic names on Windows consoles
    load_dotenv(HERE / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("ROADS_URL"), help="roads.json URL (default: $ROADS_URL)")
    ap.add_argument("--offline", action="store_true", help="use the local roads.json, do not download")
    ap.add_argument("--no-publish", action="store_true", help="do not push to the site repo")
    args = ap.parse_args()

    feed = load_feed(args.url, args.offline)
    roads = feed["roads"]
    overrides = load_json(OVERRIDES, {})
    city_cache = load_json(CITY_CACHE, {})
    route_cache = load_json(ROUTE_CACHE, {})

    for rid in overrides.get("roads", {}):
        if not rid.startswith("_") and rid not in {r["id"] for r in roads}:
            print(f"!! overrides.json: road '{rid}' is not in roads.json")

    features, changed = [], 0
    try:
        coords, names = resolve_cities(roads, overrides, city_cache)
        for r in roads:
            if r["fromCityId"] not in coords or r["toCityId"] not in coords:
                print(f"!! skipping {r['id']}: missing city coordinates")
                continue
            entry, rerouted = build_route(r, coords, overrides, route_cache)
            changed += rerouted
            status = r.get("status")
            if status not in STATUS_COLORS:
                print(f"!! {r['id']}: unknown status '{status}' - add it to STATUS_COLORS")
            features.append({
                "type": "Feature",
                "properties": {
                    "id": r["id"],
                    "from": r["from"],
                    "to": r["to"],
                    "fromCityId": r["fromCityId"],
                    "toCityId": r["toCityId"],
                    "status": status,
                    "updatedAt": r.get("updatedAt"),
                    "activeAlert": r.get("activeAlert"),
                    "roadGroupIds": r.get("roadGroupIds", []),
                    "distance_km": entry["distance_km"],
                    "stroke": STATUS_COLORS.get(status, UNKNOWN_COLOR),
                },
                "geometry": entry["geometry"],
            })
    finally:  # keep whatever was geocoded/routed even if a later request failed
        save_json(CITY_CACHE, city_cache)
        save_json(ROUTE_CACHE, route_cache, indent=None)

    used = {c for r in roads for c in (r["fromCityId"], r["toCityId"]) if c in coords}
    new_cities = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "properties": {"id": cid, "name": names[cid],
                       "overridden": cid in overrides.get("cities", {})},
        "geometry": {"type": "Point", "coordinates": [coords[cid][1], coords[cid][0]]},
    } for cid in sorted(used)]}
    new_roads = {
        "type": "FeatureCollection",
        "source": feed.get("source", {}),
        "generatedAt": now_iso(),
        "features": features,
    }

    old_roads = load_json(OUT_ROADS, None)
    changes = describe_changes(old_roads, new_roads)
    if load_json(OUT_CITIES, None) != new_cities:
        changes.append("cities")
    statuses = {}
    for f in features:
        statuses[f["properties"]["status"]] = statuses.get(f["properties"]["status"], 0) + 1
    if changes:
        save_json(OUT_ROADS, new_roads, indent=None)
        save_json(OUT_CITIES, new_cities)
        print(f"changed: {', '.join(changes)} -> GeoJSON written")
    else:
        print("no changes - GeoJSON left untouched")

    old_alerts = load_json(OUT_ALERTS, {})
    old_status = {f["properties"]["id"]: f["properties"].get("status")
                  for f in (old_roads or {}).get("features", [])}
    alerts, new_alerts, new_changes = merge_alerts(roads, old_alerts, old_status)
    if alerts != old_alerts:
        save_alerts(OUT_ALERTS, alerts)
        what = [f"{new_alerts} new alerts" if new_alerts else "",
                f"{new_changes} status changes logged" if new_changes else ""]
        changes.append(", ".join(filter(None, what)) or "alert log")
        print(f"alert log: {new_alerts} new activations, {new_changes} new status changes, "
              f"{len(alerts['events'])}/{len(alerts['changes'])} total -> alerts.json written")
    try:
        fires = build_fires(features, args.offline)
    except Exception as e:  # fire signals are extra: never let them break the road update
        print(f"!! fire signals skipped: {e}")
        fires = None
    if fires:
        new_fires, fetched = fires
        old_fires = load_json(OUT_FIRES, None) or {}
        if old_fires.get("features") != new_fires["features"] or old_fires.get("maxKm") != new_fires["maxKm"]:
            new_fires["updatedAt"] = now_iso()
            save_json(OUT_FIRES, new_fires, indent=None)
            changes.append(f"{len(new_fires['features'])} fire signals")
            print(f"fire signals: {len(new_fires['features'])} within {new_fires['maxKm']:g} km of a road "
                  f"in the last {FIRMS_HOURS} h -> fires.geojson written")
        else:
            print(f"fire signals: unchanged, {len(new_fires['features'])} "
                  f"({'downloaded' if fetched else 'from cache'})")

    print(f"done: {len(features)} segments ({changed} routed), source updatedAt "
          f"{feed.get('source', {}).get('updatedAt')}, statuses {statuses}")

    if not args.no_publish and os.environ.get("PUBLISH", "").lower() in ("1", "true", "yes"):
        summary = ", ".join(changes) if changes else "sync"  # site repo was behind (first run / failed push)
        return publish(f"Update road data: {summary} (source {feed.get('source', {}).get('updatedAt')})")


if __name__ == "__main__":
    sys.exit(main())
