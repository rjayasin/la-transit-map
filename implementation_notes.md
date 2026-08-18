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

Two recurring reasons a pin doesn't work, worth recognising before reaching for
an override:

- **The pin speaks for the wrong stretch.** Anchors attach to the nearest point
  of the *warp*, so where the warp lays one leg of a route over where another is
  drawn, a point on the drawn line attaches to the wrong leg and anchors the
  middle of the route instead of the end.
- **The pin reads as a terminus.** `trim_terminus` cuts a shape back to a pin
  near its end (`TERMINUS_TAIL`), so a pin placed inside that tail removes the
  stretch it was meant to anchor.

Snapping strategy per agency is set by the `STREET_SNAP` / `INK_SNAP` /
`SYMBOL_FEEDS` sets and `DRAWN_COLORS`.

## Verifying a change

The build is deterministic — same inputs, byte-identical `schedule.json` — so
diff two runs to see exactly what a change did. Then:

```sh
.venv/bin/python scripts/drift_check.py    # how far each route is off its drawn line
.venv/bin/python scripts/path_check.py     # hairpins and kinks
.venv/bin/python scripts/speed_check.py    # implausible speeds
.venv/bin/python scripts/debug_line.py <n> # look at it
```

`trips` is the whole week, so the checks rank weekend-only workings alongside
the rest and their trip counts are a week's, not a day's.

**`drift_check` moves its own yardstick.** It refines the mask color off the
*stored* shapes, so a change that moves shapes onto their lines also changes the
color it measures against, and before/after totals from two different runs are
not comparable. Compare both shapes against one tree, refined the way the build
refines it — off the warps.

Two blind spots to know about, since a fix can look like a no-op:

- `drift_check` scores by color, so a route sitting on a *sibling* route's ink
  of the same color scores clean.
- `path_check` scores by straightness, so a smoothly cut corner scores zero.

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
- **The Downtown call-out has nothing drawn under it.** Shapes crossing it keep
  the warp; don't interpolate a snap correction across the panel.

## Adding a feed

Add the zip under `data/gtfs/`, then the key to `FEEDS` (rail first — Metro rail
seeds the snap config) and `FEED_NAMES`. Give it a drawn color in
`DRAWN_COLORS`, or add it to `INK_SNAP` if its lines can't be separated from the
page by color. Then run the checks above; a feed that warps far off the sheet
shows up immediately in `speed_check` as off-map.
