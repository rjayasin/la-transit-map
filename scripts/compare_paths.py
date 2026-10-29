"""Render routes from two schedules side by side, before on the left and after
on the right, over the same crop of the map.

debug_line.py draws one schedule. This draws two, so a fix can be judged and
shown in one image. Paths are drawn thin and translucent so the sheet's own
line stays visible under them. Each variant gets its own colour, and the
path's first and last points are ringed green and red.

What moved is marked on both panels. A stretch further than MOVED px from
every path of the route in the other schedule gets a halo, red before and
green after, and each cluster of such stretches is boxed and numbered in the
same place on both panels. The title counts the boxes, or says the paths
match and only the stop positions moved. `--stops` adds the stops as dots.

Routes are matched across the two files by (system, route id), so a refit
written by `build_data.py --only` compares against a full build: only the
routes the refit holds are drawn.

`--before` and `--after` take a file or `git:<rev>`, which reads that
revision's schedule.json without touching the working tree. The defaults
compare the committed schedule against the working copy.

Selecting routes (combine freely; with none given, every route is drawn):
    LABEL ...          route labels as printed on the map, e.g. LS 720
    --system TEXT      substring of the system name
    --id ID[,ID]       GTFS route ids
    --group TEXT       substring of a filter group, e.g. Municipal
    --changed          only routes whose shapes or stop distances differ

`--box X0,Y0,X1,Y1` crops to one stretch in map px instead of the whole
route. Writes one JPEG per route and an index.html listing them, routes with moved
paths first, to --out.

Usage:
    scripts/compare_paths.py LS --system LADOT
    scripts/compare_paths.py --group Municipal --changed
    scripts/compare_paths.py --before git:HEAD~3 --changed --out scratch/cmp
    scripts/compare_paths.py --after scratch/refit_ladot.json --changed
    scripts/compare_paths.py --before old.json --after new.json --id 575,605
    scripts/compare_paths.py 110 --system Metro --box 1690,2220,1840,2300
"""
import argparse
import html
import json
import os
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.spatial import cKDTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import debug_line as DL  # noqa: E402

PAD = 60             # px of map kept around both paths
GAP = 12             # output px between the two panels
HEAD = 56            # output px of title band
MOVED = 2.0          # px a path must stand off the other schedule's to count as moved
CLUSTER = 12.0       # px; moved points this close share one box
MIN_RUN = 3          # moved points (1 px apart) a box needs, so rounding is not a change
BOX_PAD = 8.0        # px of map round each box
BOX_MAX = 0.5        # share of the view a box may cover before only the halos mark it
HALO_BEFORE = (230, 40, 40)
HALO_AFTER = (20, 170, 60)
BOX = (255, 170, 0)
COLORS = [(255, 0, 255), (0, 200, 255), (255, 140, 0), (0, 220, 0),
          (140, 0, 255), (255, 0, 100), (0, 120, 255), (200, 200, 0)]


