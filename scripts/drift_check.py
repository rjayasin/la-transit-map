"""Rank every route by how much of it runs off the line the sheet draws for it.

path_check asks whether a path is *shaped* like a street, looking for hairpins,
zigzags and kinks. speed_check asks whether a vehicle covers ground it has no
time for.
Both are proxies, and both are blind to the failure that matters most here: a
path that is perfectly smooth, perfectly paced, and simply on the wrong street.
Metro 180 ran a straight line across blank page north of Colorado Blvd for 180
px and scored 0 for spikes doing it.

This asks the question directly. Every route is drawn on the sheet in its
agency's ink, the PDF has that ink as vector strokes, and the animation plays
the stored shape, so the distance between the two is the deviation, in map
pixels, with nothing inferred.

    drift   arc px of the route running further than --px (default 12, about a
            line and a half wide) from its own ink, *while that ink is still
            within reach* (--far, default 60). This is the ranking: the stretches
            where the sheet does draw the route hereabouts and the path is not
            on it.
    beyond  arc px with no ink of the agency within --far at all. Nearly always
            the sheet declining to draw a stretch the feed still runs, such as
            a DASH loop the schematic ends at its terminal or a line leaving
            the sheet. There the warp is the best available, and no fault of
            the snap.
            Counted apart from drift rather than ranked with it, because it was
            drowning the real thing: LADOT's Pacoima DASH runs up to the Sylmar
            Metrolink station, the sheet draws only the Pacoima loop, and that
            read as 24% of the route being off its line.
    worst   the single furthest point *within* reach, and where it is. Paste
            it into debug_line.py, or crop the tiles there.

Points inside the Downtown call-out and under the title banner are dropped:
the sheet draws no ink there by design, so distance to it means nothing.

Where the ink can be trusted and where it can't
-----------------------------------------------
The PDF gives one stroke per drawn line for Metro bus (orange, the Rapid red,
the busway ribbon), LADOT's two olives, Montebello's sage, and the railroads.
Those rows are exact: the strokes are the drawing itself, complete underneath
every label painted over them.

Everything else has no vector ink of its own and is measured against the colour
mask instead, marked `mask` in the `src` column. A mask is knocked out wherever
a place name or a station marker crosses the line, so a mask row can be flagged
by a hole in the artwork rather than by a path that moved. Read those as "look
at this", not as "this is broken".

What this cannot see
--------------------
An agency's ink is one undifferentiated set of strokes, which is the same
blind spot the snapper has and for the same reason. A route sitting squarely on a
*sibling's* line reads as zero drift, because it is on ink of the right colour;
only leaving the agency's drawing altogether, for blank page or another
agency's streets, registers here. So a clean score means "on some line this
agency is drawn in", not "on its own line". path_check catches a subset of the
rest, by the scar the hop leaves.

--rejoin
--------
Keeps the strokes apart. The sheet draws each line as its own stroke, so a path
that rides a stroke, leaves it by more than --leave px (default 7) and comes
back to the same stroke within 400 px has cut across something on the way: a
drawn corner, dip or loop, or a stretch on a neighbouring line of the same ink.
It lists those stretches instead of the drift ranking, for every agency the PDF
has strokes for, including the ones snapped on a colour mask.

A route whose drawn line genuinely leaves a stroke and returns to it is listed
too, as is a route the sheet draws round a loop the feed does not drive. Each
row is a place to look.

Usage:
    scripts/drift_check.py                    # worst 25, all systems
    scripts/drift_check.py --top 60
    scripts/drift_check.py --system Metro     # one system
    scripts/drift_check.py --ink              # only rows the PDF can settle
    scripts/drift_check.py 180                # every drifting run on one route
    scripts/drift_check.py --px 25            # only gross departures
    scripts/drift_check.py --csv
    scripts/drift_check.py --rejoin           # chords and hops onto a neighbour
    scripts/drift_check.py --rejoin --system LADOT --leave 10
"""
import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, "scripts")

