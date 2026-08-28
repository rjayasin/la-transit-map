"""Fit a lat/lon -> pixel transform for the Downtown LA inset panel.

The panel is not a picture of downtown, it is a *rectified* drawing of it: the
real grid is rotated some 36 degrees off north and the sheet redraws it square,
spacing the streets to suit the page rather than the ground. No affine can do
that, and a poly2 fitted on the rail lines only bends where the rail is — it
came out a whole block off along 5th and 6th, which is enough to put an
east-west route on the wrong one of a one-way pair.

So the transform is fitted as the drawing is drawn: separably, in the grid's
own two axes. Ground positions are projected onto the axis a numbered street
runs along and the axis across it; each named street and avenue then gives one
breakpoint tying its grid coordinate to the pixel row or column the sheet draws
it on, and the map between them is a monotone interpolation. Every street the
sheet names lands on its own ink by construction, and the streets between are
carried along in order.

The breakpoints come from the sheet itself: long runs of drawn ink are found in
the panel, named by the street labels printed along them, and crossed with each
other to give corners that GTFS also names a stop for.

Adds an "inset" entry to data/transform.json and writes a diagnostic overlay if
an output path is given.
"""
import csv, json, re, sys
from collections import defaultdict

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

from georef import MAP, TOL

PDF = "26-1720_blt_system_map_47x47.5-2.pdf"
GTFS = "data/gtfs"
# inset frame content area on the map, px (inside black border/banner)
RECT = (3292, 2506, 3850, 3706)
# station-platform legend box inside the frame: contains sample rail-line
# artwork in the real line colors — must not attract the fit.
#
# Measured off the tile pyramid rather than eyeballed: the box's white interior
# is x 3663.5-3808.6, y 3471.4-3649.4, and this is that plus its black border
# and two px of slack. It used to run to (3850, 3675), the frame's own right
# edge and 26 px below the box, and what that swallowed was live artwork — the
# A Line's run east along Washington Blvd sits at y 3663, and the "San Pedro"
# platform east of the box at x 3820. With them cut out of the mask the A had
# nothing to snap its south-east end onto, and every point of it piled up on
# the last blue pixel west of the box. Widening the exclusion is only ever
# conservative for the *fit* in this file, so the stored transform stands.
LEGEND = (3657, 3465, 3815, 3656)
# geographic bounds of what the inset depicts, from its edge streets:
# Beaudry/110 west, Washington south, Vignes east, Stadium Way north. Keep
# tight: past the outermost street the fit is calibrated on there is nothing
# holding the extrapolation, so the builder rejects anything outside these.
GEO = (-118.270, 34.017, -118.222, 34.074)

ORANGE = (217, 129, 83)   # the local-bus ink, which is drawn along every street
INK_TOL = 38.0     # RGB, as the builder reads the same ink
RUN = 41           # px of unbroken ink before it counts as a drawn street
GROW = 7.0         # px either side of a line, gathering the rest of its ink
LABEL_NEAR = 26.0  # px a street label may stand from the line it names
MIN_CORNERS = 3    # corners a street needs before it may set a breakpoint
LAT0 = 34.05       # the panel's latitude, for scaling longitude to the ground

# What the sheet prints along a line, and the words a stop name uses for it.
# Only where the printed word isn't enough on its own.
FULL = {"LOS": ("LOS", "ANGELES"), "SAN": ("SAN", "PEDRO"),
        "NEW": ("NEW", "HIGH"), "CESAR": ("CESAR", "CHAVEZ")}
STREETS = ["TEMPLE", "ARCADIA", "ALISO", "1ST", "2ND", "3RD", "4TH", "5TH",
           "6TH", "7TH", "8TH", "9TH", "OLYMPIC", "11TH", "PICO", "VENICE",
           "WASHINGTON", "CESAR", "ALPINE", "COLLEGE"]
AVENUES = ["BEAUDRY", "FIGUEROA", "FLOWER", "HOPE", "GRAND", "OLIVE", "HILL",
           "BROADWAY", "SPRING", "MAIN", "LOS", "MAPLE", "SAN", "WALL",
           "SANTEE", "CENTRAL", "ALAMEDA", "VIGNES", "NEW"]



