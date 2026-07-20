# la-transit-map

Animated 24-hour visualization of every scheduled transit vehicle in LA —
Metro bus & rail plus 13 municipal systems and Metrolink — played over
Metro's official "Bus and Rail System" map (May 2026).

## Run

```sh
python3 -m http.server 8741
# open http://localhost:8741
```

Controls: play/pause, scrubber, speed (30–400×). Drag to pan, wheel to zoom.
URL params: `?t=8:30&speed=150&paused=1`.

## Approach

- **Background** — `map.png` (4096px base, via `sips`) plus a WebP tile
  pyramid (`tiles/{2,4,8}/`, 512px tiles ≡ 8192/16384/32768px ≈ up to
  700 dpi of the 47″ map) rendered tile-by-tile from the PDF vectors with
  PyMuPDF (`scripts/make_tiles.py`), so no giant intermediate bitmap is needed.
  Tiles cascade in as you zoom, and max zoom is capped at the deepest level's
  1:1, so the background is never shown upscaled — every reachable zoom is
  PDF-crisp. (Rendering the PDF directly in-browser via PDF.js was tried and
  rejected: it paints its huge display list on the main thread, stalling the
  animation; a full-map SVG extraction would be a 50MB+ DOM.)
- **Data** — 15 static GTFS feeds cached as zips in `data/gtfs/` (see Data
  sources). `scripts/build_data.py` extracts all trips for one service date
  (**Wed 2026-07-22**; feeds whose calendar misses that date fall back to their
  busiest covered Wednesday): **26,902 trips on 336 routes** — 1,242 Metro rail
  + 13,303 Metro bus + ~12,200 municipal/Metrolink — emitted as compact
  `schedule.json` (~5 MB): route colors/labels, shape polylines in map pixels,
  per-stop distance along shape, stop arrival times. Trips crossing midnight
  are duplicated shifted −24 h so owl service appears after 00:00.
- **Georeferencing** (`scripts/georef.py`) — the map is quasi-geographic, so a
  lat/lon→pixel transform is fitted automatically: color-mask the six drawn rail
  lines, then ICP-align GTFS rail shapes to them (affine init → 2nd-order
  polynomial warp, median error ~11 px). Result in `data/transform.json`.
- **Line snapping** — after the warp, every shape is pulled onto its system's
  *drawn* lines so vehicles ride the artwork, not raw geography. Rail snaps to
  the six rail-color masks. Buses snap per system color (Metro orange, Rapid
  red, and each municipal agency's color — legend swatches seed the palette,
  refined against pixels along the routes) using a coherence trick: the snap
  displacement is smoothed along the shape so whole stretches move to the same
  drawn street instead of points grabbing different parallels. Shapes whose
  lines aren't drawn (minor routes, Pasadena/Norwalk/Burbank grays, J-line
  silver ≈ freeway gray, Metrolink) keep the warp.
