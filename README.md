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
  −24 h so owl service appears at the start of the day. A trip begins at its
  origin's *departure* time, not its arrival: a bus staged at a terminal
  through its recovery time isn't in service yet, and drawing it there pooled
  Foothill's fleet — which times some origins up to two hours early — into
  motionless clusters at Pomona, Montclair and El Monte.
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
  - Metro and LADOT snap to the sheet's *vectors* rather than to that mask —
    see below; the mask still finds their badges, which are chips filled with
    the line color rather than strokes of it.
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
    when its own chip color is best explained by this agency's — measured
    against every *other* agency's drawn colors, but never against this
    agency's own. That distinction matters: the color a shape snaps to is
    refined off the drawn lines and drifts from the legend seed it started at
    (Foothill's came out a dozen px away), and the seed is still in the rival
    palette, so left in it the seed sits closer to the agency's own chip than
    the refined color does and rejects it as foreign. Foothill's Silver Streak
    lost 16 of its 18 "SS" badges that way and, unpinned, its busway warp sat
    up on Valley Blvd; folding the seeds into the own-color set fixes it.
  - A badge belongs to the point of the shape nearest it, which asks the warp
    to be closer to the truth than the streets are to each other. On a route
    that runs out and back on parallel streets it isn't: GTrans 2 loops north
    on Normandie and Vermont and south on Western, drawn 30 and 56 px apart,
    and the warp puts all three 25-40 px east of their ink, so every
    northbound badge came out nearest the southbound leg and the fit dragged
    each leg across the loop onto the other's street. Two badges a street
    apart claiming the same stretch is that failure showing, and the badges
    are then re-read against the shape slid bodily onto them — the warp's
    error varies slowly enough that one vector covers every leg. The slide is
    charged for its length and refused unless it clears most of the residual,
    so it only decides which badge speaks for which leg.
  - Metrolink carries no badge anywhere on the sheet, and its lines share one
    ink and one crosshatched livery, so where two run parallel the artwork
    alone cannot say which is which: through Vernon the Orange County and
    Riverside lines are drawn 26 px apart on the same heading, near enough that
    the warp put the OC's schedule closer to the Riverside track than to its
    own, and the snap left it there — under the "VERNON" label, on the line the
    sheet labels "RIVERSIDE LINE". But the sheet *does* say which is which,
    and in the words GTFS names the route with: it writes each railroad's name
    along it, repeated down its length, and metrolink's route_id is "Orange
    County Line". Those names anchor the way a numbered route's badges do,
    within 6-9 px of their own track. The 91/Perris Valley borrows the Orange
    County Line's name, sharing its track for the whole of its length here and
    drawn as that one line; the Inland Empire-Orange County line borrows
    nothing, coming no nearer than 467 px to any label on the sheet.
  - A route's badges cover the whole route, but a shape is one *variant* of
    it, so where a route forks, the badges on one fork are still inside the
    gate of a variant taking the other and drag it bodily across. Metro 487's
    Rosemead Blvd workings were pulled 143 px west onto the San Gabriel
    branch. A badge is printed on one line, so it goes to whichever variant
    passes nearest it: a shape keeps a badge while it is about as near as the
    nearest variant gets, and loses it when another explains it far better.
    On the shared trunk every variant is equally close and they all keep it.
  - A color mask can only find a line the raster shows, and the sheet draws
    lines it doesn't. Those lines are read out of the PDF's vectors instead,
    where every route is a stroke in its agency's ink — no tolerance, no rival
    colors, nothing to recover from a rendering. Four networks need it:
    - **Metrolink** rides the crosshatched railroad, inked in the same gray the
      sheet uses for place labels and minor street art, so masking that color
      selects most of the page.
    - **LADOT** is the same story in olive: its mask comes back 278k pixels,
      mostly label glyphs, and every LADOT route snapped to the nearest word.
    - **Part-time services** are drawn as thin dashed lines, and at 4096 px
      those dashes blend into the page or vanish under a heavier line alongside
      — Metro 233 through the Sepulveda Pass is dashed orange laid against the
      761's red ribbon, and none of it reaches the raster at all.
    - **The G Line busway** has the opposite problem: its ribbon is on the
      raster clearly enough, but so is everything the raster confuses with it.
      Its orange is hotter than the streets' on the sheet and all but identical
      to theirs in map.png, so its mask is read off the tile pyramid, where the
      two stay 57 apart. That keeps whole parallel streets out, and still lets
      in the orange badge chips printed alongside, whose antialiased fringe
      blends to within tolerance of the ribbon: single stray pixels under the
      "154" and "237" chips bent the line 40 px north of Burbank Blvd, and the
      chips around De Soto dragged a Canoga working diagonally across three
      blocks of blank page. The mask also stops dead where the sheet ghosts the
      busway inside its "See G Line detour inset" call-out, and the line sagged
      off it onto the dashed alignment below. The ink has one stroke per drawn
      line, no chips at all, and draws the ghosted stretch in the same ink as
      the rest.
  - Where two liveries share one ink they are two stroke styles of it, so the
    dash pattern tells them apart: the railroad's centreline under its dashed
    ticks (the ticks stick out sideways and would only pull a shape off the
    line), and LADOT's solid DASH against its dashed Commuter Express, which
    keeps either from being dragged onto the other's streets.
  - A mask smears a line across its casing, its badges and the fringe of
    whatever is drawn beside it, so a shape can sit on the mask while its own
    line is a good way off — 233 was resting against the 761 ribbon 25 px west
    of its ink. Snapping to a centreline therefore keeps the full coarse-to-
    fine ladder even once anchored, rather than the two tight passes the mask
    settles for.
  - The sheet writes a Commuter Express number as plain text beside its line
    rather than on a chip, so those anchors are taken by distance to the ink
    instead of by the agency's color under the word.
  - Shapes whose lines aren't drawn at all (minor routes, the gray-drawn
    agencies) keep the warp. So does any stretch crossing the Downtown
    call-out: the panel replaces 200 px of every route with a ghosted
    schematic, so there is nothing there to reach for, and interpolating a
    correction across it piled 534's downtown end onto the 409's Figueroa.
  - Snapping only moves a point sideways, and it pads the smoothing at the
    ends, so a rail line finishes wherever the warp left it rather than where
    the artwork stops — 70 px short of the E line's Atlantic, 24 px past the B
    line's North Hollywood and out into blank page. Both ends of every rail
    line are therefore squared up with the drawn line: an overshoot is cut back
    to the ink, an end left short is walked on along its own ink (and through
    the platform markers interrupting it) until the drawn line stops, and the
    end then stands in the middle of the platform it finished at. A walk that
    has to turn away from the line's heading, or that gets too far, is one
    where the track carries on rather than ending, and is dropped; a *long* run
    off the ink is the Downtown call-out, where nothing is drawn for 200 px and
    the warp is all there is, and is left alone.
  - The G Line busway is squared the same way, being drawn the same way: its
    own ribbon, ending at a platform. It needs it more than rail does, because
    out in the Valley the warp's 50 px error runs *along* the busway as much as
    across it, which a sideways snap cannot answer for — every end of every
    working landed somewhere other than its terminus, 47 px short of North
    Hollywood or (before the ink) 50 px past it and away down the B line's red
    toward Universal City. The squared line is then resampled back to the point
    count it came in with, so it stays index-aligned with the warp and the
    stops still carry over rather than being re-projected. With both lines now
    running platform to platform, that pins the ends exactly and leaves every
    station between them a median 10 px from its drawn platform, down from 42.
    Handing those stops to the platform matcher rail uses was worse, not
    better: the warp lags by half the distance between G Line stations, so the
    alignment minimizing total offset is the one putting every station on the
    platform *before* its own, and that is the one it found.
  - A snapped line is finally *despiked* and *unfolded*. Both take out the same
    scar, at two scales: the line running out along a piece of ink and straight
    back down it, a hairpin the artwork never makes. Despiking straightens the
    slivers, where the dart is a dozen px. Unfolding takes out the long ones —
    where the GTFS detours off the line the sheet draws (a bus round a transit
    centre, a jog through an office park, a terminal loop the schematic ends in
    a stub), the detour has no ink of its own and the snapper crushes it onto
    the ink beside it, laying tens of px of route back over itself. The run is
    replaced by the straight line between the points either side, which don't
    move; a fold at an *end* instead keeps whichever leg reaches the terminus,
    so the vehicle starts at its drawn end and drives in rather than stopping
    short of it.
  - Three things hold those passes back from the doubling-back a route really
    makes. A square street corner turns without returning and a circuit round a
    block keeps its two arms apart, so neither reads as a fold. The *warp* is
    consulted over the same stretch — where the route itself runs out and back
    down one street, the retracing is the route's and stays. And a fold that is
    the only thing reaching one of the route's printed badges is a detour the
    sheet draws, so it stays too — Metro 601's run down to the badge on Burbank
    Blvd doubles back exactly the way the snapper's folds do.
  - A third pass, *undetouring*, answers the failure none of that can see. A
    mask holds a whole agency's lines and cannot tell one of them from another,
    so where the sheet paints a label or a crossing line over a route's own ink
    the nearest ink for that stretch is a *sibling route*, and the smoothed
    displacement walks the path onto it and back. The 61-point smoothing makes
    that a gentle bulge with no sharp turn in it, so despiking and unfolding
    have nothing to charge and `path_check` scores it a flat zero — Foothill 493
    was visibly off its line at zero. What does see it is the warp: `base` and
    `full` are the same points before and after snapping, so a stretch leaving
    the warp far and returning to it is the snapper having found ink the warp
    says is not this route's. Measured against the *sustained* part of the
    correction, since a shape the badges have rightly carried bodily onto its
    street never returns to the warp at all.
  - What holds *that* back is the drawing itself. The excursion and the
    flattening that would replace it are each measured against what the route is
    drawn in — the PDF's strokes where a route has them, its agency's colour
    mask where it doesn't — and a flattening standing further off the drawing
    than the excursion does is not a flattening but the removal of the line.
    Comparing the two a good way out along the run rather than at the median is
    what makes a holed mask usable for this: a label knocks out a short piece of
    a run and the drawing resumes either side of it, while a corner cut across
    blank page is off the drawing for as long as the corner lasts. Big Blue Bus
    14's turn from Centinela onto Bluff Creek was being flattened into the chord
    across it, and Metro 180's step from Broadway onto Colorado before it.
  - Where the sheet prints no badge over a stretch that needs one, a point on
    the drawn line is placed by hand (`PINNED_ANCHORS`) and serves as a badge
    does. Two reasons it comes to that: a shared transit hub prints each of its
    routes once in the municipal gray, so Metro 2's chip at the UCLA gateway is
    Big Blue Bus's and the colour gate rightly refuses it; and the badge-to-
    badge corridor walk needs the mask to be continuous, which it is not where
    Culver CityBus's olive crosses BBB 14's gray by the Culver City Transit
    Center. A pin near an end of a shape also cuts the overshoot back to itself,
    for the schematic that ends a route at its hub while the GTFS runs on to a
    layover the map omits — BBB 14 carried on past the transit centre and
    snapped onto the railroad crosshatch down the 405.
  - Nothing is taken on faith. Every candidate is scored on the stored,
    rounded geometry the animation actually plays — by the very measure
    `scripts/path_check.py` ranks on, plus what its excursions cost — and the
    best wins, the snapper's own shape taking ties, and anything ranking worse
    on the hairpin measure than the snapper's own shape thrown out before it is
    scored at all. So no shape comes out worse than it went in. Together they
    settle 393 shapes and take two thirds off the fleet's total hairpin
    turning, with every rail line still at zero.
