"""Fit a lat/lon -> pixel transform for the Downtown LA inset panel by
aligning GTFS rail shapes to the colored rail lines drawn inside the
inset frame (same approach as georef.py for the main map — the downtown
grid is drawn rotated, so a full affine + poly2 ICP is fitted).

Adds an "inset" entry to data/transform.json and writes a diagnostic
overlay if an output path is given.
"""
import json, sys

import numpy as np
from PIL import Image
from scipy.spatial import cKDTree
from scipy.optimize import least_squares

from georef import MAP, ROUTE_COLORS, TOL, load_shapes

# inset frame content area on the map, px (inside black border/banner)
RECT = (3292, 2506, 3850, 3706)
# station-platform legend box inside the frame: contains sample rail-line
# artwork in the real line colors — must not attract the fit
LEGEND = (3655, 3465, 3850, 3675)
# geographic bounds of what the inset depicts, from its edge streets:
# Beaudry/110 west, Washington south, Vignes east, Stadium Way north.
# Keep tight: the poly2 extrapolates unpredictably outside the frame (it can
# fold distant geography back inside the rect), so the builder rejects
# anything outside these bounds.
GEO = (-118.270, 34.017, -118.222, 34.074)

# anchor stations eyeballed on the inset artwork: lon, lat -> px
ANCHORS = [
    (-118.2365, 34.0562, 3770, 2670),   # Union Station
    (-118.2585, 34.0486, 3405, 3210),   # 7th St/Metro Center
    (-118.2359, 34.0639, 3608, 2535),   # Chinatown
]


def load_inset_masks():
    im = np.asarray(Image.open(MAP).convert("RGB"), dtype=np.int32)
    x0, y0, x1, y1 = RECT
    keep = np.zeros(im.shape[:2], dtype=bool)
    keep[y0:y1, x0:x1] = True
    lx0, ly0, lx1, ly1 = LEGEND
    keep[ly0:ly1, lx0:lx1] = False
    trees = {}
    for rid, (r, g, b) in ROUTE_COLORS.items():
        if rid == "807":
            continue                     # K line never reaches downtown
        d2 = (im[:, :, 0] - r) ** 2 + (im[:, :, 1] - g) ** 2 + (im[:, :, 2] - b) ** 2
        ys, xs = np.nonzero((d2 < TOL * TOL) & keep)
        print(f"route {rid}: {len(xs)} inset px")
        if len(xs) > 100:
            trees[rid] = cKDTree(np.c_[xs, ys])
    return trees


def main():
    trees = load_inset_masks()
    shapes = load_shapes()
    rids = [r for r in trees if r in shapes]
    parts, idx = [], []
    for i, r in enumerate(rids):
        s = shapes[r]
        m = ((s[:, 0] > GEO[0]) & (s[:, 0] < GEO[2]) &
             (s[:, 1] > GEO[1]) & (s[:, 1] < GEO[3]))
        parts.append(s[m])
        idx.append(np.full(m.sum(), i))
    lonlat = np.vstack(parts)
    idx = np.concatenate(idx)
    tree_list = [trees[r] for r in rids]
    print(f"fitting {len(lonlat)} shape points against {len(rids)} colors")

    # exact affine through the three anchors as the initial guess
    A = np.array([[lon, lat, 1] for lon, lat, _, _ in ANCHORS])
    px = np.linalg.solve(A, [a[2] for a in ANCHORS])
    py = np.linalg.solve(A, [a[3] for a in ANCHORS])
    p0 = [*px, *py]

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

    # trim to points the affine puts inside the frame — everything else
    # depicts track outside the inset and can only mis-match — and refit
    for _ in range(3):
        a, b, c, d, e, f = sol.x
        x, y = a * lon + b * lat + c, d * lon + e * lat + f
        m = ((x > RECT[0] - 20) & (x < RECT[2] + 20) &
             (y > RECT[1] - 20) & (y < RECT[3] + 20))
        lon, lat, idx = lon[m], lat[m], idx[m]
        sol = least_squares(residuals, sol.x, loss="soft_l1", f_scale=8.0, xtol=1e-10)
    r = residuals(sol.x)
    print(f"fit on {len(lon)} in-frame pts: median={np.median(r):.1f}px p90={np.percentile(r,90):.1f}px")

    # ICP refinement with 2nd-order polynomial warp (as in georef.py);
    # matched-pair trimming keeps the quadratic from chasing outliers
    lon0, lat0 = lon.mean(), lat.mean()
    L, T = lon - lon0, lat - lat0
    B = np.c_[np.ones_like(L), L, T, L * L, L * T, T * T]

    a_, b_, c_, d_, e_, f2 = sol.x
    x, y = a_ * lon + b_ * lat + c_, d_ * lon + e_ * lat + f2
    CAP = 20.0
    for it in range(6):
        tx = np.empty(len(x)); ty = np.empty(len(x)); ok = np.zeros(len(x), bool)
        for i, t in enumerate(tree_list):
            m = idx == i
            dist, j = t.query(np.c_[x[m], y[m]])
            pts_m = t.data[j]
            tx[m], ty[m] = pts_m[:, 0], pts_m[:, 1]
            ok[m] = dist < CAP
        Bm = B[ok]
        reg = np.diag([0, 0, 0, 1.0, 1.0, 1.0])
        Am = Bm.T @ Bm + reg
        cx = np.linalg.solve(Am, Bm.T @ tx[ok])
        cy = np.linalg.solve(Am, Bm.T @ ty[ok])
        x, y = B @ cx, B @ cy
        err = np.hypot(x[ok] - tx[ok], y[ok] - ty[ok])
        print(f"icp {it}: matched {ok.sum()}/{len(x)} median={np.median(err):.1f}px p90={np.percentile(err,90):.1f}px")

    with open("data/transform.json") as f:
        out = json.load(f)
    out["inset"] = {
        "poly2": {"lon0": lon0, "lat0": lat0, "cx": list(cx), "cy": list(cy)},
        "rect": list(RECT),
        "geo": list(GEO),
        "residual_median_px": float(np.median(err)),
    }
    with open("data/transform.json", "w") as f:
        json.dump(out, f, indent=1)
    print("wrote inset transform")

    if len(sys.argv) > 1:
        im = Image.open(MAP).convert("RGB")
        pxl = im.load()
        for x_, y_ in zip(x, y):
            xi, yi = int(x_), int(y_)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if 0 <= xi + dx < im.width and 0 <= yi + dy < im.height:
                        pxl[xi + dx, yi + dy] = (255, 0, 255)
        im.crop((3200, 2400, 3950, 3800)).save(sys.argv[1])
        print(f"overlay -> {sys.argv[1]}")


if __name__ == "__main__":
    main()
