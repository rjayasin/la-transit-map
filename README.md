# la-transit-map

Animated 24-hour visualization of every scheduled transit vehicle in LA — Metro
bus & rail plus 12 municipal systems and Metrolink — played over Metro's
official "Bus and Rail System" map (May 2026).

## Run

```sh
python3 -m http.server 8741
# open http://localhost:8741
```

- **Controls** — play/pause, scrubber, speed (30–400×).
- **Navigation** — drag to pan. On a Mac trackpad, gestures follow Maps.app:
  two-finger swipe pans, pinch zooms. A mouse wheel zooms.
- **URL params** — `?t=8:30&speed=150&paused=1`.

## How it works

- **Background** — a 4096px `map.png` plus a WebP tile pyramid
  (`tiles/{2,4,8}/`, 512px tiles ≈ up to 700 dpi of the 47″ sheet), rendered
  from the PDF vectors. Tiles cascade in as you zoom, and zoom is capped at the
  deepest level's 1:1, so the background is never upscaled.
- **Data** — 15 static GTFS feeds are reduced to one service date and emitted
  as `schedule.json` (~7 MB): **26,902 trips on 336 routes**, as route
  colors/labels, shape polylines in map pixels, per-stop distance along each
  shape, and stop arrival times. Trips crossing midnight are duplicated at
  −24 h so owl service appears at the start of the day.
- **Georeferencing** — the map is only quasi-geographic, so a lat/lon→pixel
  transform is fitted rather than assumed: color-mask the drawn rail lines,
  then ICP-align GTFS rail shapes to them (affine init → 2nd-order polynomial
  warp, median error ~11 px). Downtown is drawn as a rotated call-out panel and
  gets its own transform.
- **Line snapping** — the warp alone leaves vehicles beside the artwork, so
  every shape is then pulled onto its system's *drawn* line.
  - Each system's lines are masked by color. Rail colors are read off the tile
    pyramid, where the printed ink is faithful; bus colors are seeded from the
    legend swatches and refined against pixels along the routes.
  - The snap displacement is smoothed along the shape, so whole stretches move
    onto the same street instead of individual points grabbing different
    parallels, with a short-window pass afterwards to take up the slack at
    junctions.
  - Place labels dim the artwork beneath them rather than hiding it, so the
    faded ink is recovered by reading a pixel as line color blended over the
    page — otherwise a broken line loses the snap to whatever parallel street
    stays continuous.
  - A whole agency shares one drawn color, so shapes are also pinned by
    *anchors*: the numbered route badges printed on the map, extracted from the
    PDF. Between consecutive badges the drawn line is walked (a coarse lattice
    and a shortest path) and sampled into intermediate anchors, so the
    correction follows the drawn corridor rather than a chord across it.
  - Route numbers collide across agencies, so a candidate badge is kept only
    when its own chip color is best explained by this agency's.
  - Shapes whose lines aren't drawn at all (minor routes, the gray-drawn
    agencies, Metrolink) keep the warp.
- **Rendering** (`index.html`, vanilla canvas) — each vehicle is a labeled
  circle in the color the map draws its line in, so every sprite matches the
  artwork it rides on. Trains pull away, run, and brake to a stand at each
  station, inside the scheduled time; buses keep their speed through the
  kerbside stops the map doesn't mark. Rail draws above buses, vehicles off the drawn map are hidden, and the day loops
  midnight→midnight.

## Source files

All the Python is offline build tooling — nothing runs in the browser. The
pipeline order is `georef.py` → `georef_inset.py` → `build_data.py`;
`make_tiles.py` is independent.

- `scripts/build_data.py` — builds `schedule.json`: picks a service date per
  feed, warps and snaps every shape, projects stops onto them, and emits the
  routes / shapes / patterns / trips plus the Downtown inset geometry.
- `scripts/georef.py` — fits the main lat/lon→pixel transform, and owns the
  map constants the rest of the pipeline shares (drawn rail colors, mask
  tolerances, excluded regions).
- `scripts/georef_inset.py` — the same fit for the Downtown call-out panel,
  whose rotated grid needs its own transform, plus that panel's geometry.
- `scripts/make_tiles.py` — rasterizes the background tile pyramid from the map
  PDF. Only needs rerunning if the PDF changes.
- `scripts/debug_line.py` — draws one route's stored path over the map, for
  checking a line against the artwork it should be riding.
- `scripts/speed_check.py` — ranks the vehicles that move implausibly fast,
  which is how a bad path usually shows itself in the animation.
