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
  drawn street instead of points grabbing different parallels. Two refinements
  keep that from leaving lines a block off: a final short-window pass, capped
  below the spacing of neighboring streets, fixes the sag the wide smoothing
  leaves at junctions; and place-name labels — which are painted over the
  artwork and so punch holes in the color masks — are bridged by a
  morphological closing, since a broken line otherwise loses the snap to
  whichever parallel street stays continuous (Metro 2 used to land a block
  south of Sunset under the "WEST HOLLYWOOD" label). Because a whole agency
  shares one drawn color, the snap is also pinned by *anchors* — the numbered
  route badges printed on the map, extracted from the PDF: a badge matching the
  route's number that sits on the agency's color mask is a point known to be on
  that route. Numbers collide across agencies, though (Big Blue Bus 3 and
  Culver CityBus 3 run bundled through Westchester), so a candidate badge is
  kept only if its own chip color is best explained by *this* agency's color
  rather than another's — Culver City's khaki "3" no longer drags Big Blue's
  route onto the wrong street. (This test is applied to the muted, mutually
  distinct municipal palettes; Metro's saturated orange fades toward other
  hues, so its dense badges are left ungated.) Shapes whose
  lines aren't drawn (minor routes, Pasadena/Norwalk/Burbank grays, J-line
  silver ≈ freeway gray, Metrolink) keep the warp.
- **Rendering** (`index.html`, vanilla canvas) — each vehicle is a circle in
  its GTFS `route_color` labeled with the route number/letter (Metro buses
  default to Metro orange; GTFS's black for Rapid 720/754/761 is overridden
  with the map's Rapid red; the J Line rides the gray busway so it draws gray;
  municipal agencies are recolored to the drawn line color sampled from the
  map, so every sprite matches the artwork it travels on; long route names
  are compressed to ≤4-char codes).
  Motion eases in/out at every stop (smoothstep between scheduled arrivals).
  Rail (`route_type` 0/1/2, incl. Metrolink) draws on top of buses. Vehicles
  that leave the drawn map area are hidden. Digital clock (e.g. `08:30`) sits
  above the Metro logo, lower left. The full day loops midnight→midnight.

## Python source files

All the Python is offline build tooling — nothing runs in the browser. The
pipeline order is `georef.py` → `georef_inset.py` → `build_data.py`, with
`make_tiles.py` independent of the other three.

```
scripts/
├── build_data.py    Builds schedule.json from every cached GTFS feed in
│                    data/gtfs/. Picks a service date per feed, warps shapes
│                    into map pixels via data/transform.json, snaps them onto
│                    the drawn line colors (using route-number badges pulled
│                    from the map PDF as anchors), projects stops onto shapes,
│                    and emits routes / shapes / patterns / trips plus the
│                    per-shape DTLA inset geometry.
├── debug_line.py    Draws one route's cached path on top of the map, to see
│                    where vehicles diverge from the drawn artwork. Reads the
│                    polylines straight out of schedule.json, so it shows
│                    exactly what the animation plays back — snapping included.
├── georef.py        Fits the lat/lon → map-pixel transform for the main map by
│                    color-masking the six drawn rail lines and ICP-aligning
│                    GTFS rail shapes to them (affine init, then a 2nd-order
│                    polynomial warp). Writes data/transform.json and a
│                    diagnostic overlay image. Also owns the shared constants
│                    build_data.py imports: rail route colors, mask tolerance,
│                    and the excluded regions of the map image.
├── georef_inset.py  Same fit again, restricted to the Downtown LA inset panel,
│                    whose rotated grid needs its own transform. Adds an
│                    "inset" entry to data/transform.json and defines the inset
│                    frame rect, legend box, and geographic bounds that
│                    build_data.py reads.
└── make_tiles.py    Rasterizes the background WebP tile pyramid
                     (tiles/{2,4,8}/, 512px tiles) tile-by-tile straight from
                     the map PDF with PyMuPDF, so no giant intermediate bitmap
                     is ever created. Only needs rerunning if the PDF changes.
```

### Checking a line against the artwork

`debug_line.py` is the tool for the recurring question "why is this bus off
its street?". It writes `scratch/debug_<system>_<line>.png`, cropped to the
route with a little margin:

```sh
.venv/bin/python scripts/debug_line.py 720            # Metro Bus 720
.venv/bin/python scripts/debug_line.py 2 --system "Big Blue"   # shared number
.venv/bin/python scripts/debug_line.py 720 --stops    # + stop positions
.venv/bin/python scripts/debug_line.py 720 --shape 0  # one variant only
.venv/bin/python scripts/debug_line.py 720 --inset    # the DTLA inset panel
.venv/bin/python scripts/debug_line.py 720 --png      # cheap map.png background
```

The background is drawn from the same high-resolution WebP tile pyramid the
app uses (`tiles/`), so a tight crop stays PDF-crisp instead of an upscaled
`map.png` — labels and route badges under the line stay legible when you zoom
in to check a block. The deepest level whose output fits a size cap is chosen
automatically; `--png` forces the cheap `map.png` background and `--level
{2,4,8}` pins a level (`--full`, the whole map, always uses `map.png`).

A route label alone is ambiguous — a dozen agencies run a line "1" — so a
shared label prompts for which system to draw, by number or by any substring
of the name. `--system` skips the prompt, and when there's nobody to ask
(piped or redirected) the script prints the `--system` flags and exits 2
instead of hanging. Each route usually has several
shape variants, listed on stdout sorted by trip count and drawn in different
colors. The top two are normally the two directions of the same corridor and
overlap almost exactly, so use `--shape N` to read them one at a time.

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