- **Rail platforms** — where a train halts is the white marker the sheet draws,
  not the warped stop, and matching each stop to its nearest one double-books
  platforms wherever the warp lags the artwork by more than the stops are
  apart. The platforms along a line *are* its stop sequence, so the two are
  aligned in order instead — one to one, and however far the warp has slid.
  Stops whose platform isn't drawn, which is most of downtown under the
  call-out panel, fall in the gaps and keep the warp.
- **Downtown inset** — a route reaching downtown is mirrored into the call-out
  panel, and which stops belong to which mirrored run is decided by comparing
  distances along the main shape. Both sides of that comparison go through one
  measure, because two callers measuring differently is a mismatch that only
  shows when the snap moves: the runs used to be placed by projecting onto the
  snapped shape while the stops were carried over from the warp, and a
  Metrolink shape shifting 28 px inside the call-out — where nothing is drawn
  and the geometry is the warp's own noise — was enough to put Union Station
  outside its own run and drop every Metrolink line out of the panel.
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
- `scripts/path_check.py` — ranks routes by how un-straight their drawn path
  is, worst first, and locates each kink. The build gates its own cleanup
  passes on this same score, so a route still high in the ranking is one the
  snapper put on the wrong ink rather than one it merely roughed up.
- `scripts/freeze_log.py` — serves the map *and* records what the page reports
  about itself, so a freeze leaves evidence outside the tab that froze.
