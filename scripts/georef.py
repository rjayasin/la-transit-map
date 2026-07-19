"""Fit a lat/lon -> map-pixel transform by aligning GTFS rail shapes
to the colored rail lines drawn on the system map.

Writes data/transform.json and a diagnostic overlay to scratch/ if given.
"""
import csv, json, sys
from collections import defaultdict

import numpy as np
from PIL import Image
from scipy.spatial import cKDTree
from scipy.optimize import least_squares

MAP = "web/map.png"
RAIL = "data/gtfs/gtfs_rail"

# GTFS route colors for the six rail lines
ROUTE_COLORS = {
    "801": (0x00, 0x72, 0xBC),  # A
    "802": (0xEB, 0x13, 0x1B),  # B
    "803": (0x58, 0xA7, 0x38),  # C
    "804": (0xFD, 0xB9, 0x13),  # E
    "805": (0xA0, 0x5D, 0xA5),  # D
    "807": (0xE5, 0x6D, 0xB1),  # K
}
TOL = 60.0  # RGB euclidean tolerance

# Regions of the image to ignore (legend, insets, title banner), in pixels
EXCLUDE = [
    (0, 0, 4096, 740),        # title banner
    (3200, 2400, 4096, 3650),  # DTLA inset
    (2400, 3020, 3300, 3650),  # legend
    (180, 1600, 700, 1930),    # G line detour inset
    (180, 2150, 700, 2420),    # D line extension inset
]


def load_masks():
    im = np.asarray(Image.open(MAP).convert("RGB"), dtype=np.int32)
    h, w, _ = im.shape
    keep = np.ones((h, w), dtype=bool)
    for x0, y0, x1, y1 in EXCLUDE:
        keep[y0:y1, x0:x1] = False
    trees = {}
    for rid, (r, g, b) in ROUTE_COLORS.items():
        d2 = (im[:, :, 0] - r) ** 2 + (im[:, :, 1] - g) ** 2 + (im[:, :, 2] - b) ** 2
        ys, xs = np.nonzero((d2 < TOL * TOL) & keep)
        print(f"route {rid}: {len(xs)} px")
        if len(xs) > 100:
            trees[rid] = cKDTree(np.c_[xs, ys])
    return trees


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
