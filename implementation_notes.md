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
agency scores clean. Work from the drawing instead.

**Find the variant.** Every variant of a route normally comes from one warp
corridor, so measure each stored shape against the drawn line over a window
round the divergence — `mask_tree` on the agency's colour, or a polyline traced
off the artwork — and the odd one out is the fault. Comparing variants against
each other instead has two traps: stored shapes keep only the points
`simplify` left, so vertex-to-vertex distance reports the gap to the nearest
*corner* and can be several times the real one, and where a route passes
through an area twice the nearest point on the other variant can be on its
other leg, which hides the divergence entirely.

**Read length as well as distance.** A path cutting the corner off a tight loop
can sit a few px from the corridor at its worst and still be obviously wrong on
screen. How much arc the shape spends inside the window is the second measure,
and a shortcut shows up there as ground it never covers — which is also what
the stops ride on, so it is the half that reaches the animation.

**Then pick the table**, by the pin test above. Placing a pin: take its
coordinates off a variant that already sits on the line, or scan `mask_tree`
across the corridor for the centreline — the drawn line is a couple of px wide
and the warp can be tens of px off it, so a coordinate guessed from the warp
lands on the wrong street.

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
.venv/bin/python scripts/speed_check.py    # implausible speeds
.venv/bin/python scripts/speed_check.py --slow   # ... and vehicles held still
.venv/bin/python scripts/debug_line.py <n> # look at it
```

`trips` is the whole week, so the checks rank weekend-only workings alongside
the rest and their trip counts are a week's, not a day's.

Count the shapes that changed, too. A fix aimed at one route should touch that
route's shapes and no others, and a table entry that reaches further than it
was meant to shows up here before it shows up anywhere else.

**`drift_check` moves its own yardstick.** It refines the mask color off the
*stored* shapes, so a change that moves shapes onto their lines also changes the
color it measures against, and before/after totals from two different runs are
not comparable. Compare both shapes against one tree, refined the way the build
refines it — off the warps.

Three blind spots to know about, since a fix can look like a no-op:

- `drift_check` scores by color, so a route sitting on a *sibling* route's ink
  of the same color scores clean.
- `path_check` scores by straightness, so a smoothly cut corner scores zero.
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

- **Mask cache keys include function source.** `code_stamp` hashes
  `inspect.getsource` of the mask-building functions, so editing them — comments
  included — invalidates `scratch/mask-cache/` and forces one cold rebuild
  (~80 s against ~30 s). Harmless, but don't be surprised by it.
- **The colour masks read the PDF too.** A place name doesn't paint out the
  line it crosses; it washes it back, under a halo and under a page-coloured
  panel that `knockout_panels` reads off the PDF, and `unfade` puts what
  survives back into the mask. So the masks are keyed on the sheet as well as
  on `map.png`, and without pymupdf they lose the panels — a gap under a name
  is then a hole again, and the snap will take a parallel street instead.
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
