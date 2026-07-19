"""Build web/schedule.json from cached GTFS feeds.

- Picks a service date (weekday) and gathers all active trips (bus + rail).
- Projects shapes into map pixel space via data/transform.json (poly2 warp).
- Rail shapes are additionally snapped onto the drawn line pixels of the map.
- Stops are projected onto shapes to get distance-along-shape per stop.
- Emits compact JSON: routes, shapes (px polylines), patterns (stop dists),
  trips (route, pattern, stop arrival times).

Trips crossing midnight (times >= 24:00) are also emitted shifted by -24h so
the after-midnight portion of "yesterday's" service appears at the start of
the simulated day.
"""
import csv, json, math, sys
from collections import defaultdict
from datetime import datetime

import numpy as np

sys.path.insert(0, "scripts")
from georef import load_masks  # noqa: E402

SERVICE_DATE = "20260722"  # a Wednesday inside the JUNE26 calendar window
DOW = "wednesday"
FEEDS = [("data/gtfs/gtfs_rail", True), ("data/gtfs/gtfs_bus", False)]
BUS_COLOR, BUS_TEXT = "E16710", "FFFFFF"

with open("data/transform.json") as f:
    TR = json.load(f)["poly2"]


def to_px(lon, lat):
    L, T = lon - TR["lon0"], lat - TR["lat0"]
    B = np.c_[np.ones_like(L), L, T, L * L, L * T, T * T]
    return B @ TR["cx"], B @ TR["cy"]


def active_services(feed):
    active = set()
    with open(f"{feed}/calendar.txt") as f:
        for row in csv.DictReader(f):
            if row[DOW] == "1" and row["start_date"] <= SERVICE_DATE <= row["end_date"]:
                active.add(row["service_id"])
    try:
        with open(f"{feed}/calendar_dates.txt") as f:
            for row in csv.DictReader(f):
                if row["date"] == SERVICE_DATE:
                    (active.add if row["exception_type"] == "1" else active.discard)(row["service_id"])
    except FileNotFoundError:
        pass
    return active


def parse_time(s):
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + int(sec)


def route_label(short, long_name):
    if short:
        return short
    # rail/busway: "Metro A Line" -> "A", "Metro G Line (Orange) 901" -> "G"
    parts = long_name.replace("Metro ", "").split()
    return parts[0]


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
    # moving-average smoothing
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
        # forbid going backwards (with small slack for projection noise)
        d2[along < lo - 30] = 1e18
        i = int(np.argmin(d2))
        lo = max(lo, along[i])
        dists.append(lo)
    return dists


