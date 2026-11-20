"""Bridges and river crossings on our road segments -> data/bridges.geojson.

It uses a compact copy of OpenStreetMap data in osm/ (committed in this repo):

  osm/bridges.json.gz   OSM bridge ways on motorway..tertiary roads in the region
  osm/rivers.json.gz    named rivers and canals in the region (simplified to 10 m)
  osm/meta.json         where and when the data was downloaded

and finds per road of the current routes:

  river     an OSM bridge on the route that crosses a named river or canal
  other     any other OSM bridge on the route (overpass, viaduct, ...)
  crossing  a place where the route crosses a river or canal with no bridge in OSM

build_geojson.py calls this automatically when a road is new or its route changed, so it
normally never needs to be run by hand.

The osm/ copy is made once from the telegram-events project (Overpass downloads):

  uv run extract_bridges.py --import-osm [DIR]   (default DIR ../../telegram-events/data/osm)
  uv run extract_bridges.py                      (rebuild data/bridges.geojson now)
"""

import argparse
import gzip
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path

from build_geojson import HERE, OUT_BRIDGES, OUT_ROADS, load_json, now_iso, road_lines, route_hash

OSM_DIR = HERE / "osm"
TELEGRAM_EVENTS_OSM = HERE.parent.parent / "telegram-events" / "data" / "osm"
RIVER_SIMPLIFY_M = 10  # river geometry tolerance in the osm/ copy

ON_ROUTE_M = 25          # every point of a bridge way must be this close to the route
SAME_DIR_COS = 0.85      # and the bridge must run along the route (|cos angle| above this, ~30 deg)
JOIN_M = 30              # bridge pieces this close along the route are one bridge (carriageways, split ways)
RIVER_ON_BRIDGE_M = 150  # a river crossing this close to a bridge is the river under that bridge
SAME_RIVER_M = 100       # crossings of the same river this close along the route count once
SAME_PLACE_M = 30        # features of different roads this close are one feature (shared stretches)
DESTROYED_KEYS = ("destroyed", "ruins", "disused", "abandoned")
BRIDGE_TAGS = ("bridge", "bridge:name", "name", "name:ru", "ref", "highway")


# ---------------------------------------------------------------- geometry

class Route:
    """A road's route in local metres, with chainage (distance from the start) per vertex."""

    def __init__(self, coords):
        lat0 = sum(c[1] for c in coords) / len(coords)
        self.kx = 111320 * math.cos(math.radians(lat0))
        self.ky = 110570
        self.pts = [self.xy(c) for c in coords]
        self.chain = [0.0]
        for a, b in zip(self.pts, self.pts[1:]):
            self.chain.append(self.chain[-1] + math.dist(a, b))
        xs, ys = [p[0] for p in self.pts], [p[1] for p in self.pts]
        self.box = (min(xs), min(ys), max(xs), max(ys))

    def xy(self, c):
        return (c[0] * self.kx, c[1] * self.ky)

    def lonlat(self, p):
        return [round(p[0] / self.kx, 6), round(p[1] / self.ky, 6)]

    def overlaps(self, box_lonlat, pad_m):
        """Does a lon/lat bounding box come within pad_m of the route's box?"""
        x0, y0, x1, y1 = self.box
        bx0, by0, bx1, by1 = box_lonlat
        return not (bx1 * self.kx < x0 - pad_m or x1 + pad_m < bx0 * self.kx
                    or by1 * self.ky < y0 - pad_m or y1 + pad_m < by0 * self.ky)

    def project(self, p):
        """(distance to the route, chainage of the closest point, direction of that route segment)."""
        best = (math.inf, 0.0, (1.0, 0.0))
        for i, (a, b) in enumerate(zip(self.pts, self.pts[1:])):
            dx, dy = b[0] - a[0], b[1] - a[1]
            seg = dx * dx + dy * dy
            t = 0 if seg == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / seg))
            d = math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)
            if d < best[0]:
                best = (d, self.chain[i] + t * math.sqrt(seg), (dx, dy))
        return best

    def at(self, chainage):
        """Point on the route at a chainage."""
        for i in range(len(self.pts) - 1):
            if self.chain[i + 1] >= chainage:
                seg = self.chain[i + 1] - self.chain[i]
                t = 0 if seg == 0 else (chainage - self.chain[i]) / seg
                a, b = self.pts[i], self.pts[i + 1]
                return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
        return self.pts[-1]

    def crossings(self, line):
        """Chainages where a polyline (local metres) crosses the route."""
        out = []
        for i, (p1, p2) in enumerate(zip(self.pts, self.pts[1:])):
            for q1, q2 in zip(line, line[1:]):
                d = (p2[0] - p1[0]) * (q2[1] - q1[1]) - (p2[1] - p1[1]) * (q2[0] - q1[0])
                if d == 0:
                    continue
                t = ((q1[0] - p1[0]) * (q2[1] - q1[1]) - (q1[1] - p1[1]) * (q2[0] - q1[0])) / d
                u = ((q1[0] - p1[0]) * (p2[1] - p1[1]) - (q1[1] - p1[1]) * (p2[0] - p1[0])) / d
                if 0 <= t <= 1 and 0 <= u <= 1:
                    out.append(self.chain[i] + t * (self.chain[i + 1] - self.chain[i]))
        return out


