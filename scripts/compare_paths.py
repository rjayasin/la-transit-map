"""Render routes from two schedules side by side, before on the left and after
on the right, over the same crop of the map.

debug_line.py draws one schedule. This draws two, so a fix can be judged and
shown in one image. Paths are drawn thin and translucent so the sheet's own
line stays visible under them. Each variant gets its own colour, stops are
dots, and the path's first and last points are ringed green and red.

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

Writes one JPEG per route and an index.html listing them to --out.

Usage:
    scripts/compare_paths.py LS --system LADOT
    scripts/compare_paths.py --group Municipal --changed
    scripts/compare_paths.py --before git:HEAD~3 --changed --out scratch/cmp
    scripts/compare_paths.py --after scratch/refit_ladot.json --changed
    scripts/compare_paths.py --before old.json --after new.json --id 575,605
"""
import argparse
import html
import json
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import debug_line as DL  # noqa: E402

PAD = 60             # px of map kept around both paths
GAP = 12             # output px between the two panels
HEAD = 56            # output px of title band
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


def panel(d, ri, box, stops, level=None):
    im, scale = DL.background(box, forced=level)
    im = im.convert("RGBA")
    ov = Image.new("RGBA", im.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(ov)
    w = max(2, int(1.6 * scale))

    def px(p):
        return ((p[0] - box[0]) * scale, (p[1] - box[1]) * scale)

    vs = variants(d, ri)
    for n, (si, _) in enumerate(vs):
        dr.line([px(p) for p in DL.pairs(d["shapes"][si])],
                fill=COLORS[n] + (170,), width=w, joint="curve")
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
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    box = (max(0, int(min(xs)) - PAD), max(0, int(min(ys)) - PAD),
           min(4096, int(max(xs)) + PAD), min(4139, int(max(ys)) + PAD))
    if box[2] - box[0] < 1 or box[3] - box[1] < 1:
        return None
    left, scale, vb = panel(before, rb, box, a.stops)
    right, _, va = panel(after, ra, box, a.stops, level=scale)
    if left.width > a.width:
        f = a.width / left.width
        size = (a.width, round(left.height * f))
        left, right = left.resize(size, Image.LANCZOS), right.resize(size, Image.LANCZOS)
    out = Image.new("RGB", (left.width * 2 + GAP, left.height + HEAD), (255, 255, 255))
    out.paste(left, (0, HEAD))
    out.paste(right, (left.width + GAP, HEAD))
    dr = ImageDraw.Draw(out)
    big, small = font(34), font(26)
    dr.text((8, 8), title, fill=(0, 0, 0), font=big)
    for x0, name, colour, vs in ((0, "BEFORE", (200, 0, 0), vb),
                                 (left.width + GAP, "AFTER", (0, 140, 0), va)):
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
    return out


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
    ap.add_argument("--no-stops", dest="stops", action="store_false",
                    help="omit the stop dots")
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
        im = compare(before, after, kb[(system, rid)], ka[(system, rid)], title, a)
        if im is None:
            print(f"skip {title}: nothing on the sheet")
            continue
        name = f"{slug(system)}_{slug(rid)}.jpg"
        im.save(os.path.join(a.out, name), quality=82)
        rows.append((title, name))
        print(f"{title} -> {os.path.join(a.out, name)}")
    with open(os.path.join(a.out, "index.html"), "w") as f:
        f.write("<!doctype html><meta charset=utf-8><title>Path comparison</title>"
                "<style>body{font:15px sans-serif;margin:16px}img{max-width:100%}"
                "h2{font-size:17px;margin:24px 0 6px}</style>\n"
                f"<p>before: {html.escape(a.before)} &middot; after: {html.escape(a.after)}"
                f" &middot; {len(rows)} routes</p>\n")
        for title, name in rows:
            f.write(f"<h2>{html.escape(title)}</h2><img loading=lazy src=\"{name}\">\n")
    print(f"{len(rows)} routes -> {os.path.join(a.out, 'index.html')}")


if __name__ == "__main__":
    main()
