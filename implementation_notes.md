# Implementation notes

Working notes for anyone (human or agent) changing the build. The README covers
what the project does; this covers what will bite you.

## Layout

`scripts/build_data.py` is the whole pipeline and is large. Rough order:

| Region | What lives there |
|---|---|
| Feeds & calendars | `FEEDS`, `FEED_NAMES`, `pick_date`, `parse_time` |
| Masks | `mask_tree`, `tile_tree`, `mask_pixels`, `unfade`, `cached_pixels` |
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
- **Module docstrings are user-facing.** Several scripts pass `__doc__` as their
  argparse `description`. Trimming one changes `--help`.
- **`index.html` must stay byte-stable across deploys.** It is the one URL that
  can't carry a version, so a cached copy has to be identical to a fresh one.
  Put changes in `app.js`, which is fetched content-addressed as `?v=<sha>`.
  `scripts/stamp_build.mjs` rewrites the `__BUILD__` / `__V_*__` placeholders at
  deploy time; don't hand-edit them. New UI markup *and its CSS* therefore ship
  from `app.js` — the popover injects its own stylesheet — or a client on a
  cached page gets the markup without the rules.
- **Live mode owns the clock, and the day.** It sets `simT` from the wall clock
  each frame rather than integrating `dt`, so the frame-gap clamp can't leave it
  behind and the transport controls are disabled rather than left to fight it.
  The zone is the schedule's, through `Intl`, so a DST switch needs no calendar
  of its own. The weekday comes from the same reading and is cached with the
  offset; a page left open past Los Angeles midnight swaps to the next day's
  trip list on the stroke.
- **The timetable is a week, not a day.** `trips` holds every working of the
  week once and `tripDays[i]` is a bitmask of the weekdays trip *i* runs on
  (bit 0 = Sunday); a day on screen is a filter over that one list, which is
  why the arrival times are built once at load. Rows identical in route,
  pattern and every time are merged, so a working that keeps to one timetable
  all week carries several day bits instead of costing several rows. Live plays
  today's day; the time-lapse plays the weekday of `date`, whatever day it is
  opened on. A trip crossing midnight is emitted again on the *next* day at
  −24 h, because the vehicles on screen at 01:00 are the previous day's last
  workings.
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
