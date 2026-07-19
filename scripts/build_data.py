"""Build schedule.json from all cached GTFS feeds in data/gtfs/.

- Gathers every trip active on the target service date (Wed 2026-07-22);
  feeds whose calendar doesn't cover it fall back to their busiest Wednesday.
- Projects shapes into map pixel space via data/transform.json (poly2 warp).
- Metro rail shapes are additionally snapped onto the drawn line pixels.
- Stops are projected onto shapes to get distance-along-shape per stop.
- Emits compact JSON: routes, shapes (px polylines), patterns (stop dists),
  trips (route, pattern, stop arrival times).

Trips crossing midnight (times >= 24:00) are also emitted shifted by -24h so
the after-midnight portion of "yesterday's" service appears at the start of
the simulated day.
"""
import csv, json, math, os, sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import numpy as np
from PIL import Image
from scipy.spatial import cKDTree

sys.path.insert(0, "scripts")
from georef import EXCLUDE, load_masks  # noqa: E402

TARGET = date(2026, 7, 22)  # a Wednesday inside the Metro JUNE26 calendar window
GTFS = "data/gtfs"
# gtfs_rail first so Metro trains draw config (snap) is applied; order otherwise cosmetic
FEEDS = ["gtfs_rail", "gtfs_bus", "bigbluebus", "culvercity", "ladot", "longbeach",
         "foothill", "torrance", "norwalk", "montebello", "gtrans", "pasadena",
         "burbank", "beachcities", "metrolink"]
METRO_BUS_COLOR, METRO_BUS_TEXT = "E16710", "FFFFFF"
FALLBACK_COLOR, FALLBACK_TEXT = "888888", "FFFFFF"

with open("data/transform.json") as f:
    TR = json.load(f)["poly2"]


def to_px(lon, lat):
    L, T = lon - TR["lon0"], lat - TR["lat0"]
    B = np.c_[np.ones_like(L), L, T, L * L, L * T, T * T]
    return B @ TR["cx"], B @ TR["cy"]


def read_csv(feed, name):
    path = f"{GTFS}/{feed}/{name}"
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def active_services(feed, d):
    ds = d.strftime("%Y%m%d")
    dow = d.strftime("%A").lower()
    active = set()
    for row in read_csv(feed, "calendar.txt"):
        if row.get(dow) == "1" and row["start_date"] <= ds <= row["end_date"]:
            active.add(row["service_id"])
    for row in read_csv(feed, "calendar_dates.txt"):
        if row["date"] == ds:
            (active.add if row["exception_type"] == "1" else active.discard)(row["service_id"])
    return active


def pick_date(feed, trips_per_service):
    """TARGET if it has service; else the busiest Wednesday the feed covers."""
    def score(d):
        return sum(trips_per_service.get(s, 0) for s in active_services(feed, d))
    if score(TARGET) > 0:
        return TARGET
    cands = set()
    for row in read_csv(feed, "calendar.txt"):
        d0 = datetime.strptime(row["start_date"], "%Y%m%d").date()
        d1 = datetime.strptime(row["end_date"], "%Y%m%d").date()
        d = d0 + timedelta(days=(2 - d0.weekday()) % 7)  # first Wednesday
        while d <= d1 and len(cands) < 400:
            cands.add(d)
            d += timedelta(days=7)
    for row in read_csv(feed, "calendar_dates.txt"):
        d = datetime.strptime(row["date"], "%Y%m%d").date()
        if d.weekday() == 2 and row["exception_type"] == "1":
            cands.add(d)
    best = max(cands, key=lambda d: (score(d), -abs((d - TARGET).days)), default=None)
    return best if best and score(best) > 0 else None


def parse_time(s):
    parts = s.split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2] if len(parts) > 2 else 0)


def route_label(short, long_name):
    s = (short or long_name or "?").strip()
    for pre in ("Metro ", "Metrolink "):
        if s.startswith(pre):
            s = s[len(pre):]
    if s.endswith(" Line"):
        s = s[:-5]
    tok = s.split()[0]
    if len(tok) <= 4:
        return tok
    t2 = tok.replace("-", "").replace("/", "")
    if len(t2) <= 4:
        return t2
    words = [w for w in s.replace("-", " ").split() if w]
    return "".join(w[0] for w in words[:3]).upper() if len(words) > 1 else tok[:3].upper()


# ---- drawn-line masks & snapping ----------------------------------------
# Every agency's lines are drawn in a distinct color; snapping each route's
# warped shape onto its color mask puts vehicles exactly on the drawn lines.

_IMG = None, None

def map_image():
    global _IMG
    if _IMG[0] is None:
        im = np.asarray(Image.open("map.png").convert("RGB"), dtype=np.int32)
        keep = np.ones(im.shape[:2], dtype=bool)
        for x0, y0, x1, y1 in EXCLUDE:
            keep[y0:y1, x0:x1] = False
        _IMG = im, keep
    return _IMG

