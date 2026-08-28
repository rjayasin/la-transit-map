# Implementation notes

Working notes for anyone (human or agent) changing the build. The README covers
what the project does; this covers what will bite you.

## Layout

`scripts/build_data.py` is the whole pipeline and is large. Rough order:

| Region | What lives there |
|---|---|
| Feeds & calendars | `FEEDS`, `FEED_NAMES`, `pick_date`, `parse_time` |
| Masks | `mask_tree`, `tile_tree`, `mask_pixels`, `unfade`, `knockout_panels`, `cached_pixels` |
| Drawn colors | `DRAWN_COLORS`, `LEGEND_SEEDS`, `BADGE_FILLS`, `refine_color` |
| PDF strokes | `pdf_ink`, `ink_tree`, `*_INK` constants |
| Anchors | `badge_words`, `route_anchors`, `trace_anchors`, `anchor_slide` |
| Corridor walks | `mask_path`, `bridges`, `align_walk` |
| Cleanup | `despike`, `unfold`, `undetour`, `trim_terminus` |
| Emit | `main` — builds `schedule.json` and prints a stats dict |

## When a line leaves its drawing

A snap follows whatever is in the tree it was handed, so a shape on the wrong
street is a statement about that tree, about the anchors, or about the sheet —
and which one it is decides the fix. Diagnose before reaching for a table.

1. **See both courses at once.** `debug_line.py <n> --system <name> --no-stops`
   puts the stored path over the artwork. Crop `tiles/<level>/<col>_<row>.webp`
   (512 px tiles, level = scale over the 4096 px base) for the drawing on its
   own — a path drawn over its line hides the line. What the sheet draws is the
   target, not what the street grid suggests.
2. **Ask the feed where the route actually goes.** The stop names in order
   (`stop_times.txt` joined to `stops.txt`) name the streets, and `to_px` puts
   them in map px. That separates the drawn corridor from the decoy running
   parallel to it before any pixel work starts.
3. **Score against the strokes, not the mask.** `pdf_ink([LEGEND_INK[feed]])`
   is the drawing itself — complete under every label, with no chips and no
   lettering in it — so the distance from the stored shape to those points is
   the deviation with nothing inferred. It is also the one yardstick a mask
   change cannot move. `drift_check --ink` is this for the agencies the PDF can
   settle.
4. **Ask whether the mask holds the line at all.** Query the agency's
   `mask_tree` with the ink points along the stretch. A run of ink several px
   from any mask pixel is a hole, and a hole is what a snap falls into: the
   shape leaves for whichever parallel corridor is unbroken.
   `ink_gap_check.py` asks the cruder version of the same question against the
   grey street art.

What the answer means:

- **A hole under a place name.** The artwork there is washed back, not painted
  out, and putting it back is `unfade`'s business. Its gates are measurable one
  pixel at a time — the fade fraction along page→color, the fit to this
  agency's blend, the fit to the best rival's — so print them for the pixels
  sitting on the ink. The gate that rejects them names the fix, and it is a fix
  for every line under every name rather than for this one route.
- **The mask holds the line and the path still leaves it.** Then it is an
  anchor question, not a mask one: nothing tells the snap which end of the
  drawing this leg belongs to. See `PINNED_ANCHORS` and the two pin failures
  below.
- **The mask holds something that is not the line** — lettering, a neighbour's
  casing — and the shape walks to it. That is a color question: `LEGEND_SEEDS`,
  `BADGE_FILLS`, or the agency belongs in `INK_SNAP` and should be snapped on
  strokes instead.
- **The sheet draws nothing under the stretch.** Then the warp is the best
  there is and the shape is already right. `drift_check` counts this apart as
  `beyond`; a yardstick that treats a sibling's ink as "near" will call it
  drift anyway, so check the artwork before believing the number.

Prefer the general fix to the particular one — a mechanism that mis-reads the
sheet mis-reads it everywhere — but price it. A change inside the mask
machinery moves every color-masked feed at once, so diff the two builds' shapes
per system, score both against the strokes, and look at the largest movers by
eye. A total that improves can still hide a route that got worse.

## Hand-tuned tables