SCHEDULE = "schedule.json"
DRIFT_PX = 12.0        # px off the ink before a stretch counts as drifting;
                       # a drawn line is ~8 px wide at 4096, so this is a line
                       # and a half: visibly beside it, not merely fuzzy
STEP = 4.0             # px between samples along the path, matching the snapper
FAR_PX = 60.0          # px; with no ink of the agency this near, the sheet is
                       # not drawing the route here, and the snapper could not
                       # have reached it if it were, its widest cap being 40
NOT_DRAWN = 60.0       # median px; past this the sheet isn't drawing this route
                       # at all (it runs off the edge, or the map omits it) and
                       # a drift number would be measuring the omission
MIN_ARC = 80.0         # px; ignore a route with almost nothing on the sheet

# Metro's bus ink is not all one colour. The Rapid ribbon and the busway are
# drawn in their own, and the J/Silver line is drawn in the freeway gray the
# masks can't separate; build_data gives it no mask either, so neither can this.
METRO_RAPID = {"720", "754", "761"}
METRO_BUSWAY = {"G"}
METRO_JLINE = {"910"}   # drawn as the freeway busway's gray ribbon, not in bus orange
METRO_UNDRAWN = {"J"}

# The sheet letters its rail lines; the feed numbers them.
RAIL_LABELS = {"801": "A", "802": "B", "803": "C",
               "804": "E", "805": "D", "807": "K"}


def ink_for(system, label):
    """(colors, dashed) of the PDF strokes a route is drawn in, or None where
    the sheet draws it in no vector ink of its own."""
    import build_data as B
    if system == "Metro Bus":
        if label in METRO_UNDRAWN:
            return None
        if label in METRO_BUSWAY:
            return B.BUSWAY_INK, None
        if label in METRO_JLINE:
            return B.JLINE_INK, None
        if label in METRO_RAPID:
            return B.RAPID_RED_INK, None
        return B.ORANGE_INK, None
    if system == "LADOT":
        # Two stroke styles of one olive: DASH solid, Commuter Express dashed,
        # and a route measured against the wrong one is measured against another
        # network entirely. schedule.json doesn't carry the distinction, so it is
        # read back out of the feed the way build_data reads it. Guessing from
        # the designation is wrong for the one Commuter Express that isn't
        # numbered.
        return B.LADOT_INK, not ladot_is_dash().get(label, True)
    if system == "Metrolink":
        return B.RAIL_INK, False
    if system == "Metro Rail":
        rid = next((k for k, n in RAIL_LABELS.items() if n == label), None)
        return (B.METRO_RAIL_INK[rid], None) if rid in B.METRO_RAIL_INK else None
    feed = next((f for f, n in B.FEED_NAMES.items() if n == system), None)
    if feed in B.LEGEND_INK:
        return [B.symbol_ink(feed, label)], None
    return None


def tree_for(system, label, cache, rail_trees):
    """(KD-tree, source) for the ink or mask a route is drawn in.

    Mirrors the choices build_data makes when it snaps, since a path measured
    against a different tree than it was fitted to is measuring the difference
    between the two trees."""
    import build_data as B
    feed = next((f for f, n in B.FEED_NAMES.items() if n == system), None)
    on_ink = system in ("Metro Bus", "LADOT", "Metrolink") or (
        feed in B.INK_SNAP or feed in B.SYMBOL_FEEDS)
    key = (system, label if on_ink or system == "Metro Rail" else "")
    if key in cache:
        return cache[key]
    if system == "Metro Rail":
        rid = next((k for k, n in RAIL_LABELS.items() if n == label), None)
        out = ((rail_trees or {}).get(rid), "mask" if rid else "-")
    elif on_ink:
        spec = ink_for(system, label)
        # Snapped on the sheet's strokes, so measured on them. A mask is no
        # use for the INK_SNAP and symbol agencies: it misses the thin parts of
        # the drawing and holds the grey street lettering instead, and the two
        # agencies the sheet symbolises have seeds refine_color cannot find
        # enough of along the drawing to return a reading at all, so the tree
        # came back None and their rows were dropped before they were scored.
        out = (B.ink_tree(*spec) if spec else None, "ink" if spec else "-")
    else:
        cols = agency_mask_colors(system)
        out = (B.mask_tree(cols, 30.0) if cols else None, "mask")
    cache[key] = out
    return out