_TREES = {}

def mask_tree(colors, tol=38.0):
    """KD-tree over map pixels within tol of ANY of the given colors."""
    key = (tuple(map(tuple, colors)), tol)
    if key not in _TREES:
        im, keep = map_image()
        m = np.zeros(keep.shape, dtype=bool)
        for rgb in colors:
            d2 = ((im - np.array(rgb)) ** 2).sum(axis=2)
            m |= d2 < tol * tol
        ys, xs = np.nonzero(m & keep)
        _TREES[key] = cKDTree(np.c_[xs, ys]) if len(xs) > 300 else None
    return _TREES[key]

# Metro's drawn line colors (sampled from the map)
ORANGE = (217, 129, 83)     # Metro Local/Rapid orange
RAPID_RED = (180, 51, 61)   # 720/754/761

# Per-agency drawn-line color seeds, sampled from the map's legend swatches.
# Thin dashes sample washed-out, so each seed is refined against pixels found
# along the agency's actual routes before masking. Pasadena Transit's color is
# plain gray (identical to street art), so it keeps the polynomial warp.
LEGEND_SEEDS = {
    "culvercity": [(215, 215, 157)],
    "gtrans": [(198, 165, 188)],
    "ladot": [(175, 170, 141), (154, 150, 117)],   # DASH + Commuter Express olives
    "longbeach": [(136, 88, 92)],
    "norwalk": [(197, 224, 223)],
    "bigbluebus": [(143, 135, 136)],
    "foothill": [(108, 133, 116)],
    "montebello": [(172, 186, 153)],
    "torrance": [(137, 139, 174)],
    "burbank": [(132, 168, 155)],
    "beachcities": [(170, 181, 169)],
}

def refine_color(shape_pts, seed, r2=55 * 55, need=250):
    """Median of pixels along the shapes that are close to the seed color."""
    im, keep = map_image()
    h, w = keep.shape
    seed = np.array(seed)
    samples = []
    for pts in shape_pts[:20]:
        for x, y in densify(pts, 10.0):
            xi, yi = int(x), int(y)
            if not (1 <= xi < w - 1 and 1 <= yi < h - 1) or not keep[yi, xi]:
                continue
            for dx, dy in ((0, 0), (2, 0), (-2, 0), (0, 2), (0, -2)):
                c = im[yi + dy, xi + dx]
                if ((c - seed) ** 2).sum() < r2:
                    samples.append(c)
    if len(samples) < need:
        return None
    return tuple(np.median(samples, axis=0).astype(int).tolist())

