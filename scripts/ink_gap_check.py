"""How far a system's paths sit from any drawn gray line.

    scripts/ink_gap_check.py                     # Pasadena Transit
    scripts/ink_gap_check.py "Big Blue Bus"      # any system name, or a prefix


PT is the one agency the build never snaps: its ink is plain gray, identical to
the street art, so LEGEND_SEEDS has no entry for it and the shapes keep the raw
polynomial warp. That makes "is it on the artwork at all" a question nobody has
asked, because the detour metric measures departures *from* the warp and here
the path *is* the warp — it scores zero by construction.

So this asks the cruder question the eye asks: is the bus on a drawn line of any
kind, or on blank paper? A path on the wrong line is a different fault and this
will not catch it; a path on no line is unambiguous.

Ink here is gray line work: low saturation, and mid-toned so that neither the
cream background nor the near-black label text counts.
"""
import json
import sys

import numpy as np
from PIL import Image
from scipy.ndimage import distance_transform_edt

Image.MAX_IMAGE_PIXELS = None
LUM = (130, 215)      # grey line work sits here; text is darker, paper lighter
SAT = 22              # max(RGB)-min(RGB); excludes the coloured agencies

a = np.asarray(Image.open("map.png").convert("RGB")).astype(np.int16)
lum = a.mean(axis=2)
sat = a.max(axis=2) - a.min(axis=2)
ink = (sat < SAT) & (lum >= LUM[0]) & (lum <= LUM[1])
print(f"grey ink: {ink.sum():,} px of {ink.size:,} ({100*ink.mean():.2f}%)", file=sys.stderr)

dist = distance_transform_edt(~ink)

d = json.load(open("schedule.json"))
want = sys.argv[1] if len(sys.argv) > 1 else "Pasadena Transit"
names = [n for n in d["systems"] if n.lower().startswith(want.lower())]
if not names:
    sys.exit(f"no system matching {want!r}; have: {', '.join(d['systems'])}")
print(f"system: {names[0]}", file=sys.stderr)
PT = d["systems"].index(names[0])
routes = {i: r for i, r in enumerate(d["routes"]) if r["sy"] == PT}
pats = {}
for t in d["trips"]:
    if t[0] in routes:
        pats.setdefault(t[0], set()).add(t[1])


def densify(p, step=2.0):
    out = []
    for i in range(len(p) - 1):
        (x0, y0), (x1, y1) = p[i], p[i + 1]
        n = max(1, int(np.hypot(x1 - x0, y1 - y0) / step))
        for k in range(n):
            out.append((x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n))
    out.append(tuple(p[-1]))
    return np.array(out)


rows = []
for ri, ps in sorted(pats.items()):
    for p in sorted(ps):
        s = d["patterns"][p]["s"]
        pts = densify(np.array(d["shapes"][s]).reshape(-1, 2))
        xi = np.clip(pts[:, 0].round().astype(int), 0, a.shape[1] - 1)
        yi = np.clip(pts[:, 1].round().astype(int), 0, a.shape[0] - 1)
        dd = dist[yi, xi]
        w = int(dd.argmax())
        rows.append((routes[ri]["n"], s, len(pts), float(np.median(dd)),
                     float(np.percentile(dd, 90)), float(dd.max()),
                     float(pts[w][0]), float(pts[w][1]),
                     float((dd > 8).mean()) * 100))

rows.sort(key=lambda r: -r[5])
print(f"{'route':>6} {'shape':>6} {'pts':>5} {'med':>6} {'p90':>6} {'max':>7} "
      f"{'>8px%':>6}  worst at")
for n, s, k, med, p90, mx, wx, wy, frac in rows:
    print(f"{n:>6} {s:>6} {k:>5} {med:6.1f} {p90:6.1f} {mx:7.1f} {frac:6.1f}  "
          f"({wx:.0f},{wy:.0f})")

allmed = np.median([r[3] for r in rows])
print(f"\n{len(rows)} shapes on {len(pats)} routes; median of medians {allmed:.1f} px")