Everything the artwork can't settle on its own is named in a table near the code
that reads it. When a route is on the wrong line, one of these is usually the
fix — try them in this order, least invasive first:

| Table | Use it when |
|---|---|
| `MAP_LABELS` | The sheet badges a route differently from the feed's `route_short_name` (or the name is prose, as for a DASH). Also fixes `orphan_check` rows |
| `SYMBOL_OWNERS` | The sheet symbolises by *operator*, so a shared symbol must be assigned to a specific route by hand |
| `BADGE_FILLS` | An agency's badge chips are its saturated legend ink, far enough from its washed line color that the color gate rejects its own badges |
| `LEGEND_SEEDS` | A refined line color has drifted somewhere the artwork isn't; name the stroke the legend actually uses |
| `PINNED_ANCHORS` | The sheet prints no badge over a stretch that needs one. A point on the drawn line then acts as a badge |
| `TRIM_TERMINI` | A pin can't both anchor and trim; give the terminus in *warp* px for the trim alone |
| `OVERRIDE_PATHS` | Nothing above can reach it. A corridor drawn by hand, spliced into the snapped shape. Last resort |
| `INSET_DIVERSIONS` | The feed routes some workings off the line the sheet draws, and only the call-out is magnified enough to show it. A box in inset px; the run inside it is flattened onto its chord |

Three recurring reasons a pin doesn't work, worth recognising before reaching
for an override:

- **The pin speaks for the wrong stretch.** Anchors attach to the nearest point
  of the *warp*, so where the warp lays one leg of a route over where another is
  drawn, a point on the drawn line attaches to the wrong leg and anchors the
  middle of the route instead of the end.
- **The warp never goes there.** A pin can only hold a stretch the warp
  actually passes through. Where the sheet straightens out a jog the route
  really makes — round a block, into a terminal, through a fairground — there
  is no warp on the drawn corridor to attach to, and no pin placed on it can
  put the line there. Only an override can.
- **The pin reads as a terminus.** `trim_terminus` cuts a shape back to a pin
  near its end (`TERMINUS_TAIL`), so a pin placed inside that tail removes the
  stretch it was meant to anchor. A limb shorter than that tail therefore
  cannot be pinned at all without losing the terminus beyond it: pinning the
  end and pinning the limb are the same choice, and only an override has both.

All three are one measurement: the distance from the intended pin to the
nearest warp point, and to the nearest point on any *other* leg of the same
warp. A few px, with the runner-up far behind, and the pin holds. Tens of px,
or a runner-up as close as the winner, and it will attach somewhere else. The
build says nothing when this happens — the shape simply comes back wrong in a
new place — so it is worth checking before a rebuild rather than after.

Snapping strategy per agency is set by the `STREET_SNAP` / `INK_SNAP` /
`SYMBOL_FEEDS` sets and `DRAWN_COLORS`.

## Chasing a divergent path

A line reported off its drawn ink is usually one *variant* of a route rather
than the route, and the checks are the wrong end to start from — `drift_check`
scores by colour, and a route that has wandered onto a sibling of the same
agency scores clean. Work from the drawing instead, in this order: trace the
corridor, stand up a harness, then search for the table entry. The first two
are what make the third cheap, and skipping them is what makes this slow.

**Trace the corridor first, and trust nothing else as ground truth.** Two
points known to be on the drawn line — a badge `route_anchors` returned, a
drawn terminus — and `mask_path(a, b, tree)` walks the centreline between them;
a walked length close to the straight distance says it stayed on one corridor
rather than going round a block. That polyline is the yardstick for every
question that follows: which variant is wrong, and whether a candidate fix
helped. A variant that *looks* right is not a substitute — sitting on the
agency's ink is not sitting on this route's line, and a sibling's corridor a
block over scores clean against every mask measure there is.

`corridor_check.py` is that walk and that scoring in one command:

```sh
.venv/bin/python scripts/corridor_check.py bigbluebus 9 --from 697,2065.7 --to 775.3,2166.4
```