def font(size):
    for path in ("/System/Library/Fonts/Helvetica.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def load(spec):
    """A schedule from a path or from `git:<rev>`."""
    if spec.startswith("git:"):
        raw = subprocess.run(["git", "show", f"{spec[4:]}:schedule.json"],
                             capture_output=True, check=True).stdout
        return json.loads(raw)
    with open(spec) as f:
        return json.load(f)


def by_key(d):
    return {(d["systems"][r["sy"]], r["id"]): i for i, r in enumerate(d["routes"])}


def variants(d, ri):
    """[(shape index, (trips, busiest pattern))], busiest first, at most 8."""
    return list(DL.shapes_for(d, ri).items())[:len(COLORS)]


def signature(d, ri):
    """What the animation plays for a route: its shapes and stop distances."""
    return [(d["shapes"][si], d["patterns"][pi]["d"] if d["patterns"][pi] else None)
            for si, (_, pi) in variants(d, ri)]


def select(before, after, a):
    kb, ka = by_key(before), by_key(after)
    keys = [k for k in ka if k in kb]
    labels = {s.lower() for s in a.labels}
    ids = set(a.id.split(",")) if a.id else None
    group = None
    if a.group:
        g = next((g for g in after.get("groups", []) if a.group.lower() in g["n"].lower()), None)
        if g is None:
            sys.exit(f"no filter group matching {a.group!r}")
        group = {after["systems"][i] for i in g["sy"]}
    out = []
    for k in keys:
        r = after["routes"][ka[k]]
        if labels and r["n"].lower() not in labels:
            continue
        if a.system and a.system.lower() not in k[0].lower():
            continue
        if ids and k[1] not in ids:
            continue
        if group is not None and k[0] not in group:
            continue
        if a.changed and signature(before, kb[k]) == signature(after, ka[k]):
            continue
        out.append(k)
    return out, kb, ka


def dense(pts, step=1.0):
    """A polyline resampled every `step` px, as an (n, 2) array."""
    P = np.asarray(pts, float)
    if len(P) < 2:
        return P
    cum = np.r_[0, np.cumsum(np.hypot(*np.diff(P, axis=0).T))]
    t = np.arange(0, cum[-1] + step, step)
    return np.c_[np.interp(t, cum, P[:, 0]), np.interp(t, cum, P[:, 1])]


def route_points(d, ri):
    out = [dense(DL.pairs(d["shapes"][si])) for si, _ in variants(d, ri)]
    return np.vstack(out) if out else np.zeros((0, 2))


def moved_masks(d, ri, other):
    """Per variant, its densified points and which of them stand off `other`."""
    tree = cKDTree(other) if len(other) else None
    out = []
    for si, _ in variants(d, ri):
        P = dense(DL.pairs(d["shapes"][si]))
        far = (tree.query(P)[0] > MOVED) if tree is not None else np.ones(len(P), bool)
        out.append((P, far))
    return out


def change_boxes(masks):
    """Boxes round clusters of moved points, in map px, biggest first."""
    pts = [P[far] for side in masks for P, far in side if far.any()]
    if not pts:
        return []
    P = np.vstack(pts)
    parent = list(range(len(P)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i, j in cKDTree(P).query_pairs(CLUSTER):
        parent[root(i)] = root(j)
    groups = {}
    for i in range(len(P)):
        groups.setdefault(root(i), []).append(i)
    boxes = []
    for idx in groups.values():
        if len(idx) < MIN_RUN:
            continue
        Q = P[idx]
        boxes.append([*(Q.min(0) - BOX_PAD), *(Q.max(0) + BOX_PAD)])
    merged = True
    while merged:                      # padded boxes that overlap become one
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]:
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    return sorted(boxes, key=lambda b: -(b[2] - b[0]) * (b[3] - b[1]))


def panel(d, ri, box, masks, halo, stops, level=None):
    im, scale = DL.background(box, forced=level)
    im = im.convert("RGBA")
    ov = Image.new("RGBA", im.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(ov)
    w = max(2, int(1.6 * scale))

    def px(p):
        return ((p[0] - box[0]) * scale, (p[1] - box[1]) * scale)

    def runs(P, far):
        i = 0
        while i < len(far):
            if not far[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(far) and far[j + 1]:
                j += 1
            yield P[max(0, i - 1):j + 2]
            i = j + 1

    vs = variants(d, ri)
    for P, far in masks:               # halo under every moved stretch
        for run in runs(P, far):
            if len(run) >= 2:
                dr.line([px(p) for p in run], fill=halo + (150,), width=w * 4, joint="curve")
    for n, ((si, _), (P, far)) in enumerate(zip(vs, masks)):
        dr.line([px(p) for p in DL.pairs(d["shapes"][si])],
                fill=COLORS[n] + (110,), width=w, joint="curve")
        for run in runs(P, far):       # moved stretches drawn solid
            if len(run) >= 2:
                dr.line([px(p) for p in run], fill=COLORS[n] + (255,), width=w, joint="curve")
    for n, (si, (_, pi)) in enumerate(vs):
        pts = DL.pairs(d["shapes"][si])
        pat = d["patterns"][pi]
        if stops and pat:
            rr = max(3, int(1.3 * scale))
            for dist in pat["d"]:
                x, y = px(DL.point_at(pts, dist))
                dr.ellipse((x - rr, y - rr, x + rr, y + rr), fill=COLORS[n] + (255,),
                           outline=(0, 0, 0, 255))
        rr = max(6, int(3 * scale))
        for p, col in ((pts[0], (0, 160, 0)), (pts[-1], (220, 0, 0))):
            x, y = px(p)
            dr.ellipse((x - rr, y - rr, x + rr, y + rr), outline=col + (255,), width=max(2, w))
    return Image.alpha_composite(im, ov).convert("RGB"), scale, vs


def compare(before, after, rb, ra, title, a):
    pts = []
    for d, ri in ((before, rb), (after, ra)):
        for si, _ in variants(d, ri):
            pts += DL.pairs(d["shapes"][si])
    if not pts:
        return None, (0, "")
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    box = a.box or (max(0, int(min(xs)) - PAD), max(0, int(min(ys)) - PAD),
                    min(4096, int(max(xs)) + PAD), min(4139, int(max(ys)) + PAD))
    if box[2] - box[0] < 1 or box[3] - box[1] < 1:
        return None, (0, "")
    mb = moved_masks(before, rb, route_points(after, ra))
    ma = moved_masks(after, ra, route_points(before, rb))
    boxes = change_boxes([mb, ma])
    if boxes:
        status = f"{len(boxes)} changed area{'s' if len(boxes) > 1 else ''}"
    elif signature(before, rb) != signature(after, ra):
        status = "paths match; stop positions moved"
    else:
        status = "no change"
    left, scale, vb = panel(before, rb, box, mb, HALO_BEFORE, a.stops)
    right, _, va = panel(after, ra, box, ma, HALO_AFTER, a.stops, level=scale)
    f = 1.0
    if left.width > a.width:
        f = a.width / left.width
        size = (a.width, round(left.height * f))
        left, right = left.resize(size, Image.LANCZOS), right.resize(size, Image.LANCZOS)
    out = Image.new("RGB", (left.width * 2 + GAP, left.height + HEAD), (255, 255, 255))
    out.paste(left, (0, HEAD))
    out.paste(right, (left.width + GAP, HEAD))
    dr = ImageDraw.Draw(out)
    big, small = font(34), font(26)
    view = (box[2] - box[0]) * (box[3] - box[1])
    k = scale * f                      # map px to output px
    for n, b in enumerate(boxes, 1):
        if (b[2] - b[0]) * (b[3] - b[1]) > BOX_MAX * view:
            continue
        for x0 in (0, left.width + GAP):
            r = (x0 + (b[0] - box[0]) * k, HEAD + (b[1] - box[1]) * k,
                 x0 + (b[2] - box[0]) * k, HEAD + (b[3] - box[1]) * k)
            dr.rectangle(r, outline=BOX, width=4)
            t = str(n)
            tw = dr.textlength(t, font=small)
            ty = max(HEAD, r[1] - 32)
            dr.rectangle((r[0], ty, r[0] + tw + 12, ty + 32), fill=BOX)
            dr.text((r[0] + 6, ty + 2), t, fill=(0, 0, 0), font=small)
    dr.text((8, 8), f"{title}: {status}", fill=(0, 0, 0), font=big)
    for x0, name, colour, vs in ((0, "BEFORE", HALO_BEFORE, vb),
                                 (left.width + GAP, "AFTER", HALO_AFTER, va)):
        dr.rectangle((x0 + 4, HEAD + 4, x0 + 12 + dr.textlength(name, font=big), HEAD + 42),
                     fill=(255, 255, 255))
        dr.text((x0 + 8, HEAD + 6), name, fill=colour, font=big)
        y = HEAD + 48
        for n, (si, (nt, _)) in enumerate(vs):
            t = f"variant {n}: shape {si}, {nt} trips"
            dr.rectangle((x0 + 4, y, x0 + 12 + dr.textlength(t, font=small), y + 30),
                         fill=(255, 255, 255))
            dr.text((x0 + 8, y), t, fill=COLORS[n], font=small)
            y += 32
    return out, (len(boxes), status)


def slug(s):
    s = "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("labels", nargs="*", help="route labels as printed on the map")
    ap.add_argument("--before", default="git:HEAD",
                    help="schedule file or git:<rev> (default git:HEAD)")
    ap.add_argument("--after", default=DL.SCHEDULE,
                    help=f"schedule file or git:<rev> (default {DL.SCHEDULE})")
    ap.add_argument("--system", help="substring of the system name")
    ap.add_argument("--id", help="GTFS route ids, comma-separated")
    ap.add_argument("--group", help="substring of a filter group name")
    ap.add_argument("--changed", action="store_true",
                    help="only routes whose shapes or stop distances differ")
    ap.add_argument("--box", type=lambda v: tuple(int(float(q)) for q in v.split(",")),
                    help="crop X0,Y0,X1,Y1 in map px instead of the whole route")
    ap.add_argument("--stops", action="store_true",
                    help="draw each variant's stops as dots")
    ap.add_argument("--width", type=int, default=1100,
                    help="max px per panel (default 1100)")
    ap.add_argument("--out", default=os.path.join(DL.OUTDIR, "compare"),
                    help="output directory (default scratch/compare)")
    a = ap.parse_args()

    before, after = load(a.before), load(a.after)
    keys, kb, ka = select(before, after, a)
    if not keys:
        sys.exit("no routes match")
    os.makedirs(a.out, exist_ok=True)
    rows = []
    for system, rid in keys:
        r = after["routes"][ka[(system, rid)]]
        title = f"{system} {r['n']} (route {rid})"
        im, (areas, status) = compare(before, after, kb[(system, rid)], ka[(system, rid)],
                                      title, a)
        if im is None:
            print(f"skip {title}: nothing on the sheet")
            continue
        name = f"{slug(system)}_{slug(rid)}.jpg"
        im.save(os.path.join(a.out, name), quality=82)
        rows.append((-areas, len(rows), title, status, name))
        print(f"{title}: {status} -> {os.path.join(a.out, name)}")
    rows.sort()
    with open(os.path.join(a.out, "index.html"), "w") as f:
        f.write("<!doctype html><meta charset=utf-8><title>Path comparison</title>"
                "<style>body{font:15px sans-serif;margin:16px}img{max-width:100%}"
                "h2{font-size:17px;margin:24px 0 6px}</style>\n"
                f"<p>before: {html.escape(a.before)} &middot; after: {html.escape(a.after)}"
                f" &middot; {len(rows)} routes</p>\n")
        for _, _, title, status, name in rows:
            f.write(f"<h2>{html.escape(title)}: {html.escape(status)}</h2>"
                    f"<img loading=lazy src=\"{name}\">\n")
    print(f"{len(rows)} routes -> {os.path.join(a.out, 'index.html')}")


if __name__ == "__main__":
    main()
