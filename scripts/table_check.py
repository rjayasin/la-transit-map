"""Check the hand-tuned tables in build_data.py against the feeds they name.

Lists every row that names a feed, route or shape the GTFS archives in
data/gtfs/ don't have, and every override spec with a field the build doesn't
read, and exits 1 if there are any. The build passes over such rows without a
word: a route key nothing matches is never looked up, and an override whose
shape_ids name no shape of its route changes nothing. A feed refresh that
renames route or shape ids is the usual cause.

Route keys are compared the way the build reads them, through route_key. Shape
ids the build makes for trips that have none ("stops-...") are not checked.
The archives are read directly, so nothing needs unzipping.
"""
import argparse
import csv
import io
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

import build_data as B
from build_cache import FEED_TABLES

# The per-feed cache tables are the ones keyed by (feed, route), bar the one
# keyed by (feed, shape id).
BY_ROUTE = sorted(FEED_TABLES - {"TRIP_SHAPE_IDS"})
BY_FEED = ("FEED_NAMES", "SHEET_ROUTES", "AGENCY_SYMBOLS", "LEGEND_INK", "LEGEND_SEEDS",
           "BADGE_FILLS", "DRAWN_COLORS", "GRID_PINS", "STROKE_MATCH", "SPUR_FITS",
           "STROKE_JOINS", "INK_SPRITES", "INK_SNAP", "STREET_SNAP", "SYMBOL_FEEDS",
           "ALIGN_SKIP", "SEAT_FEEDS")
# The fields apply_override and inset_override read.
SPEC_FIELDS = {
    "OVERRIDE_PATHS": {"box", "path", "shape_ids", "exact", "forward", "pass", "waypoints"},
    "INSET_OVERRIDE_PATHS": {"box", "path", "paths", "shape_ids", "pass"},
}


def archive_rows(feed, name, cols):
    """`cols` of every row of `name` in the feed's archive, as tuples."""
    with zipfile.ZipFile(f"{B.GTFS}/{feed}.zip") as z:
        member = next((m for m in z.namelist() if m.rsplit("/", 1)[-1] == name), None)
        if member is None:
            return []
        with z.open(member) as f:
            rows = csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            head = next(rows, [])
            idx = [head.index(c) if c in head else None for c in cols]
            return [tuple(r[i] if i is not None and i < len(r) else "" for i in idx)
                    for r in rows]


def load(T):
    """{feed: what its archive lets a table name}, for each feed in FEEDS that
    has an archive. A shape belongs to the route of the last trip in trips.txt
    that names it, which is how build_schedule assigns it, and only that
    route's overrides are applied to it."""
    gtfs = {}
    for feed in T["FEEDS"]:
        if not Path(f"{B.GTFS}/{feed}.zip").is_file():
            continue
        routes = {r for r, in archive_rows(feed, "routes.txt", ["route_id"])}
        trip_routes, trip_shapes, owner = set(), set(), {}
        for rid, sid, direction in archive_rows(feed, "trips.txt",
                                                ["route_id", "shape_id", "direction_id"]):
            trip_routes.add(rid)
            trip_shapes.add(sid)
            rid = T.get("TRIP_ROUTE_IDS", {}).get((feed, rid), rid)
            if feed == "metrolink":
                sid = T.get("METROLINK_SHAPES", {}).get((rid, direction), sid)
            if sid:
                owner[sid] = rid
        shapes = defaultdict(set)
        for sid, rid in owner.items():
            shapes[B.route_key(feed, rid)].add(sid)
        remapped = feed == "metrolink" or any(f == feed for f, _ in T.get("TRIP_SHAPE_IDS", {}))
        gtfs[feed] = {
            "routes": routes, "trip_routes": trip_routes, "trip_shapes": trip_shapes,
            "keys": {B.route_key(feed, r) for r in routes}, "shapes": shapes,
            "drawn": ({s for s, in archive_rows(feed, "shapes.txt", ["shape_id"])}
                      if remapped else set()),
        }
    return gtfs


def is_box(b):
    return (isinstance(b, (tuple, list)) and len(b) == 4
            and all(isinstance(v, (int, float)) for v in b) and b[0] < b[2] and b[1] < b[3])


def spec_problems(spec, fields, own, feed_shapes):
    """What is wrong with one override spec. `own` holds the shape ids of its
    route, `feed_shapes` those of the whole feed."""
    if not isinstance(spec, dict):
        return ["not a dict"]
    out = [f"unknown field {f!r}" for f in sorted(set(spec) - fields)]
    if not is_box(spec.get("box")):
        out.append(f"box {spec.get('box')!r} is not (x0, y0, x1, y1) with x0 < x1, y0 < y1")
    paths = spec["paths"] if "paths" in spec else [spec.get("path")]
    if any(not isinstance(p, (tuple, list)) or len(p) < 2 for p in paths):
        out.append("a path needs two points or more")
    if "pass" in spec and not isinstance(spec["pass"], int):
        out.append(f"pass {spec['pass']!r} is not an index")
    ids = spec.get("shape_ids", ())
    if isinstance(ids, str):
        out.append(f"shape_ids {ids!r} is a string, which `in` matches by substring")
        ids = ()
    for sid in ids:
        if not sid.startswith("stops-") and sid not in own:
            out.append(f"shape {sid!r} " + ("belongs to another route" if sid in feed_shapes
                                            else "is not in the feed"))
    return out