CONTACT = 2.0     # px; a path this near a stroke is riding it
RIDE = 12.0       # px of contact before a stroke counts as the line the path is on
LEAVE = 7.0       # px off that stroke before the stretch between counts
REJOIN = 400.0    # px of path between leaving a stroke and returning to it


_STROKES = {}


def stroke_ink(colors, dashed=None):
    """(points, stroke ids, arc along stroke) for every PDF stroke in `colors`,
    sampled every 1 px, on the main map.

    The same strokes `pdf_ink` reads, with each point keeping the stroke it
    came from. The sheet draws each line as its own stroke, so the id tells a
    route's own line from a neighbour's running a few px away in the same ink,
    which a distance to the ink cannot."""
    key = (tuple(map(tuple, colors)), dashed)
    if key in _STROKES:
        return _STROKES[key]
    import fitz
    import build_data as B
    page = fitz.open(B.PDF)[0]
    s = B.map_image()[0].shape[1] / page.rect.width
    P, ids, arcs = [], [], []
    for sid, it in enumerate(page.get_drawings()):
        c = it.get("color")
        if c is None or not any(sum((a - b) ** 2 for a, b in zip(c, k)) < 1e-4
                                for k in colors):
            continue
        if dashed is not None and ((it.get("dashes") or "[] 0") != "[] 0") != dashed:
            continue
        pts = []
        for seg in it["items"]:
            if seg[0] == "l":
                a = np.array([seg[1].x * s, seg[1].y * s])
                b = np.array([seg[2].x * s, seg[2].y * s])
                n = max(1, int(np.ceil(np.hypot(*(b - a)))))
                pts += list(a + (b - a) * np.linspace(0, 1, n + 1)[:, None])
            elif seg[0] == "c":
                p = [np.array([q.x * s, q.y * s]) for q in seg[1:5]]
                t = np.linspace(0, 1, 24)[:, None]
                pts += list((1 - t) ** 3 * p[0] + 3 * (1 - t) ** 2 * t * p[1]
                            + 3 * (1 - t) * t ** 2 * p[2] + t ** 3 * p[3])
        if len(pts) < 2:
            continue
        Q = np.asarray(pts, dtype=float)
        P.append(Q)
        ids.append(np.full(len(Q), sid))
        arcs.append(np.concatenate([[0], np.cumsum(np.hypot(*np.diff(Q, axis=0).T))]))
    if P:
        P, ids, arcs = np.concatenate(P), np.concatenate(ids), np.concatenate(arcs)
        keep = B.maskable(P) & ~B.inside_callout(P)
        P, ids, arcs = P[keep], ids[keep], arcs[keep]
    else:
        P, ids, arcs = np.zeros((0, 2)), np.zeros(0, int), np.zeros(0)
    from scipy.spatial import cKDTree
    _STROKES[key] = (P, ids, arcs, cKDTree(P) if len(P) else None)
    return _STROKES[key]


