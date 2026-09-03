# Implementation notes

Working notes for anyone changing the build. The README covers what the project
does; this covers what will bite you.

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
| Cleanup | `despike`, `unfold`, `undetour`, `unjitter`, `trim_terminus` |
| Emit | `main`: builds `schedule.json` and prints a stats dict |

## When a line leaves its drawing

A snap follows whatever is in the tree it was handed, so a shape on the wrong
street is a fact about that tree, about the anchors, or about the sheet. Which
one it is decides the fix, so diagnose before reaching for a table.

1. **See both courses at once.** `debug_line.py <n> --system <name> --no-stops`
   puts the stored path over the artwork. Crop `tiles/<level>/<col>_<row>.webp`
   (512 px tiles, level = scale over the 4096 px base) to see the drawing on
   its own, since a path drawn over its line hides the line. What the sheet
   draws is the target, not what the street grid suggests.
2. **Ask the feed where the route goes.** The stop names in order
   (`stop_times.txt` joined to `stops.txt`) name the streets, and `to_px` puts
   them in map px. That separates the drawn corridor from a corridor running
   parallel to it, before any pixel work starts.
3. **Score against the strokes, not the mask.** `pdf_ink([LEGEND_INK[feed]])`
   is the drawing itself: complete under every label, with no chips and no
   lettering in it. The distance from the stored shape to those points is the
   deviation with nothing inferred, and it is the one measure a mask change
   cannot move. `drift_check --ink` does this for the agencies the PDF can
   settle.
4. **Ask whether the mask holds the line at all.** Query the agency's
   `mask_tree` with the ink points along the stretch. A run of ink several px
   from any mask pixel is a hole in the mask, and a snap crossing a hole leaves
   for whichever parallel corridor is unbroken. `ink_gap_check.py` asks a
   cruder version of the same question against the grey street art.

What the answer means:

