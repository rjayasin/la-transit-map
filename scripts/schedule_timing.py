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


def repair_estimates(times, fixed, pattern):
    """Redistribute fast estimates between fixed times by displayed distance.

    Endpoints stay fixed. Infeasible fixed intervals retain their source times.
    """
    dist = eff_dist(pattern)
    played = detie(list(times), dist)
    out = list(times)
    anchors = [i for i, exact in enumerate(fixed)
               if exact or i == 0 or i == len(times) - 1]
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
