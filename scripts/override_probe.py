"""Measurements for placing an OVERRIDE_PATHS entry, without editing the build.

Each subcommand answers one question from "Placing an override" in
implementation_notes.md.

    fit FEED           Refit FEED with apply_override wrapped, and record every
                       GTFS shape's warp (`base`), its snapped shape before the
                       feed's overrides (`pre`) and after them (`post`). Writes
                       scratch/probe_FEED.pkl and scratch/refit_FEED.json. The
                       other subcommands read the pickle, and refit it when
                       build_data.py is newer.
    shapes FEED LABEL  Each variant of a route: GTFS shape id, the schedule.json
                       shape index it became, trips, ends.
    box FEED LABEL BOX For each variant, the warp's index runs inside BOX and the
                       snapped points either side of each run. One run per
                       shape is a clean box. The point outside the run is what
                       the path's end has to meet.
    where FEED LABEL BOX
                       The reverse of `box`: for each variant, the runs of its
                       snapped shape inside BOX (map px) and the warp's extent
                       over each run. The extent is where to start a box.
    strokes FEED BOX   The agency's PDF strokes meeting BOX, one per line, in
                       map px. A path is pasted from these.
    trace FEED X,Y ... The drawn course through the given points, walked along
                       the agency's PDF strokes, ready to paste as a `path`.
                       Give a point at each end and one wherever the walk
                       could take a wrong turn. --dashed/--solid pick a style.
    crop OUT BOX       The tiles under BOX, bare beside the same crop with
                       schedule shapes drawn over it (--shapes i,j,...).

BOX is X0,Y0,X1,Y1 in map px. LABEL is the printed designation or the GTFS
route id. Add --schedule FILE to read shapes from a refit instead.

    scripts/override_probe.py fit foothill
    scripts/override_probe.py box foothill 488 3080,1700,3200,1830
    scripts/override_probe.py strokes foothill 3080,1700,3200,1830
    scripts/override_probe.py crop scratch/488.jpg 3060,1680,3220,1850 --shapes 1001
"""
import argparse
import json
import os
import pickle
import sys

import numpy as np
from scipy.spatial import cKDTree

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def xy(p):
    return f"({p[0]:.1f}, {p[1]:.1f})"


def parse_box(s):
    return tuple(float(v) for v in s.split(","))


def pickle_path(feed):
    return f"scratch/probe_{feed}.pkl"


def fit(feed):
    """Refit `feed` and record every shape's warp and snapped points."""
    import build_data as B
    rec = {}
    real = B.apply_override

    def wrapped(full, base, spec, sid=None):
        tag = spec.get("_probe")
        if tag is None:
            return real(full, base, spec, sid)
        e = rec.setdefault(sid, {"rid": spec["_rid"], "base": np.array(base, float)})
        e[tag] = np.array(full, float)
        return full

    class Probe(dict):
        # A no-op spec either side of the feed's own, so every shape of the
        # feed passes through the wrapper before and after its overrides.
        def get(self, key, default=None):
            v = dict.get(self, key, default)
            if key[0] != feed:
                return v
            specs = [] if v is None else (list(v) if isinstance(v, list) else [v])
            mark = lambda t: {"box": (-9, -9, -8, -8), "path": [(0, 0), (1, 1)],
                              "_probe": t, "_rid": key[1]}
            return [mark("pre"), *specs, mark("post")]

    B.apply_override = wrapped
    B.OVERRIDE_PATHS = Probe(B.OVERRIDE_PATHS)
    B.main(["--only", feed, "--no-cache"])
    os.makedirs("scratch", exist_ok=True)
    with open(pickle_path(feed), "wb") as f:
        pickle.dump(rec, f)
    print(f"recorded {len(rec)} shapes to {pickle_path(feed)}")
    return rec


def load(feed):
    p = pickle_path(feed)
    if not os.path.exists(p) or os.path.getmtime(p) < os.path.getmtime(f"{HERE}/build_data.py"):
        return fit(feed)
    with open(p, "rb") as f:
        return pickle.load(f)


