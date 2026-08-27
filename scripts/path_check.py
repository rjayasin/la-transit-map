"""Rank every route by how far its drawn path departs from the shape a real
transit line has: long straight runs joined by smooth curves.

A bus route on this map is meant to read as straight segments with the
occasional rounded corner. The snapper can leave three kinds of scar when it
puts a shape on the wrong ink and jumps back:

  - a *cusp* — the path turns hard one way and immediately hard back, a hairpin
    a road never makes. This is the loudest signal that something is wrong.
  - a *zigzag* — a run of sharp turns alternating in direction, the path sewing
    between two parallel streets.
  - general *roughness* — many small kinks where the artwork is smooth.

The score measures turning over a short physical window (so it is blind to how
finely a shape happens to be sampled) and counts only turns sharper than a
real street corner. A right-angle turn from one avenue onto another is normal
and scores nothing; only the hairpins and the sewing do.

Points inside the Downtown call-out are dropped before scoring: the panel
ghosts every route for ~200 px and the warp is all there is there, so a zigzag
inside it is expected and not the snapper's fault.

The Downtown call-out is a second drawing of the same routes, snapped on its
own artwork, and --inset scores that instead: the panel magnifies downtown
several times over, so a stretch that reads as a wobble on the main map is a
line crossing the blocks there. It is the same measure and the same blind spot
— a path smoothly on the wrong street scores nothing either way.

    scripts/path_check.py                 # rank every route, worst first
    scripts/path_check.py --top 40        # only the 40 most suspect
    scripts/path_check.py --system Metro  # one system
    scripts/path_check.py --inset         # the Downtown call-out's own paths
    scripts/path_check.py 690             # detail for one route: every kink

The `worst` column is a map-pixel location; feed it to debug_line.py (or crop
the tiles there) to see the kink. `cusp` is the sharpest single reversal on the
route, in degrees past a right angle; `kinks` is how many sharp turns it has.
"""
import argparse
import json
import math
import os
import sys

import numpy as np

SCHEDULE = "schedule.json"

STEP = 2.0        # px between resampled points; metrics are measured on this grid
WIN = 12.0        # px on each side of a point — the window a turn is measured over
CORNER = 92.0     # deg; a turn up to a square street corner is normal, scores 0
ZIG = 45.0        # deg; two consecutive turns this sharp in opposite directions
                  # are a zigzag, whatever their individual excess over CORNER
RETRACE = 12.0    # px. A sharp turn only counts against a route when the path
                  # nearly doubles back on itself — when the point WIN px before
                  # the turn and the point WIN px after land within this of each
                  # other. That is the signature of the snapper jutting off the
                  # line and back. A real turnaround loop reaches the same angle
                  # but with width: its two arms stay a block apart, so it is left
                  # alone, along with the square corner where two avenues meet.

# The drawn map, and the band of it the animation actually shows: a vehicle is
# hidden above the title banner (y < 708) and off the sheet, so a path's shape
# out there is never seen and must not be scored. Whole feeds keep the warp and
# sail thousands of px off the page (Norwalk sits near y = 320000); scoring that
# would rank the invisible above the wrong.
MAP_W, MAP_H, BANNER = 4096, 4139, 708


def load():
    if not os.path.exists(SCHEDULE):
        sys.exit(f"missing {SCHEDULE} (run from the repo root)")
    with open(SCHEDULE) as f:
        return json.load(f)


def callout_mask(P):
    """Boolean, True where a point lies inside the Downtown call-out and so is
    exempt from scoring. Falls back to all-False if build_data can't be
    imported, which only makes the score stricter, never wrong."""
    try:
        sys.path.insert(0, "scripts")
        from build_data import inside_callout
        return np.asarray(inside_callout(P), dtype=bool)
    except Exception:
        return np.zeros(len(P), dtype=bool)


