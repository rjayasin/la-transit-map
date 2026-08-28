# la-transit-map

**[Live map → rjayasin.github.io/la-transit-map](https://rjayasin.github.io/la-transit-map/)**

Animated 24-hour visualization of every scheduled transit vehicle in LA — Metro
bus & rail plus 12 municipal systems and Metrolink — played over Metro's
official "Bus and Rail System" map (May 2026).

## Run

```sh
python3 -m http.server 8741
# open http://localhost:8741
```

- **Controls** — play/pause, scrubber, speed (30–400×). It opens at the current
  Los Angeles time and runs on from there.
- **View mode** — both modes play *today's* timetable. The popover switches
  between the time-lapse, which scrubs the whole day at 30–400×, and **Live**,
  which holds the map to Los Angeles' clock at 1×.
- **Navigation** — drag to pan. On a Mac trackpad, gestures follow Maps.app:
  two-finger swipe pans, pinch zooms. A mouse wheel zooms.
- **Tap a vehicle** to trace the line it runs, anywhere it is drawn — on the
  map or inside the Downtown call-out. Tap another to switch, empty space to
  clear.
- **URL params** — `?t=8:30&speed=150&paused=1`, or `?live` to open live.

## How it works

The background is Metro's printed map; the vehicles are GTFS schedules warped
onto it. Everything below the browser is offline build tooling that runs once
and emits `schedule.json`.

- **Background** — a 4096px `map.png` plus a WebP tile pyramid
  (`tiles/{2,4,8}/`, 512px tiles ≈ up to 700 dpi of the 47″ sheet), rendered
  from the PDF vectors. Tiles cascade in as you zoom, and zoom is capped at the
  deepest level's 1:1, so the background is never upscaled.
- **Data** — 15 static GTFS feeds are reduced to one service date per weekday
  and emitted as `schedule.json` (~12 MB): **49,960 trips on 337 routes** —
  a whole week, around 26,000 of them running on any one weekday — as route
  colors/labels, shape polylines in map pixels, per-stop distance along each
  shape, stop arrival times, and a bitmask per trip of the days it runs, so
  the week costs one list rather than seven. Trips crossing midnight are
  duplicated at −24 h onto the following day, so owl service appears at the
  start of the day.
- **Georeferencing** — the map is only quasi-geographic, so a lat/lon→pixel
  transform is fitted rather than assumed: color-mask the drawn rail lines,
  then ICP-align GTFS rail shapes to them (affine init → 2nd-order polynomial
  warp, median error ~11 px). Downtown is drawn as a rotated call-out panel and
  gets its own transform.
- **Line snapping** — the warp alone leaves vehicles beside the artwork, so
  every shape is then pulled onto its system's *drawn* line. Each system's
  lines are found either by color-masking the raster or, where a color mask
  can't separate them from the page, by reading the PDF's own strokes. Shapes
  are pinned to the route badges the sheet prints, the drawn line is walked
  between consecutive badges so corrections follow the drawn corridor rather
  than a chord across it, and the whole fit re-runs while each pass learns
  something the last one didn't have. Cleanup passes then take out the
  artifacts snapping introduces — hairpins, folds, and excursions onto a
  neighbouring route's ink. Every candidate is scored against the drawing and
  the snapper's own shape takes ties, so no shape comes out worse than it went
  in.
- **Rail platforms** — where a train halts is the white marker the sheet draws,
  not the warped stop. The platforms along a line *are* its stop sequence, so
  the two are aligned in order rather than by nearest-match, which would
  double-book platforms wherever the warp lags.
- **Downtown inset** — the sheet redraws downtown as a rotated call-out panel,
  so routes reaching it are mirrored into the panel and snapped there
  separately. Its vehicles are tappable through the same placement function the
  renderer draws them with.
- **Rendering** (`index.html` + `app.js`, vanilla canvas) — each vehicle is a
  labeled circle in the color the map draws its line in. Trains pull away, run,
  and brake to a stand at each station within the scheduled time; buses keep
  their speed through the kerbside stops the map doesn't mark. Rail draws above
  buses, vehicles off the drawn map are hidden, and the day loops
  midnight→midnight.

Where the artwork can't settle something on its own, it's named by hand in a
table in `build_data.py`. See `implementation_notes.md`.

## Source files

All the Python is offline build tooling — nothing runs in the browser. The
pipeline order is `georef.py` → `georef_inset.py` → `build_data.py`;
`make_tiles.py` is independent.

| File | What it does |
|---|---|
| `scripts/build_data.py` | Builds `schedule.json`: picks a service date per feed, warps and snaps every shape, projects stops onto them, emits routes / shapes / patterns / trips plus the inset geometry |
| `scripts/georef.py` | Fits the main lat/lon→pixel transform; owns the map constants the pipeline shares (drawn rail colors, mask tolerances, excluded regions) |
| `scripts/georef_inset.py` | Fits the Downtown call-out's own transform, plus that panel's geometry. The call-out redraws the rotated downtown grid square, so this is a separable fit in the grid's two axes rather than a polynomial in lon/lat |
| `scripts/make_tiles.py` | Rasterizes the background tile pyramid from the map PDF. Only rerun if the PDF changes |
| `index.html` | Markup, styles, and a bootstrapper. Asks `version.json` which build is live and loads the client from that URL |
| `app.js` | The client: loader, canvas renderer, controls. Fetched at `app.js?v=<sha>` |
| `scripts/stamp_build.mjs` | Run by the deploy workflow. Rewrites `index.html`'s `__BUILD__` / `__V_*__` placeholders with the build id and data content hashes |

Checks — each answers a different way for a path to be wrong:

| File | Asks |
|---|---|
| `scripts/debug_line.py` | What does one route's stored path look like over the artwork? |
| `scripts/drift_check.py` | How much of each route runs off the line the sheet draws for it? |
| `scripts/path_check.py` | Which routes have the least straight paths, and where are the kinks? |
| `scripts/speed_check.py` | Which vehicles move implausibly fast? (how a bad path shows in the animation) |
| `scripts/orphan_check.py` | Which vehicles carry a label the map never prints, so a rider can't look them up? |
| `scripts/ink_gap_check.py` | Where does an agency's mask have holes in its own drawn lines? |
| `scripts/freeze_log.py`, `freeze_report.py` | Where did the page stop? (serve-and-record, then read back) |

Browser tests, all `node scripts/<name>.mjs`; the last two need the map served
on :8741.

| File | Checks |
|---|---|
| `stall_test.mjs` | Under a fake clock: a backgrounded tab never reads as a stall, a stalled page always does |
| `deploy_test.mjs` | A stamped copy reports its build, asks for data by content hash, notices a newer build, and offers a reload instead of taking one |
| `live_stall_test.mjs` | The watchdog in a real browser: kills `rAF`, checks the stall is caught, the verdict names the page, the tab strip warns, and the re-arm restores the loop |
| `inset_tap_test.mjs` | A tap on a vehicle in the Downtown panel selects the one drawn *there*; blank panel clears the selection |
| `live_mode_test.mjs` | Live mode runs on Los Angeles' clock at 1×, survives a gap in frames, and hands the clock back on the way out; both modes open on today's timetable, at the current time |

## Rebuild data

```sh
python3 -m venv .venv && .venv/bin/pip install numpy scipy pillow pymupdf
cd data/gtfs && for z in *.zip; do unzip -o "$z" -d "${z%.zip}"; done && cd ../..
.venv/bin/python scripts/georef.py      # refit the transform (optional)
.venv/bin/python scripts/georef_inset.py   # ... and the Downtown call-out's
.venv/bin/python scripts/build_data.py  # regenerate schedule.json
.venv/bin/python scripts/make_tiles.py  # only if the map PDF changes
```

Color masks are memoized under `scratch/mask-cache/`; a rebuild is ~30 s warm
against ~80 s cold. The build is deterministic, so diffing two runs shows
exactly what a change did.

## Checking a line against the artwork

`debug_line.py` answers "why is this bus off its street?". It writes
`scratch/debug_<system>_<line>.png`, cropped to the route and drawn on the
high-resolution tiles so badges and labels under the line stay legible. A route
reaching downtown also gets a `..._inset.png`.

```sh
.venv/bin/python scripts/debug_line.py <line>
.venv/bin/python scripts/debug_line.py <line> --system <name>   # shared number
.venv/bin/python scripts/debug_line.py <line> --no-stops        # just the path
.venv/bin/python scripts/debug_line.py <line> --shape N         # one variant only
.venv/bin/python scripts/debug_line.py <line> --inset           # only the panel
```

A label shared across systems prompts for which one to draw; `--system` skips
the prompt.

Paths come straight out of `schedule.json`, so what you see is what the
animation plays back. Each route has several shape variants, listed on stdout
by trip count and drawn in different colors; the top two are usually the two
directions of one corridor, so `--shape N` reads them one at a time.

```sh
.venv/bin/python scripts/speed_check.py              # worst 25 over 120 km/h
.venv/bin/python scripts/speed_check.py --over 300   # only the egregious ones
.venv/bin/python scripts/speed_check.py --inset      # inside the Downtown panel
.venv/bin/python scripts/speed_check.py --csv        # every hit, machine-readable

.venv/bin/python scripts/orphan_check.py                 # every orphan, busiest first
.venv/bin/python scripts/orphan_check.py --mislabelled   # only the fixable ones
```

`speed_check` rows carry a **detour** ratio — path length between two stops over
the straight line between them — which separates the two causes: well above 1
means the shape wanders and the vehicle sprints to keep the schedule (a pathing
error), while ~1 means it really does travel that far and the feed is worth a
look.

`orphan_check` separates honest orphans (the agency's lines aren't drawn, or the
route is too minor to badge) from *mislabelled* ones, where the map designates
the route but not the way the feed does.

## Catching a freeze

The tab can freeze hard enough that only closing it helps — which also throws
away the console and every snapshot that would have said why. So the page
records its own liveness.

Nothing to turn on. `stalledVisibleMs` — how long the page has been visible
without drawing — is the whole diagnosis in one number, and the one to read
first:

| `stalledVisibleMs` | what it means |
| --- | --- |
| seconds of it | the freeze. Already recorded — see below |
| near zero, `sinceFrameMs` large | a backgrounded tab. No fault; rAF is supposed to stop |
| near zero, `rafGapMax` large | it stalled and recovered between snapshots |
| near zero, `slowFrames` climbing | a different failure — the main thread, not the loop |

A locked, minimized, or fully covered window reproduces the freeze signature
exactly (`fps: 0`, `sinceFrameMs` climbing into the minutes). Rule that out
first — `stalledVisibleMs` does it for you, since a hidden tab adds none of it.

Four seconds of stall trips a watchdog that snapshots to `localStorage`, so the
record outlives the tab. The page says at load that a record exists.

```js
transitFreeze()      // the snapshot at the stall, the run-up, and the verdict
transitFreeze(1)     // the run before that one
```

`verdict.browserRendering` says which of the two failures it was — read from
`document.timeline.currentTime`, which only the browser can advance:

| `verdict.browserRendering` | what stopped | what helps |
| --- | --- | --- |
| `true` | the browser is rendering; this page's rAF registration is gone | the watchdog re-arms it once a second, and it comes back on its own |
| `false` | the browser stopped updating the rendering for a visible document | nothing from in here. Reopen the tab; a reload keeps the same process |

`null` after a freeze is itself a finding — the watchdog's own timer didn't
fire, so the main thread was wedged rather than the loop starved. That is what
the black box is for: under `?trace=1` the page hands snapshots to a Worker,
which reports from off the main thread and cannot be suppressed by it.

```sh
.venv/bin/python scripts/freeze_log.py           # serve on :8741 and record
# open http://localhost:8741/index.html?trace=1 and reproduce the freeze
.venv/bin/python scripts/freeze_report.py        # read back where it stopped
```

| what it says | what it means |
| --- | --- |
| stall reports, worker still posting | the main thread is blocked or gone, but the process lives — `last` is the final state the page reported |
| nothing after some point | the content process died with the tab; the last sample is what was climbing on the way out |
| samples continue, `rafGapMax` climbs | frames are being asked for and not delivered — the compositor, not us |
| `tickLateMs` and `workerLateMs` climb together | the whole process is starved, so memory pressure or the OS |
| `driftMs` jumps | the tab was suspended, not wedged — a different problem entirely |

## Known limitations

- **Missing systems** — Glendale Beeline (download blocked), OCTA / AVTA /
  Santa Clarita / Simi Valley (essentially off-map), Amtrak, and the community
  shuttles in the map's legend with no public GTFS.
- **Stale feeds** — some municipal calendars end before the target service
  week; those systems animate their busiest covered date of the same weekday
  instead.
- **Scheduled, not realtime** — there is no vehicle feed behind either mode.
  They read today's stored timetable, so what they show is where each vehicle
  is *due*, not where it is. The day is matched by weekday against cached
  feeds, so a holiday animates its ordinary weekday's service.
- **Schematic corners** — the map distorts the far San Fernando Valley and the
  far east, where buses can drift off their drawn streets.
- **Off the sheet** — a few outer-suburban workings run past the edge of the
  printed map. They are hidden at the edge rather than drawn somewhere they
  aren't.
- **Downtown** is a ghosted call-out box on the source map; vehicles cross it on
  the main map on the warp alone, since nothing is drawn under the panel to snap
  to, and are mirrored into the panel.

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
| `montebello` | Montebello Bus Lines | [Mobility DB mirror](https://storage.googleapis.com/storage/v1/b/mdb-latest/o/us-california-montebello-bus-lines-gtfs-2201.zip?alt=media) |
| `gtrans` | GTrans (Gardena) | [Mobility DB mirror](https://storage.googleapis.com/storage/v1/b/mdb-latest/o/us-california-gtrans-gtfs-2270.zip?alt=media) |
| `pasadena` | Pasadena Transit | [Mobility DB mirror](https://storage.googleapis.com/storage/v1/b/mdb-latest/o/us-california-pasadena-transit-gtfs-41.zip?alt=media) |
| `burbank` | BurbankBus | [Mobility DB mirror](https://storage.googleapis.com/storage/v1/b/mdb-latest/o/us-california-burbankbus-gtfs-2149.zip?alt=media) |
| `beachcities` | Beach Cities Transit (Redondo) | [Mobility DB mirror](https://storage.googleapis.com/storage/v1/b/mdb-latest/o/us-california-beach-cities-transit-gtfs-1999.zip?alt=media) |
| `norwalk` | Norwalk Transit System | [City of Norwalk GTFS](https://nts.rideralerts.com/infopoint/gtfs-zip.ashx) |
