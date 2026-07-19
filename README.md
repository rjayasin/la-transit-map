# la-transit-map

Animated 24-hour visualization of every scheduled LA Metro bus and train,
played over Metro's official "Bus and Rail System" map (May 2026).

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
- **Data** — LA Metro static GTFS (bus + rail) cached as zips in `data/gtfs/`
  (source: gitlab.com/LACMTA). `scripts/build_data.py` extracts all trips for
  one service date (**Wed 2026-07-22**): 1,242 rail + 13,303 bus trips on 114
  routes, emitted as compact `schedule.json` (~3 MB) — route colors/labels,
  shape polylines in map pixels, per-stop distance along shape, stop arrival times.
  Trips crossing midnight are duplicated shifted −24 h so owl service appears after 00:00.
- **Georeferencing** (`scripts/georef.py`) — the map is quasi-geographic, so a
  lat/lon→pixel transform is fitted automatically: color-mask the six drawn rail
  lines, then ICP-align GTFS rail shapes to them (affine init → 2nd-order
  polynomial warp, median error ~11 px). Result in `data/transform.json`.
  Rail shapes are additionally *snapped* onto the drawn line pixels, so trains
  ride exactly on their painted lines; buses use the polynomial warp.
- **Rendering** (`index.html`, vanilla canvas) — each vehicle is a circle in
  its GTFS `route_color` labeled with the route number/letter (buses default to
  Metro orange; GTFS's black for Rapid 720/754/761 is overridden with the map's
  Rapid red). Motion eases in/out at every stop (smoothstep between scheduled
  arrivals). Rail draws on top of buses. Digital clock (e.g. `08:30`) sits above
  the Metro logo, lower left. The full day loops midnight→midnight.

## Rebuild data

```sh
python3 -m venv .venv && .venv/bin/pip install numpy scipy pillow
cd data/gtfs && for z in gtfs_bus gtfs_rail; do unzip -o $z.zip -d $z; done && cd ../..
.venv/bin/python scripts/georef.py      # refit transform (optional)
.venv/bin/python scripts/build_data.py  # regenerate schedule.json

# background tiles (regenerate only if the map PDF changes; needs pip install pymupdf)
.venv/bin/python scripts/make_tiles.py
```

## Known limitations

- Metro-operated service only (no Big Blue Bus, Foothill, DASH, Metrolink…).
- The map is schematic in places; buses in the far San Fernando Valley /
  far-east corners can drift off their drawn streets (rail is snapped, so trains stay true).
- A few routes (e.g. 161) extend past the map edge and park at the border.
- Downtown is a ghosted call-out box on the source map; vehicles pass over it.