It prints the traced corridor — paste-ready for a pin or an override — and then
each shape's distance to it and how much of it the shape covers, over the
stretch of the shape that runs it. It takes `--schedule`, so a `--only` refit
can be scored without a build. Read its walked-against-straight line first: a
walk that went round something is scoring everything below it against the
detour.

**Find the variant against that trace**, not against the other variants.
Comparing variants with each other has two traps: stored shapes keep only the
points `simplify` left, so vertex-to-vertex distance reports the gap to the
nearest *corner* and can be several times the real one, and where a route
passes through an area twice the nearest point on the other variant can be on
its other leg, which hides the divergence entirely. `debug_line.py` draws the
variants in listed order, so the last one drawn hides the ones under it — read
the printed legend, or pass `--shape`, before believing a colour.

**Read length as well as distance.** A path cutting the corner off a tight loop
can sit a few px from the corridor at its worst and still be obviously wrong on
screen. How much arc the shape spends inside the window is the second measure,
and a shortcut shows up there as ground it never covers — which is also what
the stops ride on, so it is the half that reaches the animation. Scoring a
candidate as (distance to the trace, fraction of the trace covered) catches
both at once.

**Refit the one route rather than rebuilding.** A full build is around two
minutes and almost all of it is shapes the change under test cannot reach.
`build_data.py --only <feed>[:<route>]` fits that route alone and writes a
`schedule.json`-shaped stub `debug_line.py --schedule` draws:

```sh
.venv/bin/python scripts/build_data.py --only bigbluebus:9
.venv/bin/python scripts/debug_line.py 9 --schedule scratch/refit_bigbluebus.json --no-stops
```

Two seconds for a route, five for a whole agency, against 72 for the build,
and the geometry is identical — it is the build's own code with the fit loop
narrowed. So a pin position is a loop, not a rebuild. What it does *not* give
you is `schedule.json`: it emits no timetable and no call-out runs, so
`drift_check`, `path_check` and `speed_check` still want a full build before
you commit.

The route token matches a route id, an id without its variant suffix, or the
designation the sheet prints, so `--only ladot:437` and `--only bigbluebus:9`
both work without looking an opaque feed id up.

**Two signatures worth recognising**, each of which names its own fix:

- *A foreign agency's badge chip printed over this agency's line.* It knocks a
  gap in the mask that no bridge will cross, because the block closes round it
  and `mask_path` prefers the drawn way round; `align_walk` then believes the
  block and the anchors land on it. The tell is a walk much longer than the
  straight distance between the two badges, with a run of mask distances of
  5–10 px under a chip. A pin *inside* the gap splits the badge-to-badge walk
  so that the leg holding the hole is shorter than `TRACE_SPAN[0]` — below that
  no walk is attempted at all and the straight interpolation, which is right
  here, stands.
- *One direction of a route right and the other wrong on the same badges.* The
  last badge before the divergence is attached to the wrong point of the warp:
  near a corner the distance to a badge tens of px off the corridor has a flat
  minimum, the two directions pick different points in it, and the
  displacements come out opposite. Beyond that last anchor `np.interp` holds it
  flat, so the whole unanchored tail inherits a correction pointing the wrong
  way. The tell is two adjacent badges whose fitted displacements disagree by
  more than a street. The fix is a pin in the tail.

**Placing a pin is a search with two constraints, both answerable before any
rebuild.** It has to be on the drawn line — take the coordinates off the trace,
never off the warp, which can be tens of px away — and it must not read as a
terminus. Walk candidate positions along the trace and print, for every shape
of the route, the distance to the nearest warp point and the arc from each end:
a pin within `TERMINUS_REACH` of the warp and within `TERMINUS_TAIL` of an end
cuts the shape back to itself. Pick from the rows that trim nothing. The one
deliberate exception is a pin *at* a drawn terminus, where the trim is the
thing you want — and where a leg is short enough that no other position on it
is trim-free, that pin plus one further up the corridor is usually the whole
fix.

**Placing an override.** `box` is matched against the warp, and the run
replaced runs from the *first* to the *last* warp point inside it: an excursion
that leaves the box in between is harmless, but a second pass through the box
later in the route swallows everything between the two. List the in-box index
runs for every shape of the route before trusting a box. One `path` serves both
directions, since the orientation is taken from the direction of travel rather
than from which end the shape enters by. Trace the corridor off the artwork and
draw the trace back over the tiles before wiring it in — a hook or a dip the
eye skips over is obvious the moment it is drawn.

