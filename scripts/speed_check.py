"""Find vehicles that move implausibly fast, which usually means a bad path.

Speed is read off exactly what the animation plays: the stop distances in
schedule.json over the timetable the client actually uses, including the
de-tying it applies to minute-quantized GTFS times. A segment covering more
ground than its schedule allows is nearly always a shape problem — a spurious
detour, or a stop projected onto the wrong part of the line — rather than a
timetable one, so each row also carries the evidence to tell those apart:

    detour  how far the path runs between the two stops against the straight
            line between them. Much over 1 means the shape wanders, and the
            vehicle has to sprint to keep the schedule. This is the pathing
            error you are usually looking for.
    ratio ~1 at high speed means the path is direct and the vehicle really
            does travel that far in that time — a genuine express hop, an
            off-map segment, or a hole in the feed.

Results are grouped per shape segment, not per trip, since one bad shape shows
up on every trip that runs it. Each row prints the debug_line.py command for
the route, so the next step is one paste away.

Usage:
    scripts/speed_check.py                  # the worst 25 over 120 km/h
    scripts/speed_check.py --over 250       # only the egregious ones
    scripts/speed_check.py --top 60
    scripts/speed_check.py --system Foothill
    scripts/speed_check.py --inset          # inside the Downtown panel
    scripts/speed_check.py --csv            # machine-readable, all of them
"""
import argparse
import csv
import json
import math
import os
import sys

sys.path.insert(0, "scripts")

SCHEDULE = "schedule.json"
DEG_KM = 111.32          # km per degree of latitude
DEFAULT_OVER = 120.0     # km/h; above an LA bus's plausible top speed


def map_scale(inset=False):
    """Map pixels per km. The sheet is quasi-geographic but very close to
    uniform in scale, so one sample at the middle stands in for all of it."""
    import numpy as np
    from build_data import to_px, to_inset_px, INSET_GEO
    lon, lat = (-118.25, 34.05) if not inset else (
        (INSET_GEO[0] + INSET_GEO[2]) / 2, (INSET_GEO[1] + INSET_GEO[3]) / 2)
    step = 0.005
    fn = to_inset_px if inset else to_px
    x0, y0 = fn(np.array([lon]), np.array([lat]))
    x1, y1 = fn(np.array([lon]), np.array([lat + step]))
    return float(math.hypot(x1[0] - x0[0], y1[0] - y0[0]) / (step * DEG_KM))


def trip_times(t):
    """Absolute stop times: the trip stores t0 then per-stop deltas."""
    times = [float(t[2])]
    for dv in t[3:]:
        times.append(times[-1] + dv)
    return times


def eff_dist(pat):
    """Distance used for de-tying, mirroring the client: downtown the main map
    is so compressed that stop distances plateau, so inset movement (at ~1/5
    scale) counts too, or tied stops there would get no time at all."""
    dd, ir, idd = pat["d"], pat.get("ir"), pat.get("id")
    if not ir:
        return dd
    eff = [0.0] * len(dd)
    for i in range(1, len(dd)):
        step = dd[i] - dd[i - 1]
        if ir[i] >= 0 and ir[i] == ir[i - 1]:
            step = max(step, abs(idd[i] - idd[i - 1]) / 5)
        eff[i] = eff[i - 1] + step
    return eff


def detie(times, dist):
    """Spread runs of (near-)tied stop times over the adjacent gap, in place —
    the same fix the client applies. GTFS times are minute-quantized, so
    consecutive stops often share a timestamp while the bus is really moving;
    measuring speed against the raw times would report teleports everywhere."""
    n = len(times)
    i = 0
    while i < n - 1:
        if times[i + 1] - times[i] > 1:
            i += 1
            continue
        j = i
        while j + 1 < n and times[j + 1] - times[j] <= 1:
            j += 1
        if j + 1 < n:
            T, U, D = times[i], times[j + 1], dist[j + 1] - dist[i]
            if D > 0:
                for m in range(i + 1, j + 1):
                    times[m] = T + (U - T) * (dist[m] - dist[i]) / D
        elif i > 0:
            T, U, D = times[i - 1], times[j], dist[j] - dist[i - 1]
            if D > 0:
                for m in range(i, j):
                    times[m] = T + (U - T) * (dist[m] - dist[i - 1]) / D
        i = j
    return times


def point_at(pts, dist):
    """Position `dist` px along a flat [x,y,x,y,...] polyline."""
    if dist <= 0 or len(pts) < 4:
        return pts[0], pts[1]
    run = 0.0
    for k in range(0, len(pts) - 2, 2):
        x0, y0, x1, y1 = pts[k], pts[k + 1], pts[k + 2], pts[k + 3]
        seg = math.hypot(x1 - x0, y1 - y0)
        if run + seg >= dist:
            f = (dist - run) / seg if seg else 0.0
            return x0 + (x1 - x0) * f, y0 + (y1 - y0) * f
        run += seg
    return pts[-2], pts[-1]


