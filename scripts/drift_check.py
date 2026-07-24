"""Rank every route by how much of it runs off the line the sheet draws for it.

path_check asks whether a path is *shaped* like a street — hairpins, zigzags,
kinks — and speed_check asks whether a vehicle covers ground it has no time for.
Both are proxies, and both are blind to the failure that matters most here: a
path that is perfectly smooth, perfectly paced, and simply on the wrong street.
Metro 180 ran a straight line across blank page north of Colorado Blvd for 180
px and scored 0 for spikes doing it.

This asks the question directly. Every route is drawn on the sheet in its
agency's ink, the PDF has that ink as vector strokes, and the animation plays
the stored shape — so the distance between the two *is* the deviation, in map
pixels, with nothing inferred.

    drift   arc px of the route running further than --px (default 12, about a
            line and a half wide) from its own ink, *while that ink is still
            within reach* (--far, default 60). This is the ranking: the stretches
            where the sheet does draw the route hereabouts and the path is not
            on it.
    beyond  arc px with no ink of the agency within --far at all. Nearly always
            the sheet declining to draw a stretch the feed still runs — a DASH
            loop the schematic ends at its terminal, a line leaving the sheet —
            and there the warp is the best there is and no fault of the snap.
            Counted apart from drift rather than ranked with it, because it was
            drowning the real thing: LADOT's Pacoima DASH runs up to the Sylmar
            Metrolink station, the sheet draws only the Pacoima loop, and that
            read as 24% of the route being off its line.
    worst   the single furthest point *within* reach, and where it is — paste it
            into debug_line.py, or crop the tiles there.

Points inside the Downtown call-out and under the title banner are dropped:
the sheet draws no ink there by design, so distance to it means nothing.

Where the ink can be trusted and where it can't
-----------------------------------------------
The PDF gives one stroke per drawn line for Metro bus (orange, the Rapid red,
the busway ribbon), LADOT's two olives, and the railroads. Those rows are exact
— the strokes are the drawing itself, complete underneath every label painted
over them.

Everything else has no vector ink of its own and is measured against the colour
mask instead, marked `mask` in the `src` column. A mask is knocked out wherever
a place name or a station marker crosses the line, so a mask row can be flagged
by a hole in the artwork rather than by a path that moved. Read those as "look
at this", not as "this is broken".

What this cannot see
--------------------
An agency's ink is one undifferentiated set of strokes — the same blind spot
the snapper has, and for the same reason. A route sitting squarely on a
*sibling's* line reads as zero drift, because it is on ink of the right colour;
only leaving the agency's drawing altogether, for blank page or another
agency's streets, registers here. So a clean score means "on some line this
agency is drawn in", not "on its own line". path_check catches a subset of the
rest, by the scar the hop leaves.

Usage:
    scripts/drift_check.py                    # worst 25, all systems
    scripts/drift_check.py --top 60
    scripts/drift_check.py --system Metro     # one system
    scripts/drift_check.py --ink              # only rows the PDF can settle
    scripts/drift_check.py 180                # every drifting run on one route
    scripts/drift_check.py --px 25            # only gross departures
    scripts/drift_check.py --csv
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
                       # and a half — visibly beside it, not merely fuzzy
STEP = 4.0             # px between samples along the path, matching the snapper
FAR_PX = 60.0          # px; with no ink of the agency this near, the sheet is
                       # not drawing the route here — and the snapper could not
                       # have reached it if it were, its widest cap being 40
NOT_DRAWN = 60.0       # median px; past this the sheet isn't drawing this route
                       # at all (it runs off the edge, or the map omits it) and
                       # a drift number would be measuring the omission
MIN_ARC = 80.0         # px; ignore a route with almost nothing on the sheet

# Metro's bus ink is not all one colour. The Rapid ribbon and the busway are
# drawn in their own, and the J/Silver line is drawn in the freeway gray the
# masks can't separate — build_data gives it no mask either, so neither can this.
METRO_RAPID = {"720", "754", "761"}
METRO_BUSWAY = {"G"}
METRO_UNDRAWN = {"J"}

# The sheet letters its rail lines; the feed numbers them.
RAIL_LABELS = {"801": "A", "802": "B", "803": "C",
               "804": "E", "805": "D", "807": "K"}


def tree_for(system, label, cache, rail_trees):
    """(KD-tree, source) for the ink or mask a route is drawn in.

    Mirrors the choices build_data makes when it snaps, since a path measured
    against a different tree than it was fitted to is measuring the difference
    between the two trees."""
    import build_data as B
    key = (system, label if system in ("Metro Bus", "LADOT") else "")
    if key in cache:
        return cache[key]
    out = (None, "-")
    if system == "Metro Bus":
        if label in METRO_UNDRAWN:
            out = (None, "-")
        elif label in METRO_BUSWAY:
            out = (B.ink_tree(B.BUSWAY_INK), "ink")
        elif label in METRO_RAPID:
            out = (B.ink_tree(B.RAPID_RED_INK), "ink")
        else:
            out = (B.ink_tree(B.ORANGE_INK), "ink")
    elif system == "LADOT":
        # Two stroke styles of one olive: DASH solid, Commuter Express dashed,
        # and a route measured against the wrong one is measured against another
        # network entirely. schedule.json doesn't carry the distinction, so it is
        # read back out of the feed the same way build_data reads it. Guessing
        # from the designation — numbered is Commuter Express, named is DASH —
        # is right 47 labels out of 48 and wrong about the one Commuter Express
        # that isn't numbered: "CE Union Station/Bunker Hill Shuttle", which the
        # sheet labels "USH" and the guess would measure against DASH's network.
        out = (B.ink_tree(B.LADOT_INK, dashed=not ladot_is_dash().get(label, True)),
               "ink")
    elif system == "Metrolink":
        out = (B.rail_line_tree(), "ink")
    elif system == "Metro Rail":
        rid = next((k for k, n in RAIL_LABELS.items() if n == label), None)
        out = ((rail_trees or {}).get(rid), "mask" if rid else "-")
    else:
        cols = agency_mask_colors(system)
        out = (B.mask_tree(cols, 30.0) if cols else None, "mask")
    cache[key] = out
    return out


_LADOT_DASH = None


def ladot_is_dash():
    """{map label: is a DASH route}, read from the feed exactly as build_data
    reads it — the long name says which livery, and the label is what the sheet
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
    on — see the header on `beyond`, which counts the rest."""
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
                      f"(no {r['sy']} ink within {a.far:g} px) — not counted as drift")
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
          f"{len(rows) - ink} against a colour mask — see the header).")
    if undrawn:
        print(f"{len(undrawn)} not drawn on the sheet, so not ranked: "
              + ", ".join(sorted(f"{r['n']} ({r['sy']})" for r in undrawn)[:12])
              + (" ..." if len(undrawn) > 12 else ""))
    print(f"drift = px of route standing over {a.px:g} px from its own drawn line. "
          f"Detail: drift_check.py <route>.")


# The sheet letters its rail lines; the feed numbers them.
RAIL_LABELS = {"801": ("A",), "802": ("B",), "803": ("C",),
               "804": ("E",), "805": ("D",), "807": ("K",)}

if __name__ == "__main__":
    main()