**Where the box ends matters as much as the path.** The box picks its run off
the warp, but each end of the hand-drawn path has to meet a point the *snap*
placed, and the snap moves points along the line as well as across it. Put an
edge where it does — at a corner, or anywhere the fit is already wandering —
and the neighbour outside the box can sit past the end of the path, which
leaves a reversal where the two meet. Nothing measures this except
`path_check`, which will report a cusp at the box edge and no longer at the
thing you set out to fix. Edges belong in long straight stretches, where the
snap can only move the line sideways, and the fix for a cusp at an edge is to
move the edge out rather than to redraw the path.

## Verifying a change

The build is deterministic — same inputs, byte-identical `schedule.json` — so
diff two runs to see exactly what a change did. Then:

```sh
.venv/bin/python scripts/drift_check.py    # how far each route is off its drawn line
.venv/bin/python scripts/path_check.py     # hairpins and kinks
.venv/bin/python scripts/path_check.py --inset   # ... in the Downtown call-out
.venv/bin/python scripts/speed_check.py    # implausible speeds
.venv/bin/python scripts/speed_check.py --slow   # ... and vehicles held still
.venv/bin/python scripts/corridor_check.py <feed> <n> --from X,Y --to X,Y
                                           # against one traced stretch of line
.venv/bin/python scripts/debug_line.py <n> # look at it
```

`trips` is the whole week, so the checks rank weekend-only workings alongside
the rest and their trip counts are a week's, not a day's.

Count the shapes that changed, too. A fix aimed at one route should touch that
route's shapes and no others, and a table entry that reaches further than it
was meant to shows up here before it shows up anywhere else. Diff against a
rebuild of the *unchanged* tree rather than against the committed
`schedule.json`: the build is deterministic for one set of libraries, not
across them, and a numpy or scipy upgrade moves a hundred shapes on its own.
Two builds — one at HEAD, one with the change — cost four minutes and are the
only way to read that count.

**`drift_check` moves its own yardstick.** It refines the mask color off the
*stored* shapes, so a change that moves shapes onto their lines also changes the
color it measures against, and before/after totals from two different runs are
not comparable. Compare both shapes against one tree, refined the way the build
refines it — off the warps.

Three blind spots to know about, since a fix can look like a no-op:

- `drift_check` scores by color, so a route sitting on a *sibling* route's ink
  of the same color scores clean.
- `path_check` scores by straightness, so a smoothly cut corner scores zero.
  It also reads the main map alone unless asked for `--inset`: the call-out is
  a second, separately snapped drawing of the same routes, and its runs are
  stored apart from the shapes.
- `speed_check --slow` scores by a speed the sheet is not always entitled to.
  Three things hold a vehicle still without anything being wrong: a shape
  trimmed to its drawn terminus, which parks every stop on the omitted layover
  tail on one point; downtown compression, where the sheet barely moves and the
  call-out does; and a circulator that really is scheduled at a walking pace.
  The row's `pile`, `panel` and `held` columns are there to tell those from a
  stall, and `held` is worth reading in px — stop spacing is ~13 px and stored
  rounded, so a single px is quantization.

A change can legitimately make `path_check` rank a route *worse* — an exact
retrace is a true 180° fold where a wrong-but-smooth path was a shallow cusp.
Check what the shape actually does before treating a rank increase as a
regression.

## Gotchas

- **The panel's Metro masks come off the pyramid.** `INSET_ORANGE` is the ink
  itself; `ORANGE` is that colour after map.png's reduction has blended it with
  the page, 30 away. The pyramid also lets `inset_tile_tree` cut the badge
  chips out, which matters more down there than anywhere: a chip is a solid
  disc of the line's own colour a few tens of px off the line, and on the
  raster it is fused to it.
