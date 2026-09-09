"""Validate a generated schedule before publishing it."""
import argparse
import json
import math


def require(ok, message):
    if not ok:
        raise ValueError(message)


def arc(flat):
    require(len(flat) >= 4 and len(flat) % 2 == 0, "invalid polyline")
    require(all(isinstance(v, (int, float)) and math.isfinite(v) for v in flat),
            "nonfinite polyline")
    return sum(math.hypot(flat[i] - flat[i - 2], flat[i + 1] - flat[i - 1])
               for i in range(2, len(flat), 2))


def validate(data):
    require(bool(data["routes"]) and bool(data["trips"]), "empty schedule")
    lengths = [arc(p) for p in data["shapes"]]
    if "_nativeShapes" in data:
        require(isinstance(data["_nativeShapes"], int)
                and 0 <= data["_nativeShapes"] <= len(lengths), "invalid native shape count")
    require(len(data["insets"]) == len(lengths), "inset/shape count mismatch")
    inset_lengths = [[arc(p) for p in runs] if runs else [] for runs in data["insets"]]
    ranges = data.get("insetRanges")
    if ranges is not None:
        require(len(ranges) == len(lengths), "inset range/shape count mismatch")
        for bounds, lens in zip(ranges, inset_lengths):
            require(len(bounds or []) == len(lens), "inset range/run count mismatch")
            last = 0
            for start, end in bounds or []:
                require(math.isfinite(start) and math.isfinite(end)
                        and last <= start <= end <= 1, "invalid inset source range")
                last = start
    for r in data["routes"]:
        require(0 <= r["sy"] < len(data["systems"]), "invalid route system")
    for i, p in enumerate(data["patterns"]):
        if p is None:
            continue
        require(0 <= p["s"] < len(lengths), f"pattern {i}: invalid shape")
        d = p["d"]
        require(len(d) >= 2 and all(math.isfinite(v) for v in d), f"pattern {i}: invalid distances")
        require(0 <= d[0] and d[-1] <= lengths[p["s"]] + 1, f"pattern {i}: distances outside shape")
        require(all(b >= a for a, b in zip(d, d[1:])), f"pattern {i}: backward distances")
        if "ir" in p:
            require(len(p["ir"]) == len(p["id"]) == len(d), f"pattern {i}: inset stop count")
            last = {}
            for r, v in zip(p["ir"], p["id"]):
                require(isinstance(r, int) and -1 <= r < len(inset_lengths[p["s"]]),
                        f"pattern {i}: invalid inset run")
                if r >= 0:
                    require(math.isfinite(v) and last.get(r, 0) <= v <= inset_lengths[p["s"]][r] + 1,
                            f"pattern {i}: invalid inset distance")
                    last[r] = v
        if "u" in p:
            u = p["u"]
            require(len(u) == len(d) and all(math.isfinite(v) and 0 <= v <= 1 for v in u)
                    and all(b >= a for a, b in zip(u, u[1:])), f"pattern {i}: invalid source distances")
    require(len(data["trips"]) == len(data["tripDays"]), "trip/day count mismatch")
    for i, (t, days) in enumerate(zip(data["trips"], data["tripDays"])):
        require(len(t) >= 4 and 0 <= t[0] < len(data["routes"])
                and 0 <= t[1] < len(data["patterns"]), f"trip {i}: invalid references")
        p = data["patterns"][t[1]]
        require(p is not None and len(t) - 2 == len(p["d"]), f"trip {i}: stop count mismatch")
        require(all(math.isfinite(v) for v in t[2:]) and all(v >= 0 for v in t[3:]),
                f"trip {i}: invalid times")
        require(isinstance(days, int) and 0 < days < 128, f"trip {i}: invalid service days")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("schedule", nargs="?", default="schedule.json")
    a = ap.parse_args()
    try:
        with open(a.schedule) as f:
            validate(json.load(f))
    except (ValueError, KeyError, TypeError, IndexError) as e:
        ap.exit(1, f"invalid schedule: {e}\n")
    print(f"valid: {a.schedule}")