- `scripts/freeze_report.py` — reads that recording back and says where the page
  stopped.
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
.venv/bin/python scripts/debug_line.py 720 --no-stops           # just the path
.venv/bin/python scripts/debug_line.py 720 --shape 0           # one variant only
.venv/bin/python scripts/debug_line.py 720 --inset             # only the panel
```

Where each vehicle halts is marked by default and, for a rail line, the
platforms the map draws — the white shapes with black strokes — are outlined
around them, so the two can be read against each other. `--no-stops` leaves
just the path.

Paths come straight out of `schedule.json`, so what you see is what the
animation plays back. A label shared across systems prompts for which one to
draw (`--system` skips it); one shared *within* a system is drawn as the map
draws it, all of them together — the sheet's "437" is LADOT's Marina del Rey
and Playa Vista workings alike. Each route has several shape variants, listed
on stdout by trip count and drawn in different colors; the top two are usually
the two directions of one corridor, so `--shape N` reads them one at a time.

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

## Catching a freeze

The tab has frozen hard enough that only closing it helps — which also throws
away the console, the devtools pane, and every snapshot that would have said
why. So the page can be made to report on itself from a *second thread* and
ship the record out of the process as it goes.

```sh
.venv/bin/python scripts/freeze_log.py           # serve on :8741 and record
# open http://localhost:8741/index.html?trace=1 and reproduce the freeze
.venv/bin/python scripts/freeze_report.py        # read back where it stopped
```

`freeze_log.py` is a drop-in for the README's `http.server`, so nothing else
about running the map changes. Under `?trace=1` the page hands a snapshot to a
Worker twice a second; the Worker posts them back and, when they stop arriving,
says so once a second with the last one attached. A frozen main thread cannot
suppress that, because the Worker is not on it.

The report answers the question the old console logs could not:

| what it says | what it means |
| --- | --- |
| stall reports, worker still posting | the main thread is blocked or gone, but the process lives — `last` is the final state the page reported |
| nothing after some point | the content process died with the tab; the last sample is what was climbing on the way out |
| samples continue, `rafGapMax` climbs | frames are being asked for and not delivered — the compositor, not us |
| `tickLateMs` and `workerLateMs` climb together | the whole process is starved, so memory pressure or the OS |
| `driftMs` jumps | the tab was suspended, not wedged — a different problem entirely |

`peakTileMB`, `imageMB` and `canvases` are there to catch the memory story, and
`heapMB` plus `measureUserAgentSpecificMemory()` the JavaScript one. Read those
two together rather than trusting either: the UA measurement reported 27.8 MB on
a page holding 102 MB of decoded images, because canvas backing stores and image
surfaces are outside its scope. If the tab dies while it stays flat, the memory
is somewhere the page cannot see.

If nothing is listening on `/_trace` the records still accumulate in
`localStorage`, so `transitTrace()` in the console after reopening the tab
returns the tail of them anyway.

## Known limitations

- **Missing systems** — Glendale Beeline (download blocked), OCTA / AVTA /
  Santa Clarita / Simi Valley (essentially off-map), Amtrak, and the community
  shuttles in the map's legend with no public GTFS.
- **Stale feeds** — Torrance Transit's newest public GTFS is from Jan 2024;
  Culver City / Montebello / GTrans calendars end just before the target date.
  Those systems animate their busiest covered Wednesday instead.
- **Metrolink, Metro bus and LADOT** snap to lines read out of the PDF vectors,
  so they sit on the drawn line: a median 1.0, 0.9 and 1.1 px off it. Two
  exceptions keep the warp — Metrolink's Inland Empire-Orange County line, most
  of whose length has no railroad drawn near it, and LADOT's Commuter Express
  142, which the sheet doesn't draw where the warp puts it.
- **Schematic corners** — the map distorts the far San Fernando Valley and the
  far east, where buses can drift off their drawn streets.
- **Downtown** is a ghosted call-out box on the source map; vehicles pass over
  it on the main map on the warp alone, since nothing is drawn under the panel
  to snap to, and are mirrored into the panel.

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
