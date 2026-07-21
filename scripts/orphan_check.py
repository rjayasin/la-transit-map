"""Find vehicles labelled with a designation the map never prints.

Every vehicle carries the route's short label, and the point of playing them
over Metro's own sheet is that you can look one up: a bus marked 720 runs along
a line the map badges 720. When the label appears nowhere on the sheet the
vehicle is an orphan — there is nothing to match it to.

Two quite different things produce one, and the report separates them:

    unnamed   the map never mentions this route at all. Usually honest: the
              agency's lines aren't drawn, or the route is too minor to badge.
              Nothing to fix in code.
    mislabelled   the map does print a designation for this route, just not
              the one we chose. route_label compresses long names down to four
              characters and can land somewhere the cartographer didn't —
              these are worth relabelling, since the badge is right there.

Route designations are read from the PDF's own text, so this compares what the
sprite says against what a reader can actually find on the map. A designation
drawn as artwork rather than set as text won't be found — the K line's roundel
is the one case in the current sheet — so treat a lone letter with suspicion.

Usage:
    scripts/orphan_check.py                 # every orphan, busiest first
    scripts/orphan_check.py --mislabelled   # only the fixable ones
    scripts/orphan_check.py --system LADOT
    scripts/orphan_check.py --min-trips 20
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, "scripts")

SCHEDULE = "schedule.json"


def map_words():
    """Every word printed on the sheet, indexed case-insensitively."""
    from build_data import badge_words
    idx = defaultdict(list)
    for region in ("main", "inset"):
        for word, spots in badge_words(region).items():
            idx[word.casefold()] += [(region, x, y) for x, y in spots]
    return idx


def route_rows(feed_names):
    """(system, label, short, long) for every route in every cached feed."""
    from build_data import FEEDS, read_csv, route_label
    out = []
    for feed in FEEDS:
        for row in read_csv(feed, "routes.txt"):
            short = (row.get("route_short_name") or "").strip()
            long_name = (row.get("route_long_name") or "").strip()
            out.append((feed_names.get(feed, feed),
                        route_label(short, long_name), short, long_name))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--system", help="substring of the system name")
    ap.add_argument("--mislabelled", action="store_true",
                    help="only routes the map designates differently")
    ap.add_argument("--min-trips", type=int, default=0,
                    help="ignore routes running fewer trips than this")
    a = ap.parse_args()

    if not os.path.exists(SCHEDULE):
        sys.exit(f"missing {SCHEDULE} (run from the repo root)")
    with open(SCHEDULE) as f:
        d = json.load(f)
    from build_data import FEED_NAMES, badge_like

    trips = Counter()
    for t in d["trips"]:
        r = d["routes"][t[0]]
        trips[(d["systems"][r["sy"]], r["n"])] += 1

    words = map_words()
    seen, rows = set(), []
    for system, label, short, long_name in route_rows(FEED_NAMES):
        if (system, label) in seen:
            continue
        seen.add((system, label))
        if a.system and a.system.lower() not in system.lower():
            continue
        n = trips.get((system, label), 0)
        if n < a.min_trips or (system, label) not in trips:
            continue
        if label.casefold() in words:
            continue                                   # the map prints it
        # Does the map print some other designation for this route? Only
        # route_short_name is searched, and only tokens shaped like a badge —
        # the long name is prose, and "Line", "City" and "Park" all appear on
        # the sheet without designating anything.
        alts = [t for t in set(short.replace("/", " ").replace("-", " ").split())
                if badge_like(t) and t.casefold() != label.casefold()
                and t.casefold() in words]
        rows.append((n, system, label, sorted(alts), short or long_name))

    rows.sort(reverse=True)
    if a.mislabelled:
        rows = [r for r in rows if r[3]]
    kind = "mislabelled" if a.mislabelled else "orphan"
    print(f"{len(rows)} {kind} route{'' if len(rows) == 1 else 's'} "
          f"of {len(trips)} running\n")
    if not rows:
        print("nothing to report")
        return
    print(f"{'trips':>6} {'label':>6} {'system':<22} {'map prints':<16} route")
    for n, system, label, alts, name in rows:
        shows = ", ".join(alts) if alts else "-"
        print(f"{n:6d} {label:>6} {system[:22]:<22} {shows[:16]:<16} {name[:38]}")
    if not a.mislabelled:
        fixable = sum(1 for r in rows if r[3])
        print(f"\n{fixable} of these are mislabelled — the map designates them, "
              f"just not as we do (--mislabelled)")


if __name__ == "__main__":
    main()
