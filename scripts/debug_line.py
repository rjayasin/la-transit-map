"""Render one route's cached (post-snap) path on top of the map, for eyeballing
where vehicles diverge from the drawn artwork.

The paths drawn are exactly what schedule.json feeds the animation — the poly2
warp plus whatever line snapping build_data.py managed — so anything that looks
off here is off in the browser too.

The background is drawn from the high-resolution WebP tile pyramid (tiles/),
so zoomed-in crops stay PDF-crisp instead of an upscaled map.png. The deepest
level whose output fits within a size cap is chosen automatically; pass --png
to fall back to map.png, or --level to force one.

A route that reaches downtown is drawn on both the main map and the rotated
call-out panel, and the two are snapped independently, so it writes both
images: debug_<system>_<line>.png and ..._inset.png. --inset draws only the
panel.

Usage:
    scripts/debug_line.py 720                # Metro Bus 720 (+ inset if any)
    scripts/debug_line.py 2 --system "Big"   # disambiguate a shared number
    scripts/debug_line.py 720 --stops        # + stop positions along the shape
    scripts/debug_line.py 720 --inset        # only the DTLA inset panel
    scripts/debug_line.py 720 --shape 3      # one variant only
    scripts/debug_line.py 720 --png          # cheap map.png background
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
TILEDIR = "tiles"
TILE = 512                 # tile edge, px (matches make_tiles.py)
TILE_LEVELS = (8, 4, 2)    # scale factors over the 4096px base, deepest first
OUT_CAP = 4000             # longest output edge before dropping to a shallower level
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
    pad = max(2, round(width * 0.4))
    dr.line(pts, fill=(0, 0, 0), width=width + 2 * pad, joint="curve")  # casing
    dr.line(pts, fill=color, width=width, joint="curve")


def draw_dot(dr, xy, color, r):
    x, y = xy
    pad = max(1, round(r * 0.25))
    dr.ellipse([x - r - pad, y - r - pad, x + r + pad, y + r + pad], fill=(0, 0, 0))
    dr.ellipse([x - r, y - r, x + r, y + r], fill=color)


def choose_level(box, forced=None):
    """Deepest tile level whose rendered crop stays under OUT_CAP px on its
    long edge, or None to signal the map.png fallback (tiles missing, --png,
    or a crop so large that even the shallowest level is oversized — e.g.
    --full, where PDF-crisp detail isn't the point)."""
    if forced == 1 or not os.path.isdir(TILEDIR):
        return None
    long_edge = max(box[2] - box[0], box[3] - box[1])
    if forced in TILE_LEVELS:
        return forced
    for lvl in TILE_LEVELS:
        if os.path.isdir(f"{TILEDIR}/{lvl}") and long_edge * lvl <= OUT_CAP:
            return lvl
    return None


def render_region(box, level):
    """Stitch the tile pyramid into the crop `box` (given in 4096px base
    coordinates) at `level`. A base coordinate p maps to this image at
    (p - box_origin) * level."""
    x0, y0, x1, y1 = (v * level for v in box)
    canvas = Image.new("RGB", (x1 - x0, y1 - y0), (255, 255, 255))
    for c in range(x0 // TILE, (x1 - 1) // TILE + 1):
        for r in range(y0 // TILE, (y1 - 1) // TILE + 1):
            path = f"{TILEDIR}/{level}/{c}_{r}.webp"
            if os.path.exists(path):
                canvas.paste(Image.open(path), (c * TILE - x0, r * TILE - y0))
    return canvas


def background(box, forced=None):
    """(image, scale) for the crop: the tile pyramid where it fits, else the
    map.png crop at 1:1."""
    level = choose_level(box, forced)
    if level is None:
        return Image.open(MAP).convert("RGB").crop(box), 1
    return render_region(box, level), level


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("line", help="route label as shown on the map, e.g. 720, A, R10")
    ap.add_argument("--system", help="substring of the system name, to disambiguate")
    ap.add_argument("--shape", type=int, help="draw only the Nth variant (see stdout)")
    ap.add_argument("--stops", action="store_true", help="mark stop positions")
    ap.add_argument("--inset", action="store_true",
                    help="only the DTLA inset panel (default draws it too, as a second file)")
    ap.add_argument("--full", action="store_true", help="whole map, no crop to the path")
    ap.add_argument("--width", type=int, default=5, help="path stroke width in base px (default 5)")
    ap.add_argument("--png", action="store_true", help="use map.png instead of the tile pyramid")
    ap.add_argument("--level", type=int, choices=(2, 4, 8),
                    help="force a tile level instead of choosing by crop size")
    ap.add_argument("-o", "--out",
                    help=f"output path (default {OUTDIR}/debug_<system>_<line>.png); "
                         "the inset panel gets the same name with _inset appended")
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

    slug = "".join(c if c.isalnum() else "-" for c in system.lower()).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")

    # A route through downtown is drawn twice on the sheet — once on the main
    # map and once, at a different scale and rotation, inside the call-out
    # panel — and the two are snapped separately, so a path can be right on one
    # and wrong on the other. Draw whichever panels this route actually appears
    # in, so a plain invocation never silently omits half the evidence.
    panels = [True] if a.inset else [False]
    if not a.inset and any(d["insets"][si] for si, _ in variants):
        panels.append(True)
    for inset in panels:
        out = a.out or os.path.join(OUTDIR, f"debug_{slug}_{a.line}.png")
        if inset and not (a.inset and a.out):   # an explicit -o for --inset
            root, ext = os.path.splitext(out)   # alone is taken at face value
            out = f"{root}_inset{ext}"
        render(d, a, variants, inset, out)


def render(d, a, variants, inset, out):
    """Draw the selected variants over one panel and write it to `out`."""
    # collect draw ops in 4096px base coordinates, then render once the crop
    # box and tile level are known
    paths, dots, drawn = [], [], []
    for n, (si, (ntrips, pi)) in enumerate(variants):
        color = COLORS[n % len(COLORS)]
        pat = d["patterns"][pi]
        if inset:
            for r, run in enumerate(d["insets"][si] or []):
                pts = pairs(run)
                paths.append((pts, color))
                drawn += pts
                if a.stops and pat and "ir" in pat:
                    for k, ir in enumerate(pat["ir"]):
                        if ir == r:
                            dots.append((point_at(pts, pat["id"][k]), color))
        else:
            pts = pairs(d["shapes"][si])
            paths.append((pts, color))
            drawn += pts
            if a.stops and pat:
                for dist in pat["d"]:
                    dots.append((point_at(pts, dist), color))

    if not drawn:
        sys.exit("nothing to draw" + (" — this route has no inset runs" if inset else ""))

    W, H = 4096, 4139
    if inset:
        box = tuple(d["insetRect"])
    elif a.full:
        box = (0, 0, W, H)
    else:
        xs = [p[0] for p in drawn]
        ys = [p[1] for p in drawn]
        box = (max(0, int(min(xs)) - PAD), max(0, int(min(ys)) - PAD),
               min(W, int(max(xs)) + PAD), min(H, int(max(ys)) + PAD))

    im, scale = background(box, forced=1 if a.png else a.level)
    dr = ImageDraw.Draw(im)
    width = max(2, min(a.width * scale, a.width * 4))
    radius = max(2, min(a.width * scale, a.width * 4))

    def to_px(p):
        return ((p[0] - box[0]) * scale, (p[1] - box[1]) * scale)

    for pts, color in paths:
        draw_path(dr, [to_px(p) for p in pts], color, width)
    for xy, color in dots:
        draw_dot(dr, to_px(xy), color, radius)

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    im.save(out)
    src = "map.png" if scale == 1 else f"tiles L{scale}"
    label = "inset" if inset else "main"
    print(f"{label:5s} crop {box} via {src} -> {out} ({im.width}x{im.height})")


if __name__ == "__main__":
    main()