- **A hole under a place name.** The artwork there is washed back, not painted
  out, and `unfade` is what puts it back. Its gates are measurable one pixel at
  a time (the fade fraction along page→color, the fit to this agency's blend,
  the fit to the best rival's), so print them for the pixels sitting on the
  ink. The gate that rejects them names the fix, and that fix applies to every
  line under every name rather than to one route.
- **A hole with nothing printed over it.** A stretch one route runs alone is
  drawn thin, and a thin line at 4096 px is a blend with what it is drawn on.
  `unfade` cannot reach it, since there is no label over it to recover it
  under, and the colour tests reject the blend outright where a background fill
  explains it better. The agency belongs in `INK_SNAP`, where the PDF strokes
  the thin lines the same as the thick ones.
- **The mask holds the line and the path still leaves it.** That is an anchor
  question, not a mask one: nothing tells the snap which end of the drawing
  this leg belongs to. See `PINNED_ANCHORS` and the pin failures below.
- **The mask holds something that is not the line** (lettering, a neighbour's
  casing) and the shape walks to it. That is a color question: `LEGEND_SEEDS`,
  `BADGE_FILLS`, or the agency belongs in `INK_SNAP` and should snap on strokes
  instead.
- **The sheet draws nothing under the stretch.** The warp is then the best
  available and the shape is already right. `drift_check` counts this
  separately as `beyond`; a measure that treats a sibling's ink as near will
  call it drift anyway, so check the artwork before believing the number.

Prefer the general fix to the particular one, since a mechanism that mis-reads
the sheet mis-reads it everywhere. But price it first. A change inside the mask
machinery moves every color-masked feed at once, so diff the two builds' shapes
per system, score both against the strokes, and look at the largest movers by
eye. A total that improves can still hide a route that got worse.

## Rail lines have none of the tables

Metro rail goes through `snap_rail`, not the machinery above. There are no
badges, so no anchors and no `PINNED_ANCHORS`. `OVERRIDE_PATHS` cannot reach it
either: an override needs the snapped and warped shapes index-aligned, and
`snap_rail` resamples. That leaves the mask and the pass ladder, so a rail line
off its drawing is one of three things.

- **The mask holds something that is not the ribbon.** `drawn_blobs` drops
  blobs too small to be drawn. This matters more than on a colour mask: the
  rail warp sits tens of px off the track, so a speck between the two is
  nearer than the line for a whole run of points and the smoothing cannot
  outvote it.
- **The line's own other limb claims it.** Where the warp's corner and the
  drawn one are a block apart, the limb the shape is not on is nearer all the
  way up to the turn, and the corner comes out as a chord. `rail_dir_tree`
  separates them by heading.
- **A corner is rounded wider than the sheet draws it.** The displacement is
  smoothed along the line, so the corner's radius is the smoothing window's.
  The last and tightest pass of the ladder is what fixes that.

## Hand-tuned tables

Everything the artwork can't settle on its own is named in a table near the code
that reads it. When a route is on the wrong line, one of these is usually the
fix. Try them in this order, least invasive first.

| Table | Use it when |
|---|---|
| `MAP_LABELS` | The sheet badges a route differently from the feed's `route_short_name` (or the name is prose, as for a DASH). Also fixes `orphan_check` rows |
| `SPLIT_LABELS` | The feed pairs two designations under one route (`235/236`) and the sheet draws them as two lines rather than one line renamed along its length |
| `SYMBOL_OWNERS` | The sheet symbolises by *operator*, so a shared symbol must be assigned to a specific route by hand |
| `BADGE_FILLS` | An agency's badge chips are its saturated legend ink, far enough from its washed line color that the color gate rejects its own badges |
| `LEGEND_SEEDS` | A refined line color has drifted somewhere the artwork isn't; name the stroke the legend actually uses |
| `PINNED_ANCHORS` | The sheet prints no badge over a stretch that needs one. A point on the drawn line then acts as a badge |
| `TRIM_TERMINI` | A pin can't both anchor and trim; give the terminus in *warp* px for the trim alone |
| `OVERRIDE_PATHS` | Nothing above can reach it. A corridor drawn by hand, spliced into the snapped shape. Last resort |
| `INSET_DIVERSIONS` | The feed routes some workings off the line the sheet draws, and only the call-out is magnified enough to show it. A box in inset px; the run inside it is flattened onto its chord |

A DASH is a special case of the first row. It is named rather than numbered, so
the designation its name yields is one the sheet never prints, and
`ladot_livery` has nothing else to anchor it on. Without a `MAP_LABELS` row a
DASH has no anchors at all, and its shape is whatever the warp and an
unanchored snap make of it. `orphan_check` lists them, but only the ones whose
guessed designation the sheet prints nowhere. It asks whether the token appears
on the sheet at all, not whether it appears near this route, so an initialism
landing on a code the cartographer used for something else half the map away
reads as correctly labelled and never appears in the report. To find those,
measure each route's label against the distance to the nearest place the sheet
prints it.

Three recurring reasons a pin doesn't work, worth recognising before reaching
for an override:

- **The pin speaks for the wrong stretch.** Anchors attach to the nearest point
  of the *warp*, so where the warp lays one leg of a route over where another
  is drawn, a point on the drawn line attaches to the wrong leg and anchors the
  middle of the route instead of the end.
- **The warp never goes there.** A pin can only hold a stretch the warp passes
  through. Where the sheet straightens out a jog the route really makes, such
  as round a block or into a terminal, there is no warp on the drawn corridor
  to attach to, and no pin placed on it can put the line there. Only an
  override can.
- **The pin reads as a terminus.** `trim_terminus` cuts a shape back to a pin
  near its end (`TERMINUS_TAIL`), so a pin inside that tail removes the stretch
  it was meant to anchor. A limb shorter than that tail cannot be pinned
  without losing the terminus beyond it: pinning the end and pinning the limb
  are the same choice, and only an override does both. A shape that finishes
  where it started is exempt, since a circuit has no overshoot, which is what
  leaves a small circulator pinnable all the way round.

A route the sheet draws as a trunk rather than as the loop the feed drives is
not a case for an override either. Only the drawn stretch can be held to the
drawing; the rest has no ink under it and keeps the warp, and one pin on the
trunk is what stops the loop being walked onto a neighbouring route's line. A
loop lying wholly *past* the drawn terminus is the exception. It is the layover
the sheet omits, whatever stops it carries, and if it runs longer than
`TERMINUS_TAIL` no trim reaches it. Fold it onto the drawn stub with an
override so the route ends where the sheet ends it.

All three come down to one measurement: the distance from the intended pin to
the nearest warp point, and to the nearest point on any *other* leg of the same
warp. A few px with the runner-up far behind, and the pin holds. Tens of px, or
a runner-up as close as the winner, and it attaches somewhere else. The build
reports nothing when this happens; the shape just comes back wrong in a new
place. Check it before rebuilding rather than after.

Snapping strategy per agency is set by the `STREET_SNAP` / `INK_SNAP` /
`SYMBOL_FEEDS` sets and `DRAWN_COLORS`.

A route's drawn line is not one stroke. The PDF splits it at corners and where
another line crosses, and the pieces stop a few px short of each other, so
chaining strokes end to end reads the line far shorter than it is. A loop can
come back as the bare L of its trunk. Instead, collect every stroke of the
agency's colour whose bounding box meets the area and draw each in its own
colour over the tiles. The picture then shows which pieces are this route's,
and that is the measure to fit against.

## Chasing a divergent path

A line reported off its drawn ink is usually one *variant* of a route rather
than the route. The checks are the wrong place to start: `drift_check` scores
by colour, so a route that has wandered onto a sibling of the same agency
scores clean. Work from the drawing instead, in this order: trace the corridor,
stand up a harness, then search for the table entry. The first two make the
third cheap, and skipping them makes this slow.

**Trace the corridor first, and trust nothing else as ground truth.** Take two
points known to be on the drawn line, such as a badge `route_anchors` returned
or a drawn terminus, and `mask_path(a, b, tree)` walks the centreline between
them. A walked length close to the straight distance means it stayed on one
corridor rather than going round a block. That polyline is the measure for
everything that follows: which variant is wrong, and whether a candidate fix
helped. A variant that looks right is not a substitute, since sitting on the
agency's ink is not the same as sitting on this route's line, and a sibling's
corridor a block over scores clean against every mask measure there is.

`corridor_check.py` is that walk and that scoring in one command:

```sh
.venv/bin/python scripts/corridor_check.py bigbluebus 9 --from 697,2065.7 --to 775.3,2166.4
```

It prints the traced corridor, ready to paste into a pin or an override, then
each shape's distance to it and how much of it the shape covers, over the
stretch of the shape that runs it. It takes `--schedule`, so a `--only` refit
can be scored without a build. Read the walked-against-straight line first: if
the walk went round something, everything below it is scored against the
detour.

**Find the variant against that trace**, not against the other variants.
Comparing variants with each other has two traps. Stored shapes keep only the
points `simplify` left, so vertex-to-vertex distance reports the gap to the
nearest corner, which can be several times the real one. And where a route
passes through an area twice, the nearest point on the other variant can be on
its other leg, which hides the divergence entirely. `debug_line.py` draws the
variants in listed order, so the last one drawn hides the ones under it. Read
the printed legend, or pass `--shape`, before believing a colour.

**Read length as well as distance.** A path cutting the corner off a tight loop
can sit a few px from the corridor at its worst and still be obviously wrong on
screen. The second measure is how much arc the shape spends inside the window;
a shortcut shows up there as ground it never covers. That is also what the
stops ride on, so it is the half that reaches the animation. Scoring a
candidate as (distance to the trace, fraction of the trace covered) catches
both at once.

**Refit the one route rather than rebuilding.** A full build is around two
minutes, and almost all of it is shapes the change under test cannot reach.
`build_data.py --only <feed>[:<route>]` fits that route alone and writes a
`schedule.json`-shaped stub that `debug_line.py --schedule` draws:

```sh
.venv/bin/python scripts/build_data.py --only bigbluebus:9
.venv/bin/python scripts/debug_line.py 9 --schedule scratch/refit_bigbluebus.json --no-stops
```

Two seconds for a route, five for a whole agency, against 72 for the build. The
geometry is identical, since this is the build's own code with the fit loop
narrowed, so testing a pin position is a loop rather than a rebuild. It does
not give you `schedule.json`: it emits no timetable and no call-out runs, so
`drift_check`, `path_check` and `speed_check` still need a full build before
you commit.

The route token matches a route id, an id without its variant suffix, or the
designation the sheet prints, so `--only ladot:437` and `--only bigbluebus:9`
both work without looking an opaque feed id up.

**Three signatures worth recognising**, each of which names its own fix:

- *A foreign agency's badge chip printed over this agency's line.* It knocks a
  gap in the mask that no bridge will cross, because the block closes round it
  and `mask_path` prefers the drawn way round. `align_walk` then believes the
  block and the anchors land on it. The sign is a walk much longer than the
  straight distance between the two badges, with a run of mask distances of
  5-10 px under a chip. A pin inside the gap splits the badge-to-badge walk so
  that the leg holding the hole is shorter than `TRACE_SPAN[0]`. Below that no
  walk is attempted, and the straight interpolation, which is right here,
  stands.
- *One direction of a route right and the other wrong on the same badges.* The
  last badge before the divergence attaches to the wrong point of the warp.
  Near a corner, the distance to a badge tens of px off the corridor has a flat
  minimum, the two directions pick different points in it, and the
  displacements come out opposite. Beyond that last anchor `np.interp` holds
  the correction flat, so the whole unanchored tail inherits one pointing the
  wrong way. The sign is two adjacent badges whose fitted displacements
  disagree by more than a street. The fix is a pin in the tail.
- *A schematic detour bracketed by two badges closer together than
  `TRACE_SPAN[0]`.* No walk is attempted over a span that short, so the
  displacement between the badges is interpolated straight and the shape cuts
  across whatever the sheet draws between them. This looks like a missing
  anchor but is not one. Adding a pin only shortens the span further, and where
  the warp runs a corridor's width off the drawing, every point of the drawn
  detour is nearest the same warp point, so the pins all speak for one stretch
  and fight each other. Use an override.

**Placing a pin is a search with two constraints, both answerable before any
rebuild.** It has to be on the drawn line, so take the coordinates off the
trace and never off the warp, which can be tens of px away. And it must not
read as a terminus. Walk candidate positions along the trace and print, for
every shape of the route, the distance to the nearest warp point and the arc
from each end. A pin within `TERMINUS_REACH` of the warp and within
`TERMINUS_TAIL` of an end cuts the shape back to itself, so pick from the rows
that trim nothing. The deliberate exception is a pin at a drawn terminus, where
the trim is what you want. Where a leg is short enough that no other position
on it is trim-free, that pin plus one further up the corridor is usually the
whole fix.

**Placing an override.** `box` is matched against the warp, and the replaced
run goes from the first to the last warp point inside it. An excursion that
leaves the box in between is harmless, but a second pass through the box later
in the route swallows everything between the two. List the in-box index runs
for every shape of the route before trusting a box. One `path` serves both
directions, since the orientation comes from the direction of travel rather
than from which end the shape enters by. Trace the corridor off the artwork and
draw the trace back over the tiles before wiring it in; a hook or a dip the eye
skips over is obvious once drawn.

**Where the box ends matters as much as the path.** The box picks its run off
the warp, but each end of the hand-drawn path has to meet a point the snap
placed, and the snap moves points along the line as well as across it. Put an
edge where it does that (at a corner, or anywhere the fit is already wandering)
and the neighbour outside the box can sit past the end of the path, leaving a
reversal where the two meet. Only `path_check` measures this, and it will
report a cusp at the box edge instead of at the thing you set out to fix. Edges
belong in long straight stretches, where the snap can only move the line
sideways. The fix for a cusp at an edge is to move the edge out, not to redraw
the path.

## Verifying a change

The build is deterministic: same inputs, byte-identical `schedule.json`. Diff
two runs to see exactly what a change did. Then:

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
route's shapes and no others, and a table entry that reaches further than
intended shows up here first. Diff against a rebuild of the *unchanged* tree
rather than against the committed `schedule.json`: the build is deterministic
for one set of libraries, not across them, and a numpy or scipy upgrade moves a
hundred shapes on its own. Two builds, one at HEAD and one with the change,
cost four minutes and are the only way to read that count.

**`drift_check` moves its own measure.** It refines the mask color off the
*stored* shapes, so a change that moves shapes onto their lines also changes
the color it measures against, and totals from two different runs are not
comparable. Compare both sets of shapes against one tree, refined the way the
build refines it, off the warps.

Moving an agency into `INK_SNAP` moves the measure too. `--ink` prints a row
only for the agencies the strokes can settle, so the run before the change has
no row for the agency at all. Score both schedules with the changed checker, by
swapping `schedule.json` between two runs of it, or the comparison is against
nothing.

Four blind spots to know about, since a fix can look like a no-op:

- Nothing scores a line for wobble. A shape that sews a couple of px either
  side of its corridor is inside its own line's width, so `drift_check` reads
  it as on the drawing and `path_check` as never turning past a corner, while
  it is still an obvious zigzag at map zoom. Total turning per px of arc is what
  shows it, and `unjitter` is what removes it.
- `drift_check` scores by color, so a route sitting on a sibling route's ink of
  the same color scores clean.
- `path_check` scores by straightness, so a smoothly cut corner scores zero,
  and putting a shape back onto a corner it was cutting scores worse. An
  out-and-back whose legs both sit on one drawn line is a true fold where the
  cut version was a shallow cusp, and a corner turned where the sheet turns it
  is a kink where the chord across it was none. Read the `sharp` column against
  the kink count before calling a rank increase a regression, and read both
  against turning measured on the stretches the drawing runs straight, which is
  the only wobble number a corner cannot move. `path_check` also reads the main
  map alone unless given `--inset`: the call-out is a second, separately snapped
  drawing of the same routes, and its runs are stored apart from the shapes.
- `speed_check --slow` scores by a speed the sheet is not always entitled to.
  Three things hold a vehicle still without anything being wrong: a shape
  trimmed to its drawn terminus, which parks every stop on the omitted layover
  tail on one point; downtown compression, where the sheet barely moves and the
  call-out does; and a circulator scheduled at a walking pace. The row's `pile`,
  `panel` and `held` columns distinguish those from a stall. Read `held` in px:
  stop spacing is ~13 px and stored rounded, so a single px is quantization.

A change can legitimately make `path_check` rank a route worse. An exact
retrace is a true 180° fold where a wrong-but-smooth path was a shallow cusp.
Check what the shape does before treating a rank increase as a regression. The
same holds for a one-way pair the sheet draws as one line: straightening the
two legs onto it turns a pair that scored nothing into a fold that scores
everything, without either leg moving a street.

## Gotchas

- **The panel's Metro masks come off the pyramid.** `INSET_ORANGE` is the ink
  itself. `ORANGE` is that colour after map.png's reduction has blended it with
  the page, 30 away. The pyramid also lets `inset_tile_tree` cut the badge chips
  out, which matters more in the panel than anywhere else: a chip is a solid
  disc of the line's own colour a few tens of px off the line, and on the raster
  it is fused to it.
- **A colour mask can be mostly lettering.** Where an agency's line is thin and
  its colour muted, map.png's reduction blends the line toward the page while
  the place names set in the same grey survive intact. Big Blue Bus through
  Pacific Palisades read 800 mask px in a frame where the pyramid finds 3400,
  and nearly all of the 800 were type. A shape fitted on that is fitted to the
  labels, and every check reads clean: `drift_check` scores it against the same
  mask, and cutting a corner smoothly costs `path_check` nothing. Use
  `LEGEND_INK` and `INK_SNAP` where the PDF carries the agency's strokes, and
  `ink_gap_check.py` to see the holes.
- **Mask cache keys include function source.** `code_stamp` hashes
  `inspect.getsource` of the mask-building functions, so editing them, comments
  included, invalidates `scratch/mask-cache/` and forces one cold rebuild
  (~80 s against ~30 s). Harmless, but expect it.
- **`--only` reads a cached shape set; a full build never does.** Which shapes a
  feed runs is settled by its timetable, and the colour the feed is masked on is
  refined off the first twenty of them, so a refit that guessed the set from
  `trips.txt` could mask on a different colour and answer a question the build
  never asked. `scratch/shape-cache/` holds the set, stamped with the size and
  mtime of the files it came from. A full build writes it and reads nothing, and
  a refit whose stamp doesn't match reads the stop times as usual. The busways
  are the exception: they anchor on the station names printed beside them, which
  come out of the timetable, so `--only gtfs_bus` and `--only gtfs_bus:901`
  parse it whatever the cache says.
- **The colour masks read the PDF too.** A place name doesn't paint out the line
  it crosses. It washes the line back, under a halo and under a page-coloured
  panel that `knockout_panels` reads off the PDF, and `unfade` puts what
  survives back into the mask. The masks are therefore keyed on the sheet as
  well as on `map.png`. Without pymupdf they lose the panels, a gap under a name
  becomes a hole again, and the snap takes a parallel street instead.
- **A build without pymupdf poisons the cache.** The badges, the strokes and the
  knockout panels all come from the PDF, and each falls back to nothing rather
  than failing. The empty stroke set is written to `scratch/mask-cache/` under a
  key that says nothing about which libraries were installed, so the next build
  reads it back and comes out wrong with no warning. Install pymupdf before
  building, and clear the cache directory if a run printed either "unavailable"
  line.
- **Module docstrings are user-facing.** Several scripts pass `__doc__` as their
  argparse `description`. Trimming one changes `--help`.
- **`index.html` must stay byte-stable across deploys.** It is the one URL that
  can't carry a version, so a cached copy has to be identical to a fresh one.
  Put changes in `app.js`, which is fetched content-addressed as `?v=<sha>`.
  `scripts/stamp_build.mjs` rewrites the `__BUILD__` / `__V_*__` placeholders at
  deploy time; don't hand-edit them. New UI markup and its CSS therefore ship
  from `app.js`, with the popover injecting its own stylesheet. Otherwise a
  client on a cached page gets the markup without the rules.
- **Live mode owns the clock.** It sets `simT` from the wall clock each frame
  rather than integrating `dt`, so the frame-gap clamp can't leave it behind,
  and the transport controls are disabled rather than left to fight it. The zone
  is the schedule's, through `Intl`, so a DST switch needs no calendar of its
  own. The time-lapse borrows that clock once, for the time it opens at.
- **Los Angeles' weekday is read on the same sample as its clock**, cached with
  the offset and re-measured a minute at a time. Live expires the sample when
  `simT` jumps backwards, since that is midnight passing and the day's trip list
  should turn over then. The time-lapse wraps past midnight every few minutes at
  speed and means nothing by it, so it waits for the resample.
- **The timetable is a week, not a day.** `trips` holds every working of the
  week once, and `tripDays[i]` is a bitmask of the weekdays trip *i* runs on
  (bit 0 = Sunday). A day on screen is a filter over that one list, which is why
  the arrival times are built once at load. Rows identical in route, pattern and
  every time are merged, so a working that keeps to one timetable all week
  carries several day bits instead of costing several rows. Both modes play
  today's weekday, so `date` is provenance, the day the artwork was fitted to,
  and not something the client reads. A trip crossing midnight is emitted again
  on the next day at −24 h, because the vehicles on screen at 01:00 are the
  previous day's last workings.
- **Trips start at the origin's departure time**, not its arrival. Some feeds
  time origins up to two hours early, and using arrival pools whole fleets into
  motionless clusters at their terminals.
- **A badge the shape cannot reach still has a vote on the slide.** Every badge
  past `ANCHOR_GATE` scores the gate, in the slide's baseline and in every
  candidate offset alike, so it says nothing about which offset is best while
  adding the same constant to both sides of the gain test. One badge far enough
  away holds that test above its threshold on its own, and no slide is then
  believed however squarely it puts the rest on their lines. That is why
  `anchor_slide` scores only the badges an offset could bring within reach. This
  bites on a route sharing its designation with a word printed on the far side
  of the sheet.
- **Badges are read against the route slid onto them.** Which variant a badge
  anchors is settled after the warp's local error is taken off (`slide_for`).
  Where the warp is out by about half the distance between two parallel drawn
  lines, a badge printed on one comes out nearest the variant running the other,
  by less than the slack `branch_anchors` allows. Over a badge cloud wider than
  `SLIDE_SPAN` one vector cannot stand for that error, so there is no slide and
  the badges are read where they lie.
- **The Downtown call-out has nothing drawn under it.** Shapes crossing it keep
  the warp. Don't interpolate a snap correction across the panel.
- **The call-out has a transform of its own shape.** It redraws the rotated
  downtown grid square and spaces the streets to suit the page, which no
  polynomial in lon/lat fits. `georef_inset` calibrates one monotone table per
  grid axis instead, on the streets the sheet names, and `to_inset_px` reads
  them. Every named street lands on its own ink. The model says nothing about
  the corner by Union Station, where the panel gives the square grid up; that
  corner is extrapolated off the last breakpoint and is the one part of the
  panel the warp is still tens of px out in.
- **A diversion in the feed is not a snapping fault, and nothing catches it.**
  Where a route is sent round a closure for some of its workings, the shape
  really does leave the line the sheet draws. `undetour` sees nothing, since it
  compares the snap against the warp and here they agree; `path_check` sees a
  smooth rectangle; and every general test tried was too blunt to use. The
  panel's ink covers the street it diverts onto, its badges are further from a
  straight variant's own warp than from the diverted one, and a shape running
  off the frame is further from its own stops than a diversion is.
  `INSET_DIVERSIONS` is the answer, and finding the next one means looking.
- **The call-out does not anchor on badges.** Anchors pull a shape onto its
  street where the warp is out by more than the streets are apart, and in the
  panel it isn't. A chip is printed beside its line, so anchoring on one drags
  the line off ink it was already sitting on. Removing that cost three quarters
  of the panel's `path_check --inset` score. If the warp there gets worse again
  this is the first thing to reconsider, which is a reason to watch the panel's
  own residual, printed by `georef_inset`.
- **A corridor walk far longer than the shape is usually going round a hole.**
  The sheet knocks its own line out for a station marker or a chip, and
  `mask_path` bridges a hole only where the mask connects nothing at all. Where
  the block closes round the hole, the walk takes the block and `align_walk`
  pins the route to it. `trace_anchors` asks again with `over_holes=True` and
  keeps that answer only when it comes back the length the shape says, which is
  the test a corner-cutting bridge fails.
- **A window counted in points is a different window on every feed.** `densify`
  puts a ceiling on the step between points and nothing under it, so a feed
  drawing its shapes finely keeps its own spacing: under a px for some feeds,
  three for others, against the 4 the constants read as. Anything meant as a
  length of line wants `span_points`, not a count.
- **A smoothing window is longer than a schematic's own corners.** `unjitter`
  averages over more line than the wander it removes, and an average pulls every
  bend inside the window toward the inside of the turn, so a corner the sheet
  draws rounded ships cut across. The average is re-seated on the drawing
  afterwards: each point's offset from the ink, smoothed over `SEAT_SPAN`, taken
  across the line only, since the along-line half of that offset carries the
  ink's own sampling back in as wobble. Smoothing the correction halves it, so
  it is applied `SEAT_PASSES` times; one pass only meets a jog drawn in a couple
  of blocks halfway. `JITTER_KEEP` charges for whatever is left. Tightening
  `JITTER_KEEP` alone is the trade the re-seating exists to avoid: read tight
  enough to bite on ordinary wander, it costs more turning than it saves drift.
- **Smoothing a shape before the cleanup ballot moves routes off their line.**
  `settle` picks between fits by how sharply each turns, so a smoother line can
  hand the ballot to a different candidate: one that flattens a real jog, or a
  refit that took another corridor. The sign is movements of tens of px on
  routes nowhere near the change. `unjitter` runs after the ballot for this
  reason, and anything else that touches the geometry should too.
- **Stop distances ride on the stored arc.** `main_dist` places the stops on the
  warp and carries that parameterization onto the stored shape point for point,
  so whatever shortens the line compresses the stops with it. `unfold` usually
  does. Where the sheet draws one line for a stretch the route drives twice, as
  with a one-way pair or a circulator's loop, every point along it offers an
  ordinary-looking fold, and flattening all of them leaves half the line. The
  stops stack up on what is left and the vehicle stands at one of them for the
  leg it should have spent driving. `FOLD_KEEP` is the floor in `settle` that
  prevents it, and `speed_check --slow` catches it when it happens. Keeping a
  retrace costs `path_check`, since a true 180° fold scores far worse than the
  smooth wrong line it replaces, and that trade is the right way round.

## Adding a feed

Add the zip under `data/gtfs/`, then the key to `FEEDS` (rail first, since Metro
rail seeds the snap config) and `FEED_NAMES`. Seed its drawn color in
`LEGEND_SEEDS`, or add it to `INK_SNAP` if its lines can't be separated from the
page by color. An agency the sheet draws no line for gets a sprite color in
`DRAWN_COLORS` instead and keeps the warp. Then run the checks above. A feed
that warps far off the sheet shows up immediately in `speed_check` as off-map.