def route_sids(rec, label, sched):
    """GTFS shape ids of a route given by printed label or route id."""
    want = {r["id"].split("-")[0] for r in sched["routes"] if r["n"] == label} | {label}
    return [s for s, e in rec.items() if e["rid"].split("-")[0] in want]


def schedule_index(sched, rec, sids):
    """sid -> (schedule shape index, trips), matched within the route's own shapes."""
    rids = {rec[s]["rid"].split("-")[0] for s in sids}
    count = {}
    for t in sched["trips"]:
        if sched["routes"][t[0]]["id"].split("-")[0] in rids:
            p = sched["patterns"][t[1]]
            if p is not None:
                count[p["s"]] = count.get(p["s"], 0) + 1
    out = {}
    for sid in sids:
        Q = rec[sid]["post"]
        tq = cKDTree(Q)
        best = None
        for si in count:
            P = np.asarray(sched["shapes"][si], float).reshape(-1, 2)
            # stored vertices lie on the dense fit; the ends tell a variant
            # from a longer one that runs over it
            d = (tq.query(P)[0].mean() + np.hypot(*(P[0] - Q[0]))
                 + np.hypot(*(P[-1] - Q[-1])))
            if best is None or d < best[0]:
                best = (d, si)
        if best:
            out[sid] = (best[1], count[best[1]], best[0])
    return out


def cmd_shapes(a):
    rec = load(a.feed)
    sched = json.load(open(a.schedule))
    sids = route_sids(rec, a.label, sched)
    idx = schedule_index(sched, rec, sids)
    for sid in sids:
        F = rec[sid]["post"]
        si, n, d = idx.get(sid, (None, 0, 0))
        moved = np.hypot(*(rec[sid]["pre"] - F).T).max()
        print(f"{sid:24s} route {rec[sid]['rid']:10s} shape {si}  trips {n:4d}  match {d:5.1f}  "
              f"{xy(F[0])} -> {xy(F[-1])}  override moved {moved:.1f}")


def cmd_box(a):
    rec = load(a.feed)
    sched = json.load(open(a.schedule))
    x0, y0, x1, y1 = parse_box(a.box)
    for sid in route_sids(rec, a.label, sched):
        e = rec[sid]
        B, F = e["base"], e[a.stage]
        ins = np.where((B[:, 0] >= x0) & (B[:, 0] <= x1) & (B[:, 1] >= y0) & (B[:, 1] <= y1))[0]
        runs = np.split(ins, np.where(np.diff(ins) > 1)[0] + 1) if len(ins) else []
        print(f"{sid:24s} {len(runs)} run{'s' if len(runs) != 1 else ''}")
        for r in runs:
            lo, hi = int(r[0]), int(r[-1])
            fmt = lambda i: xy(F[i]) if 0 <= i < len(F) else None
            print(f"    [{lo}..{hi}]  before {fmt(lo - 1)}  first {fmt(lo)}  "
                  f"last {fmt(hi)}  after {fmt(hi + 1)}")


def cmd_where(a):
    rec = load(a.feed)
    sched = json.load(open(a.schedule))
    x0, y0, x1, y1 = parse_box(a.box)
    for sid in route_sids(rec, a.label, sched):
        B, F = rec[sid]["base"], rec[sid][a.stage]
        ins = np.where((F[:, 0] >= x0) & (F[:, 0] <= x1) & (F[:, 1] >= y0) & (F[:, 1] <= y1))[0]
        runs = np.split(ins, np.where(np.diff(ins) > 1)[0] + 1) if len(ins) else []
        print(f"{sid:24s} {len(runs)} run{'s' if len(runs) != 1 else ''} of {len(F)}")
        for r in runs:
            w = B[r]
            print(f"    [{r[0]}..{r[-1]}]  snapped {xy(F[r[0]])} -> {xy(F[r[-1]])}  "
                  f"warp extent {w[:, 0].min():.0f},{w[:, 1].min():.0f},"
                  f"{w[:, 0].max():.0f},{w[:, 1].max():.0f}")