def main():
    rail_trees = load_masks()

    routes, route_idx = [], {}
    shapes_raw = {}           # shape_id -> [(lon,lat),...]
    shape_ids_used = {}       # shape_id -> shape index (assigned later)
    trips_out = []
    patterns, pattern_idx = [], {}
    stops_all = {}
    stats = defaultdict(int)

    for feed, is_rail in FEEDS:
        active = active_services(feed)
        with open(f"{feed}/stops.txt") as f:
            for row in csv.DictReader(f):
                stops_all[row["stop_id"]] = (float(row["stop_lon"]), float(row["stop_lat"]))
        rmeta = {}
        with open(f"{feed}/routes.txt") as f:
            for row in csv.DictReader(f):
                label = route_label(row["route_short_name"], row["route_long_name"])
                color = row["route_color"] or BUS_COLOR
                if color == "000000":
                    color = "B4333D"  # map's Rapid red (GTFS says black; map draws red)
                text = row["route_text_color"] or BUS_TEXT
                rmeta[row["route_id"]] = (label, color, text, row["route_id"] if is_rail else "")
        trip_info = {}
        with open(f"{feed}/trips.txt") as f:
            for row in csv.DictReader(f):
                if row["service_id"] in active:
                    trip_info[row["trip_id"]] = (row["route_id"], row["shape_id"])
        stop_times = defaultdict(list)
        with open(f"{feed}/stop_times.txt") as f:
            for row in csv.DictReader(f):
                ti = row["trip_id"]
                if ti in trip_info:
                    stop_times[ti].append((int(row["stop_sequence"]), parse_time(row["arrival_time"]), row["stop_id"]))

        for ti, sts in stop_times.items():
            rid, sid = trip_info[ti]
            if len(sts) < 2 or sid not in shapes_raw and sid not in shape_ids_used:
                pass
            sts.sort()
            times = [t for _, t, _ in sts]
            stop_seq = tuple(s for _, _, s in sts)
            if rid not in route_idx:
                label, color, text, rail_route = rmeta[rid]
                route_idx[rid] = len(routes)
                routes.append({"n": label, "c": "#" + color, "t": "#" + text, "rail": is_rail})
            pkey = (sid, stop_seq)
            if pkey not in pattern_idx:
                pattern_idx[pkey] = None  # resolved in pass 2
                patterns.append(pkey)
            trips_out.append((route_idx[rid], pkey, times))
            stats["trips_rail" if is_rail else "trips_bus"] += 1
            if sid not in shapes_raw:
                shapes_raw[sid] = None  # fill below
        # load shapes for this feed
        need = {sid for sid, v in shapes_raw.items() if v is None}
        tmp = defaultdict(list)
        with open(f"{feed}/shapes.txt") as f:
            for row in csv.DictReader(f):
                if row["shape_id"] in need:
                    tmp[row["shape_id"]].append((int(row["shape_pt_sequence"]), float(row["shape_pt_lon"]), float(row["shape_pt_lat"])))
        rail_color_by_shape = {}
        if is_rail:
            with open(f"{feed}/trips.txt") as f2:
                for row in csv.DictReader(f2):
                    rail_color_by_shape[row["shape_id"]] = row["route_id"]
        for sid, p in tmp.items():
            p.sort()
            lon = np.array([x[1] for x in p]); lat = np.array([x[2] for x in p])
            x, y = to_px(lon, lat)
            pts = list(zip(x, y))
            if is_rail:
                rid_ = rail_color_by_shape.get(sid)
                if rid_ in rail_trees:
                    pts = snap_rail(densify(pts), rail_trees[rid_])
            shapes_raw[sid] = simplify(pts)

    # finalize shapes + cumulative dists
    shapes_out, cums = [], []
    for sid, pts in shapes_raw.items():
        if pts is None:
            continue
        shape_ids_used[sid] = len(shapes_out)
        P = np.asarray(pts)
        seg = np.hypot(*np.diff(P, axis=0).T)
        cums.append(np.concatenate([[0], np.cumsum(seg)]))
        shapes_out.append([round(v, 1) for xy in P for v in xy])

    # resolve patterns
    patterns_out = []
    for i, (sid, stop_seq) in enumerate(patterns):
        si = shape_ids_used.get(sid)
        if si is None:
            patterns_out.append(None)
            continue
        P = np.asarray(shapes_raw[sid])
        stop_px = []
        for s in stop_seq:
            lon, lat = stops_all[s]
            x, y = to_px(np.array([lon]), np.array([lat]))
            stop_px.append((x[0], y[0]))
        d = project_stops(P, cums[si], stop_px)
        patterns_out.append({"s": si, "d": [round(v) for v in d]})
        pattern_idx[(sid, stop_seq)] = i

    # emit trips (plus midnight-wrapped copies)
    trips_final = []
    for ridx, pkey, times in trips_out:
        pi = pattern_idx[pkey]
        if pi is None or patterns_out[pi] is None:
            stats["skipped_no_shape"] += 1
            continue
        t0 = times[0]
        deltas = [times[k] - times[k - 1] for k in range(1, len(times))]
        trips_final.append([ridx, pi, t0] + deltas)
        if times[-1] > 86400:
            trips_final.append([ridx, pi, t0 - 86400] + deltas)
            stats["wrapped"] += 1

    out = {"date": SERVICE_DATE, "routes": routes, "shapes": shapes_out,
           "patterns": patterns_out, "trips": trips_final}
    with open("web/schedule.json", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    stats["routes"] = len(routes)
    stats["shapes"] = len(shapes_out)
    stats["patterns"] = len(patterns_out)
    stats["trips_total"] = len(trips_final)
    print(dict(stats))
    print(f"built {datetime.now().isoformat(timespec='seconds')}")


if __name__ == "__main__":
    main()
