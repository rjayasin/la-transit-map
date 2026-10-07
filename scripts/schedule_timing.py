"""Timing shared by the schedule builder and motion diagnostics."""


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
    """Spread runs of (near-)tied stop times over the adjacent gap, in place,
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


# About 120 km/h at the main map's 44.3 px/km scale.
MAX_ESTIMATED_SPEED = 1.47


def infeasible_windows(times, dist, anchors):
    """Runs of anchors whose fixed times cannot be met at MAX_ESTIMATED_SPEED,
    widened over the neighbouring anchors until the run as a whole can.

    Some feeds publish timing points minutes apart for stops several km apart
    by road, and a schematic map stretches that distance further. Played as
    given, the vehicle sprints between them and idles either side. Widening
    stops at the first window that is feasible, so it takes time only from the
    intervals next to the fault. A fault no window can absorb is left alone."""
    fast = lambda a, b: (dist[anchors[b]] - dist[anchors[a]]
                         > MAX_ESTIMATED_SPEED * (times[anchors[b]] - times[anchors[a]]))
    windows = []
    for k in range(len(anchors) - 1):
        if not fast(k, k + 1) or (windows and windows[-1][1] > k):
            continue
        lo, hi = k, k + 1
        while fast(lo, hi) and (lo > 0 or hi < len(anchors) - 1):
            left = lo - 1 if lo > 0 else None
            right = hi + 1 if hi < len(anchors) - 1 else None
            slack = lambda a, b: (MAX_ESTIMATED_SPEED * (times[anchors[b]] - times[anchors[a]])
                                  - (dist[anchors[b]] - dist[anchors[a]]))
            if right is None or (left is not None and slack(left, hi) >= slack(lo, right)):
                lo = left
            else:
                hi = right
        if not fast(lo, hi):
            if windows and windows[-1][1] >= lo:
                lo = windows.pop()[0]
            windows.append((lo, hi))
    return [(anchors[a], anchors[b]) for a, b in windows]


def repair_estimates(times, fixed, pattern):
    """Redistribute fast estimates between fixed times by displayed distance.

    Endpoints stay fixed. A fixed interval too fast to play takes time from its
    neighbours (infeasible_windows); one that no neighbour can absorb retains
    its source times.
    """
    dist = eff_dist(pattern)
    played = detie(list(times), dist)
    out = list(times)
    anchors = [i for i, exact in enumerate(fixed)
               if exact or i == 0 or i == len(times) - 1]
    windows = infeasible_windows(times, dist, anchors)
    for a, b in windows:
        span, duration = dist[b] - dist[a], times[b] - times[a]
        for i in range(a + 1, b):
            out[i] = round(times[a] + duration * (dist[i] - dist[a]) / span)
    anchors = [i for i in anchors if not any(a < i < b for a, b in windows)]
    for a, b in zip(anchors, anchors[1:]):
        span, duration = dist[b] - dist[a], times[b] - times[a]
        if b == a + 1 or span <= 0 or duration <= 0:
            continue
        if span > MAX_ESTIMATED_SPEED * duration:
            continue
        if not any(dist[i+1] - dist[i] > MAX_ESTIMATED_SPEED *
                   max(0, played[i+1] - played[i]) for i in range(a, b)):
            continue
        for i in range(a + 1, b):
            out[i] = round(times[a] + duration * (dist[i] - dist[a]) / span)
    return out