- **A colour mask can be mostly lettering.** Where an agency's line is thin and
  its colour muted, map.png's reduction blends the line toward the page while
  the place names set in the same grey survive intact — Big Blue Bus through
  Pacific Palisades read 800 mask px in a frame where the pyramid finds 3400,
  and nearly all of the 800 were type. A shape fitted on that is fitted to the
  labels, and every check reads clean: `drift_check` scores it against the same
  mask, and cutting a corner smoothly costs `path_check` nothing. `LEGEND_INK`
  and `INK_SNAP` are the way out where the PDF carries the agency's strokes;
  `ink_gap_check.py` is how to see the holes.
- **Mask cache keys include function source.** `code_stamp` hashes
  `inspect.getsource` of the mask-building functions, so editing them — comments
  included — invalidates `scratch/mask-cache/` and forces one cold rebuild
  (~80 s against ~30 s). Harmless, but don't be surprised by it.
- **`--only` reads a cached shape set; a full build never does.** Which shapes a
  feed runs is settled by its timetable, and the colour the feed is masked on is
  refined off the first twenty of them — so a refit that guessed the set from
  `trips.txt` could mask on a different colour and answer a question the build
  never asked. `scratch/shape-cache/` holds the set, stamped with the size and
  mtime of the files it came from; a full build writes it and reads nothing, and
  a refit whose stamp doesn't match reads the stop times as usual. The busways
  are the exception a refit can't take: they anchor on the station names printed
  beside them, which come out of the timetable, so `--only gtfs_bus` and
  `--only gtfs_bus:901` parse it whatever the cache says.
- **The colour masks read the PDF too.** A place name doesn't paint out the
  line it crosses; it washes it back, under a halo and under a page-coloured
  panel that `knockout_panels` reads off the PDF, and `unfade` puts what
  survives back into the mask. So the masks are keyed on the sheet as well as
  on `map.png`, and without pymupdf they lose the panels — a gap under a name
  is then a hole again, and the snap will take a parallel street instead.
- **A build without pymupdf poisons the cache.** The badges, the strokes and
  the knockout panels all come from the PDF, and each falls back to nothing
  rather than failing — but the empty stroke set is written to
  `scratch/mask-cache/` under a key that says nothing about which libraries
  were installed, so the *next* build reads it back and comes out wrong with no
  warning at all. Install pymupdf before building, and clear the cache
  directory if a run printed either "unavailable" line.
- **Module docstrings are user-facing.** Several scripts pass `__doc__` as their
  argparse `description`. Trimming one changes `--help`.
- **`index.html` must stay byte-stable across deploys.** It is the one URL that
  can't carry a version, so a cached copy has to be identical to a fresh one.
  Put changes in `app.js`, which is fetched content-addressed as `?v=<sha>`.
  `scripts/stamp_build.mjs` rewrites the `__BUILD__` / `__V_*__` placeholders at
  deploy time; don't hand-edit them. New UI markup *and its CSS* therefore ship
  from `app.js` — the popover injects its own stylesheet — or a client on a
  cached page gets the markup without the rules.
- **Live mode owns the clock.** It sets `simT` from the wall clock each frame
  rather than integrating `dt`, so the frame-gap clamp can't leave it behind and
  the transport controls are disabled rather than left to fight it. The zone is
  the schedule's, through `Intl`, so a DST switch needs no calendar of its own.
  The time-lapse only borrows that clock once, for the time it opens at.
- **Los Angeles' weekday is read on the same sample as its clock**, cached with
  the offset and re-measured a minute at a time. Live expires the sample itself
  when `simT` jumps backwards — that is midnight passing, and the day's trip
  list should turn over on the stroke. The time-lapse wraps past midnight every
  few minutes at speed and means nothing by it, so it waits for the resample.
- **The timetable is a week, not a day.** `trips` holds every working of the
  week once and `tripDays[i]` is a bitmask of the weekdays trip *i* runs on
  (bit 0 = Sunday); a day on screen is a filter over that one list, which is
  why the arrival times are built once at load. Rows identical in route,
  pattern and every time are merged, so a working that keeps to one timetable
  all week carries several day bits instead of costing several rows. Both modes
  play today's weekday, so `date` is now provenance — the day the artwork was
  fitted to — and not something the client reads. A trip crossing midnight is
  emitted again on the *next* day at −24 h, because the vehicles on screen at
  01:00 are the previous day's last workings.
