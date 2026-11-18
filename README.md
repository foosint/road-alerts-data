# road-alerts-data

Turns the road-alert feed (`roads.json`) into GeoJSON road paths that follow the real road network, and publishes them in this repo under [data/](data/). The [road-alerts](../road-alerts) map site reads the files straight from here.

The feed lists road segments as from/to pairs of towns in Crimea, Kherson, Zaporizhzhia, Donetsk and Luhansk. For each segment the script:
- finds the coordinates of both towns;
- routes the segment along real roads;
- writes the result as GeoJSON, with the segment's alert `status`, its `updatedAt` time and a colour.

It is meant to run as a cron job on a Raspberry Pi. When something has changed, it commits `data/` and pushes this repo.

```
cron (Pi) ── build_geojson.py ──► data/roads.geojson, data/cities.geojson
                                       │ only if something changed
                                       ▼
                         git commit data/ + push (this repo)
                                       │
                                       ▼
   road-alerts site polls https://raw.githubusercontent.com/<you>/road-alerts-data/main/data/…
```

## Requirements

- [uv](https://docs.astral.sh/uv/). The project pins Python 3.12, and uv installs it automatically. The script has no third-party dependencies.
- `git` and `ssh`, for publishing.
- Internet access to:
  - the feed URL;
  - `nominatim.openstreetmap.org`, to geocode new towns;
  - `router.project-osrm.org`, to route new or changed segments;
  - `github.com`, to publish.
- This repo must be **public**, because the site reads the files without authentication.

## Usage

```sh
uv run build_geojson.py                 # download the feed, update, publish if PUBLISH=1
uv run build_geojson.py --url URL       # override ROADS_URL
uv run build_geojson.py --offline       # use the local roads.json, do not download
uv run build_geojson.py --no-publish    # never commit or push
```

## Configuration

Settings come from environment variables or a `.env` file next to the script. Real environment variables take precedence. Copy [.env.example](.env.example) to `.env`; `.env` is git-ignored.

| Variable | Default | Meaning |
|---|---|---|
| `ROADS_URL` | — | Feed URL. Without it, and without `--url`, the local `roads.json` is used. |
| `PUBLISH` | `0` | `1` commits `data/` and pushes after a change. Set it **only on the Pi**, so that one machine writes the data. |
| `PUBLISH_SSH_KEY` | — | Private key file for git over SSH, i.e. the deploy key. |
| `PUBLISH_GIT_NAME` / `PUBLISH_GIT_EMAIL` | `road-alerts-data` / `road-alerts-data@bot.invalid` | Author of the data commits. |

## Raspberry Pi setup

### 1. Deploy key: write access to this repo only

A deploy key is an SSH key that grants access to a single repository. No personal access token is involved, nothing expires, and you can revoke it per repository.

```sh
ssh-keygen -t ed25519 -N "" -C "road-alerts pi" -f ~/.ssh/road_alerts_data_deploy
cat ~/.ssh/road_alerts_data_deploy.pub
```

On GitHub, open the `road-alerts-data` repo and go to **Settings → Deploy keys → Add deploy key**. Paste the public key and tick **Allow write access**.

### 2. Clone and configure

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
GIT_SSH_COMMAND="ssh -i ~/.ssh/road_alerts_data_deploy -o IdentitiesOnly=yes" \
  git clone git@github.com:<you>/road-alerts-data.git ~/road-alerts-data
cd ~/road-alerts-data && cp .env.example .env
```

Edit `.env`:

```
ROADS_URL=https://…/roads.json
PUBLISH=1
PUBLISH_SSH_KEY=/home/pi/.ssh/road_alerts_data_deploy
```

Run it once by hand: `uv run build_geojson.py`. The first run on a fresh clone has no `cache/` yet, so it geocodes all towns and routes all segments, which takes about 1–2 minutes. The routes it produces are the same as the committed ones, so nothing is committed unless the feed itself changed.

### 3. Cron

Open the crontab with `crontab -e`:

```cron
*/5 * * * * cd /home/pi/road-alerts-data && flock -n /tmp/road-alerts.lock /home/pi/.local/bin/uv run build_geojson.py >> /home/pi/road-alerts-data/run.log 2>&1
```

- `flock -n` skips a run if the previous one is still busy.
- Use the full path to `uv`, because cron has a minimal `PATH`.
- A run without changes makes no geocoding, routing or git network requests. A changed `source.updatedAt` alone needs no geocoding or routing either, only a commit and push.

### Keeping the automated commits off your GitHub profile

The data commits don't count toward your account's contribution graph or activity, for two reasons:

- **The pusher is the deploy key, not a user.** A deploy key belongs to the repository, not to a GitHub account. So pushes made with it aren't recorded as your activity.
- **The author is a bot identity.** Commits are authored as `road-alerts-data <road-alerts-data@bot.invalid>`. That address isn't linked to any GitHub account, so GitHub attributes the commits to no one. The script sets this through `GIT_AUTHOR_*` and `GIT_COMMITTER_*`, which take precedence over any `user.email` in the Pi's git config.

Two things to keep this way:

- **Don't** set `PUBLISH_GIT_EMAIL` to an address that's linked to your account, including your `…@users.noreply.github.com` address.
- **Don't** push with your personal SSH key or token.

You don't need a separate bot account. You would only need one if the repo should be owned by someone else, for example a GitHub organization.

Commits happen only when the data changed. That includes a new `source.updatedAt`, so if the feed refreshes its timestamp every few minutes, expect a commit on most runs. Each commit changes only a few bytes, and git stores it as a small delta.

### Editing from another machine

You can change `overrides.json` or the script anywhere and push. Before each push, the Pi runs `pull --rebase --autostash`, so it picks up your commits on its next data change. Uncommitted local edits on the Pi survive the pull.

Don't run with `PUBLISH=1` on a second machine. Both machines would then commit `data/`, and the commits could conflict.

## Files

| Path | In git | Edited by | Purpose |
|---|---|---|---|
| `data/roads.geojson` | yes | script | **Published:** one LineString per segment. |
| `data/cities.geojson` | yes | script | **Published:** one Point per town. |
| `overrides.json` | yes | **you** | Manual corrections to coordinates and routes. See [Manual corrections](#manual-corrections). |
| `build_geojson.py` | yes | — | The script. |
| `.env` | no | **you** | Local settings. See `.env.example`. |
| `roads.json` | no | script | Latest downloaded feed. Overwritten only when a download is valid (contains a `roads` list). |
| `cache/cities.json` | no | script | Coordinates found for each town. |
| `cache/routes.json` | no | script | Route geometry for each segment, with a fingerprint of the inputs it was built from. |
| `run.log` | no | cron | Log output. |

`cache/` is local working state. If you delete it, every town is geocoded and every segment is routed again on the next run. Anything that must not be lost belongs in `overrides.json`, which is in git.

## How it works

Each run goes through five steps:

1. **Load the feed.** The script downloads `roads.json`, or reads the local copy with `--offline`.
2. **Resolve town coordinates.** Each city ID is looked up in this order:
   1. `overrides.json` → `cities`
   2. `cache/cities.json`
   3. a Nominatim (OpenStreetMap) search on the Russian name, limited to the region

   Many town names exist in several oblasts. The search therefore drops matches more than 150 km (`GEOCODE_MAX_KM`) from the town at the other end of the road, and then keeps Nominatim's own ranking. That way a real town wins over a same-named village that just happens to be closer. The pick and up to three alternatives are printed, so you can correct them in `overrides.json` if needed.
3. **Build the routes.** Every segment has a fingerprint, computed from:
   - its start and end coordinates;
   - its `via` waypoints;
   - its manual geometry, if any.

   If the fingerprint matches the one stored in `cache/routes.json`, the stored geometry is reused unchanged. Otherwise the segment is routed again with OSRM (driving profile), or its manual geometry is used. This happens only when:
   - a segment is **new**;
   - an **override** for that segment changes;
   - one of its **towns moves**.

   Changes to `status`, `updatedAt` or alerts never re-route a segment.
4. **Detect changes.** The new output is compared with the files in `data/`. These count as a change:
   - a segment was added or removed;
   - a segment's `status`, `updatedAt` or route changed;
   - any other segment property changed;
   - a town changed;
   - the feed's `source.updatedAt` changed, even if no segment changed. This keeps the "Feed updated" time on the map current.

   Without a change, the files are left untouched.
5. **Publish** (with `PUBLISH=1`):
   - The script commits `data/` if it differs from the last commit. Only `data/` is committed, never other local edits. The commit message summarises the change, for example `Update road data: 2 status, 2 updatedAt (source …)`.
   - If the clone is ahead of GitHub, it runs `pull --rebase --autostash` and then `push`.
   - If a push fails, for example because the network is down, the commit stays local and is pushed on the next run.

### Console output

```
++ geocoded Sevastopol (Севастополь) -> (44.605443, 33.522084)  Севастополь, ...
     alternative (44.56555, 33.459599)  Севастополь, Украина
++ routed Simferopol-Sevastopol: 78.4 km (OSRM, x1.31 straight line)
~~ re-routed Kalanchak-Chaplinka: 45.0 km (OSRM via 1 point(s), x1.98 straight line)  <-- check, long detour
!! Some-Road: unknown status 'foo' - add it to STATUS_COLORS
changed: 1 new, 2 status, 3 updatedAt -> GeoJSON written
done: 37 segments (2 routed), source updatedAt 2026-11-17T22:49:13.612Z, statuses {...}
publish: committed 'Update road data: 1 new, 2 status, 3 updatedAt (source ...)'
publish: pushed
```

- `++` means a new town or segment.
- `~~` means a segment was re-routed because its inputs changed.
- `!!` means something needs your attention.
- `<-- check` flags a route that is more than 1.6 times the straight-line distance. This often means an endpoint is in the wrong place.

## Output format

### `data/roads.geojson`

```json
{
  "type": "FeatureCollection",
  "source": { "kind": "sochs", "available": true, "freshness": "fresh", "updatedAt": "2026-11-17T22:49:13.612Z" },
  "generatedAt": "2026-11-18T08:00:00Z",
  "features": [
    {
      "type": "Feature",
      "properties": {
        "id": "Torez-Mius",
        "from": "Торез", "to": "Миус",
        "fromCityId": "Torez", "toCityId": "Mius",
        "status": "neutral",
        "updatedAt": "2026-11-17T09:48:33.030Z",
        "activeAlert": null,
        "roadGroupIds": ["dnr"],
        "distance_km": 36.3,
        "stroke": "#2f9e44"
      },
      "geometry": { "type": "LineString", "coordinates": [[38.618, 48.026], "..."] }
    }
  ]
}
```

- `source` is copied from the feed. `source.updatedAt` is the time of the feed's last update.
- `updatedAt` on each feature is that segment's own last update.
- `id` matches the road `id` in `roads.json`.
- `stroke` is the line colour for the status. The site uses it directly, and so does any tool that follows the simplestyle convention.
- Coordinates are WGS84 (EPSG:4326) in `[lon, lat]` order.

The published files are at `https://raw.githubusercontent.com/<you>/road-alerts-data/main/data/roads.geojson` and `…/cities.geojson`. GitHub caches them for up to 5 minutes.

### Status colours

Colours are set in `STATUS_COLORS` at the top of `build_geojson.py`:

| status | colour |
|---|---|
| `neutral` | `#2f9e44` green |
| `busy` | `#e03131` red |
| anything else | `#ae3ec9` purple, with a warning in the console |

The feed uses only `neutral` and `busy`. If a new status appears, add it here. A colour change counts as a change, so it gets published.

## Manual corrections

Edit `overrides.json`. The next run re-routes only the affected segments and publishes the result.

Coordinates are `[lat, lon]`, or `{ "lat": …, "lon": … }` for towns. The exception is `geometry`, which is plain GeoJSON and uses `[lon, lat]`.

```json
{
  "cities": {
    "Doneck": { "lat": 47.93313, "lon": 37.823858 }
  },
  "roads": {
    "Kalanchak-Chaplinka": { "via": [[46.3245, 33.4844]] },
    "Torez-Mius": { "from": [48.03, 38.61], "to": [48.29, 38.54] },
    "Some-Road": { "geometry": { "type": "LineString", "coordinates": [[38.0, 48.0], [38.1, 48.1]] } }
  }
}
```

| Override | Effect |
|---|---|
| `cities.<cityId>` | Moves a town. Every segment that starts or ends there is re-routed. Also use this for towns the search can't find, such as `Veselovoznesenka`. |
| `roads.<roadId>.from` / `.to` | Moves one segment's endpoint without changing the town for other segments. |
| `roads.<roadId>.via` | List of waypoints the route must pass through, in order. Use this to keep a route on a specific main road. |
| `roads.<roadId>.geometry` | A hand-drawn LineString used exactly as given, with no routing. |

- Keys that start with `_` are ignored, as is any `note` field.
- If you delete an override, its segments go back to the automatic coordinates and route.

To find the right coordinates, open [openstreetmap.org](https://www.openstreetmap.org) and right-click a point. "Show address" displays its lat/lon.

## Limitations

- **The routing ignores the current situation.** It doesn't know about front lines, checkpoints, closures or destroyed bridges. It takes the shortest road in OpenStreetMap, which may not be the road actually in use. Use `via` or `geometry` overrides where that matters.
- **Endpoints are town centres**, snapped by the router to the nearest road.
- **Removed segments** drop out of the output but stay in `cache/routes.json`. If a segment comes back, its old route is reused.
- **Public services.** The script uses the public Nominatim and OSRM servers at about 1 request per second. For heavy use, point `OSRM` and `NOMINATIM` in the script at your own servers.