def cmd_strokes(a):
    import fitz
    import build_data as B
    x0, y0, x1, y1 = parse_box(a.box)
    ink = (tuple(float(v) for v in a.rgb.split(",")) if a.rgb else B.LEGEND_INK[a.feed])
    page = fitz.open(B.PDF)[0]
    s = 4096 / page.rect.width
    for it in page.get_drawings():
        c = it.get("color")
        if c is None or sum((p - q) ** 2 for p, q in zip(c, ink)) >= 1e-4:
            continue
        dashed = (it.get("dashes") or "[] 0") != "[] 0"
        pts = []
        for seg in it["items"]:
            if seg[0] == "l":
                q = [(seg[1].x * s, seg[1].y * s), (seg[2].x * s, seg[2].y * s)]
            elif seg[0] == "c":
                p = [np.array([v.x * s, v.y * s]) for v in seg[1:5]]
                t = np.linspace(0, 1, 6)[:, None]
                q = [tuple(v) for v in ((1 - t) ** 3 * p[0] + 3 * (1 - t) ** 2 * t * p[1]
                                        + 3 * (1 - t) * t ** 2 * p[2] + t ** 3 * p[3])]
            else:
                continue
            if pts and np.hypot(pts[-1][0] - q[0][0], pts[-1][1] - q[0][1]) < 0.05:
                q = q[1:]
            pts += q
        P = np.asarray(pts)
        if not len(P) or not ((P[:, 0] >= x0) & (P[:, 0] <= x1)
                              & (P[:, 1] >= y0) & (P[:, 1] <= y1)).any():
            continue
        w = it.get("width") or 0
        print(f"{'dashed' if dashed else 'solid '} w={w * s:.1f}: "
              + ", ".join(f"({x:.1f}, {y:.1f})" for x, y in P))


GAP_COST = 3.0    # a hop between strokes costs this much more per px than ink


def cmd_trace(a):
    import heapq
    import build_data as B
    ink = (tuple(float(v) for v in a.rgb.split(",")) if a.rgb else B.LEGEND_INK[a.feed])
    pts = [parse_box(p) for p in a.points]
    P = B.pdf_ink([ink], dashed=a.dashed, step=0.5)
    lo = np.min(pts, axis=0) - a.pad
    hi = np.max(pts, axis=0) + a.pad
    P = P[np.all((P >= lo) & (P <= hi), axis=1)]
    t = cKDTree(P)
    # Neighbours along a stroke are 0.5 px apart. The pieces of one drawn line
    # stop a few px short of each other, so hops up to --gap px are allowed at
    # a premium, which keeps the walk on one stroke wherever one continues.
    nbr = [[] for _ in P]
    for i, j in t.query_pairs(a.gap):
        d = float(np.hypot(*(P[i] - P[j])))
        w = d if d <= 0.8 else d * GAP_COST
        nbr[i].append((j, w))
        nbr[j].append((i, w))

    def walk(s, g):
        dist, prev, q = {s: 0.0}, {}, [(0.0, s)]
        while q:
            d, u = heapq.heappop(q)
            if u == g:
                break
            if d > dist[u]:
                continue
            for v, w in nbr[u]:
                if d + w < dist.get(v, np.inf):
                    dist[v], prev[v] = d + w, u
                    heapq.heappush(q, (d + w, v))
        if g not in dist:
            sys.exit(f"no stroke path from {xy(P[s])} to {xy(P[g])}; raise --gap or add a point")
        out = [g]
        while out[-1] != s:
            out.append(prev[out[-1]])
        return [P[i] for i in out[::-1]]

    idx = [int(t.query(p)[1]) for p in pts]
    for p, i in zip(pts, idx):
        if np.hypot(*(P[i] - p)) > 3:
            print(f"# {xy(p)} is {np.hypot(*(P[i] - p)):.1f} px from the nearest stroke", file=sys.stderr)
    course = [P[idx[0]]]
    for s, g in zip(idx, idx[1:]):
        course += walk(s, g)[1:]
    course = np.asarray(course)
    keep = B.simplify(course, tol=a.tol, mask=True)[1]
    out = course[keep]
    arc = np.hypot(*np.diff(course, axis=0).T).sum()
    print(f"# {len(out)} points, {arc:.1f} px", file=sys.stderr)
    print("[" + ", ".join(f"({x:.1f}, {y:.1f})" for x, y in out) + "]")