def resample(P):
    """`P` redrawn at a uniform STEP, with the arclength of each new point."""
    P = np.asarray(P, dtype=float)
    seg = np.hypot(*np.diff(P, axis=0).T)
    cum = np.concatenate([[0], np.cumsum(seg)])
    if cum[-1] < 2 * STEP:
        return P, cum
    t = np.arange(0, cum[-1], STEP)
    return np.c_[np.interp(t, cum, P[:, 0]), np.interp(t, cum, P[:, 1])], t


def turn_series(P):
    """Signed turn angle (deg) at each point, measured over +/- WIN px. Positive
    is a left turn, negative right; 0 is dead straight. Endpoints, which have no
    full window, are 0."""
    n = len(P)
    if n < 3:
        return np.zeros(n)
    w = max(1, int(round(WIN / STEP)))
    out = np.zeros(n)
    for i in range(w, n - w):
        a = P[i] - P[i - w]
        b = P[i + w] - P[i]
        na, nb = math.hypot(*a), math.hypot(*b)
        if na < 1e-6 or nb < 1e-6:
            continue
        cross = a[0] * b[1] - a[1] * b[0]
        dot = a[0] * b[0] + a[1] * b[1]
        out[i] = math.degrees(math.atan2(cross, dot))
    return out


def score_shape(P):
    """(score, kinks, worst_cusp_deg, worst_xy, detail) for one polyline.

    score is the total 'excess turning' the path has beyond what straight runs
    and square corners would need — the currency the ranking sorts on."""
    R, _ = resample(P)
    if len(R) < 5:
        return 0.0, 0, 0.0, None, []
    onmap = ((R[:, 0] >= 0) & (R[:, 0] <= MAP_W)
             & (R[:, 1] >= BANNER) & (R[:, 1] <= MAP_H))
    keep = onmap & ~callout_mask(R)
    theta = turn_series(R)
    theta[~keep] = 0.0
    mag = np.abs(theta)

    # Does the path double back on itself here? Compare the point a window before
    # each turn with the point a window after: on a spike they nearly coincide,
    # on a corner or a loop they stay apart. Only doubling-back turns are scored.
    w = max(1, int(round(WIN / STEP)))
    retrace = np.zeros(len(R), dtype=bool)
    if len(R) > 2 * w:
        gap = np.hypot(*(R[2 * w:] - R[:-2 * w]).T)
        retrace[w:len(R) - w] = gap < RETRACE

    # A turn only counts for what it does beyond a square corner, so genuine
    # right-angle turns onto a cross street cost nothing and only hairpins pay.
    excess = np.where(retrace, np.maximum(0.0, mag - CORNER), 0.0)

    # Zigzag: adjacent sharp turns in opposite directions. The snapper sewing
    # between two streets makes these even when no single turn is a full cusp,
    # so they are charged on their own, by how sharp the tighter of the pair is.
    zig = 0.0
    detail = []
    for i in range(1, len(theta)):
        if (abs(theta[i]) > ZIG and abs(theta[i - 1]) > ZIG
                and np.sign(theta[i]) != np.sign(theta[i - 1])):
            pay = min(abs(theta[i]), abs(theta[i - 1])) - ZIG
            zig += pay
            detail.append((R[i], min(abs(theta[i]), abs(theta[i - 1])), "zigzag"))

    kink_idx = np.nonzero(excess > 0)[0]
    for i in kink_idx:
        detail.append((R[i], mag[i], "cusp"))

    score = float(excess.sum() + zig)
    kinks = int(len(kink_idx))
    # the sharpest turn reported is the sharpest *scored* one, so the worst-
    # location column points at a real defect rather than a legitimate loop
    scored = np.where((excess > 0), mag, 0.0)
    worst = float(scored.max()) if scored.any() else 0.0
    worst_xy = (tuple(np.round(R[int(scored.argmax())]).astype(int))
                if scored.any() else None)
    return score, kinks, worst, worst_xy, detail