def snap_coherent(pts, tree, caps=(40.0, 26.0, 14.0), win=61):
    """Snap a warped polyline onto a drawn-line mask. The displacement field is
    smoothed along the line so whole stretches move to the same drawn street
    instead of individual points grabbing different parallels. Returns None if
    the line isn't substantially drawn on the map."""
    P = np.array(densify(pts, 4.0), dtype=float)
    n = len(P)
    if n < 8 or tree is None:
        return None
    idx = np.arange(n)
    for ci, cap in enumerate(caps):
        d, j = tree.query(P)
        ok = d < cap
        if ci == 0 and ok.sum() < n * 0.5:
            return None                    # mostly undrawn: keep the warp
        if ok.sum() < 4:
            return None
        disp = np.full((n, 2), np.nan)
        disp[ok] = tree.data[j[ok]] - P[ok]
        k = np.ones(win) / win
        for c in (0, 1):
            col = np.interp(idx, idx[~np.isnan(disp[:, c])], disp[:, c][~np.isnan(disp[:, c])])
            disp[:, c] = np.convolve(np.pad(col, win // 2, mode="edge"), k, "valid")
        P = P + disp
    d, j = tree.query(P)                   # final tight snap + light smoothing
    ok = d < 8
    P[ok] = tree.data[j[ok]]
    k = np.ones(7) / 7
    for c in (0, 1):
        P[:, c] = np.convolve(np.pad(P[:, c], 3, mode="edge"), k, "valid")
    return [tuple(p) for p in P]


def densify(pts, max_step=6.0):
    out = [pts[0]]
    for p in pts[1:]:
        q = out[-1]
        d = math.hypot(p[0] - q[0], p[1] - q[1])
        for i in range(1, int(d // max_step) + 1):
            t = i * max_step / d
            out.append((q[0] + (p[0] - q[0]) * t, q[1] + (p[1] - q[1]) * t))
        if out[-1] != p:
            out.append(p)
    return out


def snap_rail(pts, tree, cap=45.0, win=9):
    """Snap polyline points to nearest drawn-line pixel; smooth; keep original
    where the drawn line has gaps (stations, ghosted downtown)."""
    arr = np.array(pts)
    dist, j = tree.query(arr)
    snapped = np.where((dist < cap)[:, None], tree.data[j][:, [0, 1]], arr)
    k = np.ones(win) / win
    sm = np.copy(snapped)
    for c in (0, 1):
        sm[:, c] = np.convolve(np.pad(snapped[:, c], win // 2, mode="edge"), k, "valid")
    return [tuple(p) for p in sm]


def simplify(pts, tol=1.2):
    """Douglas-Peucker."""
    pts = np.asarray(pts)
    keep = np.zeros(len(pts), bool)
    keep[[0, -1]] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 <= i0 + 1:
            continue
        seg = pts[i1] - pts[i0]
        L2 = seg @ seg
        rel = pts[i0 + 1:i1] - pts[i0]
        if L2 == 0:
            d = np.hypot(rel[:, 0], rel[:, 1])
        else:
            t = np.clip(rel @ seg / L2, 0, 1)
            proj = np.outer(t, seg)
            d = np.hypot(*(rel - proj).T)
        im = int(np.argmax(d))
        if d[im] > tol:
            k = i0 + 1 + im
            keep[k] = True
            stack += [(i0, k), (k, i1)]
    return pts[keep]


def project_stops(shape_px, cum, stop_px):
    """Distance along shape for each stop, forced monotonic."""
    P = np.asarray(shape_px)
    A, Bv = P[:-1], P[1:] - P[:-1]
    L2 = (Bv ** 2).sum(1)
    L2[L2 == 0] = 1e-9
    dists = []
    lo = 0.0
    for sx, sy in stop_px:
        rel = np.array([sx, sy]) - A
        t = np.clip((rel * Bv).sum(1) / L2, 0, 1)
        proj = A + Bv * t[:, None]
        d2 = ((proj - [sx, sy]) ** 2).sum(1)
        along = cum[:-1] + t * np.sqrt(L2)
        d2[along < lo - 30] = 1e18
        i = int(np.argmin(d2))
        lo = max(lo, along[i])
        dists.append(lo)
    return dists


def main():
    rail_trees = load_masks()

    routes, route_idx = [], {}      # route_idx[(feed, route_id)]
    shapes_raw = {}                 # (feed, shape_id) -> [(x,y)...] px
    trips_out = []
    patterns, pattern_idx = [], {}  # key (feed, shape_id, stop_seq)
    stops_px = {}                   # (feed, stop_id) -> (x, y)
    stats = defaultdict(int)

    for feed in FEEDS:
        if not os.path.isdir(f"{GTFS}/{feed}"):
            print(f"{feed}: missing, skipped")
            continue
        is_metro = feed in ("gtfs_rail", "gtfs_bus")

        trip_rows = read_csv(feed, "trips.txt")
        tps = defaultdict(int)
        for row in trip_rows:
            tps[row["service_id"]] += 1
        day = pick_date(feed, tps)
        if day is None:
            print(f"{feed}: no usable service date, skipped")
            continue
        active = active_services(feed, day)

        srows = read_csv(feed, "stops.txt")
        if srows:
            xs, ys = to_px(np.array([float(r["stop_lon"]) for r in srows]),
                           np.array([float(r["stop_lat"]) for r in srows]))
            for r, x, y in zip(srows, xs, ys):
                stops_px[(feed, r["stop_id"])] = (x, y)

        rmeta = {}
        for row in read_csv(feed, "routes.txt"):
            label = route_label(row.get("route_short_name", ""), row.get("route_long_name", ""))
            color = (row.get("route_color") or "").strip()
            text = (row.get("route_text_color") or "").strip()
            if not color:
                color, text = (METRO_BUS_COLOR, METRO_BUS_TEXT) if is_metro else (FALLBACK_COLOR, FALLBACK_TEXT)
            if color == "000000" and is_metro:
                color = "B4333D"  # map's Rapid red (GTFS says black; map draws red)
            rail = row.get("route_type") in ("0", "1", "2")
            rmeta[row["route_id"]] = (label, color, text or "FFFFFF", rail)

        trip_info = {row["trip_id"]: (row["route_id"], row.get("shape_id", ""))
                     for row in trip_rows if row["service_id"] in active}

        stop_times = defaultdict(list)
        for row in read_csv(feed, "stop_times.txt"):
            ti = row["trip_id"]
            if ti in trip_info and (row.get("arrival_time") or "").strip():
                stop_times[ti].append((int(row["stop_sequence"]), parse_time(row["arrival_time"]), row["stop_id"]))

        n_before = len(trips_out)
        used_shapes = set()
        for ti, sts in stop_times.items():
            if len(sts) < 2:
                continue
            rid, sid = trip_info[ti]
            sts.sort()
            times = [t for _, t, _ in sts]
            stop_seq = tuple(s for _, _, s in sts)
            rkey = (feed, rid)
            if rkey not in route_idx:
                label, color, text, rail = rmeta[rid]
                route_idx[rkey] = len(routes)
                routes.append({"n": label, "c": "#" + color, "t": "#" + text, "rail": rail})
            pkey = (feed, sid, stop_seq)
            if pkey not in pattern_idx:
                pattern_idx[pkey] = len(patterns)
                patterns.append(pkey)
            trips_out.append((route_idx[rkey], pkey, times))
            used_shapes.add(sid)

        # load shapes used by this feed
        tmp = defaultdict(list)
        for row in read_csv(feed, "shapes.txt"):
            if row["shape_id"] in used_shapes:
                tmp[row["shape_id"]].append((int(row["shape_pt_sequence"]),
                                             float(row["shape_pt_lon"]), float(row["shape_pt_lat"])))
        route_by_shape = {row.get("shape_id", ""): row["route_id"] for row in trip_rows}
        warped = {}
        for sid, p in tmp.items():
            p.sort()
            x, y = to_px(np.array([q[1] for q in p]), np.array([q[2] for q in p]))
            warped[sid] = list(zip(x, y))

        # snap shapes onto the drawn lines of this system where they exist
        agency_tree = None
        if feed in LEGEND_SEEDS and warped:
            cols = [refine_color(list(warped.values()), s) for s in LEGEND_SEEDS[feed]]
            cols = [c for c in cols if c]
            if cols:
                agency_tree = mask_tree(cols, 30.0)
            print(f"  {feed} drawn color(s): {cols}")

        snapped = 0
        for sid, pts in warped.items():
            out_pts = None
            if feed == "gtfs_rail":
                tree = rail_trees.get(route_by_shape.get(sid))
                if tree is not None:
                    out_pts = snap_rail(densify(pts), tree)
            elif feed == "gtfs_bus":
                rid0 = (route_by_shape.get(sid) or "").split("-")[0]
                if rid0 in ("720", "754", "761"):
                    out_pts = snap_coherent(pts, mask_tree([RAPID_RED]))
                elif rid0 == "910":
                    pass   # J/Silver: drawn color collides with freeway gray
                else:
                    out_pts = snap_coherent(pts, mask_tree([ORANGE]))
            elif agency_tree is not None:
                out_pts = snap_coherent(pts, agency_tree)
            if out_pts is not None:
                snapped += 1
            shapes_raw[(feed, sid)] = simplify(out_pts if out_pts is not None else pts)
        n_trips = len(trips_out) - n_before
        stats[feed] = n_trips
        print(f"{feed}: {n_trips} trips on {day} ({snapped}/{len(warped)} shapes snapped)")

    # finalize shapes + cumulative dists (including stop-derived pseudo-shapes)
    shapes_out, cums, shape_index = [], [], {}

    def add_shape(key, pts):
        P = np.asarray(pts)
        seg = np.hypot(*np.diff(P, axis=0).T)
        shape_index[key] = len(shapes_out)
        cums.append(np.concatenate([[0], np.cumsum(seg)]))
        shapes_out.append([round(v, 1) for xy in P for v in xy])

    for key, pts in shapes_raw.items():
        add_shape(key, pts)

    patterns_out = []
    for feed, sid, stop_seq in patterns:
        spx = [stops_px[(feed, s)] for s in stop_seq]
        key = (feed, sid)
        if key not in shape_index:            # no shape in feed: polyline through stops
            if len(set(spx)) < 2:
                patterns_out.append(None)
                stats["skipped_no_shape"] += 1
                continue
            shapes_raw[key] = spx
            add_shape(key, spx)
        si = shape_index[key]
        d = project_stops(shapes_raw[key], cums[si], spx)
        patterns_out.append({"s": si, "d": [round(v) for v in d]})

    trips_final = []
    for ridx, pkey, times in trips_out:
        pi = pattern_idx[pkey]
        if patterns_out[pi] is None:
            continue
        t0 = times[0]
        deltas = [times[k] - times[k - 1] for k in range(1, len(times))]
        trips_final.append([ridx, pi, t0] + deltas)
        if times[-1] > 86400:
            trips_final.append([ridx, pi, t0 - 86400] + deltas)
            stats["wrapped"] += 1

    out = {"date": TARGET.strftime("%Y%m%d"), "routes": routes, "shapes": shapes_out,
           "patterns": patterns_out, "trips": trips_final}
    with open("schedule.json", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    stats["routes"] = len(routes)
    stats["shapes"] = len(shapes_out)
    stats["patterns"] = len(patterns_out)
    stats["trips_total"] = len(trips_final)
    print(dict(stats))
    print(f"built {datetime.now().isoformat(timespec='seconds')}")


if __name__ == "__main__":
    main()
