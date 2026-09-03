"""Score a route's shapes against one stretch of drawn line, traced off the
artwork rather than inferred from a colour.

`drift_check` cannot do this. Drift is measured against an agency's whole mask,
so a shape that has wandered onto a *sibling* route drawn in the same ink
scores clean, which is what a route reported off its line usually is. Here the
target is one corridor: `mask_path` walks the agency's drawn pixels between two
points known to be on it, and every shape is measured against that walk.

Give it two points on the drawn line: a badge the sheet prints, a drawn
terminus, a coordinate read off the tiles. It prints the walk, ready to paste
into a pin or an override, then per shape:

  off     distance from the shape to the corridor, over the window
  cover   how much of the corridor the shape comes within COVER px of

Both matter. A path that cuts the corner off a loop can sit a few px from the
corridor at its worst and still be obviously wrong. It shows up as ground the
shape never covers, which is also what the stops ride on.

A walk much longer than the straight distance between the two points means it
went round a block rather than along the line, because the drawing is
interrupted between them, usually by a chip printed over it. It says so when
that happens, and everything below that line is then scored against the detour
rather than the corridor, so pick a pair that doesn't straddle the interruption
and ask again. The interruption may itself be the fault you are chasing; see
implementation_notes.md.

    scripts/corridor_check.py bigbluebus 9 --from 697,2065.7 --to 775.3,2166.4
    scripts/corridor_check.py gtfs_bus 720 --from ... --to ... --schedule s.json
"""
import argparse
import json
import os
import sys

import numpy as np
from scipy.spatial import cKDTree

sys.path.insert(0, "scripts")
from build_data import (FEEDS, FEED_NAMES, INK_SNAP, LEGEND_INK, LEGEND_SEEDS,  # noqa: E402
                        densify, ink_tree, mask_tree, mask_path, read_cols,
                        read_csv, refine_color, stroke_color, to_px)

SCHEDULE = "schedule.json"
REACH = 150.0   # px a shape may pass from an end of the corridor and still be
                # said to run it; wider than any warp error, narrow enough that
                # a route which simply isn't there says so
COVER = 6.0     # px within which a shape counts as covering the corridor
STEP = 1.0      # px; sampling pitch for both curves


def agency_tree(feed):
    """What to walk the corridor on: the feed's strokes where the build snaps
    on them, and its colour mask otherwise, refined the way the build refines
    it. drift_check chooses between the two the same way and for the same
    reason: where an agency's line is thin, its mask can be more lettering than
    line, and a walk then follows the words across the block instead of the
    corridor, which is the one thing this tool must not do."""
    if feed in INK_SNAP and feed in LEGEND_INK:
        tree = ink_tree([LEGEND_INK[feed]])
        if tree is not None:
            return tree
    used = {row["shape_id"] for row in read_csv(feed, "trips.txt")}
    tmp = {}
    for sid, seq, lon, lat in read_cols(
            feed, "shapes.txt",
            ("shape_id", "shape_pt_sequence", "shape_pt_lon", "shape_pt_lat")):
        if sid in used:
            tmp.setdefault(sid, []).append((int(seq), float(lon), float(lat)))
    warped = []
    for p in tmp.values():
        p.sort()
        x, y = to_px(np.array([q[1] for q in p]), np.array([q[2] for q in p]))
        warped.append(list(zip(x, y)))
    seeds = LEGEND_SEEDS.get(feed)
    if not seeds:
        sys.exit(f"{feed} has no drawn colour ({', '.join(sorted(LEGEND_SEEDS))})")
    good = [c for c in (refine_color(warped, s) for s in seeds) if c]
    good = good or [c for c in [stroke_color(feed)] if c]
    if not good:
        sys.exit(f"{feed}: could not refine a drawn colour to mask on")
    return mask_tree(good, 30.0)


def route_shapes(sched, label, system):
    """Every stored shape of the routes that print as `label`, busiest first."""
    hits = {i for i, r in enumerate(sched["routes"])
            if r["n"].lower() == label.lower()
            and (not system or system.lower() in sched["systems"][r["sy"]].lower())}
    if not hits:
        sys.exit(f"no route labelled {label!r}"
                 + (f" in a system matching {system!r}" if system else ""))
    per = {}
    for t in sched["trips"]:
        if t[0] in hits:
            pat = sched["patterns"][t[1]]
            if pat:
                per[pat["s"]] = per.get(pat["s"], 0) + 1
    return sorted(per.items(), key=lambda kv: -kv[1])