- `scripts/orphan_check.py` — lists vehicles labelled with a designation the
  map never prints, which a rider has no way to look up.
- `index.html` — the whole client: loader, canvas renderer, and controls.

## Rebuild data

```sh
python3 -m venv .venv && .venv/bin/pip install numpy scipy pillow pymupdf
cd data/gtfs && for z in *.zip; do unzip -o "$z" -d "${z%.zip}"; done && cd ../..
.venv/bin/python scripts/georef.py      # refit the transform (optional)
.venv/bin/python scripts/build_data.py  # regenerate schedule.json
.venv/bin/python scripts/make_tiles.py  # only if the map PDF changes
```

- The color masks depend only on the artwork and the code that reads it, so
  they are memoized under `scratch/mask-cache/`, keyed by a digest that
  invalidates itself when either changes. A rebuild is **~30 s** warm against
  **~80 s** cold; delete the directory to force a cold one.
- The build is deterministic — the same inputs give a byte-identical
  `schedule.json`, so diffing two runs shows exactly what a change did.

## Checking a line against the artwork

`debug_line.py` answers "why is this bus off its street?". It writes
`scratch/debug_<system>_<line>.png`, cropped to the route, drawn on the
high-resolution tiles so badges and labels under the line stay legible. A route
reaching downtown is drawn on both the main map and the call-out panel, and the
two are snapped independently, so it writes `..._inset.png` as well.

```sh
.venv/bin/python scripts/debug_line.py 720                     # Metro Bus 720
.venv/bin/python scripts/debug_line.py 2 --system "Big Blue"   # shared number
.venv/bin/python scripts/debug_line.py 720 --stops             # + stop positions
.venv/bin/python scripts/debug_line.py 720 --shape 0           # one variant only
.venv/bin/python scripts/debug_line.py 720 --inset             # only the panel
```

Paths come straight out of `schedule.json`, so what you see is what the
animation plays back. A shared route label prompts for which system to draw
(`--system` skips it). Each route has several shape variants, listed on stdout
by trip count and drawn in different colors; the top two are usually the two
directions of one corridor, so `--shape N` reads them one at a time.

### Finding bad paths without looking for them

`speed_check.py` works the other way round: it scans every stop-to-stop segment
for vehicles moving faster than a bus can, which is how a bad path shows itself
in the animation.

```sh
.venv/bin/python scripts/speed_check.py              # worst 25 over 120 km/h
.venv/bin/python scripts/speed_check.py --over 300   # only the egregious ones
.venv/bin/python scripts/speed_check.py --inset      # inside the Downtown panel
.venv/bin/python scripts/speed_check.py --csv        # every hit, machine-readable
```

Speed is measured against the timetable the client actually animates,
de-tying included, so minute-quantized GTFS times don't register as teleports.
Each row carries a **detour** ratio — path length between the two stops over
the straight line between them — which separates the two causes: well above 1
means the shape wanders and the vehicle sprints to keep the schedule (a pathing
error), while ~1 means it really does travel that far and the feed is worth a
look. Rows are grouped per shape segment, since one bad shape shows up on every
trip that runs it, and each route prints its `debug_line.py` command.

### Vehicles you can't look up

`orphan_check.py` compares each vehicle's label against every word the map
prints, and reports the ones a rider could never find.

```sh
.venv/bin/python scripts/orphan_check.py                 # every orphan, busiest first
.venv/bin/python scripts/orphan_check.py --mislabelled   # only the fixable ones
.venv/bin/python scripts/orphan_check.py --min-trips 20
```

It separates the two causes. Most orphans are honest — the agency's lines
aren't drawn, or the route is too minor to badge. The interesting ones are
*mislabelled*: the map does designate the route, just not as we do, so the
badge is sitting right there to copy.

## Known limitations

- **Missing systems** — Glendale Beeline (download blocked), OCTA / AVTA /
  Santa Clarita / Simi Valley (essentially off-map), Amtrak, and the community
  shuttles in the map's legend with no public GTFS.
- **Stale feeds** — Torrance Transit's newest public GTFS is from Jan 2024;
  Culver City / Montebello / GTrans calendars end just before the target date.
  Those systems animate their busiest covered Wednesday instead.
- **Metrolink** has no shape geometry in its feed, so its trains run
  station-to-station straight lines.
- **Schematic corners** — the map distorts the far San Fernando Valley and the
  far east, where buses can drift off their drawn streets.
- **Downtown** is a ghosted call-out box on the source map; vehicles pass over
  it on the main map and are mirrored into the panel.

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