def simplify(pts, tol_m):
    """Douglas-Peucker on [lon, lat] points with a tolerance in metres."""
    if len(pts) < 3:
        return pts
    kx = 111320 * math.cos(math.radians(pts[0][1]))
    P = [(x * kx, y * 110570) for x, y in pts]
    keep = [False] * len(P)
    keep[0] = keep[-1] = True
    stack = [(0, len(P) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = P[a], P[b]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy) or 1e-9
        far, idx = 0.0, -1
        for i in range(a + 1, b):
            d = abs(dy * (P[i][0] - ax) - dx * (P[i][1] - ay)) / length
            if d > far:
                far, idx = d, i
        if far > tol_m:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(pts, keep) if k]


# ---------------------------------------------------------------- matching

def is_destroyed(tags):
    return any(tags.get(k) == "yes" or any(t.startswith(k + ":") for t in tags) for k in DESTROYED_KEYS)


def bridge_name(tags):
    """The bridge's own name; a plain street or road name is not one."""
    if tags.get("bridge:name"):
        return tags["bridge:name"]
    name = tags.get("name:ru") or tags.get("name") or ""
    return name if any(w in name.lower() for w in ("мост", "міст", "путепровод", "шляхопровід", "bridge")) else None


def road_bridges(route, ways):
    """OSM bridge ways that run along the route, merged into bridges by chainage range."""
    pieces = []
    for w in ways:
        if not route.overlaps(w["box"], ON_ROUTE_M):
            continue
        pts = [route.xy(c) for c in w["geom"]]
        proj = [route.project(p) for p in pts]
        if max(d for d, _, _ in proj) > ON_ROUTE_M:
            continue
        bx, by = pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1]
        rx, ry = proj[len(proj) // 2][2]
        norm = math.hypot(bx, by) * math.hypot(rx, ry)
        if norm and abs(bx * rx + by * ry) / norm < SAME_DIR_COS:
            continue  # crosses the route (a road over or under it), not on it
        cs = [c for _, c, _ in proj]
        pieces.append({"c0": min(cs), "c1": max(cs), "ways": [w]})
    pieces.sort(key=lambda p: p["c0"])
    merged = []
    for p in pieces:
        if merged and p["c0"] <= merged[-1]["c1"] + JOIN_M:
            merged[-1]["c1"] = max(merged[-1]["c1"], p["c1"])
            merged[-1]["ways"] += p["ways"]
        else:
            merged.append(p)
    return merged


def river_key(name):
    """Same river in Russian and Ukrainian spelling ("Кальмиус" / "Кальміус") -> same key."""
    n = name.lower().replace("река ", "").replace("річка ", "")
    return n.translate(str.maketrans({"і": "и", "ї": "и", "є": "е", "ґ": "г", "'": "", "’": ""})).strip()


def road_rivers(route, rivers):
    """(chainage, river) per place the route crosses a river; once per river within SAME_RIVER_M."""
    hits = []
    for rv in rivers:
        if route.overlaps(rv["box"], 0):
            hits += [(c, rv) for c in route.crossings([route.xy(c) for c in rv["geom"]])]
    hits.sort(key=lambda h: h[0])
    out = []
    for c, rv in hits:
        if not any(river_key(rv["name"]) == river_key(o[1]["name"]) and abs(c - o[0]) <= SAME_RIVER_M for o in out):
            out.append((c, rv))
    return out


# ---------------------------------------------------------------- osm/ copy

def import_osm(src):
    """Copy the telegram-events OSM downloads into a compact osm/ folder."""
    for name in ("bridges.json", "rivers.json"):
        if not (src / name).exists():
            sys.exit(f"!! {src / name} not found - pass the telegram-events data/osm folder")
    OSM_DIR.mkdir(exist_ok=True)
    bridges = []
    for e in json.loads((src / "bridges.json").read_text(encoding="utf-8"))["elements"]:
        if len(e.get("geometry", [])) >= 2:
            tags = {k: v for k, v in e["tags"].items() if k in BRIDGE_TAGS or k.split(":")[0] in DESTROYED_KEYS}
            bridges.append({"id": e["id"], "tags": tags,
                            "geom": [[round(p["lon"], 6), round(p["lat"], 6)] for p in e["geometry"]]})
    rivers = []
    for e in json.loads((src / "rivers.json").read_text(encoding="utf-8"))["elements"]:
        geom = [[round(p["lon"], 5), round(p["lat"], 5)] for p in e.get("geometry", [])]
        if len(geom) >= 2 and e["tags"].get("name"):
            rivers.append({"id": e["id"], "name": e["tags"].get("name:ru") or e["tags"]["name"],
                           "waterway": e["tags"].get("waterway"), "geom": simplify(geom, RIVER_SIMPLIFY_M)})
    for name, data in (("bridges", bridges), ("rivers", rivers)):
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        (OSM_DIR / f"{name}.json.gz").write_bytes(gzip.compress(raw, mtime=0))
    src_meta = load_json(src / "meta.json", {})
    meta = {
        "source": "OpenStreetMap via Overpass, copied from telegram-events/data/osm",
        "importedAt": now_iso(),
        "bridges": {"fetched_at": (src_meta.get("bridges") or {}).get("fetched_at"), "ways": len(bridges)},
        "rivers": {"fetched_at": (src_meta.get("rivers") or {}).get("fetched_at"), "ways": len(rivers),
                   "simplified_m": RIVER_SIMPLIFY_M},
    }
    (OSM_DIR / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"osm/: {len(bridges)} bridge ways, {len(rivers)} rivers/canals imported from {src}")


def osm_available():
    return all((OSM_DIR / n).exists() for n in ("bridges.json.gz", "rivers.json.gz"))


def load_osm():
    def read(name):
        items = json.loads(gzip.decompress((OSM_DIR / f"{name}.json.gz").read_bytes()).decode("utf-8"))
        for w in items:
            xs, ys = [c[0] for c in w["geom"]], [c[1] for c in w["geom"]]
            w["box"] = (min(xs), min(ys), max(xs), max(ys))
        return items

    return read("bridges"), read("rivers")


# ---------------------------------------------------------------- output

def build_bridges(road_features):
    """The bridges.geojson FeatureCollection for these road features (needs osm/)."""
    ways, rivers = load_osm()
    fetched = (load_json(OSM_DIR / "meta.json", {}).get("bridges") or {}).get("fetched_at") or "?"

    found = []  # (road id, kind, [lon, lat], props)
    for f in road_features:
        rid = f["properties"]["id"]
        for coords in road_lines(f):
            route = Route(coords)
            bridges = road_bridges(route, ways)
            crossings = road_rivers(route, rivers)
            for b in bridges:
                near = [(c, rv) for c, rv in crossings
                        if b["c0"] - RIVER_ON_BRIDGE_M <= c <= b["c1"] + RIVER_ON_BRIDGE_M]
                tags = [w["tags"] for w in b["ways"]]
                refs = Counter(t["ref"] for t in tags if t.get("ref"))
                streets = Counter(t.get("name:ru") or t.get("name") for t in tags if t.get("name"))
                found.append((rid, "river" if near else "other", route.lonlat(route.at((b["c0"] + b["c1"]) / 2)), {
                    "name": next((n for n in map(bridge_name, tags) if n), None),
                    "street": streets.most_common(1)[0][0] if streets else None,
                    "ref": refs.most_common(1)[0][0] if refs else None,
                    "river": ", ".join({river_key(rv["name"]): rv["name"] for _, rv in near}.values()) or None,
                    "waterway": near[0][1].get("waterway") if near else None,
                    "structure": next((t["bridge"] for t in tags if t.get("bridge") not in (None, "yes")), None),
                    "length_m": round(b["c1"] - b["c0"]),
                    "destroyed": any(is_destroyed(t) for t in tags),
                    "osm": sorted({w["id"] for w in b["ways"]}),
                }))
            for c, rv in crossings:
                if any(b["c0"] - RIVER_ON_BRIDGE_M <= c <= b["c1"] + RIVER_ON_BRIDGE_M for b in bridges):
                    continue  # the river under a bridge
                found.append((rid, "crossing", route.lonlat(route.at(c)),
                              {"river": rv["name"], "waterway": rv.get("waterway"), "osm_river": rv["id"]}))

    # Routes that share a stretch of road find the same bridge: one feature, all roads listed.
    groups = []
    for rid, kind, pt, props in found:
        kx = 111320 * math.cos(math.radians(pt[1]))
        same = next((g for g in groups if g["kind"] == kind and math.hypot(
            (g["pt"][0] - pt[0]) * kx, (g["pt"][1] - pt[1]) * 110570) <= SAME_PLACE_M), None)
        if same:
            if rid not in same["roads"]:
                same["roads"].append(rid)
        else:
            groups.append({"kind": kind, "pt": pt, "roads": [rid], "props": props})

    features = []
    for g in groups:
        p = g["props"]
        key = f"{g['kind']}:{p.get('osm') or p.get('osm_river')}:{g['pt']}"
        features.append({
            "type": "Feature",
            "properties": {"id": hashlib.sha1(key.encode()).hexdigest()[:10], "kind": g["kind"], "roads": g["roads"],
                           **{k: v for k, v in p.items() if v not in (None, [], False)}},
            "geometry": {"type": "Point", "coordinates": g["pt"]},
        })
    return {
        "type": "FeatureCollection",
        "generatedAt": now_iso(),
        "source": f"OpenStreetMap (bridges fetched {fetched[:10]})",
        "routes": {f["properties"]["id"]: route_hash(f) for f in road_features},
        "features": features,
    }


def write_bridges(result):
    """One feature per line, so a rebuild after a route change gives a readable diff."""
    head = json.dumps({k: v for k, v in result.items() if k != "features"}, ensure_ascii=False,
                      separators=(",", ":"))
    lines = ",\n".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) for x in result["features"])
    OUT_BRIDGES.parent.mkdir(parents=True, exist_ok=True)
    OUT_BRIDGES.write_text(head[:-1] + ',"features":[\n' + lines + "\n]}\n", encoding="utf-8")


