"""Fit a lat/lon -> map-pixel transform by aligning GTFS rail shapes
to the colored rail lines drawn on the system map.

Writes data/transform.json and a diagnostic overlay to scratch/ if given.
"""
import csv, json, os, sys
from collections import defaultdict

import numpy as np
from PIL import Image
from scipy.spatial import cKDTree
from scipy.optimize import least_squares

MAP = "map.png"
TILES = "tiles"
TILE = 512          # tile edge, px (matches make_tiles.py)
RAIL = "data/gtfs/gtfs_rail"

# What the map *draws* for each rail line, sampled off the tile pyramid along
# the line's georeferenced path. Mostly the GTFS route_color, but not always —
# the C line is printed a good deal lighter than its branding (88,167,56), far
# enough that keying the mask off GTFS missed the line entirely once the
# tolerance was tight. These are for finding artwork; vehicle sprites still
# take their fill from the feed.
ROUTE_COLORS = {
    "801": (2, 114, 186),    # A
    "802": (238, 30, 38),    # B
    "803": (110, 194, 102),  # C
    "804": (254, 186, 18),   # E
    "805": (158, 94, 166),   # D
    "807": (230, 114, 174),  # K
}
TOL = 60.0  # RGB euclidean tolerance, for masks sampled off map.png
MASK_LEVEL = 2    # tile pyramid level the rail masks are sampled at
MASK_TOL = 24.0   # RGB tolerance there, where the drawn colors are faithful

# Regions of the image to ignore (legend, insets, title banner), in pixels
EXCLUDE = [
    # Title banner. Its fill is within mask tolerance of Metro orange, so the
    # cut has to clear the band's antialiased edge (last tainted row is 707) —
    # but no further: it used to stop at 740, hiding 32 rows of live map along
    # with the badges on them, which cost Metro 690 the anchor at its Olive
    # View terminus and left the tail running up into the banner.
    (0, 0, 4096, 708),
    (3200, 2400, 4096, 3650),  # DTLA inset
    (2400, 3020, 3300, 3650),  # legend
    (180, 1600, 700, 1930),    # G line detour inset
    (180, 2150, 700, 2420),    # D line extension inset
]


def load_masks(level=MASK_LEVEL, tol=MASK_TOL):
    """KD-tree of the pixels drawing each rail line, in map-pixel coordinates.

    Sampled from the tile pyramid, not map.png. At 4096 px the narrower ribbons
    are two or three pixels wide and the downscale blends them with whatever
    runs alongside: the K line beside the C line through Aviation reads
    (201,112,155) against the (230,114,174) actually printed there, 35 away. A
    tolerance loose enough to keep that also admits a neighbouring agency's
    pink badge chip (43 away) — and the snap, finding the chip nearer than the
    line, took it, which is what bent the K line east of Aviation north of
    Mariposa. One pyramid level deeper the same line reads within 6 of its true
    color, so the mask both aims better and leaves room for a tolerance tight
    enough to keep foreign chips out.

    Read one tile row at a time: at level 2 the sheet is 8192 px across, and
    there is no reason to hold all of it at once. The result is memoized on
    disk, since it depends only on the tiles and this function."""
    import build_data                    # late: build_data imports this module
    stamp = (level, tol, ROUTE_COLORS, EXCLUDE,
             build_data.code_stamp(load_masks),
             build_data.art_stamp(f"{TILES}/{level}/0_0.webp"))
    out = build_data.cached_pixels(("rail", stamp), lambda: _rail_pixels(level, tol))
    trees = {}
    for rid in sorted(ROUTE_COLORS):
        pts = out[out[:, 2] == int(rid)][:, :2]
        print(f"route {rid}: {len(pts)} px")
        if len(pts) > 100:
            trees[rid] = cKDTree(pts / level)
    return trees


def _rail_pixels(level, tol):
    """Every rail-mask pixel as (x, y, route id), in tile pixels."""
    kept = []
    cols, rows = 0, 0
    while os.path.exists(f"{TILES}/{level}/{cols}_0.webp"):
        cols += 1
    while os.path.exists(f"{TILES}/{level}/0_{rows}.webp"):
        rows += 1
    if not cols or not rows:
        sys.exit(f"no tiles under {TILES}/{level} (run make_tiles.py)")
    for r in range(rows):
        stripe = [np.asarray(Image.open(f"{TILES}/{level}/{c}_{r}.webp").convert("RGB"))
                  for c in range(cols)]
        band = np.hstack(stripe).astype(np.int32)
        y0 = r * TILE
        keep = np.ones(band.shape[:2], dtype=bool)
        for ex0, ey0, ex1, ey1 in EXCLUDE:      # EXCLUDE is in map px
            keep[max(0, ey0 * level - y0):max(0, ey1 * level - y0),
                 ex0 * level:ex1 * level] = False
        for rid, rgb in ROUTE_COLORS.items():
            d2 = ((band - np.array(rgb)) ** 2).sum(axis=2)
            ys, xs = np.nonzero((d2 < tol * tol) & keep)
            if len(xs):
                kept.append(np.c_[xs, ys + y0, np.full(len(xs), int(rid))])
    return np.vstack(kept) if kept else np.zeros((0, 3), dtype=int)