def route_shapes(d):
    """{route index: [(shape index, trip count)]}, and a trip total per route."""
    from collections import defaultdict
    per = defaultdict(lambda: defaultdict(int))
    for t in d["trips"]:
        pat = d["patterns"][t[1]]
        if pat is not None:
            per[t[0]][pat["s"]] += 1
    return per


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("route", nargs="?", help="detail one route label instead of ranking")
    ap.add_argument("--system", help="restrict to systems matching this substring")
    ap.add_argument("--top", type=int, default=30, help="rows to print (default 30)")
    ap.add_argument("--min-trips", type=int, default=1,
                    help="ignore routes with fewer than this many daily trips")
    ap.add_argument("--inset", action="store_true",
                    help="score the Downtown panel's paths instead")
    a = ap.parse_args()

    d = load()
    per = route_shapes(d)

    rows = []
    for ridx, shapes in per.items():
        route = d["routes"][ridx]
        system = d["systems"][route["sy"]]
        if a.system and a.system.lower() not in system.lower():
            continue
        if a.route and route["n"].lower() != a.route.lower():
            continue
        trips = sum(shapes.values())
        if trips < a.min_trips:
            continue
        best = (0.0, 0, 0.0, None, [], None)
        total_kinks, drawn = 0, 0
        for si, ntr in shapes.items():
            # one polyline per shape on the main map; in the panel a shape can
            # enter it more than once, and each run is scored on its own
            polys = (d["insets"][si] or []) if a.inset else [d["shapes"][si]]
            drawn += len(polys)
            for pts in polys:
                sc, kinks, worst, worst_xy, detail = score_shape(
                    np.array(pts, dtype=float).reshape(-1, 2))
                total_kinks += kinks
                if sc > best[0]:
                    best = (sc, kinks, worst, worst_xy, detail, si)
        if not drawn:
            continue                  # the panel doesn't reach this route
        rows.append({"n": route["n"], "sy": system, "trips": trips,
                     "score": best[0], "cusp": best[2], "kinks": total_kinks,
                     "worst": best[3], "shape": best[5], "detail": best[4]})

    rows.sort(key=lambda r: -r["score"])

    if a.route:
        if not rows:
            sys.exit(f"no route labelled {a.route!r}"
                     + (f" in a system matching {a.system!r}" if a.system else ""))
        for r in rows:
            print(f"\n{r['n']}  {r['sy']}  score {r['score']:.0f}  "
                  f"sharpest {r['cusp']:.0f} deg  {r['kinks']} kink(s)  "
                  f"worst shape {r['shape']}")
            det = sorted(r["detail"], key=lambda x: -x[1])
            seen = []
            for xy, deg, kind in det:
                if any(math.hypot(xy[0] - s[0], xy[1] - s[1]) < WIN * 2 for s in seen):
                    continue      # collapse a cluster to its sharpest point
                seen.append(xy)
                print(f"   {kind:6s} {deg:5.0f} deg at ({xy[0]:.0f},{xy[1]:.0f})")
        return

    print(f"{'rank':>4}  {'route':<7} {'system':<22} {'trips':>5} {'score':>7} "
          f"{'sharp':>6} {'kinks':>5}  worst-location")
    for i, r in enumerate(rows[:a.top], 1):
        w = f"({r['worst'][0]},{r['worst'][1]})" if r["worst"] else "-"
        flag = "  <-- suspect" if r["score"] > 200 else ""
        print(f"{i:>4}  {r['n']:<7} {r['sy'][:22]:<22} {r['trips']:>5} "
              f"{r['score']:>7.0f} {r['cusp']:>6.0f} {r['kinks']:>5}  {w}{flag}")
    print(f"\n{len(rows)} routes scored. score = total degrees of turning beyond "
          f"straight runs and square corners;\nhigher is more suspect. 'sharp' is "
          f"the single sharpest turn (deg). Detail: path_check.py <route>.")


if __name__ == "__main__":
    main()