- **Trips start at the origin's departure time**, not its arrival — some feeds
  time origins up to two hours early, and using arrival pools whole fleets into
  motionless clusters at their terminals.
- **Badges are read against the route slid onto them.** Which variant a badge
  anchors is settled after the warp's local error is taken off (`slide_for`):
  where the warp is out by about half the distance between two parallel drawn
  lines, a badge printed on one of them comes out nearest the variant running
  the other, by less than the slack `branch_anchors` allows. Over a badge cloud
  wider than `SLIDE_SPAN` one vector cannot stand for that error, so there is no
  slide and the badges are read where they lie.
- **The Downtown call-out has nothing drawn under it.** Shapes crossing it keep
  the warp; don't interpolate a snap correction across the panel.
- **The call-out has a transform of its own shape.** It redraws the rotated
  downtown grid square and spaces the streets to suit the page, which no
  polynomial in lon/lat fits: `georef_inset` calibrates one monotone table per
  grid axis instead, on the streets the sheet names, and `to_inset_px` reads
  them. Every named street therefore lands on its own ink, and the model says
  nothing about the corner by Union Station, where the panel gives the square
  grid up — that corner is extrapolated off the last breakpoint and is the one
  part of the panel the warp is still tens of px out in.
- **A diversion in the feed is not a snapping fault, and nothing catches it.**
  Where a route is sent round a closure for some of its workings, the shape
  really does leave the line the sheet draws — so `undetour` sees nothing (it
  compares the snap against the warp, and here they agree), `path_check` sees a
  smooth rectangle, and every general test tried came back too blunt to use:
  the panel's ink covers the street it diverts onto, its badges are further
  from a straight variant's own warp than from the diverted one, and a shape
  running off the frame is further from its own stops than a diversion is.
  `INSET_DIVERSIONS` is the answer, and finding the next one means looking.
- **The call-out does not anchor on badges.** Anchors pull a shape onto its
  street where the warp is out by more than the streets are apart, and down
  there it isn't: a chip is printed *beside* its line, so anchoring on one now
  drags the line off ink it was already sitting on. It cost three quarters of
  the panel's `path_check --inset` score to stop. If the warp there ever gets
  worse again this is the first thing to reconsider — and the reason to keep an
  eye on the panel's own residual, which `georef_inset` prints.
- **A corridor walk far longer than the shape is usually going round a hole.**
  The sheet knocks its own line out for a station marker or a chip, and
  `mask_path` bridges a hole only where the mask connects nothing at all — so
  where the block closes round it, the walk takes the block and `align_walk`
  pins the route to it. `trace_anchors` asks again with `over_holes=True` and
  keeps that answer only when it comes back the length the shape says, which is
  the test a corner-cutting bridge fails.
- **Stop distances ride on the stored arc.** `main_dist` places the stops on the
  warp and carries that parameterization onto the stored shape point for point,
  so whatever shortens the line compresses the stops with it. `unfold` is what
  usually does: where the sheet draws one line for a stretch the route drives
  twice — a one-way pair, a circulator's loop — every point along it offers an
  ordinary-looking fold, and flattening the lot leaves half the line. The stops
  then stack up on what is left and the vehicle stands at one of them for the
  leg it should have spent driving. `FOLD_KEEP` is the floor in `settle` that
  stops it, and `speed_check --slow` is what catches it when it happens. Keeping
  a retrace costs `path_check` — a true 180° fold scores far worse than the
  smooth wrong line it replaces — and that trade is the right way round.

## Adding a feed

Add the zip under `data/gtfs/`, then the key to `FEEDS` (rail first — Metro rail
seeds the snap config) and `FEED_NAMES`. Seed its drawn color in
`LEGEND_SEEDS`, or add it to `INK_SNAP` if its lines can't be separated from the
page by color; an agency the sheet draws no line for gets a sprite color in
`DRAWN_COLORS` instead and keeps the warp. Then run the checks above; a feed
that warps far off the sheet shows up immediately in `speed_check` as off-map.