def check(T, gtfs):
    """One line per problem with the tables in `T` (build_data's globals, or a
    dict standing in for them) against `gtfs` (see load)."""
    out = []
    feeds = set(T["FEEDS"])
    for feed in T["FEEDS"]:
        if feed not in gtfs:
            out.append(f"FEEDS: no archive {B.GTFS}/{feed}.zip")
        if feed not in T.get("FEED_NAMES", {}):
            out.append(f"FEED_NAMES: no name for {feed!r}")
    for name in BY_FEED:
        out += [f"{name}: {f!r} is not in FEEDS" for f in T.get(name, ()) if f not in feeds]
    for group, members in T.get("FILTER_GROUPS", {}).items():
        out += [f"FILTER_GROUPS {group!r}: {f!r} is not in FEEDS" for f in members if f not in feeds]
    out += [f"SYMBOL_INK {k}: {k[0]!r} is not in FEEDS" for k in T.get("SYMBOL_INK", {})
            if k[0] not in feeds]

    for name in BY_ROUTE:
        for key, value in T.get(name, {}).items():
            feed, route = key
            where = f"{name} {key}"
            if feed not in gtfs:
                out.append(f"{where}: no feed {feed!r} in FEEDS with an archive")
            elif key != B.route_key(feed, route):
                out.append(f"{where}: never read, since the build looks it up as "
                           f"{B.route_key(feed, route)}")
            elif key not in gtfs[feed]["keys"]:
                out.append(f"{where}: {feed} has no route {route!r}")
            elif name in SPEC_FIELDS:
                every = set().union(*gtfs[feed]["shapes"].values())
                for i, spec in enumerate(value if isinstance(value, list) else [value]):
                    out += [f"{where} spec {i}: {p}" for p in spec_problems(
                        spec, SPEC_FIELDS[name], gtfs[feed]["shapes"][key], every)]
            elif name == "INSET_DIVERSIONS":
                out += [f"{where}: box {b!r} is not (x0, y0, x1, y1)"
                        for b in value if not is_box(b)]

    for feed, routes in T.get("SHEET_ROUTES", {}).items():
        if feed in gtfs:
            out += [f"SHEET_ROUTES {feed!r}: no route {r!r}" for r in sorted(routes)
                    if r not in gtfs[feed]["routes"]]
    for (feed, trip_route), route in T.get("TRIP_ROUTE_IDS", {}).items():
        if feed in gtfs:
            if trip_route not in gtfs[feed]["trip_routes"]:
                out.append(f"TRIP_ROUTE_IDS {(feed, trip_route)}: no trip names {trip_route!r}")
            if route not in gtfs[feed]["routes"]:
                out.append(f"TRIP_ROUTE_IDS {(feed, trip_route)}: no route {route!r}")
    for (feed, sid), to in T.get("TRIP_SHAPE_IDS", {}).items():
        if feed in gtfs:
            if sid not in gtfs[feed]["trip_shapes"]:
                out.append(f"TRIP_SHAPE_IDS {(feed, sid)}: no trip names {sid!r}")
            if to not in gtfs[feed]["drawn"]:
                out.append(f"TRIP_SHAPE_IDS {(feed, sid)}: no shape {to!r} in shapes.txt")
    if "metrolink" in gtfs:
        for (route, direction), sid in T.get("METROLINK_SHAPES", {}).items():
            if route not in gtfs["metrolink"]["routes"]:
                out.append(f"METROLINK_SHAPES {(route, direction)}: no route {route!r}")
            if sid not in gtfs["metrolink"]["drawn"]:
                out.append(f"METROLINK_SHAPES {(route, direction)}: no shape {sid!r} in shapes.txt")
    if "gtfs_rail" in gtfs:
        for name in ("RAIL_LOOPS", "METRO_RAIL_INK"):
            out += [f"{name}: gtfs_rail has no route {r!r}" for r in T.get(name, {})
                    if r not in gtfs["gtfs_rail"]["routes"]]
    return out


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__,
                            formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    T = vars(B)
    problems = check(T, load(T))
    for line in problems:
        print(line)
    if problems:
        sys.exit(f"table check failed: {len(problems)} problems")
    rows = sum(len(T[name]) for name in BY_ROUTE)
    ids = sum(len(s.get("shape_ids", ())) for name in SPEC_FIELDS for v in T[name].values()
              for s in (v if isinstance(v, list) else [v]))
    print(f"ok: {rows} route rows in {len(BY_ROUTE)} tables, {ids} shape ids, "
          f"{len(T['FEEDS'])} feeds")