def rejoins(pts, ink, contact=CONTACT, ride=RIDE, leave=LEAVE, rejoin=REJOIN):
    """Stretches where a path leaves a stroke it was riding and comes back to
    the same stroke a short way on, standing off it in between.

    That is a chord across a drawn corner, dip or loop, or a stretch run on a
    neighbouring line of the same ink. Both sit on the agency's ink, so the
    drift measure cannot see them. A route whose drawn line really leaves a
    stroke and comes back to it, such as two routes parting for a block, is
    flagged too: each row is a place to look, not a verdict.

    Returns (worst px off the stroke, px of path between, leave xy, rejoin xy,
    worst xy) per stretch."""
    P, ids, arcs, tree = ink
    S = densify(pts, STEP)
    if tree is None or len(S) < 2:
        return []
    dist, j = tree.query(S)
    on = dist < contact
    need = int(np.ceil(ride / STEP))
    out = []
    by_stroke = {}
    for k in np.nonzero(on)[0]:
        by_stroke.setdefault(int(ids[j[k]]), []).append(k)
    for sid, ks in by_stroke.items():
        # runs of consecutive samples on this stroke, long enough to be riding it
        runs, cur = [], [ks[0]]
        for k in ks[1:]:
            if k == cur[-1] + 1:
                cur.append(k)
            else:
                runs.append(cur)
                cur = [k]
        runs.append(cur)
        runs = [r for r in runs if len(r) >= need]
        if len(runs) < 2:
            continue
        mine = ids == sid
        from scipy.spatial import cKDTree
        own = cKDTree(P[mine])
        own_arc = arcs[mine]
        for r0, r1 in zip(runs, runs[1:]):
            k0, k1 = r0[-1], r1[0]
            gap = (k1 - k0) * STEP
            if gap > rejoin:
                continue
            d_own, i_own = own.query(S[k0:k1 + 1])
            # a stroke the path meets far apart along its own length is two
            # visits to one long line, not one stretch cut short
            if abs(own_arc[i_own[-1]] - own_arc[i_own[0]]) > 3 * gap + 40:
                continue
            m = int(np.argmax(d_own))
            if d_own[m] < leave:
                continue
            out.append((float(d_own[m]), gap, tuple(S[k0]), tuple(S[k1]),
                        tuple(S[k0 + m])))
    return out


_LADOT_DASH = None


def ladot_is_dash():
    """{map label: is a DASH route}, read from the feed exactly as build_data
    reads it: the long name says which livery, and the label is what the sheet
    prints. A label covering both liveries (none does today) would resolve to
    DASH, which is the commoner of the two."""
    global _LADOT_DASH
    if _LADOT_DASH is None:
        import build_data as B
        _LADOT_DASH = {}
        for row in B.read_csv("ladot", "routes.txt"):
            label = B.MAP_LABELS.get(("ladot", row["route_id"])) or B.route_label(
                row.get("route_short_name", ""), row.get("route_long_name", ""))
            _LADOT_DASH.setdefault(label, "DASH" in (row.get("route_long_name") or ""))
    return _LADOT_DASH


_AGENCY_COLS = {}


def agency_mask_colors(system):
    """The drawn colour of an agency with no vector ink, refined off its own
    stored shapes the way build_data refines it off the warped ones."""
    import build_data as B
    if system in _AGENCY_COLS:
        return _AGENCY_COLS[system]
    feed = next((f for f, n in B.FEED_NAMES.items() if n == system), None)
    seeds = B.LEGEND_SEEDS.get(feed)
    if not seeds:
        _AGENCY_COLS[system] = None
        return None
    shapes = _SHAPES_BY_SYSTEM.get(system, [])
    cols = [c for c in (B.refine_color(shapes, s) for s in seeds) if c]
    _AGENCY_COLS[system] = cols or None
    return _AGENCY_COLS[system]


_SHAPES_BY_SYSTEM = {}


def quietly(fn, *args):
    """Run a build_data helper without its progress chatter. The mask loaders
    report per-line pixel counts to stdout, which is useful in a build and only
    noise in front of a ranking."""
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args)


def densify(pts, step=STEP):
    """Resample a polyline at a fixed spacing, so drift is measured per map
    pixel of route rather than per stored vertex."""
    P = np.asarray(pts, dtype=float)
    if len(P) < 2:
        return P
    seg = np.hypot(*np.diff(P, axis=0).T)
    cum = np.concatenate([[0], np.cumsum(seg)])
    if cum[-1] <= 0:
        return P[:1]
    t = np.arange(0, cum[-1], step)
    return np.c_[np.interp(t, cum, P[:, 0]), np.interp(t, cum, P[:, 1])]