def score(P, ref, pa, pb):
    """A shape against the corridor, over the stretch of itself that runs it.

    The window is the shape between its closest approaches to the two ends,
    not a box round the corridor: a box drawn round a diagonal takes in the
    rest of the route as well, and the excursion being looked for is then
    reported alongside legs that were never in question. A shape that has
    wandered still counts, because its two approaches bracket the wandering.
    A shape passing the area twice can bracket across both passes; read the
    walked-against-straight line above and the `n` column when it looks odd."""
    da = np.hypot(*(P - np.asarray(pa)).T)
    db = np.hypot(*(P - np.asarray(pb)).T)
    if min(da.min(), db.min()) > REACH:
        return None
    lo, hi = sorted((int(da.argmin()), int(db.argmin())))
    Q = P[lo:hi + 1]
    if len(Q) < 2:
        return None
    off = cKDTree(ref).query(Q)[0]
    reach = cKDTree(Q).query(ref)[0]
    return dict(n=len(Q), p50=float(np.median(off)), p90=float(np.quantile(off, 0.9)),
                mx=float(off.max()), cover=float((reach < COVER).mean()))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("feed", help=f"one of {', '.join(FEEDS)}")
    ap.add_argument("route", help="route label as the sheet prints it, e.g. 9")
    ap.add_argument("--from", dest="a", required=True, metavar="X,Y",
                    help="a point on the drawn line, in map px")
    ap.add_argument("--to", dest="b", required=True, metavar="X,Y",
                    help="another point on the same drawn line")
    ap.add_argument("--system",
                    help="substring of the system name (default: the feed's own, "
                         "so a designation several operators use stays this one's)")
    ap.add_argument("--schedule", default=SCHEDULE,
                    help=f"read shapes from this instead of {SCHEDULE}; "
                         "a build_data.py --only refit writes one")
    a = ap.parse_args()
    if a.feed not in FEEDS:
        sys.exit(f"no feed {a.feed!r}; one of {', '.join(FEEDS)}")
    if not os.path.exists(a.schedule):
        sys.exit(f"missing {a.schedule} (run from the repo root)")
    pa, pb = (tuple(float(v) for v in s.split(",")) for s in (a.a, a.b))
    # The feed picks the mask; without this it would not also pick the route,
    # and a designation half the county's operators use (2, 4, 7) would be
    # scored for all of them against one agency's corridor.
    system = a.system or FEED_NAMES.get(a.feed, "")

    tree = agency_tree(a.feed)
    walk, length = mask_path(pa, pb, tree)
    if walk is None:
        sys.exit(f"the {a.feed} mask does not connect {pa} to {pb}: "
                 "either a point is off the drawn line, or the drawing is broken "
                 "between them by more than a bridge will cross")
    straight = float(np.hypot(*(np.asarray(pb) - np.asarray(pa))))
    print(f"corridor: {len(walk)} steps, {length:.0f} px walked against "
          f"{straight:.0f} px straight"
          + ("   <-- went round something; check the two points"
             if length > 1.35 * straight else ""))
    print("  " + ", ".join(f"({x:.1f},{y:.1f})" for x, y in walk))

    ref = np.asarray(densify([tuple(p) for p in walk], STEP), dtype=float)
    with open(a.schedule) as f:
        sched = json.load(f)
    print("\nshape  trips     n    off p50    p90    max   cover")
    for si, ntrips in route_shapes(sched, a.route, system):
        flat = sched["shapes"][si]
        P = np.asarray(densify(list(zip(flat[0::2], flat[1::2])), STEP), dtype=float)
        s = score(P, ref, pa, pb)
        if s is None:
            print(f"{si:5d}  {ntrips:5d}     (does not run this corridor)")
        else:
            print(f"{si:5d}  {ntrips:5d}  {s['n']:4d}    {s['p50']:6.1f} {s['p90']:6.1f} "
                  f"{s['mx']:6.1f}    {s['cover']:.2f}")


if __name__ == "__main__":
    main()