def panel_ink(tol=INK_TOL):
    """The panel's local-bus ink as a boolean image, and its origin."""
    Image.MAX_IMAGE_PIXELS = None
    im = np.asarray(Image.open(MAP).convert("RGB"), dtype=np.int32)
    x0, y0, x1, y1 = RECT
    sub = im[y0:y1, x0:x1]
    m = ((sub - np.array(ORANGE)) ** 2).sum(2) < tol * tol
    lx0, ly0, lx1, ly1 = LEGEND
    m[ly0 - y0:ly1 - y0, lx0 - x0:lx1 - x0] = False
    return m


def panel_words():
    """{word: [(x, y) map px]} for the text the sheet sets inside the frame."""
    import fitz
    page = fitz.open(PDF)[0]
    Image.MAX_IMAGE_PIXELS = None
    s = Image.open(MAP).size[0] / page.rect.width
    x0, y0, x1, y1 = RECT
    out = defaultdict(list)
    for wx0, wy0, wx1, wy1, w, *_ in page.get_text("words"):
        cx, cy = (wx0 + wx1) / 2 * s, (wy0 + wy1) / 2 * s
        if x0 <= cx < x1 and y0 <= cy < y1:
            out[w.strip().upper()].append((cx, cy))
    return dict(out)


def ink_runs(mask, axis):
    """Every long run of ink, as the line it lies on: a first-order fit of the
    coordinate across the run against the one along it, plus that run's extent.

    Opened along the axis first, so a street survives and the words, chips and
    corners crossing it do not."""
    k = np.ones((1, RUN) if axis == "h" else (RUN, 1), bool)
    lab, _ = ndi.label(ndi.binary_opening(mask, k), structure=np.ones((3, 3), bool))
    x0, y0 = RECT[0], RECT[1]
    out = []
    for i, sl in enumerate(ndi.find_objects(lab), 1):
        sub = lab[sl] == i
        yy, xx = np.nonzero(sub)
        X, Y = xx + sl[1].start + x0, yy + sl[0].start + y0
        a, b = (X, Y) if axis == "h" else (Y, X)
        if a.max() - a.min() < RUN:
            continue
        out.append((np.polyfit(a, b, 1), a.min(), a.max()))
    return out


def line_of(fits, label, words, ink_xy, axis):
    """The drawn line the sheet writes `label` alongside, refitted over all the
    ink lying along it — the opening leaves a street in fragments, and a
    fragment is too short to say which way the whole line runs."""
    best = None
    for lx, ly in words.get(label, ()):
        a, b = (lx, ly) if axis == "h" else (ly, lx)
        for f, lo, hi in fits:
            # only a run that reaches the label: a short fragment's slope says
            # nothing about where its line is a hundred px away
            if not lo - 20 <= a <= hi + 20:
                continue
            d = abs(np.polyval(f, a) - b)
            if d < LABEL_NEAR and (best is None or d < best[1]):
                best = (f, d, a, b)
    if best is None:
        return None
    f, _, la_, lb_ = best
    a, b = (ink_xy[0], ink_xy[1]) if axis == "h" else (ink_xy[1], ink_xy[0])
    for _ in range(3):
        m = np.abs(np.polyval(f, a) - b) < GROW
        if m.sum() < RUN:
            return None
        f = np.polyfit(a[m], b[m], 1)
    # the growth must not have walked the line off the label that named it
    return None if abs(np.polyval(f, la_) - lb_) > LABEL_NEAR else f


def one_each(named, words, axis):
    """At most one name per drawn line. Two labels landing on one line means a
    street whose own ink the opening lost, and the loser would set a breakpoint
    on a line it is nowhere near."""
    keep = {}
    for nm, f in named.items():
        at = (RECT[0] + RECT[2]) / 2 if axis == "h" else (RECT[1] + RECT[3]) / 2
        c = np.polyval(f, at)
        rival = next((k for k, g in keep.items() if abs(np.polyval(g, at) - c) < 12), None)
        if rival is None:
            keep[nm] = f
            continue
        def gap(n, g):
            return min((abs(np.polyval(g, x if axis == "h" else y) - (y if axis == "h" else x))
                        for x, y in words.get(n, ())), default=1e9)
        if gap(nm, f) < gap(rival, keep[rival]):
            del keep[rival]
            keep[nm] = f
    return keep


