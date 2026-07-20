"""Render one route's cached (post-snap) path on top of the map, for eyeballing
where vehicles diverge from the drawn artwork.

The paths drawn are exactly what schedule.json feeds the animation — the poly2
warp plus whatever line snapping build_data.py managed — so anything that looks
off here is off in the browser too.

Usage:
    scripts/debug_line.py 720                # Metro Bus 720
    scripts/debug_line.py 2 --system "Big"   # disambiguate a shared number
    scripts/debug_line.py 720 --stops        # + stop positions along the shape
    scripts/debug_line.py 720 --inset        # the DTLA inset panel instead
    scripts/debug_line.py 720 --shape 3      # one variant only
"""
import argparse
import json
import os
import sys
from collections import defaultdict

from PIL import Image, ImageDraw

MAP = "map.png"
SCHEDULE = "schedule.json"
OUTDIR = "scratch"
PAD = 150          # px of map kept around the path's bounding box

# high-contrast against the map's muted palette; one per shape variant
COLORS = [
    (255, 0, 255), (0, 255, 255), (255, 255, 0), (0, 255, 0),
    (255, 128, 0), (128, 0, 255), (255, 0, 128), (0, 128, 255),
]


def load():
    for p in (MAP, SCHEDULE):
        if not os.path.exists(p):
            sys.exit(f"missing {p} (run from the repo root)")
    with open(SCHEDULE) as f:
        return json.load(f)


def find_route(d, label, system):
    """Route indices whose label matches, narrowed by a system substring."""
    hits = [i for i, r in enumerate(d["routes"]) if r["n"].lower() == label.lower()]
    if system:
        s = system.lower()
        hits = [i for i in hits if s in d["systems"][d["routes"][i]["sy"]].lower()]
    if not hits:
        sys.exit(f"no route labeled {label!r}"
                 + (f" in a system matching {system!r}" if system else ""))
    if len(hits) > 1:
        return pick_system(d, label, hits)
    return hits[0]