def load_shapes():
    """Return {route_id: Nx2 array of (lon, lat)} decimated shape points."""
    shape_route = {}
    with open(f"{RAIL}/trips.txt") as f:
        for row in csv.DictReader(f):
            shape_route[row["shape_id"]] = row["route_id"]
    pts = defaultdict(list)
    with open(f"{RAIL}/shapes.txt") as f:
        for row in csv.DictReader(f):
            rid = shape_route.get(row["shape_id"])
            if rid:
                pts[(rid, row["shape_id"])].append(
                    (int(row["shape_pt_sequence"]), float(row["shape_pt_lon"]), float(row["shape_pt_lat"]))
                )
    by_route = defaultdict(set)
    for (rid, sid), p in pts.items():
        p.sort()
        for _, lon, lat in p[::4]:  # decimate
            by_route[rid].add((round(lon, 5), round(lat, 5)))
    return {rid: np.array(sorted(s)) for rid, s in by_route.items()}


def main():
    trees = load_masks()
    shapes = load_shapes()
    rids = [r for r in trees if r in shapes]
    lonlat = np.vstack([shapes[r] for r in rids])
    idx = np.concatenate([[i] * len(shapes[r]) for i, r in enumerate(rids)])
    tree_list = [trees[r] for r in rids]

    # initial guess from two eyeballed anchors (plate carree-ish)
    p0 = [4046.0, 0.0, 4046 * 118.1927 + 1930, 0.0, -4964.0, 4964 * 33.7681 + 3330]

    lon, lat = lonlat[:, 0], lonlat[:, 1]

    def residuals(p):
        a, b, c, d, e, f = p
        x = a * lon + b * lat + c
        y = d * lon + e * lat + f
        res = np.empty(len(x))
        for i, t in enumerate(tree_list):
            m = idx == i
            dist, _ = t.query(np.c_[x[m], y[m]])
            res[m] = dist
        return res

    r0 = residuals(p0)
    print(f"init: mean={r0.mean():.1f}px median={np.median(r0):.1f}px")
    sol = least_squares(residuals, p0, loss="soft_l1", f_scale=8.0, xtol=1e-10)
    r = residuals(sol.x)
    print(f"fit:  mean={r.mean():.1f}px median={np.median(r):.1f}px p90={np.percentile(r,90):.1f}px")
    # --- ICP refinement with 2nd-order polynomial warp ---
    # basis: [1, lon', lat', lon'^2, lon'*lat', lat'^2] on centered coords
    lon0, lat0 = lon.mean(), lat.mean()
    L, T = lon - lon0, lat - lat0
    B = np.c_[np.ones_like(L), L, T, L * L, L * T, T * T]

    a_, b_, c_, d_, e_, f2 = sol.x
    x, y = a_ * lon + b_ * lat + c_, d_ * lon + e_ * lat + f2
    CAP = 45.0
    for it in range(6):
        tx = np.empty(len(x)); ty = np.empty(len(x)); ok = np.zeros(len(x), bool)
        for i, t in enumerate(tree_list):
            m = idx == i
            dist, j = t.query(np.c_[x[m], y[m]])
            pts_m = t.data[j]
            tx[m], ty[m] = pts_m[:, 0], pts_m[:, 1]
            ok[m] = dist < CAP
        # ridge-regularized least squares on matched pairs
        Bm = B[ok]
        reg = np.diag([0, 0, 0, 1.0, 1.0, 1.0])
        A = Bm.T @ Bm + reg
        cx = np.linalg.solve(A, Bm.T @ tx[ok])
        cy = np.linalg.solve(A, Bm.T @ ty[ok])
        x, y = B @ cx, B @ cy
        err = np.hypot(x[ok] - tx[ok], y[ok] - ty[ok])
        print(f"icp {it}: matched {ok.sum()}/{len(x)} median={np.median(err):.1f}px p90={np.percentile(err,90):.1f}px")

    out = {"poly2": {"lon0": lon0, "lat0": lat0, "cx": list(cx), "cy": list(cy)},
           "affine": list(sol.x), "map_width": 4096, "map_height": 4139,
           "residual_median_px": float(np.median(err))}
    with open("data/transform.json", "w") as f:
        json.dump(out, f, indent=1)

    # diagnostic overlay
    im = Image.open(MAP).convert("RGB")
    px = im.load()
    for x_, y_ in zip(x, y):
        x, y = int(x_), int(y_)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if 0 <= x + dx < im.width and 0 <= y + dy < im.height:
                    px[x + dx, y + dy] = (255, 0, 255)
    im.save(sys.argv[1] if len(sys.argv) > 1 else "scratch_overlay.png")


if __name__ == "__main__":
    main()