def cross(fs, fa):
    """Where a street y=fs(x) meets an avenue x=fa(y)."""
    x, y = np.polyval(fa, RECT[1]), None
    for _ in range(8):
        y = np.polyval(fs, x)
        x = np.polyval(fa, y)
    return x, y


def stop_words():
    """(word set, lon, lat) for every stop downtown.

    The feeds name a corner half a dozen ways — "6TH / MAIN", "MAIN ST & 6TH
    ST", "CESAR E CHAVEZ / BROADWAY", some with a direction in brackets — so a
    corner is matched on the words a stop name uses rather than on its form.
    Downtown only: half the county has a 1st and a Main."""
    out = []
    for feed in ("gtfs_bus", "ladot"):
        path = f"{GTFS}/{feed}/stops.txt"
        try:
            f = open(path, encoding="utf-8-sig")
        except OSError:
            continue
        with f:
            for r in csv.DictReader(f):
                lo, la = float(r["stop_lon"]), float(r["stop_lat"])
                if GEO[0] < lo < GEO[2] and GEO[1] < la < GEO[3]:
                    out.append((set(re.findall(r"[A-Z0-9]+",
                                               r["stop_name"].upper())), lo, la))
    return out


def corners():
    """(lon, lat, x, y, "STREET/AVENUE") for every corner both name."""
    mask = panel_ink()
    ys, xs = np.nonzero(mask)
    ink = (xs + RECT[0], ys + RECT[1])
    words = panel_words()
    H, V = ink_runs(mask, "h"), ink_runs(mask, "v")
    st = {s: line_of(H, s, words, ink, "h") for s in STREETS}
    av = {a: line_of(V, a, words, ink, "v") for a in AVENUES}
    st = one_each({k: v for k, v in st.items() if v is not None}, words, "h")
    av = one_each({k: v for k, v in av.items() if v is not None}, words, "v")
    print(f"named {len(st)} streets, {len(av)} avenues")
    stops = stop_words()
    out = []
    for s, fs in st.items():
        for a, fa in av.items():
            x, y = cross(fs, fa)
            if not (RECT[0] < x < RECT[2] and RECT[1] < y < RECT[3]):
                continue
            sw, aw = set(FULL.get(s, (s,))), set(FULL.get(a, (a,)))
            hit = [(lo, la) for w, lo, la in stops if sw <= w and aw <= w]
            if hit:
                lo, la = np.median(hit, axis=0)   # both kerbs, and both ways
                out.append((float(lo), float(la), x, y, f"{s}/{a}"))
    return out, st, av


def grid_axes(pairs, keys):
    """The unit ground direction the numbered streets run in, read off the
    corners each of them owns."""
    vs = []
    for s in keys:
        P = np.array([(p[0] * np.cos(np.radians(LAT0)), p[1])
                      for p in pairs if p[4].split("/")[0] == s])
        if len(P) < 2:
            continue
        v = np.linalg.svd(P - P.mean(0), full_matrices=False)[2][0]
        vs.append(v * np.sign(v[0] or 1.0))
    v = np.mean(vs, axis=0)
    return v / np.hypot(*v)


def breakpoints(pairs, coord, drawn, group, names):
    """One (grid coordinate, drawn pixel) per named line, from its corners.

    Medians, so a corner whose stop the feed put on the wrong block moves
    neither end. A line with only a corner or two to speak for it is left out
    rather than allowed to set the spacing on its own."""
    out = []
    for nm in names:
        m = np.array([p[4].split("/")[group] == nm for p in pairs])
        if m.sum() >= MIN_CORNERS:
            out.append((float(np.median(coord[m])), float(np.median(drawn[m])),
                        nm, int(m.sum())))
    out.sort()
    return out