def segments(d, inset):
    """One record per shape segment, worst speed across the trips running it."""
    best = {}
    for t in d["trips"]:
        pat = d["patterns"][t[1]]
        if not pat:
            continue
        times = detie(trip_times(t), eff_dist(pat))
        if inset:
            ir, idd = pat.get("ir"), pat.get("id")
            if not ir:
                continue
            dist, runs = idd, ir
        else:
            dist, runs = pat["d"], None
        for i in range(len(times) - 1):
            dt = times[i + 1] - times[i]
            if dt <= 0:
                continue
            if runs is not None:                  # inset: same run, or no move
                if runs[i] < 0 or runs[i] != runs[i + 1]:
                    continue
            span = abs(dist[i + 1] - dist[i])
            key = (t[0], pat["s"], i, runs[i] if runs else -1)
            rec = best.get(key)
            if rec is None or span / dt > rec[0]:
                best[key] = (span / dt, span, dt, t[1])
    return best


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--over", type=float, default=DEFAULT_OVER,
                    help=f"report segments faster than this, km/h (default {DEFAULT_OVER:.0f})")
    ap.add_argument("--top", type=int, default=25, help="rows to print (default 25)")
    ap.add_argument("--system", help="substring of the system name")
    ap.add_argument("--inset", action="store_true", help="check the Downtown panel instead")
    ap.add_argument("--csv", action="store_true", help="write every hit as CSV to stdout")
    a = ap.parse_args()

    if not os.path.exists(SCHEDULE):
        sys.exit(f"missing {SCHEDULE} (run from the repo root)")
    with open(SCHEDULE) as f:
        d = json.load(f)
    pxkm = map_scale(a.inset)

    segs = segments(d, a.inset)
    rows = []
    for (ridx, si, i, run), (pps, span, dt, pi) in segs.items():
        route = d["routes"][ridx]
        system = d["systems"][route["sy"]]
        if a.system and a.system.lower() not in system.lower():
            continue
        kmh = pps / pxkm * 3600
        if kmh < a.over:
            continue
        pat = d["patterns"][pi]
        pts = (d["insets"][si][run] if a.inset else d["shapes"][si])
        d0 = (pat["id"] if a.inset else pat["d"])[i]
        d1 = (pat["id"] if a.inset else pat["d"])[i + 1]
        p0, p1 = point_at(pts, d0), point_at(pts, d1)
        straight = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        off = not all(-50 <= p[0] <= 4146 and -50 <= p[1] <= 4189 for p in (p0, p1))
        rows.append({
            "kmh": kmh, "route": route["n"], "system": system, "shape": si,
            "stop": i, "run": run, "path_km": span / pxkm,
            "straight_km": straight / pxkm, "detour": span / straight if straight > 1e-9 else float("inf"),
            "seconds": dt, "x": round(p0[0], 1), "y": round(p0[1], 1), "offmap": off,
        })
    rows.sort(key=lambda r: -r["kmh"])

    if a.csv:
        w = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()) if rows else
                           ["kmh", "route", "system", "shape", "stop", "run",
                            "path_km", "straight_km", "detour", "seconds", "x", "y"])
        w.writeheader()
        for r in rows:
            w.writerows([{k: (round(v, 3) if isinstance(v, float) else v)
                          for k, v in r.items()}])
        return

    where = "inset run" if a.inset else "shape"
    total = len(segs)
    print(f"{len(rows)} of {total} {where} segments over {a.over:.0f} km/h "
          f"({100 * len(rows) / max(total, 1):.1f}%; map scale {pxkm:.1f} px/km)\n")
    if not rows:
        print("nothing to report")
        return
    print(f"{'km/h':>7} {'route':>6} {'system':<20} {'shape':>5} {'stop':>5} "
          f"{'path':>7} {'direct':>7} {'detour':>7} {'secs':>6}  where")
    for r in rows[:a.top]:
        det = "inf" if r["detour"] == float("inf") else f'{r["detour"]:.1f}x'
        print(f'{r["kmh"]:7.0f} {r["route"]:>6} {r["system"][:20]:<20} {r["shape"]:5d} '
              f'{r["stop"]:5d} {r["path_km"]:6.2f}k {r["straight_km"]:6.2f}k {det:>7} '
              f'{r["seconds"]:6.0f}  ({r["x"]:.0f},{r["y"]:.0f})'
              + ("  off-map" if r["offmap"] else ""))
    if len(rows) > a.top:
        print(f"... {len(rows) - a.top} more (--top)")

    worst = {}
    for r in rows:
        worst.setdefault((r["route"], r["system"]), r)
    print("\nroutes to look at, worst first:")
    for (n, sysname), r in list(worst.items())[:12]:
        flag = ("off-map — line isn't drawn" if r["offmap"] else
                "detour" if r["detour"] > 1.6 else "direct — check the feed")
        print(f'  {r["kmh"]:6.0f} km/h  {flag:<24} '
              f'scripts/debug_line.py {n} --system "{sysname}"')


if __name__ == "__main__":
    main()