def cmd_crop(a):
    import debug_line as DL
    from PIL import Image, ImageDraw
    sched = json.load(open(a.schedule))
    box = [int(v) for v in parse_box(a.box)]
    img, s = DL.background(box, a.level)
    bare = img.copy()
    dr = ImageDraw.Draw(img, "RGBA")
    cols = [(*c, 210) for c in DL.COLORS]
    for k, si in enumerate(int(v) for v in (a.shapes.split(",") if a.shapes else [])):
        f = sched["shapes"][si]
        pts = [((x - box[0]) * s, (y - box[1]) * s) for x, y in zip(f[0::2], f[1::2])]
        dr.line(pts, fill=cols[k % len(cols)], width=max(2, s))
    w, h = img.size
    out = Image.new("RGB", (w * 2 + 10, h), "white")
    out.paste(bare, (0, 0))
    out.paste(img, (w + 10, 0))
    if out.width > a.width:
        out = out.resize((a.width, int(h * a.width / out.width)))
    out.save(a.out, quality=88)
    print(a.out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fit")
    p.add_argument("feed")
    p = sub.add_parser("shapes")
    p.add_argument("feed")
    p.add_argument("label")
    p.add_argument("--schedule", default="schedule.json")
    p = sub.add_parser("box")
    p.add_argument("feed")
    p.add_argument("label")
    p.add_argument("box")
    p.add_argument("--stage", choices=("pre", "post"), default="post",
                   help="snapped points before or after the feed's overrides")
    p.add_argument("--schedule", default="schedule.json")
    p = sub.add_parser("where")
    p.add_argument("feed")
    p.add_argument("label")
    p.add_argument("box")
    p.add_argument("--stage", choices=("pre", "post"), default="post")
    p.add_argument("--schedule", default="schedule.json")
    p = sub.add_parser("strokes")
    p.add_argument("feed")
    p.add_argument("box")
    p.add_argument("--rgb", help="stroke colour as r,g,b in 0..1, if not LEGEND_INK[feed]")
    p = sub.add_parser("trace")
    p.add_argument("feed")
    p.add_argument("points", nargs="+")
    style = p.add_mutually_exclusive_group()
    style.add_argument("--dashed", action="store_true", default=None)
    style.add_argument("--solid", dest="dashed", action="store_false")
    p.add_argument("--rgb", help="stroke colour as r,g,b in 0..1, if not LEGEND_INK[feed]")
    p.add_argument("--gap", type=float, default=4.0, help="widest hop between strokes, px")
    p.add_argument("--pad", type=float, default=30.0, help="px of ink kept round the points")
    p.add_argument("--tol", type=float, default=0.15, help="simplify tolerance, px")
    p = sub.add_parser("crop")
    p.add_argument("out")
    p.add_argument("box")
    p.add_argument("--shapes", help="schedule shape indices to draw")
    p.add_argument("--schedule", default="schedule.json")
    p.add_argument("--level", type=int, default=4, choices=(2, 4, 8))
    p.add_argument("--width", type=int, default=1800)
    a = ap.parse_args()
    if a.cmd == "fit":
        fit(a.feed)
    else:
        globals()[f"cmd_{a.cmd}"](a)


if __name__ == "__main__":
    main()
