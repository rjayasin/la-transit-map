"""Check exported paths against fixed coordinates traced from the artwork."""
import argparse
import json
import math
from pathlib import Path

from schedule_check import validate


def curve(flat):
    pts = list(zip(flat[::2], flat[1::2]))
    cum = [0.0]
    for a, b in zip(pts, pts[1:]):
        cum.append(cum[-1] + math.dist(a, b))
    return pts, cum


def at(pts, cum, d):
    for i in range(1, len(pts)):
        if d <= cum[i]:
            f = min(1, max(0, (d - cum[i-1]) / max(cum[i] - cum[i-1], 1e-12)))
            return tuple(a + (b-a) * f for a, b in zip(pts[i-1], pts[i]))
    return pts[-1]


def foot(p, pts, cum):
    best = (math.inf, 0)
    for i, (a, b) in enumerate(zip(pts, pts[1:])):
        dx, dy = b[0]-a[0], b[1]-a[1]
        f = min(1, max(0, ((p[0]-a[0])*dx + (p[1]-a[1])*dy) / max(dx*dx+dy*dy, 1e-12)))
        q = (a[0]+f*dx, a[1]+f*dy)
        candidate = (math.dist(p, q), cum[i] + f*(cum[i+1]-cum[i]))
        best = min(best, candidate)
    return best


def sample(pts, cum, start=0, end=None):
    end = cum[-1] if end is None else end
    n = max(1, math.ceil(abs(end-start)))
    return [at(pts, cum, start + (end-start)*i/n) for i in range(n+1)]


def corridor_error(flat, reference, gate):
    pts, cum = curve(flat)
    ref, rcum = curve([v for xy in reference for v in xy])
    a, b = foot(ref[0], pts, cum), foot(ref[-1], pts, cum)
    if max(a[0], b[0]) > gate:
        return None
    window = sample(pts, cum, min(a[1], b[1]), max(a[1], b[1]))
    wp, wc = curve([v for xy in window for v in xy])
    off = max(foot(p, ref, rcum)[0] for p in window)
    cover = max(foot(p, wp, wc)[0] for p in sample(ref, rcum))
    return off, cover


def check(data, fixtures):
    validate(data)
    for case in fixtures:
        routes = {i for i, r in enumerate(data["routes"])
                  if r["n"] == case["route"] and data["systems"][r["sy"]] == case["system"]}
        patterns = {t[1] for t in data["trips"] if t[0] in routes}
        if "platform" in case:
            hits = 0
            for pi in patterns:
                p = data["patterns"][pi]
                pts, cum = curve(data["shapes"][p["s"]])
                hits += any(math.dist(at(pts,cum,d),case["platform"]) <= case["tolerance"] for d in p["d"])
            if hits < case["min_matches"]:
                raise ValueError(f"{case['name']}: platform reached by only {hits} patterns")
        else:
            hits = 0
            for si in {data["patterns"][pi]["s"] for pi in patterns}:
                error = corridor_error(data["shapes"][si], case["path"], case["gate"])
                if error is None:
                    continue
                hits += 1
                if max(error) > case["tolerance"]:
                    raise ValueError(f"{case['name']}: shape {si}, off={error[0]:.2f}, uncovered={error[1]:.2f}px")
            if hits < case["min_matches"]:
                raise ValueError(f"{case['name']}: only {hits} shapes reach the reference corridor")
        print(f"ok: {case['name']} ({hits} matches)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("schedule", nargs="?", default="schedule.json")
    ap.add_argument("--fixtures", default=str(Path(__file__).parent / "fixtures/corridors.json"))
    a = ap.parse_args()
    try:
        check(json.loads(Path(a.schedule).read_text()), json.loads(Path(a.fixtures).read_text()))
    except (ValueError, KeyError, IndexError) as e:
        ap.exit(1, f"geometry check failed: {e}\n")