- **Rendering** (`index.html`, vanilla canvas) — each vehicle is a circle in
  its GTFS `route_color` labeled with the route number/letter (Metro buses
  default to Metro orange; GTFS's black for Rapid 720/754/761 is overridden
  with the map's Rapid red; long route names are compressed to ≤4-char codes).
  Motion eases in/out at every stop (smoothstep between scheduled arrivals).
  Rail (`route_type` 0/1/2, incl. Metrolink) draws on top of buses. Vehicles
  that leave the drawn map area are hidden. Digital clock (e.g. `08:30`) sits
  above the Metro logo, lower left. The full day loops midnight→midnight.

## Rebuild data

```sh
python3 -m venv .venv && .venv/bin/pip install numpy scipy pillow pymupdf
cd data/gtfs && for z in *.zip; do unzip -o "$z" -d "${z%.zip}"; done && cd ../..
.venv/bin/python scripts/georef.py      # refit transform (optional)
.venv/bin/python scripts/build_data.py  # regenerate schedule.json (pymupdf: badge anchors from the PDF)

# background tiles (regenerate only if the map PDF changes)
.venv/bin/python scripts/make_tiles.py
```

## Known limitations

- Missing systems: Glendale Beeline (download blocked), OCTA / AVTA / Santa
  Clarita / Simi Valley (essentially off-map), Amtrak, and the small community
  shuttles in the map's "Municipal & Neighboring" legend without public GTFS.
- Stale feeds: Torrance Transit's newest public GTFS is from Jan 2024;
  Culver City / Montebello / GTrans calendars end just before the target date —
  those systems animate their busiest covered Wednesday instead.
- Metrolink's feed has no shape geometry; its trains run station-to-station
  straight lines (and vanish at the map edge, as do all off-map segments).
- The map is schematic in places; buses in the far San Fernando Valley /
  far-east corners can drift off their drawn streets (Metro rail is snapped,
  so trains stay true).
- Downtown is a ghosted call-out box on the source map; vehicles pass over it.

## Data sources

Background map: [LA Metro "Bus and Rail System" map (May 2026)](https://www.metro.net/riding/maps/)
— the PDF in this repo, © LACMTA.

GTFS feeds (cached in `data/gtfs/`, fetched 2026-07-19). Metro feeds come from
LACMTA's GitLab; municipal feeds are the latest-copy mirrors of the
[Mobility Database](https://mobilitydatabase.org) catalog; Metrolink is from
its official site.

| Feed | Agency | Source |
|---|---|---|
| `gtfs_bus` | LA Metro bus | [gitlab.com/LACMTA/gtfs_bus](https://gitlab.com/LACMTA/gtfs_bus) |
| `gtfs_rail` | LA Metro rail | [gitlab.com/LACMTA/gtfs_rail](https://gitlab.com/LACMTA/gtfs_rail) |
| `metrolink` | Metrolink | [metrolinktrains.com GTFS](https://metrolinktrains.com/globalassets/about/gtfs/gtfs.zip) |
| `bigbluebus` | Big Blue Bus (Santa Monica) | [Mobility DB mirror](https://storage.googleapis.com/storage/v1/b/mdb-latest/o/us-california-big-blue-bus-gtfs-37.zip?alt=media) |
| `culvercity` | Culver CityBus | [Mobility DB mirror](https://storage.googleapis.com/storage/v1/b/mdb-latest/o/us-california-culver-city-bus-gtfs-38.zip?alt=media) |
| `ladot` | LADOT (DASH, Commuter Express) | [Mobility DB mirror](https://storage.googleapis.com/storage/v1/b/mdb-latest/o/us-california-los-angeles-department-of-transportation-ladot-gtfs-1210.zip?alt=media) |
| `longbeach` | Long Beach Transit | [Mobility DB mirror](https://storage.googleapis.com/storage/v1/b/mdb-latest/o/us-california-long-beach-transit-lbt-gtfs-1198.zip?alt=media) |
| `foothill` | Foothill Transit | [Mobility DB mirror](https://storage.googleapis.com/storage/v1/b/mdb-latest/o/us-california-foothill-transit-gtfs-101.zip?alt=media) |
| `torrance` | Torrance Transit | [Mobility DB mirror](https://storage.googleapis.com/storage/v1/b/mdb-latest/o/us-california-torrance-transit-gtfs-34.zip?alt=media) |
| `norwalk` | Norwalk Transit | [Mobility DB mirror](https://storage.googleapis.com/storage/v1/b/mdb-latest/o/us-california-norwalk-transit-system-nts-gtfs-2242.zip?alt=media) |
| `montebello` | Montebello Bus Lines | [Mobility DB mirror](https://storage.googleapis.com/storage/v1/b/mdb-latest/o/us-california-montebello-bus-lines-gtfs-2201.zip?alt=media) |
| `gtrans` | GTrans (Gardena) | [Mobility DB mirror](https://storage.googleapis.com/storage/v1/b/mdb-latest/o/us-california-gtrans-gtfs-2270.zip?alt=media) |
| `pasadena` | Pasadena Transit | [Mobility DB mirror](https://storage.googleapis.com/storage/v1/b/mdb-latest/o/us-california-pasadena-transit-gtfs-41.zip?alt=media) |
| `burbank` | BurbankBus | [Mobility DB mirror](https://storage.googleapis.com/storage/v1/b/mdb-latest/o/us-california-burbankbus-gtfs-2149.zip?alt=media) |
| `beachcities` | Beach Cities Transit (Redondo) | [Mobility DB mirror](https://storage.googleapis.com/storage/v1/b/mdb-latest/o/us-california-beach-cities-transit-gtfs-1999.zip?alt=media) |