def summary(result):
    feats = result["features"]
    kinds = Counter(x["properties"]["kind"] for x in feats)
    destroyed = sum(1 for x in feats if x["properties"].get("destroyed"))
    return (f"{len(feats)} features: {kinds['river']} river bridges, {kinds['other']} other bridges, "
            f"{kinds['crossing']} river crossings without a bridge, {destroyed} destroyed")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--import-osm", type=Path, nargs="?", const=TELEGRAM_EVENTS_OSM, metavar="DIR",
                    help=f"copy the OSM data into osm/ first (default DIR: {TELEGRAM_EVENTS_OSM})")
    args = ap.parse_args()
    if args.import_osm:
        import_osm(args.import_osm)
    if not osm_available():
        sys.exit("!! osm/ is empty - run with --import-osm once")
    roads = load_json(OUT_ROADS, None)
    if not roads:
        sys.exit(f"!! {OUT_ROADS} not found - run build_geojson.py first")
    result = build_bridges(roads["features"])
    write_bridges(result)
    print(f"bridges.geojson: {summary(result)}")
    for f in roads["features"]:
        rid = f["properties"]["id"]
        mine = [x["properties"] for x in result["features"] if rid in x["properties"]["roads"]]
        k = Counter(m["kind"] for m in mine)
        rv = ", ".join(m["river"] for m in mine if m.get("river"))
        print(f"  {rid}: {k['river']} river, {k['other']} other, {k['crossing']} crossing" + (f"  ({rv})" if rv else ""))


if __name__ == "__main__":
    main()