def pick_system(d, label, hits):
    """Ask which system's route to draw. Falls back to printing the --system
    flags and exiting when there's nobody to ask (piped, redirected, CI)."""
    names = [d["systems"][d["routes"][i]["sy"]] for i in hits]
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        print(f"{label!r} exists in several systems — pass --system:", file=sys.stderr)
        for n in names:
            print(f"  --system {n!r}", file=sys.stderr)
        sys.exit(2)
    print(f"Route {label!r} runs in several systems:", file=sys.stderr)
    for n, name in enumerate(names, 1):
        print(f"  {n:2d}) {name}", file=sys.stderr)
    while True:
        try:
            raw = input(f"select [1-{len(hits)}, or q to quit]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            sys.exit(2)
        if raw.lower() in ("q", "quit", "exit"):
            sys.exit(2)
        if raw.isdigit() and 1 <= int(raw) <= len(hits):
            return hits[int(raw) - 1]
        # also accept a unique substring of the system name
        m = [i for i, name in enumerate(names) if raw and raw.lower() in name.lower()]
        if len(m) == 1:
            return hits[m[0]]
        print("  not a valid choice", file=sys.stderr)


def shapes_for(d, ridx):
    """{shape index: (trip count, one pattern using it)}, busiest first."""
    trips_per_pattern = defaultdict(int)
    for t in d["trips"]:
        if t[0] == ridx:
            trips_per_pattern[t[1]] += 1
    per_shape = defaultdict(lambda: [0, None])
    for pi, n in trips_per_pattern.items():
        pat = d["patterns"][pi]
        if pat is None:
            continue
        e = per_shape[pat["s"]]
        e[0] += n
        if e[1] is None or n > trips_per_pattern[e[1]]:
            e[1] = pi
    return dict(sorted(per_shape.items(), key=lambda kv: -kv[1][0]))


def pairs(flat):
    return list(zip(flat[0::2], flat[1::2]))


def point_at(pts, dist):
    """Position `dist` px along a polyline (clamped at both ends)."""
    if dist <= 0:
        return pts[0]
    run = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        seg = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        if run + seg >= dist:
            t = (dist - run) / seg if seg else 0.0
            return (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
        run += seg
    return pts[-1]


def draw_path(dr, pts, color, width):
    dr.line(pts, fill=(0, 0, 0), width=width + 4, joint="curve")   # casing
    dr.line(pts, fill=color, width=width, joint="curve")


def draw_dot(dr, xy, color, r):
    x, y = xy
    dr.ellipse([x - r - 1, y - r - 1, x + r + 1, y + r + 1], fill=(0, 0, 0))
    dr.ellipse([x - r, y - r, x + r, y + r], fill=color)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("line", help="route label as shown on the map, e.g. 720, A, R10")
    ap.add_argument("--system", help="substring of the system name, to disambiguate")
    ap.add_argument("--shape", type=int, help="draw only the Nth variant (see stdout)")
    ap.add_argument("--stops", action="store_true", help="mark stop positions")
    ap.add_argument("--inset", action="store_true", help="draw the DTLA inset runs")
    ap.add_argument("--full", action="store_true", help="whole map, no crop to the path")
    ap.add_argument("--width", type=int, default=5, help="path stroke width (default 5)")
    ap.add_argument("-o", "--out", help=f"output path (default {OUTDIR}/debug_<line>.png)")
    a = ap.parse_args()

    d = load()
    ridx = find_route(d, a.line, a.system)
    route = d["routes"][ridx]
    system = d["systems"][route["sy"]]
    per_shape = shapes_for(d, ridx)
    if not per_shape:
        sys.exit(f"{route['n']} ({system}) has no shapes with trips")

    print(f"{route['n']}  {system}  color {route['c']}"
          f"  {'rail' if route['rail'] else 'bus'}")
    variants = list(per_shape.items())
    for n, (si, (ntrips, pi)) in enumerate(variants):
        npts = len(d["shapes"][si]) // 2
        runs = d["insets"][si]
        print(f"  [{n}] shape {si}: {ntrips} trips, {npts} pts"
              f"{f', {len(runs)} inset run(s)' if runs else ''}")
    if a.shape is not None:
        if not 0 <= a.shape < len(variants):
            sys.exit(f"--shape must be 0..{len(variants) - 1}")
        variants = [variants[a.shape]]

    im = Image.open(MAP).convert("RGB")
    dr = ImageDraw.Draw(im)
    drawn = []

    for n, (si, (ntrips, pi)) in enumerate(variants):
        color = COLORS[n % len(COLORS)]
        pat = d["patterns"][pi]
        if a.inset:
            for r, run in enumerate(d["insets"][si] or []):
                pts = pairs(run)
                draw_path(dr, pts, color, a.width)
                drawn += pts
                if a.stops and pat and "ir" in pat:
                    for k, ir in enumerate(pat["ir"]):
                        if ir == r:
                            draw_dot(dr, point_at(pts, pat["id"][k]), color, a.width)
        else:
            pts = pairs(d["shapes"][si])
            draw_path(dr, pts, color, a.width)
            drawn += pts
            if a.stops and pat:
                for dist in pat["d"]:
                    draw_dot(dr, point_at(pts, dist), color, a.width)

    if not drawn:
        sys.exit("nothing to draw" + (" — this route has no inset runs" if a.inset else ""))

    if a.inset:
        box = tuple(d["insetRect"])
    elif a.full:
        box = (0, 0, im.width, im.height)
    else:
        xs = [p[0] for p in drawn]
        ys = [p[1] for p in drawn]
        box = (max(0, int(min(xs)) - PAD), max(0, int(min(ys)) - PAD),
               min(im.width, int(max(xs)) + PAD), min(im.height, int(max(ys)) + PAD))
    im = im.crop(box)

    slug = "".join(c if c.isalnum() else "-" for c in system.lower()).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    out = a.out or os.path.join(
        OUTDIR, f"debug_{slug}_{a.line}{'_inset' if a.inset else ''}.png")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    im.save(out)
    print(f"crop {box} -> {out} ({im.width}x{im.height})")


if __name__ == "__main__":
    main()