def measure(pts, tree, px, far=FAR_PX):
    """Where a path stands off its ink. Returns a dict, or None where nothing
    can be judged. `drift` counts only ground the sheet actually draws the route
    on. See the header on `beyond`, which counts the rest."""
    import build_data as B
    P = densify(pts)
    if len(P) < 2 or tree is None:
        return None
    P = P[B.maskable(P)]                   # on the sheet, outside the call-out
    if len(P) < 2:
        return None
    d = tree.query(P)[0]
    arc = (len(P) - 1) * STEP
    if arc < MIN_ARC:
        return None
    near = d <= far                        # the sheet draws this route hereabouts
    beyond = float((~near).sum()) * STEP
    if near.sum() < 2:
        return None
    dn = d[near]
    off = near & (d > px)
    k = int(np.argmax(np.where(near, d, -1)))
    # the contiguous stretches that are off, so a route can be told "one bad
    # corner" from "wrong for half its length"
    runs, i = [], 0
    while i < len(off):
        if not off[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(off) and off[j + 1]:
            j += 1
        runs.append((float((j - i + 1) * STEP), float(d[i:j + 1].max()),
                     tuple(P[i + int(np.argmax(d[i:j + 1]))])))
        i = j + 1
    return {"drift": float(off.sum()) * STEP, "arc": arc, "beyond": beyond,
            "p90": float(np.percentile(dn, 90)), "max": float(d[k]),
            "worst": tuple(P[k]), "med": float(np.median(dn)), "runs": runs}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("route", nargs="?", help="one route label, for its drifting runs")
    ap.add_argument("--system", help="substring of the system name")
    ap.add_argument("--top", type=int, default=25, help="rows to print (default 25)")
    ap.add_argument("--px", type=float, default=DRIFT_PX,
                    help=f"px off the line before it counts (default {DRIFT_PX:g})")
    ap.add_argument("--far", type=float, default=FAR_PX,
                    help=f"px with no ink this near counts as beyond the "
                         f"drawing, not as drift (default {FAR_PX:g})")
    ap.add_argument("--ink", action="store_true",
                    help="only routes the PDF's own strokes can settle")
    ap.add_argument("--min-trips", type=int, default=1,
                    help="ignore routes with fewer trips than this")
    ap.add_argument("--csv", action="store_true", help="every row as CSV to stdout")
    ap.add_argument("--rejoin", action="store_true",
                    help="list stretches that leave a drawn stroke and come back "
                         "to the same one (chords, and runs on a neighbour's line)")
    ap.add_argument("--leave", type=float, default=LEAVE,
                    help=f"with --rejoin, px off the stroke before a stretch is "
                         f"listed (default {LEAVE:g})")
    a = ap.parse_args()

    if not os.path.exists(SCHEDULE):
        sys.exit(f"missing {SCHEDULE} (run from the repo root)")
    with open(SCHEDULE) as f:
        d = json.load(f)
    import build_data as B
    from georef import load_masks

    def pairs(flat):
        return np.asarray(flat, dtype=float).reshape(-1, 2)

    trips_per_shape, route_of_shape = {}, {}
    for t in d["trips"]:
        pat = d["patterns"][t[1]]
        if not pat:
            continue
        trips_per_shape[pat["s"]] = trips_per_shape.get(pat["s"], 0) + 1
        route_of_shape.setdefault(pat["s"], t[0])
    for si, ridx in route_of_shape.items():
        sysname = d["systems"][d["routes"][ridx]["sy"]]
        _SHAPES_BY_SYSTEM.setdefault(sysname, []).append(
            [tuple(p) for p in pairs(d["shapes"][si])])

    if a.rejoin:
        return rejoin_report(d, route_of_shape, trips_per_shape, a)

    rail_trees = quietly(load_masks)
    cache, rows, undrawn = {}, [], []
    per_route = {}
    for si, ridx in route_of_shape.items():
        route = d["routes"][ridx]
        sysname = d["systems"][route["sy"]]
        if a.system and a.system.lower() not in sysname.lower():
            continue
        if a.route and route["n"].lower() != a.route.lower():
            continue
        tree, src = tree_for(sysname, route["n"], cache, rail_trees)
        if tree is None or (a.ink and src != "ink"):
            continue
        m = measure(pairs(d["shapes"][si]), tree, a.px, a.far)
        if m is None:
            continue
        key = (route["n"], sysname)
        e = per_route.setdefault(key, {"n": route["n"], "sy": sysname, "src": src,
                                       "trips": 0, "drift": 0.0, "arc": 0.0,
                                       "beyond": 0.0, "p90": 0.0, "max": 0.0,
                                       "worst": None, "med": 0.0, "runs": [],
                                       "shape": None})
        e["trips"] += trips_per_shape.get(si, 0)
        if m["drift"] > e["drift"] or e["shape"] is None:
            e.update(drift=m["drift"], arc=m["arc"], p90=m["p90"], med=m["med"],
                     runs=m["runs"], shape=si)
        e["beyond"] = max(e["beyond"], m["beyond"])
        if m["max"] > e["max"]:
            e["max"] = m["max"]
            e["worst"] = m["worst"]

    for e in per_route.values():
        if e["trips"] < a.min_trips:
            continue
        (undrawn if e["med"] > NOT_DRAWN else rows).append(e)
    rows.sort(key=lambda r: -r["drift"])

    if a.route:
        if not rows and not undrawn:
            sys.exit(f"no measurable route labelled {a.route!r}"
                     + (f" in a system matching {a.system!r}" if a.system else ""))
        for r in rows + undrawn:
            print(f"\n{r['n']}  {r['sy']}  via {r['src']}  shape {r['shape']}")
            print(f"  {r['drift']:.0f} px of {r['arc']:.0f} drift over {a.px:g} px "
                  f"({100 * r['drift'] / max(1, r['arc']):.1f}%)   "
                  f"median {r['med']:.1f}  p90 {r['p90']:.1f}  max {r['max']:.1f}")
            if r["beyond"]:
                print(f"  {r['beyond']:.0f} px beyond the drawing "
                      f"(no {r['sy']} ink within {a.far:g} px), not counted as drift")
            for length, mx, xy in sorted(r["runs"], key=lambda x: -x[0])[:12]:
                print(f"   {length:6.0f} px off, worst {mx:5.1f} px "
                      f"at ({xy[0]:.0f},{xy[1]:.0f})")
        return

    if a.csv:
        import csv
        w = csv.writer(sys.stdout)
        w.writerow(["route", "system", "src", "trips", "drift_px", "arc_px",
                    "beyond_px", "pct", "median", "p90", "max",
                    "worst_x", "worst_y"])
        for r in rows:
            w.writerow([r["n"], r["sy"], r["src"], r["trips"], round(r["drift"]),
                        round(r["arc"]), round(r["beyond"]),
                        round(100 * r["drift"] / max(1, r["arc"]), 1),
                        round(r["med"], 1), round(r["p90"], 1), round(r["max"], 1),
                        round(r["worst"][0]), round(r["worst"][1])])
        return

    print(f"{'rank':>4}  {'route':<7} {'system':<22} {'src':<4} {'trips':>5} "
          f"{'drift':>7} {'of':>6} {'pct':>5} {'beyond':>7} {'p90':>6} {'max':>6}"
          f"  worst-location")
    for i, r in enumerate(rows[:a.top], 1):
        w = f"({r['worst'][0]:.0f},{r['worst'][1]:.0f})" if r["worst"] else "-"
        flag = "  <-- suspect" if r["drift"] > 150 else ""
        print(f"{i:>4}  {r['n']:<7} {r['sy'][:22]:<22} {r['src']:<4} {r['trips']:>5} "
              f"{r['drift']:>7.0f} {r['arc']:>6.0f} "
              f"{100 * r['drift'] / max(1, r['arc']):>4.0f}% "
              f"{r['beyond']:>7.0f} {r['p90']:>6.1f} {r['max']:>6.1f}  {w}{flag}")
    ink = sum(1 for r in rows if r["src"] == "ink")
    print(f"\n{len(rows)} routes measured ({ink} against the PDF's strokes, "
          f"{len(rows) - ink} against a colour mask; see the header).")
    if undrawn:
        print(f"{len(undrawn)} not drawn on the sheet, so not ranked: "
              + ", ".join(sorted(f"{r['n']} ({r['sy']})" for r in undrawn)[:12])
              + (" ..." if len(undrawn) > 12 else ""))
    print(f"drift = px of route standing over {a.px:g} px from its own drawn line. "
          f"Detail: drift_check.py <route>.")


def rejoin_report(d, route_of_shape, trips_per_shape, a):
    """Print `rejoins` for every selected shape, sharpest first. One row per
    route and place: the variants of a route usually share a fault, and the
    row names the variant that stands furthest off."""
    rows = []
    for si, ridx in route_of_shape.items():
        route = d["routes"][ridx]
        sysname = d["systems"][route["sy"]]
        if a.system and a.system.lower() not in sysname.lower():
            continue
        if a.route and route["n"].lower() != a.route.lower():
            continue
        if trips_per_shape.get(si, 0) < a.min_trips:
            continue
        spec = ink_for(sysname, route["n"])
        if spec is None:
            continue
        ink = stroke_ink(*spec)
        pts = np.asarray(d["shapes"][si], dtype=float).reshape(-1, 2)
        for dev, gap, p0, p1, worst in rejoins(pts, ink, leave=a.leave):
            rows.append((dev, gap, route["n"], sysname, si, p0, p1, worst))
    rows.sort(key=lambda r: -r[0])
    seen, out = set(), []
    for r in rows:
        place = (r[2], r[3], round(r[7][0] / 20), round(r[7][1] / 20))
        if place not in seen:
            seen.add(place)
            out.append(r)
    if a.csv:
        import csv
        w = csv.writer(sys.stdout)
        w.writerow(["route", "system", "shape", "off_px", "gap_px", "leave_x",
                    "leave_y", "rejoin_x", "rejoin_y", "worst_x", "worst_y"])
        for dev, gap, n, sy, si, p0, p1, wst in out:
            w.writerow([n, sy, si, round(dev, 1), round(gap), *(round(v, 1) for v in
                                                                 (*p0, *p1, *wst))])
        return
    print(f"{'rank':>4}  {'route':<7} {'system':<22} {'shape':>5} {'off':>6} "
          f"{'gap':>5}  leaves -> rejoins            worst")
    for i, (dev, gap, n, sy, si, p0, p1, wst) in enumerate(out[:a.top], 1):
        print(f"{i:>4}  {n:<7} {sy[:22]:<22} {si:>5} {dev:>6.1f} {gap:>5.0f}  "
              f"({p0[0]:.0f},{p0[1]:.0f}) -> ({p1[0]:.0f},{p1[1]:.0f})"
              f"{'':<8}({wst[0]:.0f},{wst[1]:.0f})")
    print(f"\n{len(out)} stretches leave a drawn stroke by over {a.leave:g} px and "
          f"come back to it within {REJOIN:g} px.")
    print("Most are routes whose drawn line does part from that stroke; look at "
          "each before fixing it. `shape` indexes schedule.json's shapes, which "
          "debug_line.py <route> lists beside each variant.")


if __name__ == "__main__":
    main()