def interp(v, table):
    """A grid coordinate through the breakpoints, in pixels.

    Linear past either end on the spacing of the last pair, which keeps the
    edge of the frame finite — the builder rejects anything past GEO anyway,
    and a curve fitted here would fold distant ground back inside the rect."""
    c = np.array([r[0] for r in table]); p = np.array([r[1] for r in table])
    out = np.interp(v, c, p)
    lo, hi = v < c[0], v > c[-1]
    if lo.any():
        out[lo] = p[0] + (v[lo] - c[0]) * (p[1] - p[0]) / (c[1] - c[0])
    if hi.any():
        out[hi] = p[-1] + (v[hi] - c[-1]) * (p[-1] - p[-2]) / (c[-1] - c[-2])
    return out


def main():
    pairs, st_lines, av_lines = corners()
    print(f"{len(pairs)} corners the sheet and the feeds both name")
    lon = np.array([p[0] for p in pairs]); lat = np.array([p[1] for p in pairs])
    X = np.array([p[2] for p in pairs]); Y = np.array([p[3] for p in pairs])
    lon0, lat0 = float(lon.mean()), float(lat.mean())
    e = grid_axes(pairs, st_lines)
    ea = np.array([-e[1], e[0]])
    k = float(np.cos(np.radians(LAT0)))

    def coords(lo, la):
        g = np.c_[(lo - lon0) * k, la - lat0]
        return g @ e, g @ ea

    s, t = coords(lon, lat)
    ave = breakpoints(pairs, s, X, 1, av_lines)
    sts = breakpoints(pairs, t, Y, 0, st_lines)
    print("\navenue breakpoints (grid s -> panel x):")
    for c, p, nm, n in ave:
        print(f"   {nm:10s} {c:+.5f} -> {p:7.1f}  ({n} corners)")
    print("street breakpoints (grid t -> panel y):")
    for c, p, nm, n in sts:
        print(f"   {nm:10s} {c:+.5f} -> {p:7.1f}  ({n} corners)")

    err = np.hypot(interp(s, ave) - X, interp(t, sts) - Y)
    ok = err < 60.0            # a corner the feed names somewhere else entirely
    print(f"\nresidual on {ok.sum()} of {len(err)} corners: "
          f"median {np.median(err[ok]):.1f} px  p90 {np.percentile(err[ok], 90):.1f}  "
          f"max {err[ok].max():.1f}")
    for i in np.argsort(-err)[:5]:
        print(f"   worst {pairs[i][4]:16s} {err[i]:8.1f} px")

    with open("data/transform.json") as f:
        out = json.load(f)
    out["inset"] = {
        "grid": {"lon0": lon0, "lat0": lat0, "lon_scale": k,
                 "along": [float(v) for v in e],
                 "x": [[c, p] for c, p, _, _ in ave],
                 "y": [[c, p] for c, p, _, _ in sts]},
        "rect": list(RECT),
        "geo": list(GEO),
        "residual_median_px": float(np.median(err[ok])),
    }
    with open("data/transform.json", "w") as f:
        json.dump(out, f, indent=1)
    print("wrote inset transform")

    if len(sys.argv) > 1:
        im = Image.open(MAP).convert("RGB")
        d = __import__("PIL.ImageDraw", fromlist=["ImageDraw"]).Draw(im)
        for nm, f in st_lines.items():
            d.line([(RECT[0], np.polyval(f, RECT[0])),
                    (RECT[2], np.polyval(f, RECT[2]))], fill=(0, 140, 255), width=2)
        for nm, f in av_lines.items():
            d.line([(np.polyval(f, RECT[1]), RECT[1]),
                    (np.polyval(f, RECT[3]), RECT[3])], fill=(0, 190, 0), width=2)
        for i, (_, _, x, y, _) in enumerate(pairs):
            c = (255, 0, 255) if ok[i] else (255, 140, 0)
            d.ellipse([x - 5, y - 5, x + 5, y + 5], outline=c, width=2)
        im.crop((RECT[0] - 40, RECT[1] - 40, RECT[2] + 40, RECT[3] + 40)).save(sys.argv[1])
        print(f"overlay -> {sys.argv[1]}")


if __name__ == "__main__":
    main()
