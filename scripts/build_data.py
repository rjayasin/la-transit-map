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
import colorsys, csv, hashlib, inspect, json, math, os, re, sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import numpy as np
from PIL import Image
from scipy import ndimage as ndi, sparse
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

sys.path.insert(0, "scripts")
from georef import (EXCLUDE, MASK_LEVEL, MASK_TOL, ROUTE_COLORS, TILE, TILES,  # noqa: E402
                    TOL, load_masks, tile_scan)
from georef_inset import GEO as INSET_GEO, LEGEND as INSET_LEGEND, RECT as INSET_RECT  # noqa: E402

TARGET = date(2026, 7, 22)  # a Wednesday inside the Metro JUNE26 calendar window
GTFS = "data/gtfs"
# gtfs_rail first so Metro trains draw config (snap) is applied; order otherwise cosmetic
# Norwalk Transit is not here, and the feed is gone with it. The Mobility
# Database entry catalogued as "us-california-norwalk-transit-system-nts" is
# Norwalk Transit *District*, Connecticut — America/New_York, a 203 phone
# number, and routes to SoNo Station, Wilton Center and Greenwich. Its shapes
# warp to around (-10000, 320000), a quarter of a million px off the sheet, so
# all 531 of its trips animated nowhere: every one of them off-map in
# speed_check, none of them drawable, and debug_line died on the negative crop.
FEEDS = ["gtfs_rail", "gtfs_bus", "bigbluebus", "culvercity", "ladot", "longbeach",
         "foothill", "torrance", "montebello", "gtrans", "pasadena",
         "burbank", "beachcities", "metrolink"]
WORKERS = min(8, (os.cpu_count() or 4))   # threads for the mask fits
METRO_BUS_COLOR, METRO_BUS_TEXT = "E16710", "FFFFFF"
FALLBACK_COLOR, FALLBACK_TEXT = "888888", "FFFFFF"

# display names for the system filter UI, keyed by feed
FEED_NAMES = {
    "gtfs_rail": "Metro Rail", "gtfs_bus": "Metro Bus",
    "bigbluebus": "Big Blue Bus", "culvercity": "Culver CityBus",
    "ladot": "LADOT", "longbeach": "Long Beach Transit",
    "foothill": "Foothill Transit", "torrance": "Torrance Transit",
    "montebello": "Montebello Bus Lines",
    "gtrans": "GTrans", "pasadena": "Pasadena Transit",
    "burbank": "BurbankBus", "beachcities": "Beach Cities Transit",
    "metrolink": "Metrolink",
}

# Metrolink trips.txt leaves shape_id empty; shapes.txt carries per-line
# in/out geometry instead. direction_id orientation is not uniform across
# lines — this mapping was measured by monotone-fitting each direction's
# stop sequence against both shapes (wrong one fits ~1000 px off, right
# one <1 px).
METROLINK_SHAPES = {
    ("91 Line", "0"): "91in", ("91 Line", "1"): "91out",
    ("Antelope Valley Line", "0"): "AVout", ("Antelope Valley Line", "1"): "AVin",
    ("Inland Emp.-Orange Co. Line", "0"): "IEOCin", ("Inland Emp.-Orange Co. Line", "1"): "IEOCout",
    ("Orange County Line", "0"): "OCin", ("Orange County Line", "1"): "OCout",
    ("Riverside Line", "0"): "RIVERout", ("Riverside Line", "1"): "RIVERin",
    ("San Bernardino Line", "0"): "SBout", ("San Bernardino Line", "1"): "SBin",
    ("Ventura County Line", "0"): "VTout", ("Ventura County Line", "1"): "VTin",
}

with open("data/transform.json") as f:
    _TRJ = json.load(f)
TR = _TRJ["poly2"]
TR_INSET = _TRJ.get("inset", {}).get("poly2")
if "geo" in _TRJ.get("inset", {}):
    INSET_GEO = tuple(_TRJ["inset"]["geo"])   # fitted frame coverage wins


def to_px(lon, lat):
    L, T = lon - TR["lon0"], lat - TR["lat0"]
    B = np.c_[np.ones_like(L), L, T, L * L, L * T, T * T]
    return B @ TR["cx"], B @ TR["cy"]


def to_inset_px(lon, lat):
    L, T = lon - TR_INSET["lon0"], lat - TR_INSET["lat0"]
    B = np.c_[np.ones_like(L), L, T, L * L, L * T, T * T]
    return B @ TR_INSET["cx"], B @ TR_INSET["cy"]


def read_csv(feed, name):
    path = f"{GTFS}/{feed}/{name}"
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def read_cols(feed, name, cols):
    """Rows of just `cols`, as tuples. DictReader builds a dict per row, which
    is most of the parse time on the multi-million-row stop_times.txt files;
    the hot loops only ever want a handful of columns. Missing columns come
    back as ''."""
    path = f"{GTFS}/{feed}/{name}"
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        r = csv.reader(f)
        try:
            head = next(r)
        except StopIteration:
            return []
        idx = [head.index(c) if c in head else -1 for c in cols]
        n = len(head)
        return [tuple(row[i] if 0 <= i < len(row) else "" for i in idx)
                for row in r if len(row) >= n - 1]


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


# Designations the sheet prints that the feed never says. An agency that brands
# a route instead of numbering it gets badged with the brand, and nothing in its
# GTFS carries that: Foothill's 707 is the Silver Streak, badged SS in eighteen
# places, and route_url is the only field that even hints at it. Labelling it
# 707 costs twice over — a rider sees a designation the map never prints, and
# the badges are also the anchors, so the shape has nothing pinning it to its
# own drawn line and wanders onto whichever Foothill green runs nearest.
#
# A whole agency can be designated that way too. The sheet's "Municipal &
# Neighboring Bus Lines" legend gives each of the smaller operators one symbol
# for the operator rather than one per route, and writes that symbol along its
# lines: Beach Cities Transit is "BC" everywhere, its 102 and 109 alike, and
# neither number is printed anywhere on the sheet. So both routes are badged BC
# — which is also what gives them anchors, exactly as the Silver Streak's do.
#
# A DASH is named rather than numbered, and the sheet abbreviates the name its
# own way. LADOT's feed calls the Wilmington loop "Wilmington Clockwise", which
# route_label initialises to "WC"; the sheet badges it "WM", four times along
# the loop. Watts is the same story and lands on the same four characters, so
# those two DASHes ran under one designation that the sheet prints for neither
# of them — a rider looking up a "WC" in Wilmington finds "WM", and one in
# Watts finds "WT". Both directions of a loop share the sheet's badge: the
# clockwise and counterclockwise workings are drawn as one line.
MAP_LABELS = {
    ("foothill", "20707"): "SS",   # Silver Streak
    ("beachcities", "4815"): "BC",  # 109, LAX Transit Center / Palos Verdes Blvd
    ("beachcities", "4819"): "BC",  # 102, Redondo Beach Pier / Green Line
    ("burbank", "3162"): "BU",      # Pink Route
    ("burbank", "3163"): "BU",      # NoHo - Airport
    ("ladot", "708"): "WM",         # DASH Wilmington, clockwise
    ("ladot", "710"): "WM",         # DASH Wilmington, counterclockwise
    ("ladot", "713"): "WT",         # DASH Watts, clockwise
    ("ladot", "714"): "WT",         # DASH Watts, counterclockwise
    # Two more of the same, and neither designation is one a reader could guess
    # from the feed's name. route_label takes the first token of "Boyle
    # Heights" and, at five characters, initialises it to "BH" — which the
    # sheet prints nowhere at all; it badges that loop "BE", three times. And
    # "El Sereno/City Terrace" it doesn't compress at all, because the first
    # token is already short enough to keep: every one of those buses carried
    # an "El", which the sheet does print seven times and never once for this
    # route — EL SEGUNDO, EL MONTE, place names. The loop is badged "SC",
    # Sereno/City Terrace, five times from Rose Hill round to City Terrace.
    ("ladot", "4867"): "BE",        # DASH Boyle Heights
    ("ladot", "4868"): "SC",        # DASH El Sereno/City Terrace
    # And Southeast, which has to come with them: route_label initialises
    # "Southeast Clockwise" to "SC" as well, so badging El Sereno the way the
    # sheet does would have put two unrelated loops, one in El Sereno and one
    # in Central-Alameda, under one designation — the very thing this table
    # exists to undo. "SC" was never Southeast's anyway; the sheet badges it
    # "SE", twice, 9 and 14 px from both directions' warps, and prints "SC"
    # only for Studio City up in the Valley and inside the words "SC AV".
    ("ladot", "1757"): "SE",        # DASH Southeast, clockwise
    ("ladot", "1758"): "SE",        # DASH Southeast, counterclockwise
    # Two Valley loops, and the second is the case this table exists for at its
    # sharpest. "Northridge" initialises to "NOR", which the sheet prints
    # nowhere — an honest orphan, and orphan_check has been listing it. "Van
    # Nuys/Studio City Clockwise" doesn't get initialised at all: its first
    # token is four characters, so route_label keeps it, and every one of those
    # buses ran badged "Van" — which the sheet *does* print, three times, in
    # "Van Nuys" the district and "Van Nuys Bl" the street, and not once as a
    # route. So it never showed up as an orphan while being the more misleading
    # of the two: a rider who goes looking for "Van" on the map finds it. The
    # sheet badges these loops "NR", once, on the drawn Wilbur Ave, and "VS",
    # twice, on Hazeltine and on Moorpark. Naming them also anchors them —
    # sheet_tokens is all a DASH has, so before this both loops snapped with no
    # anchor at all.
    ("ladot", "798"): "NR",         # DASH Northridge
    ("ladot", "799"): "VS",         # DASH Van Nuys/Studio City, clockwise
    ("ladot", "800"): "VS",         # DASH Van Nuys/Studio City, counterclockwise
}


def printed_on_map(token):
    """Whether the sheet prints this token anywhere — the test of whether a
    rider could look a vehicle's label up."""
    return any(token in badge_words(r) for r in ("main", "inset"))


def route_label(short, long_name):
    """The designation a vehicle carries, kept to four characters.

    A paired short name is badged on the map as its parts: 14/37 is printed
    "14" here and "37" there, never "1437", so running the halves together
    labels every one of those buses with something no rider can find. Where a
    designation splits, prefer whichever form the map actually prints — that
    keeps 14/37 as "14" while leaving Metrolink's IE-OC alone, since the sheet
    prints neither "IE" nor "IEOC" and the joined form at least reads as the
    line's name.

    A lettered working of a numbered route goes the same way. LADOT runs 437A
    round Marina del Rey and 437B through Playa Vista, and the sheet draws one
    line badged "437" — so both suffixes cost twice over, exactly as 14/37
    does: a rider sees a designation the map never prints, and the badges are
    also the anchors, so the shape has nothing pinning it to its own drawn
    line. 437A's run down Via Marina, where the warp is 130 px out, needed
    them."""
    s = (short or long_name or "?").strip()
    for pre in ("Metro ", "Metrolink "):
        if s.startswith(pre):
            s = s[len(pre):]
    if s.endswith(" Line"):
        s = s[:-5]
    tok = s.split()[0]
    stem = re.match(r"(\d+)[A-Za-z]+$", tok)
    if stem and not printed_on_map(tok) and printed_on_map(stem.group(1)):
        return stem.group(1)
    if len(tok) <= 4:
        return tok
    joined = tok.replace("-", "").replace("/", "")
    for part in re.split(r"[/-]", tok):
        if badge_like(part) and not printed_on_map(joined) and printed_on_map(part):
            return part
    if len(joined) <= 4:
        return joined
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
_BG = None

def bg_palette(k=12):
    """The map's dominant colors (background, freeways, parks, water...).
    Muted agency line colors can sit within mask tolerance of these — e.g.
    Culver City's khaki vs. freeway tan — so masks exclude pixels that match
    an infrastructure color better than the agency color."""
    global _BG
    if _BG is None:
        im, keep = map_image()
        sub = im[::4, ::4][keep[::4, ::4]]
        codes = (sub[:, 0] >> 3) * 1024 + (sub[:, 1] >> 3) * 32 + (sub[:, 2] >> 3)
        vals, counts = np.unique(codes, return_counts=True)
        top = vals[np.argsort(counts)[-k:]]
        _BG = np.c_[top // 1024, (top // 32) % 32, top % 32] * 8 + 4
    return _BG

LABEL_HALO = 24     # px; how far a label's knockout reaches past its glyphs
LABEL_REACH = 28    # px; how far into a gap the artwork is worth recovering
FADE_MIN = 0.30     # faintest line-over-page blend still read as drawn line
FADE_MARGIN = 6.0   # how much better this agency's blend must fit than a rival's


def box_dilate(m, radius):
    """Binary dilation by a (2*radius+1)² box. Separable running sums, so the
    cost doesn't grow with the radius the way a flat structuring element does —
    these radii are tens of pixels across the whole sheet."""
    f = m.astype(np.float32)
    for axis in (0, 1):
        f = ndi.uniform_filter1d(f, 2 * radius + 1, axis=axis, mode="constant")
    return f > 0


_GLYPHS = {}


def glyphs(sub):
    """Pixels that look like label text: near-gray and dark enough. Keyed by
    region shape and cached — it costs a full-sheet dilation and is the same
    for every agency, only the "not this agency's own color" part differs."""
    key = sub.shape
    if key not in _GLYPHS:
        mx, mn = sub.max(axis=2), sub.min(axis=2)
        _GLYPHS[key] = ndi.binary_dilation((mx - mn) < 26,
                                           np.ones((5, 5), bool)) & (mx < 215)
    return _GLYPHS[key]


def unfade(m, sub, d2a, tol, colors):
    """Re-add drawn-line pixels that a place-name label has dimmed.

    Labels sit on top of the artwork, and a color mask breaks wherever a name
    crosses a line — "WEST HOLLYWOOD" puts a ~45 px hole in Metro 2's Sunset
    line, and the snap then locks onto whichever parallel street stays unbroken
    (Metro 2 was landing a block south, on Santa Monica). But the label isn't
    painting the line out: under its halo the map knocks the artwork back
    toward the page, and Sunset crosses "WEST HOLLYWOOD" at roughly 40%
    opacity. The line is still there, just too pale for the mask's tolerance.

    So inside the halo, take a pixel that reads as this agency's color painted
    over the page at partial opacity: near the segment from the page color to
    the line color, and at least FADE_MIN of the way along it. Muted line
    colors dim into ordinary map grays, so — as in the mask itself — a pixel
    counts only when this agency's blend explains it better than any background
    or rival agency's does; without that test Big Blue Bus's gray claimed every
    light gray on the sheet. Recovery stays within LABEL_REACH of real artwork,
    since the point is to close gaps in drawn lines, not to find new ones.

    Glyphs still interrupt what's recovered, but only by a stroke width at a
    time, which nearest-pixel snapping rides straight over. Bridging those too
    (a morphological closing kept where text is) was tried and removed: it
    couldn't span this gap anyway — Sunset turns its corner inside the label —
    and it pulled the line low, since dilating "HOLLYWOOD" into the mask hangs
    a word-sized blob of text off the underside of the street."""
    text = glyphs(sub) & (d2a > (tol * 1.6) ** 2)  # an agency's own gray line isn't a label
    bgs = bg_palette()
    paper = np.asarray(bgs[-1], dtype=float)   # densest color: the page itself
    ys, xs = np.nonzero(box_dilate(text, LABEL_HALO) & box_dilate(m, LABEL_REACH) & ~m)
    out = np.zeros_like(m)
    if not len(ys):
        return out
    P = sub[ys, xs].astype(float) - paper

    def fit(c):
        """(distance², fraction of the way to `c`) for the page→c blend line."""
        d = np.asarray(c, dtype=float) - paper
        a = np.clip((P @ d) / (d @ d), 0, 1)
        return ((P - a[:, None] * d) ** 2).sum(1), a

    own, lit = np.full(len(P), np.inf), np.zeros(len(P), bool)
    for c in colors:
        d2, a = fit(c)
        own, lit = np.minimum(own, d2), lit | (a > FADE_MIN)
    # One independent fit per rival color, min-reduced — the bulk of the build.
    # numpy drops the GIL for work this size, so threads give real parallelism
    # and, unlike splitting the pixels up, every fit sees the same arithmetic
    # it would have alone.
    rivals = [r for r in np.vstack([bgs, rival_palette()])
              if min(((np.asarray(c) - r) ** 2).sum() for c in colors) >= 24 * 24
              and ((r - paper) ** 2).sum() >= 24 * 24]   # not our color, not the page
    rival = np.full(len(P), np.inf)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for d2 in pool.map(lambda r: fit(r)[0], rivals):
            rival = np.minimum(rival, d2)
    ok = lit & (own < (tol * 0.6) ** 2) & (np.sqrt(own) + FADE_MARGIN < np.sqrt(rival))
    out[ys[ok], xs[ok]] = True
    return out


MASK_CACHE = "scratch/mask-cache"


def art_stamp(*paths):
    """Cheap identity for the artwork a mask is read from: size and mtime of
    each file. Digesting the pixels would cost more than some of the masks."""
    out = []
    for p in paths:
        st = os.stat(p)
        out.append((p, st.st_size, st.st_mtime_ns))
    return out


def code_stamp(*fns):
    """Identity for the code that produces a mask, so editing any of it
    invalidates the cache by itself. Beats a version constant nobody remembers
    to bump, and unrelated edits elsewhere in this file don't disturb it."""
    src = "".join(inspect.getsource(f) for f in fns)
    return hashlib.blake2b(src.encode(), digest_size=8).hexdigest()


def cached_pixels(key, build):
    """Coordinates from `build()`, memoized on disk under a digest of `key`.

    The masks depend only on the artwork and the code that reads it — never on
    a GTFS feed — so they come out identical on every run, and rebuilding all
    28 of them was ~80% of the build. Only the coordinate array is stored; the
    KD-tree is rebuilt from it in well under the time it takes to read one."""
    h = hashlib.blake2b(repr(key).encode(), digest_size=16).hexdigest()
    path = f"{MASK_CACHE}/{h}.npy"
    if os.path.exists(path):
        try:
            return np.load(path)
        except Exception:
            pass                               # truncated or stale format
    pts = build()
    os.makedirs(MASK_CACHE, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:                 # a handle, or np.save would
        np.save(f, pts)                        # append .npy to the temp name
    os.replace(tmp, path)                      # atomic, so a killed build can't
    return pts                                 # leave a half-written entry


def mask_tree(colors, tol=38.0, region="main"):
    """KD-tree over map pixels within tol of ANY of the given colors and
    closer to one of them than to any dominant background color. region
    "main" is the map outside EXCLUDE; "inset" is the DTLA inset frame
    (minus its legend), which the main masks deliberately exclude.

    Everything is computed on the region's bounding box rather than the whole
    sheet — the inset is 2% of the map's area and there is one mask per
    agency color per shape, so full-image passes dominated the build."""
    key = (tuple(map(tuple, colors)), tol, region)
    if key not in _TREES:
        pts = cached_pixels(
            ("mask", key, art_stamp("map.png"), EXCLUDE,
             code_stamp(mask_pixels, unfade, box_dilate, bg_palette, rival_palette)),
            lambda: mask_pixels(colors, tol, region))
        _TREES[key] = cKDTree(pts) if len(pts) > 300 else None
    return _TREES[key]


STATION_MIN_AREA = 120     # px at MASK_LEVEL; a plain circle is about 220
STATION_RING_DARK = 0.5    # fraction of a marker's border that must be stroke


def station_markers(level=MASK_LEVEL):
    """Centres of the station markers the map draws, as an Nx3 array of
    (x, y, area) in map pixels.

    A station is a white shape with a black stroke — usually a circle, a pair
    of conjoined circles where lines meet, an oblong where more do, and odder
    oblongs again inside the Downtown panel. All of them share the same two
    properties, so the shape itself needn't be classified: the fill is pure
    white where the page around it is cream, and the border is stroke. Nothing
    else on the sheet is both, at this size — label halos are white but
    unstroked, and a freeway shield is stroked but is nowhere near a rail line,
    which is what the caller matches against."""
    def build():
        band = []
        for r in range(tile_rows(level)):
            band.append(np.hstack([
                np.asarray(Image.open(f"{TILES}/{level}/{c}_{r}.webp").convert("RGB"))
                for c in range(tile_cols(level))]))
        im = np.vstack(band).astype(np.int16)
        white, dark = im.min(2) >= 244, im.max(2) <= 90
        lab, n = ndi.label(white)
        areas = ndi.sum(white, lab, range(1, n + 1))
        big = np.nonzero(areas >= STATION_MIN_AREA)[0] + 1
        if not len(big):
            return np.zeros((0, 3))
        boxes = ndi.find_objects(lab)
        cy, cx = np.array(ndi.center_of_mass(white, lab, big)).T
        out = []
        for k, idx in enumerate(big):
            ys, xs = boxes[idx - 1]
            sub = lab[max(0, ys.start - 4):ys.stop + 4, max(0, xs.start - 4):xs.stop + 4] == idx
            d = dark[max(0, ys.start - 4):ys.stop + 4, max(0, xs.start - 4):xs.stop + 4]
            ring = ndi.binary_dilation(sub, np.ones((5, 5), bool)) & ~sub
            if ring.any() and d[ring].mean() >= STATION_RING_DARK:
                out.append((cx[k] / level, cy[k] / level, areas[idx - 1] / level ** 2))
        return np.array(out) if out else np.zeros((0, 3))
    return cached_pixels(("stations", level, STATION_MIN_AREA, STATION_RING_DARK,
                          art_stamp(f"{TILES}/{level}/0_0.webp"),
                          code_stamp(station_markers)), build)


_MARKER_TREE = []


def marker_tree():
    """station_markers() as a KD-tree, or None where the sheet drew none."""
    global _MARKER_TREE
    if _MARKER_TREE == []:
        M = station_markers()
        _MARKER_TREE = cKDTree(M[:, :2]) if len(M) else None
    return _MARKER_TREE


def tile_cols(level):
    n = 0
    while os.path.exists(f"{TILES}/{level}/{n}_0.webp"):
        n += 1
    return n


def tile_rows(level):
    n = 0
    while os.path.exists(f"{TILES}/{level}/0_{n}.webp"):
        n += 1
    return n


def tile_tree(colors, tol, level=MASK_LEVEL):
    """KD-tree of pixels matching `colors` read off the tile pyramid, in map
    pixels. For artwork whose printed color map.png's downscale destroys."""
    key = (tuple(map(tuple, colors)), tol, level)
    if key not in _TREES:
        pts = cached_pixels(
            ("tile", key, EXCLUDE, art_stamp(f"{TILES}/{level}/0_0.webp"),
             code_stamp(tile_scan)),
            lambda: tile_scan(colors, level, tol))
        _TREES[key] = cKDTree(pts[:, :2] / level) if len(pts) > 300 else None
    return _TREES[key]


def tile_region(box, level=MASK_LEVEL):
    """The tile pyramid over a map-px box, as an HxWx3 array at `level`."""
    x0, y0, x1, y1 = (int(v * level) for v in box)
    out = np.zeros((y1 - y0, x1 - x0, 3), dtype=np.int32)
    for ty in range(y0 // TILE, (y1 - 1) // TILE + 1):
        for tx in range(x0 // TILE, (x1 - 1) // TILE + 1):
            path = f"{TILES}/{level}/{tx}_{ty}.webp"
            if not os.path.exists(path):
                continue
            im = np.asarray(Image.open(path).convert("RGB"))
            ax0, ay0 = max(x0, tx * TILE), max(y0, ty * TILE)
            ax1, ay1 = min(x1, (tx + 1) * TILE), min(y1, (ty + 1) * TILE)
            out[ay0 - y0:ay1 - y0, ax0 - x0:ax1 - x0] = \
                im[ay0 - ty * TILE:ay1 - ty * TILE, ax0 - tx * TILE:ax1 - tx * TILE]
    return out


_INSET_TREES = {}

# A route chip printed inside a station's label plate is filled with the line's
# own colour, so no colour mask can tell it from the line. In the magnified
# call-out it is a solid disc of that colour sitting a few tens of px off the
# ribbon — near enough to capture a shape whose warp passes closer to it than
# to the line, and both the B and the D dived into their own chips on the "7th
# St/Metro Center" plate and came back. A drawn line is longer than a chip is
# wide whichever way it runs, though: every ribbon component in the panel spans
# at least 19 map px along its length and the chips all measure 8-9 square. So
# a component that fits inside a chip's own footprint in both axes is not a line.
INSET_CHIP_SPAN = 12.0     # map px


def inset_tile_tree(colors, tol=MASK_TOL, level=MASK_LEVEL):
    """A mask of the Downtown call-out read off the tile pyramid, where the
    printed color is faithful, rather than off map.png's blend of it.

    Rail on the main map has always been masked this way. The call-out was not,
    and it is where it matters most, because the panel is the one place the
    sheet redraws every downtown line at a legible size — so a line that misses
    its mask there misses it in the only view that shows the difference.
    map.png renders the E Line's printed (254,186,18) as (233,181,74), which is
    60.0 away: exactly the rail tolerance, so `< 60` matched not one pixel of
    it in the whole panel and the E kept its raw warp from 7th/Metro Center to
    Little Tokyo, cutting diagonally across the blocks it is drawn along. On
    the pyramid the same gold sits 0.0 from its own color.

    The scan is the call-out only, which `tile_tree` cannot do — EXCLUDE cuts
    this rectangle out of the sheet, being a redrawing of a network that is
    also drawn elsewhere on it, and every other caller wants that."""
    key = (tuple(map(tuple, colors)), tol, level)
    if key not in _INSET_TREES:
        def build():
            band = tile_region(INSET_RECT, level)
            d2 = np.full(band.shape[:2], np.inf, dtype=float)
            for rgb in colors:
                d2 = np.minimum(d2, ((band - np.array(rgb)) ** 2).sum(2))
            m = d2 < tol * tol
            lx0, ly0, lx1, ly1 = INSET_LEGEND    # the panel's own key, not route
            m[(ly0 - INSET_RECT[1]) * level:(ly1 - INSET_RECT[1]) * level,
              (lx0 - INSET_RECT[0]) * level:(lx1 - INSET_RECT[0]) * level] = False
            lab, n = ndi.label(m)
            span = INSET_CHIP_SPAN * level
            for k, (ys, xs) in enumerate(ndi.find_objects(lab), 1):
                if max(ys.stop - ys.start, xs.stop - xs.start) < span:
                    m[ys, xs] &= lab[ys, xs] != k      # a chip, not a line
            ys, xs = np.nonzero(m)
            return np.c_[xs / level + INSET_RECT[0], ys / level + INSET_RECT[1]]
        P = cached_pixels(
            ("inset-tile", key, INSET_RECT, INSET_LEGEND,
             art_stamp(f"{TILES}/{level}/0_0.webp"),
             code_stamp(inset_tile_tree, tile_region)), build)
        _INSET_TREES[key] = cKDTree(P) if len(P) > 300 else None
    return _INSET_TREES[key]


def mask_pixels(colors, tol, region):
    """Coordinates of every pixel the mask covers, as an Nx2 array."""
    im, keep_full = map_image()
    if region == "inset":
        x0, y0, x1, y1 = INSET_RECT
        keep = np.ones((y1 - y0, x1 - x0), dtype=bool)
        lx0, ly0, lx1, ly1 = INSET_LEGEND
        keep[ly0 - y0:ly1 - y0, lx0 - x0:lx1 - x0] = False  # sample artwork
    else:
        x0, y0 = 0, 0
        y1, x1 = keep_full.shape
        keep = keep_full
    sub = im[y0:y1, x0:x1]
    d2a = np.full(keep.shape, np.inf)
    for rgb in colors:
        d2a = np.minimum(d2a, ((sub - np.array(rgb)) ** 2).sum(axis=2))
    m = d2a < tol * tol
    # The background test only matters where the color test already passed, so
    # run it on those pixels rather than the sheet — a few hundred thousand
    # instead of 17 million, and identical either way.
    ys, xs = np.nonzero(m)
    if len(ys):
        P, dc = sub[ys, xs], d2a[ys, xs]
        ok = np.ones(len(ys), dtype=bool)
        for bg in bg_palette():
            if min(((np.array(c) - bg) ** 2).sum() for c in colors) < 24 * 24:
                continue                   # bg IS this agency's color; keep
            ok &= dc < ((P - bg) ** 2).sum(1)
        m[ys[~ok], xs[~ok]] = False
    # after the background filter: line dimmed under a label reads as page
    # color, which every background test above would reject
    m = m | unfade(m, sub, d2a, tol, colors)
    ys, xs = np.nonzero(m & keep)
    return np.c_[xs + x0, ys + y0]

# Metro's drawn line colors (sampled from the map)
ORANGE = (217, 129, 83)     # Metro Local/Rapid orange
RAPID_RED = (180, 51, 61)   # 720/754/761
BUSWAY_GRAY = "969CA0"      # J Line 910/950 freeway busway ribbon
JLINE_GRAY = (173, 184, 191)     # that ribbon as the PDF strokes it (see JLINE_INK)
BUSWAY_ORANGE = (243, 123, 33)   # G Line busway ribbon, as printed on the sheet
BUSWAY_TOL = 30.0
# The busway holds that one ribbon and nothing else, so the snap can reach much
# further than it dares on the shared orange, and can follow the line closely
# instead of smoothing whole stretches together.
BUSWAY_CAPS = (100.0, 50.0, 25.0, 12.0)
BUSWAY_WIN = 9

# Drawn colors for feeds whose lines can't be color-masked, sampled from the
# map, so vehicle sprites still match the artwork they ride on.
DRAWN_COLORS = {
    # Plain gray, the same as the street art. Re-read once the shapes were
    # snapped onto that art: it had been sampled at (204,193,184) and the
    # sprites came out visibly paler than the lines they ride on. A line two or
    # three px wide is mostly edge, so anything that averages across its width —
    # or takes the dominant cluster, which for a gray line is the palest blend
    # rather than the fill — reads the page as much as the ink. Measured along
    # the snapped paths and taken across the line rather than along it, the
    # printed gray is this, and a direct profile of the drawn corridor at
    # (2225,1348) agrees: (160,160,151) to (185,178,172).
    "pasadena": (172, 171, 161),
    "metrolink": (120, 124, 126),  # crosshatched railroad gray
}

# ---- the street grid, for the one agency that has no line of its own ----
#
# Pasadena Transit is drawn in the same gray as the street art, so no color
# finds its lines and not the grid — which is why it was left on the raw warp.
# But look at what the sheet actually does: it gives PT no livery at all. It
# prints "PT" beside the street the route runs on, in gray text, and the line
# under that label is the street. There is nothing else to find.
#
# For every other agency that would be fatal. Here it is not, because a PT bus
# runs *on* those streets: the grid is the right thing to snap to, and the only
# question is which street of it. The warp already answers that nearly
# everywhere — measured against this mask its median error is 3 px, i.e. it is
# on the correct street and a line-width off it — and the fault is entirely in
# the excursions, where it drifts into the white between two streets and stays
# there for a few hundred px. Snapping with a short reach closes those without
# ever having to choose a street, because the stretches either side of an
# excursion are already on the right one and the smoothed field carries it.
#
# The mask for it is the PDF's street strokes, and it had to be: a raster
# version was tried first and got the median down to 1.0 px while leaving the
# case that prompted all this untouched. See street_ink() for why — in short,
# the sheet's lettering is gray of the same hue, and a word's strokes are a
# better match for a route than the one-px street 27 px away.
# A grid runs both ways at once, and that is the whole difficulty: a route
# crossing it is *on* ink at every intersection. PT 31, 32 and 33 run the length
# of Washington Blvd — the GTFS stop list says so, forty consecutive
# "Washington Blvd & …" — with the warp 26 px north of the drawn Washington, and
# the only ink under that run is where it crosses Fair Oaks, Lake and Altadena
# Dr, each 0-2 px away. Those crossings contribute a displacement of zero, and
# smoothed over the 61-point window they outvote every point that wants to move
# and hold the whole run out in the white. Widening the cap does nothing at all —
# tried, 26 to 34, no change — because reach was never the problem. The problem
# is that a street running north-south is allowed to claim a point travelling
# east-west.
#
# So the ink is binned by the direction it runs, and a point may only be claimed
# by ink going roughly its own way. For a colored livery this would be pointless:
# the mask is already that one route's line and everything in it is the right
# direction by construction. For the grid it is what makes the mask usable at all.
DIR_BINS = 6                # 30 deg apart; a line has no sense, so 0..180
DIR_SLACK = 1               # bins either side: accept within ~45 deg


# The street layer, taken from the PDF's strokes rather than from the raster.
#
# A raster mask cannot be used for this one. Every other agency's mask is keyed
# on a color only that agency's lines are drawn in, so lettering that happens to
# fall inside the tolerance is a rim of speckle around the words and
# solid_pixels() deals with it. The street gray has no such luck: the sheet sets
# its labels in a gray of the same hue, and the antialiased strokes of a word are
# indistinguishable from a one-px street — the "NEW YORK" label sits 8 px from
# where PT 31/32/33 run and drew them to itself, while the drawn Washington Blvd
# 27 px away is a single row of pixels at this resolution. No luminance band
# separates the two, and no direction test can either, since a horizontal word is
# horizontal.
#
# get_drawings() returns strokes, and text is not a stroke. It also gives the
# direction exactly, from the segment itself, instead of having it estimated off
# a blurred raster.
STREET_STROKES = [(0.604, 0.561, 0.579), (0.61, 0.563, 0.583)]
STREET_STEP = 2.0          # px between sampled points along a stroke


def street_ink(step=STREET_STEP):
    """Points along every street stroke, as (x, y, direction bin) — Nx3."""
    def build():
        try:
            import fitz
        except Exception as e:
            print(f"PDF ink extraction unavailable: {e}")
            return np.zeros((0, 3))
        page = fitz.open(PDF)[0]
        s = map_image()[0].shape[1] / page.rect.width
        out = []
        for it in page.get_drawings():
            c = it.get("color")
            if c is None or not any(sum((a - b) ** 2 for a, b in zip(c, k)) < 1e-4
                                    for k in STREET_STROKES):
                continue
            runs = []
            for seg in it["items"]:
                if seg[0] == "l":
                    runs.append([(seg[1].x * s, seg[1].y * s),
                                 (seg[2].x * s, seg[2].y * s)])
                elif seg[0] == "c":
                    p = [np.array([q.x * s, q.y * s]) for q in seg[1:5]]
                    t = np.linspace(0, 1, 12)[:, None]
                    b = ((1 - t) ** 3 * p[0] + 3 * (1 - t) ** 2 * t * p[1]
                         + 3 * (1 - t) * t ** 2 * p[2] + t ** 3 * p[3])
                    runs.append([tuple(q) for q in b])
            for r in runs:
                if len(r) < 2:
                    continue
                q = np.asarray(densify(r, step), dtype=float)
                if len(q) < 2:
                    continue
                d = np.gradient(q, axis=0)
                a = np.mod(np.arctan2(d[:, 1], d[:, 0]), np.pi)
                bins = np.minimum((a / (np.pi / DIR_BINS)).astype(np.int64),
                                  DIR_BINS - 1)
                out.append(np.c_[q, bins])
        if not out:
            return np.zeros((0, 3))
        P = np.vstack(out)
        keep = map_image()[1]
        h, w = keep.shape
        P = P[(P[:, 0] >= 0) & (P[:, 0] < w) & (P[:, 1] >= 0) & (P[:, 1] < h)]
        P = P[keep[P[:, 1].astype(int), P[:, 0].astype(int)]]
        return P[~inside_callout(P[:, :2])]

    return cached_pixels(
        ("street-ink", tuple(map(tuple, STREET_STROKES)), step, DIR_BINS,
         art_stamp(PDF), EXCLUDE, CALLOUT,
         code_stamp(street_ink, densify, inside_callout)),
        build)


class DirectionalTree:
    """A KD-tree that will only match ink running the same way as the line.

    Stands in for a cKDTree everywhere snap_coherent uses one: `.data`, and a
    `.query` that takes the polyline in order — which is what lets it know the
    heading at all, since the points arrive along the line rather than as a bag.
    `.plain` is the undirected tree, for solid_pixels."""

    def __init__(self, xyb):
        self.data = np.ascontiguousarray(xyb[:, :2], dtype=float)
        self.plain = cKDTree(self.data)
        bins = xyb[:, 2].astype(int)
        self._idx = [np.nonzero(bins == b)[0] for b in range(DIR_BINS)]
        self._trees = [cKDTree(self.data[i]) if len(i) else None
                       for i in self._idx]

    def query(self, P):
        P = np.asarray(P, dtype=float)
        step = np.gradient(P, axis=0)
        ang = np.mod(np.arctan2(step[:, 1], step[:, 0]), np.pi)
        pb = np.minimum((ang / (np.pi / DIR_BINS)).astype(np.int64), DIR_BINS - 1)
        best_d = np.full(len(P), np.inf)
        best_j = np.zeros(len(P), dtype=np.int64)
        for b in range(DIR_BINS):
            sel = np.nonzero(pb == b)[0]
            if not len(sel):
                continue
            for o in range(-DIR_SLACK, DIR_SLACK + 1):
                nb = (b + o) % DIR_BINS
                t = self._trees[nb]
                if t is None:
                    continue
                d, j = t.query(P[sel])
                win = d < best_d[sel]
                best_d[sel[win]] = d[win]
                best_j[sel[win]] = self._idx[nb][j[win]]
        return best_d, best_j


def street_tree():
    key = ("street-ink", tuple(map(tuple, STREET_STROKES)), DIR_BINS, STREET_STEP)
    if key not in _TREES:
        xyb = street_ink()
        _TREES[key] = DirectionalTree(xyb) if len(xyb) > 300 else None
    return _TREES[key]


# Short, because the grid is dense and a long reach would let a stretch hop to
# the next street over. The excursions this exists to close run 15-30 px against
# a 3 px median, and the coherent field does most of the work: the points either
# side of an excursion are already on the right street and hold it there.
#
# 26 was tried first and was a hair too short for the case that prompted all
# this. PT 31, 32 and 33 all run the length of Washington Blvd — the GTFS stop
# list says so, forty consecutive "Washington Blvd & …" — and the warp puts them
# 26-28 px north of the drawn Washington, in the white between it and Woodbury
# Rd, which is 58 px away on the other side. At a cap of 26 nothing along that
# stretch was in reach and the field stayed flat. 34 reaches Washington with
# margin and still cannot reach past the neighbouring street, and the nearest
# candidate wins, so the choice between the two is made by the 26 against the 32
# rather than by the cap.
STREET_CAPS = (34.0, 20.0, 10.0)

# Feeds with no drawn line of their own, snapped to the grid instead. Metrolink
# is deliberately not here: it has a livery (a crosshatched railroad gray) and
# its own anchors in the names the sheet writes along each track.
STREET_SNAP = {"pasadena"}

# Per-agency drawn-line color seeds, sampled from the map's legend swatches.
# Thin dashes sample washed-out, so each seed is refined against pixels found
# along the agency's actual routes before masking. Pasadena Transit's color is
# plain gray (identical to street art), so it keeps the polynomial warp.
# Badge fill colors that differ from the drawn line color: used only for
# anchor detection (the words sit on light chips), never for line snapping.
#
# Long Beach Transit wants one for the other half of the job. Its chips are not
# hard to *find* — they are the same maroon as its lines and sit inside its own
# mask, so the presence test has always passed them — but they are that maroon
# saturated, the legend's ink, read off 466 badges as (126,33,58) with a couple
# of px of spread. Its lines are the same ink laid thin over a cream page, and
# come back a washed (134,98,101) once refine_color has been over them. Those
# are 66 apart, further than the chip stands from Metro's Rapid red (180,51,61)
# — so the gate asking whether a chip is better explained by some *other*
# agency's colour than by this one's answered "Metro", every time, and threw out
# every badge Long Beach Transit has. The whole network snapped unanchored.
# Naming the chip colour is what puts it back in the agency's own set.
#
# Route 8 is what that cost. The sheet badges it "8" four times along the 223rd
# St line the feed names it after ("223rd St / Wardlow Rd"), and the warp puts
# it a block south — near enough to Sepulveda for the mask to take it there,
# and out across Carson, where the sheet draws nothing between the two, near
# enough to nothing at all: the path left the drawn line at Main St and ran a
# diagonal over blank page under the word CARSON. With the chip in the own-set
# the four badges are the 8's again, and they are on 223rd.
BADGE_FILLS = {
    "foothill": (118, 140, 120),
    "longbeach": (126, 33, 58),
    # LADOT's DASH chips, read off every one of them within a couple of px:
    # a flat olive, darker and far less washed than either livery's drawn
    # stroke. Wanted because a DASH designation is two capitals and the sheet
    # is covered in two-capital words — see ladot_livery, where this is the
    # test that tells a chip from a street label.
    "ladot": (105, 103, 55),
}

LEGEND_SEEDS = {
    "culvercity": [(215, 215, 157)],
    "gtrans": [(198, 165, 188)],
    "ladot": [(175, 170, 141), (154, 150, 117)],   # DASH + Commuter Express olives
    # Two, because Long Beach Transit's mask was only ever finding the *edges*
    # of its thick lines. The sheet strokes them (98,38,53) and the shoulder
    # where that meets the cream page reads (137,92,91); refine_color, sampling
    # along a warp that is mostly beside the line, settles on the shoulder —
    # (133,98,99) — and at tol 30 that takes in the shoulder and nothing else.
    # The core of a thick line is 47 away and out; so is every line the sheet
    # draws *thin*, which at 4096 px is a single-pixel core of (112,60,70)
    # between two pale blends, and has no pixel inside the mask anywhere along
    # it. Route 22 down Clark Av is drawn that way and there was nothing for it
    # to snap to: the nearest mask pixel to its path is 44 px off, on the
    # lettering, while its own line runs 10 px away. Naming the stroke itself
    # as a second seed refines to (104,48,58) and puts the ink in the mask.
    # Over the agency, measured as the share of path standing more than 12 px
    # from any LBT ink, that is 4.83% to 4.32%; the 91 round Cal State Long
    # Beach covers 59/78/59% of its drawn staple against 74/90/74%.
    "longbeach": [(136, 88, 92), (98, 38, 53)],
    "bigbluebus": [(143, 135, 136)],
    "foothill": [(62, 100, 78)],    # dark evergreen lines; legend swatch too pale
    "montebello": [(172, 186, 153)],
    "torrance": [(137, 139, 174)],
    "burbank": [(132, 168, 155)],
    # The same evergreen Foothill is drawn in — the sheet's legend gives both
    # agencies the one swatch, and the sheet is 60 km wide enough that they
    # never meet. The seed here used to be (170,181,169), a pale gray-green
    # that is nothing on the page: refine_color drifted it to a flat gray, the
    # mask came back as street art and lettering, and both Beach Cities routes
    # snapped to whatever ran nearest.
    "beachcities": [(62, 100, 78)],
}

# The two operators the sheet symbolises by *agency* rather than by route, and
# so the two that snap on their legend ink rather than on a color mask. Neither
# has a route number printed anywhere: the sheet writes "BC" beside every Beach
# Cities line and "BU" beside every BurbankBus one, and that is the whole of
# what it says about either. Those codes are set as plain text alongside the
# line the way a Commuter Express number is, never on a chip, so the mask's
# presence test — the agency's own pixels *under* the word — finds a handful of
# antialiased glyph strokes and rejects them, and both agencies snapped with no
# anchors at all. The strokes answer both halves at once: they are where the
# line is, and distance to them is a test a word standing beside a line can
# pass. See `route_anchors`'s `near`, which LADOT's Commuter Express uses for
# exactly this.
SYMBOL_FEEDS = {"beachcities", "burbank"}

# Which route each of those symbols stands for, where the sheet's own answer is
# "the operator" and the routes cannot be told apart any other way.
#
# One code for the whole agency means every one of its words is a candidate
# anchor for every one of its routes, and `branch_anchors` is what divides them
# up: a word speaks for whichever variant passes nearest it. That reading holds
# while the warp is nearer the truth than the routes are to each other, and in
# Burbank it is not. Out here the warp is 60-90 px out and turning with it — it
# puts the Pink Route's Downtown Burbank terminus 81 px north of where the sheet
# draws it, its Riverside Dr 48 px north and 50 px west — while the two routes
# are drawn a few blocks apart. So the distances came out backwards: of the three
# "BU"s on the sheet, the Pink Route was nearest to all three, and the Orange
# Route was left with the one it happened to win outright, 31.6 px against 48.5.
#
# The two it should not have had are the "BU" printed under Burbank Bl and the
# one on Buena Vista — the Orange Route's own streets, which the Pink Route
# never touches. Anchored to them, the Pink Route was fitted bodily onto the
# Orange Route's drawing: down Buena Vista, out to Olive and back up in a
# 130 px triangle over blank page, then west along Burbank Bl and round the
# North Hollywood loop — 62% of the Orange Route's drawn corridor covered by
# the wrong route, and 12% of its own.
#
# Nothing on the sheet can settle this, so it is settled by hand. A route listed
# here takes exactly the words listed for it and nothing else; a route of the
# same agency left out of the table is divided up by `branch_anchors` as before.
# Both BurbankBus routes are here, which makes the division complete: with the
# words handed to the lines they are printed on, the Pink Route needs no pin and
# no hand-drawn corridor — it comes out on 100% of its own drawn line, a median
# 1.0 px off it. Beach Cities is not here and does not need to be: its two
# routes run a mile apart at the nearest, further than the warp is wrong down
# there, and `branch_anchors` divides its "BC"s correctly on its own.
SYMBOL_OWNERS = {
    ("burbank", "3162"): [(1425.4, 1431.8)],                    # Pink Route
    ("burbank", "3163"): [(1368.0, 1369.2), (1407.0, 1303.2)],  # Orange Route
}
SYMBOL_OWNER_NEAR = 6.0   # px a listed point may stand from the word it names

# Each agency's line as the sheet's own legend swatch strokes it, straight off
# the PDF. Where an agency's seed can't be refined against the artwork these
# say where its ink is, so the reading can be taken on the drawn line itself.
LEGEND_INK = {
    "bigbluebus": (0.604, 0.5608, 0.5794),
    "culvercity": (0.8051, 0.8047, 0.4456),
    "gtrans": (0.7995, 0.5765, 0.74),
    "longbeach": (0.4995, 0.1196, 0.2324),
    "foothill": (0.1677, 0.4166, 0.3111),
    "beachcities": (0.1677, 0.4166, 0.3111),   # one swatch serves both
    "montebello": (0.6295, 0.7205, 0.5582),
    "torrance": (0.4859, 0.5034, 0.7086),
    "burbank": (0.2691, 0.5799, 0.5177),
}

# Agencies that snap onto those strokes rather than onto their colour mask, the
# way Metro and LADOT do: the mask still supplies the anchors, since a badge is
# a chip filled with the line colour and not a stroke of it, but the line the
# shape is pulled onto is the drawing itself.
#
# Montebello is here because a colour mask cannot hold its sage at all. Its
# thick corridors — the stretches two or three routes share — sit inside the
# refined (165,174,149) and mask solidly; the stretches only one route runs are
# drawn thin, and at 4096 px a thin sage line is a blend with the cream page
# that reads (183,194,162) and paler. 35% of the agency's strokes have no mask
# pixel on them. What *is* in the mask instead is the sheet's grey street
# lettering, which blends into range from the other side — "PICO RIVERA",
# "WASHINGTON", "PASSONS", "BROADWAY" all come back as Montebello ink.
#
# So the 50 down Washington Bl had a mask with its own line missing and the
# words printed beside it present, and the badge-to-badge walk did what the
# drawing told it: from the "50" at Washington & Montebello Bl the lettering
# bridges north onto the thick 10/60 corridor along Whittier Bl, which runs
# unbroken to Whittier, and 338 px that way beat the 304 px of drawn Washington
# — so the walk was believed, and the shape rode Whittier Bl across Pico Rivera
# and dropped back down to the badge at Mar Vista as a 165-degree cusp. West of
# there the same hole put the Grande Vista jog — Soto up to Olympic, east, and
# down Grande Vista back to Washington, which the sheet draws as a compact
# dog-leg — 158 px round Metro's orange Olympic Bl instead of the 76 px the
# sheet draws it in.
#
# The Long Beach answer — naming the thin stroke as a second seed — is no use
# here. LBT's thin lines have a dark core, (112,60,70) against a cream page, so
# a second seed lands on the ink and nothing else. Montebello's thin lines have
# no core: the missed readings smear from (183,194,162) to (221,224,199) with
# no cluster in them, and a seed pale enough to cover the strokes takes the page
# with it — the mask goes from 167k pixels to 977k, six times the artwork.
#
# The PDF has the same lines as vectors, thin and thick alike, complete under
# every label painted over them and with no chips or lettering in them at all.
# On drift_check, which measures this agency on those strokes now for the same
# reason, seven of the eight routes improve and every one of the 37 shapes
# moves: the 50 goes from 27% of its arc standing over 12 px off its own ink to
# 0, the 90 40% to 3, the 30 27% to 0, the 70 24% to 9, the 10 17% to 1, the 20
# 7% to 3, the 40 zero throughout. The eighth is the 60, whose drifting arc is
# 212 px either way — its northern loop up to Whittier Narrows runs over page
# the sheet draws it no line on, and only the share of that classed as beyond
# the drawing moves, 40 px to 24.
INK_SNAP = {"montebello"}


def refine_color(shape_pts, seed, r2=55 * 55, need=250):
    """Median of pixels along the shapes that are close to the seed color.
    Pixels that match a dominant background color better than the seed are
    dropped — street gray sits within sampling range of the muted agency
    seeds and would drag the median gray (Montebello's sage came out gray)."""
    im, keep = map_image()
    h, w = keep.shape
    seed = np.array(seed)
    bgs = [b for b in bg_palette() if ((b - seed) ** 2).sum() > 24 * 24]
    samples = []
    for pts in shape_pts[:20]:
        for x, y in densify(pts, 10.0):
            xi, yi = int(x), int(y)
            if not (1 <= xi < w - 1 and 1 <= yi < h - 1) or not keep[yi, xi]:
                continue
            for dx, dy in ((0, 0), (2, 0), (-2, 0), (0, 2), (0, -2)):
                c = im[yi + dy, xi + dx]
                d2s = ((c - seed) ** 2).sum()
                if d2s < r2 and all(((c - b) ** 2).sum() > d2s for b in bgs):
                    samples.append(c)
    if len(samples) < need:
        return None
    return tuple(np.median(samples, axis=0).astype(int).tolist())


def stroke_color(feed):
    """What map.png makes of `feed`'s drawn line, read on the sheet's own
    strokes rather than along a route.

    `refine_color` samples along the warp, which asks the warp to be on the
    line already. That holds for most of the municipal agencies and fails for
    exactly the two that need it most. Beach Cities Transit is drawn twice, up
    the coast and out to LAX, and its warp is far enough off both that a
    correct seed still comes back gray — the samples are the street art it is
    lying over, not its own evergreen. BurbankBus is drawn barely at all: 1144
    stroke points on the whole sheet, and two shapes to sample along, which
    cannot reach the 250 samples a refinement needs however well aimed.

    The legend says which ink is whose, so there is somewhere better to look:
    the strokes themselves. The dominant cluster of what sits on them is the
    line's own fill — a median across a 1-2 px line at 4096 px is half page,
    the reading `drawn_color` describes — and it needs no route to be right
    about anything. Returns None where the sheet strokes too little of the
    agency to read."""
    rgb = LEGEND_INK.get(feed)
    if rgb is None:
        return None
    P = pdf_ink([rgb]).astype(int)
    im, keep = map_image()
    h, w = keep.shape
    P = P[(P[:, 0] >= 0) & (P[:, 0] < w) & (P[:, 1] >= 0) & (P[:, 1] < h)]
    if len(P) < 200:
        return None
    px = im[P[:, 1], P[:, 0]].astype(int)
    bins = (px // 12) @ np.array([10000, 100, 1])
    vals, counts = np.unique(bins, return_counts=True)
    return tuple(np.median(px[bins == vals[counts.argmax()]], axis=0)
                 .astype(int).tolist())


SPRITE_LEVEL = 2      # tile pyramid level the sprite colors are read from
HUE_TOL = 30.0        # deg a sampled pixel may differ from the seed's hue
_TILES = {}


def tile_pixel(x, y, level=SPRITE_LEVEL):
    """Color at map pixel (x, y) read off the tile pyramid, or None off-sheet.
    Tiles are cached as uint8; a route touches only a hundred or so."""
    tx, ty = int(x * level), int(y * level)
    key = (level, tx // TILE, ty // TILE)
    im = _TILES.get(key)
    if im is None:
        path = f"{TILES}/{level}/{key[1]}_{key[2]}.webp"
        if not os.path.exists(path):
            return None
        im = _TILES[key] = np.asarray(Image.open(path).convert("RGB"))
    return im[ty % TILE, tx % TILE].astype(np.int32)


def tile_patch(cx, cy, r, level=SPRITE_LEVEL):
    """The pixels of a (2r+1) map-px square around (cx, cy), off the tile
    pyramid, as an Nx3 array — or None where the pyramid doesn't cover it.
    Spans tile boundaries; the tiles it touches join `tile_pixel`'s cache."""
    x0, y0 = int((cx - r) * level), int((cy - r) * level)
    x1, y1 = int((cx + r) * level) + 1, int((cy + r) * level) + 1
    if x0 < 0 or y0 < 0:
        return None
    out = []
    for ty in range(y0 // TILE, (y1 - 1) // TILE + 1):
        for tx in range(x0 // TILE, (x1 - 1) // TILE + 1):
            key = (level, tx, ty)
            im = _TILES.get(key)
            if im is None:
                path = f"{TILES}/{level}/{tx}_{ty}.webp"
                if not os.path.exists(path):
                    return None
                im = _TILES[key] = np.asarray(Image.open(path).convert("RGB"))
            ax0, ay0 = max(x0, tx * TILE), max(y0, ty * TILE)
            ax1, ay1 = min(x1, (tx + 1) * TILE), min(y1, (ty + 1) * TILE)
            if ax1 <= ax0 or ay1 <= ay0:
                continue
            out.append(im[ay0 - ty * TILE:ay1 - ty * TILE,
                          ax0 - tx * TILE:ax1 - tx * TILE].reshape(-1, 3))
    return np.vstack(out).astype(int) if out else None


def drawn_color(shape_pts, seed, r2=55 * 55, need=250, level=SPRITE_LEVEL):
    """The color the map prints this agency's lines in, for its vehicle sprites.

    Read off the tile pyramid rather than map.png, and taken as the dominant
    color cluster rather than a median. map.png is a 4096 px reduction of a 47"
    sheet, so a thin drawn line is mostly edge there and averaging its blend
    with the page desaturates it: Foothill's evergreen came out (77,102,85), a
    gray-green, against the (54,103,77) actually printed. The dominant cluster
    recovers the line's own fill, the same way badge_line_color recovers a
    chip's, and every agency ends up at least as saturated as before.

    Pixels better explained by a dominant background color are dropped, as in
    refine_color. Where the seed is a colored line rather than a gray one, two
    more filters apply, because a dominant cluster is far more outlier-prone
    than a median: near-neutral pixels go, or the street grays the line crosses
    outvote it (LADOT's DASH olive came out neutral); and so does anything off
    the seed's hue, or a foreign line crossing does (LADOT then came out pink).
    Fading a line into the page keeps its hue, so this costs nothing real.

    The mask colors stay with refine_color: those search map.png, so they want
    that image's rendering of the line, not the sheet's true ink."""
    seed = np.array(seed)
    bgs = [b for b in bg_palette() if ((b - seed) ** 2).sum() > 24 * 24]
    chroma = int(seed.max() - seed.min())
    floor = chroma * 0.5 if chroma >= 20 else 0
    seed_hue = colorsys.rgb_to_hsv(*(seed / 255))[0] * 360
    samples = []
    for pts in shape_pts[:20]:
        for x, y in densify(pts, 10.0):
            for dx, dy in ((0, 0), (2, 0), (-2, 0), (0, 2), (0, -2)):
                c = tile_pixel(x + dx, y + dy, level)
                if c is None or int(c.max() - c.min()) < floor:
                    continue
                if floor:
                    h = colorsys.rgb_to_hsv(*(c / 255))[0] * 360
                    if min(abs(h - seed_hue) % 360, 360 - abs(h - seed_hue) % 360) > HUE_TOL:
                        continue
                d2s = ((c - seed) ** 2).sum()
                if d2s < r2 and all(((c - b) ** 2).sum() > d2s for b in bgs):
                    samples.append(c)
    if len(samples) < need:
        return None
    S = np.asarray(samples)
    bins = (S // 12) @ np.array([10000, 100, 1])
    vals, counts = np.unique(bins, return_counts=True)
    return tuple(np.median(S[bins == vals[counts.argmax()]], axis=0).astype(int).tolist())

# ---- drawn lines from the source PDF's vectors ---------------------------
# A color mask can only find a line the raster actually shows, and the sheet
# draws lines it does not. The two ways that happens both leave a shape snapped
# to whatever *is* visible nearby, which is a neighbouring street:
#
#  - The color is the page's own furniture. Metrolink rides the crosshatched
#    railroad, inked in the same gray the sheet uses for place labels and minor
#    street art, so masking that color selects most of the page — 141k pixels
#    spread over the whole sheet. LADOT's olive is the same story: its mask
#    comes back 278k pixels, most of them label glyphs, and every LADOT route
#    snaps to the nearest word instead of to its own line.
#  - The line is too slight to survive the rendering. A part-time service is
#    drawn as a thin dashed line, and at 4096 px those dashes blend into the
#    page or vanish under a heavier line drawn alongside — Metro 233 through
#    the Sepulveda Pass is dashed orange laid against the 761's red ribbon, and
#    nothing of it reaches the raster at all.
#
# But the sheet is a vector PDF, and every route on it is a stroke in its
# agency's ink: no tolerance, no rival colors, no rendering to recover it from.
# Where two liveries share one ink they are two stroke styles of it — the
# railroad's centreline under its dashed ticks, LADOT's solid DASH against its
# dashed Commuter Express — so the dash pattern selects between them too.

PDF = "26-1720_blt_system_map_47x47.5-2.pdf"
INK_STEP = 3.0      # px between samples along a stroke read from the PDF

# The inks, as the PDF has them. An agency's lines are laid down in two colors
# a rounding apart, so each is listed with both.
RAIL_INK = [(0.655, 0.664, 0.673)]
ORANGE_INK = [(0.961, 0.513, 0.272)]                       # Metro Local
RAPID_RED_INK = [(0.844, 0.086, 0.207)]                    # 720/754/761 rapid ribbon
BUSWAY_INK = [(0.957, 0.474, 0.126)]                       # G Line busway ribbon
# The J Line's transitway ribbon, drawn 5.5 wide in the line's own #ADB8BF —
# which is, to the byte, the route_color Metro's GTFS gives it. The raster is
# where its color collides with freeway gray, not the PDF: 1104 stroke points,
# all of them on this line bar two legend swatches down at y≈3700, which no cap
# here can reach from the route.
JLINE_INK = [(0.678, 0.723, 0.75)]
LADOT_INK = [(0.409, 0.398, 0.173), (0.419, 0.4, 0.164)]   # DASH + Commuter Express

# Where a feed's lines are read out of the PDF, the color it draws them in is
# the ink itself — no sampling, and no rendering to recover it from. It has to
# be, for the same reason the lines do: `drawn_color` samples along the *warp*,
# and LADOT's is a median 20 px off its drawn line, so most of what it reads is
# the page. Both liveries came out around (151,150,114), a pale khaki against
# the (104,101,44) the sheet prints, and the vehicles rode visibly lighter than
# the lines beneath them — the failure the note on DRAWN_COLORS["pasadena"]
# describes, at twice the size.
#
# One ink is also one sprite color for both liveries, and that is right: the
# dashes are a stroke style, not a second olive — both of the roundings above
# appear under both styles. The two legend seeds are two readings of the one
# ink, a solid line and a dashed one blending differently into the page at
# 4096 px, and they stay as seeds for the color masks, which search that
# rendering and want it. Only the sprites, which are a rendering of nothing,
# take the ink.
INK_SPRITES = {"ladot": tuple(round(v * 255) for v in LADOT_INK[0])}

# The mask holds railroads and nothing else, so — as with the busway ribbon —
# the snap can reach much further than it dares on a shared color. It needs to:
# the warp sits a median 18-59 px off the drawn track, and 90% of it inside 60.
RAIL_CAPS = (100.0, 50.0, 25.0, 12.0)
RAIL_WIN = 9
# A mask smears each line across its casing, its badges and the fringe of
# whatever is drawn beside it, so a shape can sit on the mask while its own
# line is a good way off — through the Sepulveda Pass Metro 233 is drawn 25 px
# east of the 761 ribbon it was resting against. Ink is the centreline alone,
# and answering for that distance takes the coarse-to-fine ladder rather than
# the two tight passes anchored snapping settles for on the mask.
INK_CAPS = (40.0, 26.0, 14.0)

# Each LADOT livery's ink holds that livery and nothing else, so its snap can
# reach as far as the railroad's. It needs to: the warp is a median 20 px off
# the drawn line, but out at Marina del Rey it is 100, and a shorter reach left
# 437's run down Via Marina with nothing in range and piled it up on Admiralty.
LADOT_CAPS = RAIL_CAPS
LADOT_WIN = 9

_INK = {}


def pdf_ink(colors, dashed=None, step=INK_STEP):
    """Points along every stroke the sheet draws in one of `colors`, in map px.

    dashed picks the stroke style: False for the solid strokes, True for the
    dashed ones, None for either. Beziers are sampled rather than taken at
    their control points: a drawn line follows long curves, and the endpoints
    alone leave gaps a snap falls into. Points inside the regions the masks
    skip are dropped, so a shape on the main map can't reach ink drawn in the
    legend or inside the Downtown call-out."""
    key = (tuple(map(tuple, colors)), dashed, step)
    if key not in _INK:
        _INK[key] = cached_pixels(
            ("pdf-ink", key, art_stamp(PDF), EXCLUDE, CALLOUT,
             code_stamp(pdf_ink, _ink_build, inside_callout)),
            lambda: _ink_build(colors, dashed, step))
    return _INK[key]


def _ink_build(colors, dashed, step):
    try:
        import fitz
    except Exception as e:
        print(f"PDF ink extraction unavailable: {e}")
        return np.zeros((0, 2))
    doc = fitz.open(PDF)
    page = doc[0]
    s = map_image()[0].shape[1] / page.rect.width
    runs = []
    for it in page.get_drawings():
        c = it.get("color")
        if c is None or not any(sum((a - b) ** 2 for a, b in zip(c, k)) < 1e-4
                                for k in colors):
            continue
        if dashed is not None and ((it.get("dashes") or "[] 0") != "[] 0") != dashed:
            continue
        for seg in it["items"]:
            if seg[0] == "l":
                runs.append([(seg[1].x * s, seg[1].y * s), (seg[2].x * s, seg[2].y * s)])
            elif seg[0] == "c":
                p = [np.array([q.x * s, q.y * s]) for q in seg[1:5]]
                t = np.linspace(0, 1, 12)[:, None]
                b = ((1 - t) ** 3 * p[0] + 3 * (1 - t) ** 2 * t * p[1]
                     + 3 * (1 - t) * t ** 2 * p[2] + t ** 3 * p[3])
                runs.append([tuple(q) for q in b])
    out = []
    for r in runs:
        if len(r) > 1:
            out += densify(r, step)
    P = np.asarray(out, dtype=float) if out else np.zeros((0, 2))
    keep = map_image()[1]
    h, w = keep.shape
    P = P[(P[:, 0] >= 0) & (P[:, 0] < w) & (P[:, 1] >= 0) & (P[:, 1] < h)]
    P = P[keep[P[:, 1].astype(int), P[:, 0].astype(int)]]
    return P[~inside_callout(P)]


_INK_TREES = {}


def ink_tree(colors, dashed=None):
    """KD-tree over one ink's strokes, or None where the sheet draws too few."""
    key = (tuple(map(tuple, colors)), dashed)
    if key not in _INK_TREES:
        P = pdf_ink(colors, dashed)
        _INK_TREES[key] = cKDTree(P) if len(P) > 300 else None
    return _INK_TREES[key]


def rail_line_tree():
    # the solid centreline only; the dashed style is the ticks across it, and
    # they stick out sideways from the line and would only pull a shape off it
    return ink_tree(RAIL_INK, dashed=False)


# ---- route-number badge anchors from the source PDF ----------------------
# The map draws a numbered badge on every route line. All routes of one agency
# share a drawn color, so mask snapping alone can lock a shape onto a parallel
# street belonging to a different route. Badge text extracted from the vector
# PDF pins each shape to its own drawn line: a word matching the route's number
# that sits on the agency's color mask is a point known to be ON that route.

_BADGES = None                     # {"main": {...}, "inset": {...}}

def badge_words(region="main"):
    """{token: [(x, y) map px, ...]} for every word on the map, split into
    the main map and the DTLA inset frame (whose badges anchor inset runs)."""
    global _BADGES
    if _BADGES is None:
        _BADGES = {"main": {}, "inset": {}}
        try:
            import fitz
            doc = fitz.open("26-1720_blt_system_map_47x47.5-2.pdf")
            page = doc[0]
            im, _ = map_image()
            s = im.shape[1] / page.rect.width
            main, inset = defaultdict(list), defaultdict(list)
            ix0, iy0, ix1, iy1 = INSET_RECT
            lx0, ly0, lx1, ly1 = INSET_LEGEND
            for x0, y0, x1, y1, w, *_r in page.get_text("words"):
                cx, cy = (x0 + x1) / 2 * s, (y0 + y1) / 2 * s
                if ix0 <= cx < ix1 and iy0 <= cy < iy1:
                    if not (lx0 <= cx < lx1 and ly0 <= cy < ly1):
                        inset[w.strip()].append((cx, cy))
                elif not any(ex0 <= cx < ex1 and ey0 <= cy < ey1
                             for ex0, ey0, ex1, ey1 in EXCLUDE):
                    main[w.strip()].append((cx, cy))
            _BADGES = {"main": dict(main), "inset": dict(inset)}
        except Exception as e:                      # missing pdf / pymupdf
            print(f"badge extraction unavailable: {e}")
    return _BADGES[region]


_PHRASES = None


def map_phrases():
    """{PHRASE: [(x, y) map px, ...]} for every run of text the sheet sets as a
    single line, upper-cased.

    badge_words() keys on one word, which is what a route badge is. A railroad
    is named rather than numbered — "ORANGE COUNTY LINE", set in italics along
    its own track — and no word of that identifies anything on its own: the
    sheet prints "ORANGE" as a city and a street, and "LINE" against five other
    railroads."""
    global _PHRASES
    if _PHRASES is None:
        _PHRASES = {}
        try:
            import fitz
            page = fitz.open(PDF)[0]
            s = map_image()[0].shape[1] / page.rect.width
            runs = defaultdict(list)
            for w in page.get_text("words"):
                runs[(w[5], w[6])].append(w)          # (block, line)
            out = defaultdict(list)
            for ws in runs.values():
                ws.sort(key=lambda w: w[7])           # word order within the line
                xs = [v for w in ws for v in (w[0], w[2])]
                ys = [v for w in ws for v in (w[1], w[3])]
                cx = (min(xs) + max(xs)) / 2 * s
                cy = (min(ys) + max(ys)) / 2 * s
                if any(ex0 <= cx < ex1 and ey0 <= cy < ey1
                       for ex0, ey0, ex1, ey1 in EXCLUDE):
                    continue
                out[" ".join(w[4].strip() for w in ws).upper()].append((cx, cy))
            _PHRASES = dict(out)
        except Exception as e:                        # missing pdf / pymupdf
            print(f"phrase extraction unavailable: {e}")
    return _PHRASES


LABEL_NEAR = 24.0   # px a printed railroad name may stand from its own track

# A route the sheet doesn't name rides one it does. The 91/Perris Valley shares
# the Orange County Line's track for the whole of its length on this sheet —
# its warp passes within 28 px of all three "ORANGE COUNTY LINE" labels — and is
# drawn as that one line, so the sheet never writes "91" along it. Borrowing the
# name is the only thing that holds it there: it took the Riverside track
# through Vernon exactly as the Orange County Line itself did. The
# Inland Empire-Orange County Line is the other unnamed one, and it borrows
# nothing — it comes no nearer than 467 px to any label on the sheet, running
# off the east edge instead.
SHARED_RAIL_LABEL = {"91 Line": "Orange County Line"}


def line_name_anchors(name, tree, near=LABEL_NEAR):
    """Anchors from the name the sheet writes along a railroad.

    Metrolink prints no badge anywhere on the map, and its lines share one ink
    and one crosshatched livery, so where two of them run parallel the artwork
    alone cannot say which is which. Through Vernon the Orange County and
    Riverside lines are drawn 26 px apart on the same heading, close enough that
    the warp lands the OC's schedule nearer the Riverside track than its own,
    and the snap put it there — under the "VERNON" label, on the line the sheet
    labels "RIVERSIDE LINE".

    The sheet does say which is which, though, and in the very words GTFS names
    the route with: metrolink's route_id is "Orange County Line". Each name is
    written along its own track and repeated down its length, so it pins the
    line at intervals the way a numbered route's badges do. The label is set
    beside the track rather than on it — a measured 5.5 to 9 px off — so the
    anchor is the nearest ink to the label, not the label itself."""
    if tree is None:
        return []
    out = []
    for cx, cy in map_phrases().get(SHARED_RAIL_LABEL.get(name, name).upper(), ()):
        d, j = tree.query([cx, cy])
        if d < near:
            out.append(tuple(tree.data[j]))
    return out


def badge_line_color(cx, cy, r=7):
    """Dominant fill color of the chip a route-number badge sits on.

    A badge is the number drawn on a small colored chip. In a tiny window the
    only pixels are the cream map background, the dark number glyphs, and the
    chip fill. Discard the background (cream/white — every channel high; keyed
    off min-channel so a saturated fill like Metro orange (217,129,83), bright
    in red but low in blue, isn't mistaken for it) and the glyphs (near-black),
    then take the largest color cluster of what remains: that is the chip.
    A plain median would blend chip and glyph and pull a saturated orange chip
    halfway to gray; the dominant cluster recovers the true fill (orange badges
    land within ~1 px of orange this way, ~48 px via the median). Returns None
    when there's no fill to read.

    Read off the tile pyramid, where the print is faithful, and only from
    map.png where the pyramid doesn't reach. map.png is a 4096 px reduction of
    a 47" sheet and a badge chip is a few px across in it, so its fill is mixed
    with the page before anything here can look at it — and the gate above this
    reads that mix as some *other* agency's color and throws the badge away.
    Foothill's seven "190" chips read (78,111,89) through (160,173,153) in
    map.png, a 131 px spread over one printed color, and six of the seven were
    rejected: 190 kept a single badge for the whole route and, with nothing
    pinning it, cut the corner at Badillo and again across Workman. Off the
    tiles the same seven all read (53-56,103-104,78-81), within a few px of the
    (62,100,78) legend seed. The reduction was the whole of the difference.

    This makes the color gate sharper rather than looser: against a chip read
    true, the nearest rival color sits 75 px away instead of 18."""
    sub = tile_patch(cx, cy, r)
    if sub is None:
        im, _ = map_image()
        h, w = im.shape[:2]
        xi, yi = int(round(cx)), int(round(cy))
        if not (r <= xi < w - r and r <= yi < h - r):
            return None
        sub = im[yi - r:yi + r + 1, xi - r:xi + r + 1].reshape(-1, 3).astype(int)
    fill = sub[(sub.min(1) <= 190) & (sub.max(1) >= 70)]   # not cream, not glyph
    if len(fill) < 6:
        return None
    codes = (fill // 24) @ np.array([10000, 100, 1])       # coarse color bins
    vals, counts = np.unique(codes, return_counts=True)
    return np.median(fill[codes == vals[counts.argmax()]], axis=0)


STATION_NEAR = 30.0    # px a printed station name may sit from its own ribbon
STATION_GATE = 160.0   # px the warp may be out and the name still be that stop's


def station_anchors(stops, tree, names, positions, near=STATION_NEAR, gate=STATION_GATE):
    """Anchors from the station names the map prints, for a line drawn as its
    own ribbon.

    A numbered route is pinned by its badges, but a rapid-transit line carries
    station names instead, and the G Line has no badge anywhere on the sheet.
    Its stations are all labelled, though, so each stop can be matched to its
    own label and the label to the ribbon beside it.

    The label sits next to the ribbon rather than on it, so the anchor is the
    nearest mask pixel to the label, not the label itself. Names repeat across
    the map — "College" appears twenty times — so a candidate has to be both
    within `near` of this line's ribbon and within `gate` of where the warp
    puts that stop. Neither test alone is enough: proximity to the ribbon
    matched Pierce College's stop to Valley College's label, and the warp is
    too far out in the Valley to be trusted on its own."""
    if tree is None:
        return []
    out = []
    for sid in stops:
        name, pos = names.get(sid, ""), positions.get(sid)
        if not name or pos is None:
            continue
        best = None
        for word in name.replace("/", " ").replace("-", " ").split():
            if len(word) <= 3:                 # too short to identify a station
                continue
            for cx, cy in badge_words().get(word, ()):
                if math.hypot(cx - pos[0], cy - pos[1]) > gate:
                    continue
                d, j = tree.query([cx, cy])
                if d < near and (best is None or d < best[0]):
                    best = (d, tuple(tree.data[j]))
        if best:
            out.append(best[1])
    return out


def badge_like(token):
    """Whether a route's token could be printed on the map as a badge.

    Tokens come from route_short_name, which for the numbered agencies is the
    badge itself but for the named ones is prose — "Leimert/Slauson Clockwise"
    yields "Leimert", "Slauson", "Clockwise". Those are street and place names,
    and the map is covered in street and place names, so any of them landing on
    the agency's color anchors the shape to a spot the route never goes near.

    A badge is a code, not a word: it carries a digit (720, 1X, 431B, 20cc) or
    it is an initialism short enough to fit the chip (LC, SYL, R3). Case
    settles the rest — a chip is set in capitals, so "Bay", "Del" and "Los"
    are prose out of a route's name however short they are."""
    return token.isalnum() and (any(c.isdigit() for c in token)
                                or (token.isupper() and len(token) <= 3))


_RIVAL_PALETTE = None


def rival_palette():
    """Every agency's drawn color, as a lookup for whose badge a chip is.
    Metro exact colors plus each municipal legend seed / badge fill — enough
    to tell one agency's chip from another's, which is all the anchor test
    needs."""
    global _RIVAL_PALETTE
    if _RIVAL_PALETTE is None:
        cols = [ORANGE, RAPID_RED, *ROUTE_COLORS.values()]
        for seeds in LEGEND_SEEDS.values():
            cols += seeds
        cols += list(BADGE_FILLS.values())
        _RIVAL_PALETTE = np.array(cols, dtype=float)
    return _RIVAL_PALETTE


BADGE_NEAR_INK = 25.0   # px a plain-text route number may stand from its line


def route_anchors(tokens, tree, region="main", colors=None, margin=8.0, near=None):
    """Badge positions for any of the route's number tokens that lie on the
    agency's drawn-line mask (rejects same-number badges of other agencies,
    highway shields, street labels). The agency color must be present AT the
    word itself (badge fill / colored glyph strokes), not merely nearby —
    another agency's badge drawn against this agency's line must not match.

    near: for a tree of PDF strokes rather than mask pixels, the px a word may
    stand from the line to belong to it. The presence test above counts the
    agency's *pixels* under the word, which is how a badge chip reads — but a
    line's strokes are sampled along its length, so a handful of them fall
    under a word however squarely it sits on the line. And a number the sheet
    sets as plain text beside its line, which is how it writes a Commuter
    Express, never stands on the line at all. Distance to the ink covers both,
    and being one agency's ink it is already the test the colors stood in for.

    colors: the agency's drawn color(s). A route number can belong to several
    agencies (Big Blue Bus 3 and Culver CityBus 3 run bundled through
    Westchester), and their lines pass close enough that a foreign badge clips
    this agency's mask and passes the presence test above. So also read the
    badge's own chip color and drop it when some *other* agency's color
    explains it better than this one's — Culver City's khaki "3" is far nearer
    Culver City's own color than Big Blue's gray. This is relative, not a fixed
    tolerance: a chip that is merely a faded shade of the agency's own color
    (its own color still the closest) is kept, so genuine badges survive.
    Passed only for agencies where the drawn colors are muted and mutually
    distinct; Metro's saturated orange fades toward other agencies' hues and
    so is left ungated."""
    if tree is None:
        return []
    own = [np.array(c, dtype=float) for c in (colors or [])]
    rivals = None
    if own:
        pal = rival_palette()
        own_arr = np.array(own)
        # drop palette entries that ARE this agency's own color
        d_to_own = np.sqrt(((pal[:, None, :] - own_arr[None, :, :]) ** 2).sum(2)).min(1)
        rivals = pal[d_to_own > 15]
    pts = []
    for t in sorted(t for t in tokens if badge_like(t)):
                                    # sorted: a set of strings iterates in an
                                    # order that varies with the hash seed, and
                                    # anchor order decides snap tie-breaks
        for cx, cy in badge_words(region).get(t, ()):
            if near is not None:
                if tree.query([[cx, cy]])[0][0] > near:
                    continue
            # >=10: a real badge fill / glyph has dozens of mask pixels here;
            # stray antialiased fringes near a foreign badge have a few
            elif len(tree.query_ball_point([cx, cy], 6.0)) < 10:
                continue
            if own:
                bc = badge_line_color(cx, cy)
                if bc is not None:
                    own_d = min(np.sqrt(((bc - c) ** 2).sum()) for c in own)
                    riv_d = np.sqrt(((rivals - bc) ** 2).sum(1)).min()
                    if riv_d < own_d - margin:
                        continue               # another agency's color fits better
            pts.append((cx, cy))
    return pts


def ladot_livery(tokens, is_dash, sheet_tokens=()):
    """The olive strokes a LADOT route is drawn among, and its badges on them.

    LADOT's two liveries are two stroke styles of one ink, so the dash pattern
    is what keeps a DASH loop and a Commuter Express off each other's streets.
    Which style a route is drawn in is not the route's *name* to say, though.
    The dashes mark part-time service, which is what a Commuter Express usually
    is and what a DASH never is — so the name predicts the style, and predicts
    it wrongly for the one Commuter Express that runs all day. The sheet draws
    the 142 solid, like a DASH, from San Pedro across the harbour to Long
    Beach, and looking for it among the dashes finds nothing at all: the
    nearest dash to it is a median 292 px away, no cap reaches that, and the
    route kept the warp — a diagonal across the water, where the sheet draws
    Ocean Blvd.

    So the name only proposes a livery and the printed badges settle it. Every
    other Commuter Express has all of its badges on the dashed strokes; the
    142 has all four of its on the solid ones. A DASH is left with the name's
    answer, since it has nothing to settle anything with: a DASH is named, not
    numbered, and the designation the feed's name yields is one the sheet
    doesn't print — an initialism route_label made up ("WC", "PCN", "LHC"), or
    a single letter the sheet also gives Metro's rail lines.

    Which is not to say a DASH cannot be anchored, only that the feed cannot
    say what to anchor it on. `sheet_tokens` is the designation read off the
    artwork instead, by hand, in MAP_LABELS — and being the sheet's own word
    for this route it names badges printed along this route's line and no
    other. Nothing else the feed offers is allowed to anchor a DASH: an
    initialism is a guess, and a guess that lands on a code the sheet does
    print is worse than one that lands on nothing. "Southeast Clockwise" comes
    out SC, which the sheet prints twenty-nine times, seven of them standing
    on olive — and the nearest of those seven is 326 px from the Southeast
    loop, the furthest 846.

    The Wilmington loop is what this is for. Unanchored, 37% of it stood over
    12 px off its own ink — the snap has to choose between the loop's own
    olive and the streets of the grid drawn in the same ink beside it, and
    with nothing pinning it, it took a chord across the Anaheim/Figueroa
    corner and sewed the rest between neighbouring blocks."""
    prefer = ink_tree(LADOT_INK, dashed=not is_dash)
    if is_dash:
        # On the chip, not merely near the ink. A Commuter Express number is
        # set as plain text beside its line, so distance to the ink is all
        # there is to go on; a DASH designation is printed on a chip, and it
        # needs to be, because two capitals is also the shape of half the
        # words on the sheet. "El Sereno/City Terrace" is badged SC, and the
        # sheet also writes "SC AV" as a street label 97 px from the loop —
        # inside the anchor gate, 4 px from LADOT's olive where the Lincoln
        # Heights DASH passes, and it dragged the shape 200 px west to reach
        # it. The chip is a flat olive (BADGE_FILLS) and the street label is
        # the teal the sheet letters streets in, so the colour under the word
        # separates them outright.
        return prefer, route_anchors(set(sheet_tokens), prefer,
                                     near=BADGE_NEAR_INK,
                                     colors=[BADGE_FILLS["ladot"]])
    anc = route_anchors(tokens, prefer, near=BADGE_NEAR_INK)
    other = ink_tree(LADOT_INK, dashed=is_dash)
    alt = route_anchors(tokens, other, near=BADGE_NEAR_INK)
    return (other, alt) if len(alt) > len(anc) else (prefer, anc)


# Points on a route's drawn line, in map px, placed by hand where the sheet
# prints no badge the anchoring can use. They serve as badges do — and where one
# falls near an end of the shape, trim_terminus also cuts the overshoot back to
# it.
#
# A shared transit hub prints each of its routes once, in the municipal gray, so
# the sheet's "2" at the UCLA gateway is Big Blue Bus's, not Metro's, and
# route_anchors rightly ignores a gray chip off the orange line. Metro 2 is then
# left with nothing pinning its west end, and two things go wrong: the snap
# drifts the last stretch off the Hilgard it is drawn on, bodily west onto the
# 602/Westwood corridor; and the schematic ends the route at the hub while the
# GTFS runs on ~80 px past it to the real layover, which the snap chases down
# toward Wilshire. A hub point fixes both.
#
# Big Blue Bus 14 needs one at each end of the same failure. Its grey stops at
# the Culver City Transit Center marker while the GTFS carries on ~100 px past
# it to the layover at Bristol Pkwy, over ground the sheet gives the route no
# line on — and there is ink down there all the same, the railroad's crosshatch
# and the glyphs of "GREEN VALLEY" and "405 574", near enough BBB's grey to be
# in the mask, so the tail snapped onto that and drove off down the freeway
# corridor. And between its "14" on Centinela and its "14" at the hub the sheet
# prints nothing for 245 px, over exactly the stretch where the route leaves
# Centinela and turns east along Bluff Creek. `trace_anchors` should walk that
# corridor and pin it, and cannot: the olive Culver CityBus line crosses the
# grey by the transit centre and takes a 14 px bite out of the mask, which
# breaks the walk in two. Widening the walk's reach past that bite does not help
# and is how you find out the mask is the problem — at 6 px it steps across the
# glyphs of the "Culver City Transit Center" label instead and comes back with a
# shortcut through the words. So the stretch is interpolated straight between
# the two badges, which is the chord across the corner, and the snap then has to
# find its way back from 27 px out with a 26 px cap. One point on the drawn
# Bluff Creek gives the interpolation the corner and halves that: worst 27.3 px
# off the ink to 9.8, and the whole route a median 1.0.
PINNED_ANCHORS = {
    ("gtfs_bus", "2"): [(1001, 1801)],   # Metro 2 west end at the UCLA hub
    ("bigbluebus", "4061"): [             # BBB 14
        (1138, 2215),                     #   south end at Culver City TC
        (1090, 2271),                     #   Bluff Creek, east of the corner
    ],
    # Long Beach Transit 2's west end, on the drawn Sepulveda a little east of
    # the Figueroa corner the sheet finishes the route at. The warp puts that
    # corner 69 px south of where the sheet draws it and then runs on down
    # Figueroa to the layover, so the shape came out with a 60 px spur hanging
    # off the end of the drawn line into blank page — the tail the eye reads as
    # the line starting nowhere. There is a "2" printed at the corner itself,
    # and once the agency's chips are believed the fit does use it; what it
    # cannot do is shorten the shape, and the spur is past the last badge.
    #
    # East of the corner rather than on it because one pin has to serve both
    # directions and they run a street apart down here: the corner is 36 px
    # from the westbound working, a pixel outside the reach, and the 119 px it
    # overshoots by is past the trim limit besides. This sits on the drawn ink,
    # 16 and 24 px from the two of them, and trims both.
    ("longbeach", "2"): [(1610, 3044)],
    # Long Beach Transit 92 round Cal State Long Beach, on the drawn campus
    # east side a few px above the corner it turns onto 7th St at. The sheet
    # takes the route down Bellflower Bl, east along Beach Dr, south past the
    # campus and west along 7th — a 265 px staple round three sides of the
    # university — and the two badges bracketing it are the "92" on Bellflower
    # at (2219,3122) and the middle chip of the 91/92/93 stack on 7th at
    # (2215,3255).
    #
    # Between them the drawn web offers a way through that is shorter, and it
    # is a corridor rather than a stray pixel: Pacific Coast Hwy's diagonal
    # merges into Bellflower just above the Beach corner, so the walk comes
    # down Bellflower, crosses onto PCH where the two meet and runs straight
    # to 7th — 153 px against the staple's 265. The length band is exactly
    # what catches a walk cutting a corner the route really turns, and it
    # comes within a hair of catching this one: the warp's own arc between the
    # badges reads 214, so the ratio is 0.72 against a 0.75 floor. Refused as
    # a shortcut, the walk goes in through the aligned-walk fallback instead —
    # the two courses do align, both running south down the page — and that is
    # self-confirming. With the shape pulled onto PCH the next pass measures
    # the arc at 171, the ratio reads 0.90 and the walk is believed outright.
    # Both directions came out cutting the corner off the campus, running
    # diagonally over blank page between Bellflower and 7th: 39% and 38% of
    # the drawn staple had the stored path within 8 px of it.
    #
    # One point on the drawn east side splits the stretch in two and leaves
    # neither half a shortcut to take. Bellflower-to-pin walks 168 against an
    # arc of 171 and is believed on the first pass; pin-to-7th walks 79
    # against 43, out of band while the shape is still cutting the corner and
    # taken by alignment, then believed at 1.04 on the second pass. Over the
    # staple that is a median 3.4 and 8.8 px off the drawn corridor down to
    # 2.6 and 2.8, worst 30.7 and 25.6 down to 8.2 and 8.5, and 39%/38% of it
    # covered to 97%/99%.
    #
    # Low on the east side rather than up on Beach Dr, because a pin only
    # closes the shortcuts that lie south of it: at (2288,3245) the stretch
    # below the pin still cuts back across the campus to the badge and
    # coverage comes out 43%/70%, and a pin on Beach Dr at (2272,3219) leaves
    # 54%/52%. Down here it holds rather than working by accident: moved 4 px
    # in any direction it still covers 91% of the staple or better.
    #
    # The 91, the 93 and the 94 are drawn along the same staple and cut the
    # same corner for the same reason, so the one point serves all four. With
    # the ink seed above in place it takes the 91 from 23/33/32% of the staple
    # covered to 74/90/74, the 93 from 26/13/25 to 83/97/83, and the 94 from
    # 41/46/30/41 to 83/84/41/83 — the 94's last working being a short one that
    # only touches the top of the staple. The 41, the 46, the 171 and the 175
    # run through the campus in the feed as well, and are not here: the sheet
    # draws none of them past the "41 45 46" chips on Pacific Coast Hwy, so
    # there is no staple of theirs to pin them to.
    ("longbeach", "91"): [(2288, 3262)],
    ("longbeach", "92"): [(2288, 3262)],
    ("longbeach", "93"): [(2288, 3262)],
    ("longbeach", "94"): [(2288, 3262)],
    # Long Beach Transit 131 on the drawn Redondo Av, halfway between the "131"
    # chip printed on it at (2113,3286) and the one on 2nd St at (2161,3342).
    # The sheet takes the route straight down Redondo and round onto 2nd, ~100
    # px; the walk between those two badges came out 92 px going another way
    # entirely — east along 4th St, south down Ximeno Av and back east — and
    # being the shorter of the two it won, was believed at 0.97, and took the
    # shape with it. Both corridors are drawn, so nothing in the length band
    # can separate them.
    #
    # What makes the wrong one shorter is the chips themselves. A chip is
    # filled with the legend's saturated maroon, which is nothing like the mask
    # colour, but its antialiased border blends through it, so every chip on
    # the sheet is a ring of mask pixels — and the sheet stacks them. The
    # "111"/"112" pair and the "121"/"131" pair are printed in one column at
    # x=2161, which bridges the 24 px between where the drawn Ximeno stops and
    # 2nd St; the "131" chip on Redondo bridges the 4 px from Redondo to 4th.
    # With both rungs the ladder is a corridor, and a corridor is what the walk
    # is looking for. One pin on Redondo between the two badges splits the
    # stretch so neither half can reach the ladder: 27% of the drawn corridor
    # covered to 100%, a median 15.3 px off it to 1.8.
    ("longbeach", "131"): [(2113, 3320)],
    # Long Beach Transit 111 halfway down the drawn Lakewood Bl in Lakewood,
    # between the "111" at the top of the 111/112/192 stack at (2136,2810) and
    # the one printed on Lakewood at (2148,2913). This one is the length band's
    # *other* edge, and it is what makes it a one-direction fault: the sheet's
    # corridor between those two badges walks 109 px, the southbound working's
    # arc across it reads 79.5, and 1.37 is over the 1.35 ceiling — so the walk
    # went in through the aligned-walk fallback, which lays a single node where
    # a believed walk would lay three, and what the fit interpolated between
    # the badges was the chord. That chord is a staircase down across the
    # "LAKEWOOD" label and over three blocks of blank page. The two northbound
    # workings read the same stretch as 113.5 against 148.3, well inside the
    # band, believe the walk and come down the drawn Lakewood correctly.
    #
    # A pin halfway leaves each half short enough that the straight
    # interpolation *is* the corridor. Measured as arc standing more than 12 px
    # from any LBT ink, the two southbound workings go 6% to 3% and the
    # northbound one stays at 0; of the drawn corridor itself they cover 44%
    # against 92%. It holds 4 px in any direction.
    ("longbeach", "111"): [(2146, 2880)],
    # Long Beach Transit 61's Artesia end, on the drawn Artesia Bl a little east
    # of where the 51 curves off it down Long Beach Bl. The route comes up
    # Atlantic, turns west along Artesia — the stops say so the whole way,
    # Artesia at Butler, Long Beach Bl, Harbor and Santa Fe — and finishes at
    # Artesia (A Line) Station. The sheet draws exactly that: one stroke at
    # y=2752.2 running from the Atlantic corner west to x=1849, where the
    # station box is.
    #
    # None of that stretch was anchored. The northernmost "61" is printed at
    # (1993.5,2770.1), on Atlantic below the corner, and there is no badge west
    # of it at all — 140 px of drawn corridor and the terminus with nothing on
    # them. Past the last anchor the interpolation clamps to that anchor's own
    # displacement, and through here the warp stands a near-uniform 24 px south
    # of the artwork (it lays Artesia at y≈2777), so the corridor arrived ~18 px
    # low with the snap left to find its own way back.
    #
    # What it found was the 51. That line's curve off Artesia down Long Beach Bl
    # reaches y=2766..2790 between x=1924 and 1949 — across the warp's own
    # Artesia — so the shape caught it, came off the drawn line at x=1949, and
    # ran 80 px west at y≈2774 over blank page, through the letters of
    # "VICTORIA", before hooking up to the station from below. The northbound
    # workings finished at (1849,2786), 44 px south of the station they are drawn
    # into. drift_check ranked the route second in its system for it: 112 px of
    # 820 off the ink, worst 36.9 at (1844,2783).
    #
    # One pin is all it takes, the warp's error here being a straight shift
    # rather than a rotation: with a point on the drawn Artesia as the last
    # anchor, the clamp carries that same 24 px correction over the rest of the
    # stretch and lands the whole of it on the stroke.
    #
    # East of the Long Beach Bl junction rather than out where the sag is worst,
    # because a pin further west is a terminus as far as trim_terminus can tell.
    # At (1900,2752.2) it stands 24.7 px from the warp, inside the 35 px reach,
    # with 99 px of shape beyond it, inside the 110 px tail — so the station
    # approach is read as overshoot and cut off, and the line ends in mid-block
    # on Artesia at x=1902 with 62% of the drawn corridor covered. A second pin
    # at the corner itself is no help either: it takes the northbound workings
    # round the turn and folds the southbound one into a V beside it, one pin
    # having to serve both directions.
    #
    # The three full-length workings go from a median 4.3, 7.4 and 7.2 px off
    # the drawn corridor (worst 22.6, 34.0, 34.0) to 1.2 (worst 16.3), and from
    # 57% of it covered to 92%. It holds 4 px in any direction. What is left is
    # the layover loop inside the station box, where the sheet draws no line to
    # be on, and ~10 px of the Atlantic corner cut.
    ("longbeach", "61"): [(1930, 2752.2)],
    # Metro 217's Eagle Rock end, on the drawn Colorado a little east of where
    # Broadway turns down onto it. The sheet's badges run out before the route
    # does: the easternmost "217" is printed at that turn, at (1752,1493), and
    # the route carries on ~90 px past it to its terminus at Colorado &
    # Eagledale with nothing pinning the stretch. Through Glendale the warp
    # stands ~40 px north of the artwork — the sheet draws Broadway at y=1476
    # and the warp puts it at 1436 — and what is drawn 40 px north of Colorado
    # Blvd is the 501 along the Ventura Fwy, in Metro's own orange, 2 px from
    # where the warp left the tail. So the last stretch snapped onto the freeway
    # and ran 110 px east along it, and Eagle Rock's terminus came out on the
    # 134.
    #
    # That tail is also what cost the route its corner, 200 px back down the
    # line. The walk between the badges at (1752,1493) and (1685,1532) does
    # recover Brand and Broadway — 128 px of drawn corridor against a first-pass
    # arc of 57, so it goes in through the aligned-walk fallback, and by the
    # second pass the arc reads 116 and the walk is believed outright. But the
    # fit carrying that corner also carries the tail, and with the tail held up
    # on the freeway the corner has nowhere to land: the line comes down off the
    # freeway onto Broadway, darts 18 px southeast toward the turn and doubles
    # straight back up it. That is 452 deg of hairpin against the chord's zero,
    # so the whole aligned fit was refused on `stored_penalty` and what shipped
    # was the chord — cut diagonally across the blocks between Los Feliz and the
    # turn, with the freeway tail still on the end of it. Pinned, the same fit
    # has somewhere to go, scores no hairpin at all and is kept: over the
    # Glendale end the two full-length workings go from a median 20 px off the
    # drawn corridor (worst 46) to a median 2 (worst 12).
    #
    # One pin and not two, though the turn itself is still the loosest part of
    # the run — up to 12 px off the drawn bend, pulled by that last badge, which
    # is printed 17 px below the line it labels. A second pin on the drawn
    # Broadway at (1745,1476) tidies the westbound working and drags the
    # eastbound one to a median 13 px off the corridor: one pin has to serve
    # both directions, and here it cannot.
    ("gtfs_bus", "217"): [(1802, 1499)],
    # Foothill Transit's five downtown expresses and the Silver Streak with
    # them, on the drawn busway corridor through East LA: its two ends and the
    # middle of the diagonal between them.
    #
    # The 493, 495, 498, 499 and 699 all come in from the San Gabriel Valley on
    # the El Monte Busway, and the sheet draws them as one evergreen line beside
    # the grey busway ribbon — along y=1919.2 from the call-out's edge at
    # x=1811 east to x=1878, up a diagonal to (2022.8,1860.2), and straight east
    # from there. Every badge they have is past the far end of it: the sheet
    # sets the five numbers in one run at Cal State LA, "493" at (2266,1866)
    # through "699" at (2319,1866), and prints none of them west of that.
    #
    # Through here the warp stands *north* of the artwork and by a distance that
    # changes along it: it lays the corridor at y≈1878-1891 where the sheet
    # draws it at 1919, and at the badges' own x it is 63 px north. So the
    # westernmost badge attaches with a displacement of (0,+63), the clamp
    # carries that south over everything west of it, and by then the drawn line
    # has dropped away southwest — leaving the five routes 30-40 px *past* it,
    # down the E Line's corridor through Pico/Aliso, Mariachi Plaza and Soto and
    # along Cesar Chavez. A median 32.7 to 36.2 px off their own drawn line,
    # worst 76, with 34% of it covered.
    #
    # The Silver Streak is the control, and says this is anchoring rather than
    # the warp: it is drawn along the same corridor with the same warp under it,
    # the sheet prints an "SS" chip at (1845.6,1921.0) on this stretch, and on
    # that one anchor it came out a median 1.8-2.4 px off with 81-97% covered.
    #
    # Three points and not one, because one only closes the clamp. Pinned at the
    # west end alone, the stretch from there to the badges is interpolated
    # straight and cuts the bend the corridor makes at (2022.8,1860.2): the west
    # comes right and 167 px in the middle go 40 px south of the line instead,
    # 67% covered. With the bend pinned too, and the diagonal's midpoint to hold
    # it, the five routes go to a median 1.5-1.8 px off and 90-100% covered.
    # The Silver Streak takes the same three — it is the same corridor, and its
    # own chip is one anchor over a 490 px stretch — and goes from 80-97%
    # covered to 94-100%, its two long workings from a p90 of 19.0 px off the
    # line to 4.2. They hold 4 px in any direction.
    ("foothill", "20493"): [(1878.2, 1918.7), (1950.0, 1889.6), (2022.8, 1860.2)],
    ("foothill", "10495"): [(1878.2, 1918.7), (1950.0, 1889.6), (2022.8, 1860.2)],
    ("foothill", "20498"): [(1878.2, 1918.7), (1950.0, 1889.6), (2022.8, 1860.2)],
    ("foothill", "10499"): [(1878.2, 1918.7), (1950.0, 1889.6), (2022.8, 1860.2)],
    ("foothill", "10699"): [(1878.2, 1918.7), (1950.0, 1889.6), (2022.8, 1860.2)],
    ("foothill", "20707"): [(1878.2, 1918.7), (1950.0, 1889.6), (2022.8, 1860.2)],
    # The two Valley DASH loops MAP_LABELS has just named, which the sheet
    # badges once and twice respectively — enough to say which olive is theirs,
    # nowhere near enough to lay a loop on it. Out here the warp is at its
    # worst, and worst *unevenly*: it stands a median 41 px off the drawn
    # Northridge loop and 94 px off it at the far corner, 27 and 78 px for Van
    # Nuys/Studio City. Both are wider than the blocks the loops are drawn
    # around, so each leg went to whichever olive it landed nearest. Measured as
    # the share of its own drawn loop a shape runs within 8 px of, Northridge
    # covered 26% and the two Van Nuys workings 61% each.
    #
    # Van Nuys/Studio City comes right: three pins take both directions to
    # 100%, the whole circuit a median half a pixel off its own ink. Northridge
    # gets to 88% and stops there, and the missing 12% is one thing — the stub
    # where the loop leaves Nordhoff at Corbin, drops a block south and comes
    # back east along Parthenia, which stays cut as a corner. No pin reaches it.
    # The drawn Corbin stands 7 px from the *warp's Wilbur* leg, which is the
    # last leg of the circuit, so a pin there reads as a terminus the shape
    # overruns and trim_terminus cuts 66 px off the end of the loop instead of
    # anchoring it. Move the pin east along the drawn Parthenia far enough to
    # clear the trim's 35 px reach and it attaches to the warp's Reseda leg
    # instead, half a loop away, where its displacement pushes the wrong street.
    # A search over the whole drawn loop turns up coordinates that do cover the
    # stub and not one that survives being moved 4 px in any direction — which
    # is a number working by accident rather than an anchor that holds.
    # Metrolink's Riverside Line through Vernon, on its own drawn track a little
    # east of where the Orange County Line branches off it. The two run out of
    # downtown as one corridor, split at (1845,2077) west of Soto and fan apart
    # going southeast — 34 px between them at x=1875, 43 px at x=1925 — and the
    # stored path took the wrong one: 90 px of it rode the Orange County track,
    # from x=1844 to x=1900, before climbing back onto its own. This is the
    # failure `line_name_anchors` was written for, in the other direction; the
    # names are what tell the two apart, and here the Riverside Line runs out of
    # them.
    #
    # The sheet writes "RIVERSIDE LINE" four times, and the first is at
    # (1954,2116) — 392 px into a 4,010 px shape. Before it the interpolation
    # has nothing to interpolate between and clamps, so the whole head of the
    # line carries that one anchor's own displacement, (-2.5,+21). That is right
    # where the label is and wrong everywhere upstream: the warp already runs
    # within 6 px of its own track from x=1850 to x=1925, and +21 px lands it
    # midway between the two — 9 px from each at x=1850, 16.6 against 17.4 at
    # x=1875 — where a snap with a 100 px first cap is deciding by fractions of
    # a pixel and the smoothing carries whole stretches to whichever won.
    #
    # Neither check can see it, and for the reason the snapper couldn't either:
    # one crosshatched livery holds every railroad on the sheet, so a line on
    # its neighbour's track is on ink of the right colour. drift_check scores
    # the Riverside Line 12 px of 2,700 and what it flags is the diagonal
    # *between* the tracks, worst 18 px at (1908,2110); path_check scores it a
    # flat zero, the excursion being smooth.
    #
    # One point on the drawn track 15 px east of the split, where the warp
    # passes 10 px away, gives the head something of its own to sit on. Both
    # workings go from a median 14 px off their own track (worst 37) to under a
    # pixel (worst 1.4), and from 39% of the drawn stretch covered to 100%. It
    # holds 4 px in any direction.
    ("metrolink", "Riverside Line"): [(1860, 2077.4)],
    ("ladot", "798"): [(645, 1236), (652, 1140)],       # DASH Northridge
    ("ladot", "799"): [(1032, 1472), (999, 1336),       # DASH Van Nuys/
                       (1180, 1507)],                   #   Studio City, cw
    ("ladot", "800"): [(1032, 1472), (999, 1336),
                       (1180, 1507)],                   #   ...and ccw
}

# Termini given in *warp* px instead of on the drawing, for trimming only — they
# are never read as anchors. A pin in PINNED_ANCHORS has to do both jobs at once,
# and that only works where the warp lands near enough to the drawn terminus for
# one point to serve as both.
#
# Torrance 5 is where it doesn't. The sheet ends the route at Pacific Coast Hwy &
# Crenshaw, badging it there; the GTFS carries on 1.4 km up Crenshaw Bl to the
# layover at Palos Verdes Dr N, over ground the sheet gives the route no line on
# — and, as with Big Blue Bus 14, there is ink down there all the same, the grey
# the sheet draws Crenshaw and Palos Verdes Dr in being near enough Torrance's
# own, so the tail snapped onto that and hung off the end of the line into blank
# page. This is a schematic corner: the warp puts the PCH junction at (1412,
# 3189), 59 px from where the sheet draws it. A pin on the drawn corner cannot
# trim it, and not because 59 px is out of reach — the trim takes the *nearest*
# point of the shape, and the layover's own start point, which the warp lands 26
# px from that corner, is nearer to it than the terminus is. Every point of the
# drawn corridor answers the same way: the corner or the layover's start, never
# the terminus in between. So the terminus is named where the warp puts it.
TRIM_TERMINI = {
    ("torrance", "5"): [(1412, 3189)],   # PCH & Crenshaw, in warp px
}

TERMINUS_REACH = 35.0   # px a shape must pass within of a pin to be cut to it
TERMINUS_TAIL = 110.0   # px of overshoot past the pin that gets trimmed off


def trim_terminus(pts, pins):
    """Cut a shape back to a pinned terminus it overshoots. The schematic ends a
    route at its hub; the GTFS runs on to a layover the map omits, and snapping
    that tail onto whatever line runs past the hub leaves the vehicle wandering
    beyond its drawn end. Where the shape passes close to a pin with only a short
    tail beyond, drop the tail so the shape ends there. A pin further into the
    route than that has more shape on both sides of it than a layover is long,
    and only anchors the snap.

    Every pin is measured against the whole shape and the cuts applied together,
    rather than each against what the last one left. Trimming in sequence lets
    the cuts compound: Big Blue Bus 14's hub takes ~100 px off the end, which
    brings the pin on Bluff Creek — 160 px into the route, and no kind of
    terminus — inside the tail limit as measured from the new end, and the
    second pass cut the route back to that as well."""
    P = np.asarray(densify(pts, 4.0), dtype=float)
    if len(P) < 2:
        return [tuple(p) for p in P]
    cum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(P, axis=0).T))])
    lo, hi = 0, len(P) - 1
    for hx, hy in pins or ():
        d = np.hypot(P[:, 0] - hx, P[:, 1] - hy)
        j = int(d.argmin())
        if d[j] > TERMINUS_REACH:
            continue
        head, tail = cum[j], cum[-1] - cum[j]
        if head < tail and head <= TERMINUS_TAIL:
            lo = max(lo, j)
        elif tail < head and tail <= TERMINUS_TAIL:
            hi = min(hi, j)
    return [tuple(p) for p in P[lo:hi + 1]]


# Hand-drawn geometry that replaces warp+snap over a stretch the snapper cannot
# reconstruct. Keyed by (feed, route): a `box` that brackets the stretch (the
# shape must pass through it exactly once) and a `path` tracing the drawn line
# across it. Both are map px; the box is matched against the pre-snap warp, so
# it goes where the shape is *before* snapping, not where the ink is drawn.
#
# Metro 761 turns south from Sunset onto Hilgard into UCLA, and three things
# defeat the machinery at once: the georeference warp lands the corner ~45 px
# southwest of the drawn red and non-uniformly compresses Hilgard; the 761's red
# ink is broken by the UCLA station-box marker, so the badge-to-badge corridor
# walk can't cross it; and the 45 px error scrambles anchor order (the UCLA chip
# attaches to a Sunset warp point inside the anchor gate). The snap then
# fragments the squared corner into a diagonal cut with a loop past it. The
# corner is short and unambiguous on the sheet, so it is drawn by hand — the box
# sits south and west of the drawn corridor, where the warp lands the shape.
OVERRIDE_PATHS = {
    ("gtfs_bus", "761"): {
        "box": (930, 1786, 1002, 1861),
        "path": [
            (930, 1744), (985, 1744), (1010, 1744), (1022, 1745), (1030, 1750),
            (1035, 1755), (1038, 1765), (1038, 1778), (1037, 1789), (1035, 1795),
            (1029, 1800), (1019, 1804), (1010, 1806), (1003, 1808), (1001, 1816),
            (1001, 1830), (1001, 1845), (1001, 1858),
        ],   # Sunset -> Hilgard corner -> down into the UCLA gateway
    },
    # Montebello 10 runs Atlantic from Cesar Chavez south to Whittier — the
    # stops say so for the whole stretch (Atlantic/Pomona, /4th, /Eagle, /6th,
    # /Hubbard) — and the sheet draws it that way, straight down the corridor it
    # labels ATLANTIC. The stored path instead cut a chord from the Chavez
    # corner southwest to 6th and Fraser, 65 px off the drawn line at its worst,
    # and rejoined Whittier by running back east up a stretch of it the route
    # never touches. The same three failures as the 761, and here they compound:
    #   - The warp puts Atlantic on its true southwest slant while the sheet
    #     draws it vertical, so it lands the Whittier junction ~68 px west of the
    #     drawn one and turns one straight corner into a dog-leg.
    #   - That dog-leg inflates the arc between the two "10" badges bracketing
    #     the stretch to 211 px against the drawn corridor's 145, so the
    #     badge-to-badge walk reads 0.69 and TRACE_DETOUR throws it out as a
    #     shortcut. Re-fitting cannot rescue it the way it does Metro 240's
    #     corner: the badges pin the two ends and the interpolation between them
    #     keeps the arc, so the ratio is the same on every pass.
    #   - Anchor order is scrambled too, which is why a pin is no use here: a
    #     point on the drawn Atlantic is *nearer the warp's Whittier leg* than
    #     its Atlantic one, so it attaches to the wrong stretch of the shape.
    # Widening the walk's band is no answer either — at 0.60 the walk is
    # believed and comes back cutting the junction, down Atlantic to the 40's
    # corridor and diagonally across to the badge, which sits on the mask like
    # every chip does. The corridor is two streets and a corner, so it is drawn
    # by hand; the box sits west of it, where the warp lands the shape, and the
    # short Whittier workings that never reach Atlantic don't enter it.
    ("montebello", "10"): {
        "box": (2085, 1966, 2158, 2078),
        "path": [
            (2177, 1961), (2177, 1976), (2177, 1992), (2177, 2007), (2176, 2019),
            (2175, 2027), (2173, 2034), (2170, 2040), (2166, 2046), (2162, 2052),
            (2159, 2058), (2157, 2064), (2157, 2071),
        ],   # Atlantic, from below the Chavez corner to the Whittier junction
    },
    # Metro 501's North Hollywood end. The sheet runs it in off the 134, round
    # the corner at Lankershim and up the Lankershim corridor into the station;
    # the stored path instead left the drawn line a block short of that corner,
    # carried on west across blank page and stopped 55 px out in the open, on
    # the 549's own thin orange beside the busway — a line ending nowhere, which
    # is the one thing a terminus must not look like.
    #
    # A pin cannot answer it, and for a reason the warp makes plain: the 501
    # leaves North Hollywood down Lankershim, turns east, and serves Olive and
    # Alameda in Burbank before it reaches the freeway, and the warp lays that
    # Burbank leg straight over where the sheet draws Lankershim. So every point
    # of the drawn corridor — the station itself, the corner, anywhere between —
    # is nearer the warp's Burbank leg, a hundred px further into the route,
    # than it is to the warp's own Lankershim. A pin there anchors the middle of
    # the route and leaves the end where it was.
    #
    # The corridor is two straight runs and a corner, and the sheet draws it
    # plainly, so it is drawn by hand. The box brackets the warp from the
    # terminus to where the shape rejoins the drawn 501 on the freeway; both
    # workings enter it exactly once, at their North Hollywood end. The path
    # runs the last 12 px past where the ink stops, onto the station marker
    # itself, so the vehicle finishes at North Hollywood rather than at the edge
    # of the box the sheet draws around it.
    ("gtfs_bus", "501"): {
        "box": (1180, 1320, 1345, 1440),
        "path": [
            (1289.5, 1382.5), (1303.0, 1393.5), (1310.6, 1406.0),
            (1318.5, 1419.8), (1326.8, 1432.4), (1333.0, 1443.1),
            (1338.7, 1452.9), (1343.6, 1455.7),
        ],   # North Hollywood station -> Lankershim -> the corner onto the 134
    },
    # Metro 251's Eagle Rock end. The route comes up Eagle Rock Bl, turns west
    # along Colorado and finishes at Colorado & Eagledale, where Colorado meets
    # Broadway by Eagle Rock Plaza — the lower of the two lines the sheet draws
    # converging there. The southbound workings run a layover loop out to
    # Verdugo Rd and back east along Broadway before they start south.
    #
    # The stored path instead climbed off the plaza onto the Ventura Fwy, ran
    # 67 px east along the 501's orange, came down onto Colorado a block past
    # Eagle Rock Bl and doubled back west to the corner it should have turned
    # at. The northbound workings made the same excursion lower and smaller:
    # out along the 81's drawn Yosemite Dr for 25 px, up to Colorado and back
    # west. Neither check sees any of it — the 501 and Yosemite Dr are Metro's
    # own orange, so the excursion is ink of the right colour and drift_check
    # scores the route 1.4% with a worst point of 20.6 px, and it is smooth, so
    # path_check scores it a flat zero.
    #
    # The warp is why. Through Eagle Rock it stands ~80 px east and ~25 px
    # north of the artwork: the Colorado & Eagle Rock corner comes out at
    # (1866,1471) where the sheet draws it at (1784,1496), and the terminus at
    # (1813,1454) against the sheet's (1745,1490). What is drawn where the warp
    # lays Colorado is the 501 along the Ventura Fwy at y=1455.7, 2 px away and
    # in Metro's own orange — the 217's failure one street west, and the same
    # freeway.
    #
    # The sheet prints three "251"s here and they bracket the fault without
    # reaching into it. Both plaza chips attach to the same point of the warp,
    # 24 px into the shape, and the next anchor is 216 px of arc further on,
    # down Eagle Rock Bl; everything between is a straight interpolation
    # between two displacements 74 px apart, across a corner the warp puts 80
    # px east of the drawn one. There is no walk to recover the corridor
    # either — the two chips bracketing that corner are 38 px apart on the
    # sheet, under TRACE_SPAN's 60 px floor. The crossed-badge slide does fire,
    # two chips a street apart claiming one point of the shape being exactly
    # what it is for, and is refused on every pass, as it should be: the route
    # is 1,400 px long and this error is local to its last 200.
    #
    # Nor can a pin be placed in between, for Montebello 10's reason. Every
    # point of the drawn corridor from the terminus to the Eagle Rock Bl
    # junction is nearer the warp's *layover leg* than the Colorado run it
    # would have to speak for: the drawn corner at (1784,1497) stands 43 px
    # from the warp 24 px into the shape and 86 px from the warp's own corner.
    # Pinned on the stub, at the corner, and on Colorado east of it, all three
    # attach to that same point and leave the freeway run where it was.
    #
    # Two streets and a corner, so it is drawn by hand. The box brackets the
    # warp's whole Eagle Rock end — the layover loop included, since that is
    # where the climb onto the freeway starts — and stops where the warp is
    # back on the drawn Eagle Rock Bl. Both directions enter it once, and the
    # short workings that turn back at Avenue 28 & Idell never reach it. The
    # loop is not in the path: the sheet draws its Verdugo and Broadway legs as
    # the line *above* Colorado, and running the corridor round them would
    # finish the northbound working at the junction rather than at the
    # terminus. So the two directions share the one stretch of Colorado and the
    # loop plays out as a crawl along it while the bus lays over. Measured
    # against the drawn corridor over the last 200 px of the route, the
    # southbound workings go from a median 22.6 px off it (worst 67.7) to 0.7
    # (worst 1.0) and the northbound ones from 1.3 (worst 26.2) to 1.0, with
    # the corridor covered 82% and 89% to 100%.
    ("gtfs_bus", "251"): {
        "box": (1780, 1430, 1875, 1580),
        "path": [
            (1745.0, 1489.5), (1757.0, 1489.5), (1769.0, 1489.5),
            (1777.0, 1489.6), (1780.5, 1490.2), (1782.5, 1492.0),
            (1783.6, 1494.5), (1784.0, 1497.5), (1784.0, 1510.0),
            (1784.0, 1525.0), (1784.0, 1545.0), (1784.0, 1570.0),
        ],   # Colorado, from the Broadway junction the sheet ends the route at
             # east to Eagle Rock Bl and south down the boulevard. The south end
             # stops 10 px short of the box edge: the snap picks the boulevard
             # up at y=1567 southbound and y=1573 northbound, and 1570 seams to
             # both within 3 px rather than leaving one of them a 10 px step.
    },
    # LADOT 142 (route_id 870) through San Pedro. The sheet draws the corridor
    # its stop list describes and draws it plainly — Miner & Harbor, west along
    # 7th, north up Gaffey, east along Ocean — and the westbound working lands
    # on all of it. The eastbound one keeps a 60 px bulge across the corner,
    # and the reason is the warp, which is what a bodily slide cannot answer
    # for here:
    #   - San Pedro is one of the schematic corners, and the warp is ~78 px
    #     south of the drawn 7th and Gaffey — while out at the Long Beach end
    #     of the same 350 px of shape it is dead on. `anchor_slide` assumes an
    #     error that "varies slowly enough that one vector covers every leg",
    #     and over this route it does not.
    #   - So the slide it settles on is a compromise, and one the gain test now
    #     refuses on its merits rather than on a length bound: a vector that
    #     suits one end of the route and not the other cannot clear half the
    #     residual. That test used to be loose enough (0.7) that the 49 px
    #     compromise passed it, and only the old 48 px length bound stood in
    #     the way — by a single pixel, while the westbound working's 44 px
    #     compromise went through. That was the whole of the difference between
    #     the two directions, and it was luck rather than judgement.
    #   - Unfitted, the badges cross: the waterfront chip at the foot of Harbor
    #     is nearest the *7th St* run of the warp rather than the shape's own
    #     start, and the Ocean corner and Gaffey chips land on the same 1 px of
    #     shape 33 px apart. A pin cannot help for the same reason it cannot
    #     help Montebello 10 — it would attach to the wrong stretch too.
    # The box brackets the warp's whole San Pedro end and stops short of where
    # the snap picks Ocean Blvd up correctly on its own.
    ("ladot", "870"): {
        "box": (1560, 3380, 1700, 3500),
        "path": [
            (1617.5, 3404.5), (1617.5, 3390.0), (1617.5, 3378.9),
            (1616.0, 3375.6), (1614.2, 3375.6), (1580.0, 3375.6),
            (1555.1, 3375.6), (1551.8, 3372.4), (1551.8, 3350.0),
            (1551.8, 3326.0), (1554.9, 3322.9), (1600.0, 3322.9),
            (1682.0, 3322.9),
        ],   # Miner & Harbor -> 7th -> Gaffey -> east along Ocean. The east end
             # stops a little short of the box, since the shape resumes at the
             # snapper's own first point past it and that one sits at x=1691:
             # running the corridor out to the box edge puts a 7 px backtrack in
             # the seam, which is a 179 deg cusp however short it is.
    },
    # Montebello 20's Whittier Bl stub. The route comes down Montebello Bl, turns
    # west along Whittier, runs out to Garfield Av and comes straight back — the
    # stop list ends the westward run at Garfield & Whittier and picks up again
    # heading east — and the sheet draws that as one line, Whittier being the
    # 10's corridor and shared. What shipped was a wedge instead: the two legs
    # ran a few px apart either side of the drawn Whittier, up to 17.7 px off it
    # southbound and 24.3 px northbound, and the fold came 14 px short of the
    # drawn Garfield corner as a 155 deg cusp — which is the point `path_check`
    # ranked the route on. The northbound legs left the drawing altogether: 21
    # px of their run stands off every Montebello stroke on the sheet, a
    # rectangle over blank page north of Whittier.
    #
    # Three failures, and they compound:
    #   - The feed's own shapes. The southbound return from Garfield to
    #     Montebello & Whittier is a single straight segment — a chord, not a
    #     traced street. The northbound ones are worse: from Greenwood &
    #     Carmelita they jump straight to Garfield & Whittier, run south down
    #     Garfield and east along a street the route never touches, come back to
    #     Carmelita, jump to Garfield & Whittier a second time and only then run
    #     east. That spurious loop is the rectangle.
    #   - A schematic corner. The sheet stretches the Montebello Bl junction
    #     south: the warp puts Whittier & Montebello at (2279,2105) against the
    #     drawn (2271,2136), while at the Garfield end it is 7 px out. The warp's
    #     Whittier is *rotated* against the drawn one rather than displaced along
    #     it, so no one correction fits both ends of a 90 px stub.
    #   - The stub is 170 px of arc out and back, and the snap smooths its
    #     displacement over 61 densified points — 244 px. The two legs are
    #     averaged into each other and into the Montebello Bl and Greenwood legs
    #     either side, so neither can land on the line while the other pulls.
    # A pin does reach both legs — `badge_passes` gives an anchor to every pass a
    # shape makes at a point — and it is still no answer, because the fit
    # interpolates away from it: pinned mid-stub on the drawn Whittier, the
    # southbound working runs 12 px *past* the drawn Garfield corner to (2200,
    # 2095) and the northbound one stops 3 px short of it, and the wedge is
    # traded for an overshoot rather than closed.
    #
    # So the corridor is drawn by hand, and it is the one override that doubles
    # back: the path runs from the junction west along the drawn Whittier to
    # Garfield and returns along it. That is also what lets one path serve both
    # directions — the run's net displacement is nil, so the orientation test is
    # a no-op and the path has to close on itself, which it does. The box
    # brackets the warp from just west of the Montebello Bl junction out past
    # Garfield and down as far as the Greenwood corridor; each direction enters
    # it once, the southbound off Montebello Bl and the northbound where its
    # chord from Greenwood crosses in, and the feed's spurious Garfield loop
    # falls inside and is replaced with the rest. Over the stub the four
    # workings go from a median 2.9 px (southbound) and 9.4 px (northbound) off
    # the drawn corridor to 0.9, and from 74% and 66% of it covered to 100%.
    #
    # `path_check` ranks the route worse for it, and that is the trade rather
    # than a regression: the retrace is now exact, so the fold at Garfield is a
    # true 180 deg where before it was a 155 deg cusp *beside* the line. It is
    # the DASH Wilmington case — the sheet draws the stub, so the doubling back
    # is the artwork's and not the snapper's.
    ("montebello", "20"): {
        "box": (2186, 2080, 2276, 2146),
        "path": [
            (2271.0, 2136.5), (2260.0, 2129.4), (2252.0, 2124.9),
            (2244.0, 2120.4), (2236.0, 2115.8), (2228.0, 2111.2),
            (2220.0, 2106.6), (2212.0, 2103.0), (2220.0, 2106.6),
            (2228.0, 2111.2), (2236.0, 2115.8), (2244.0, 2120.4),
            (2252.0, 2124.9), (2260.0, 2129.4), (2271.0, 2136.5),
        ],   # Whittier Bl: the Montebello Bl junction out to Garfield and back
    },
    # Torrance 5's south end, from the terminus at Pacific Coast Hwy & Crenshaw
    # east along PCH and north into Arlington. TRIM_TERMINI cuts the layover off
    # the shape; what is left still comes out beside the drawing rather than on
    # it, and for the reason that put the terminus out of the trim's reach in the
    # first place — this is a schematic corner, and the warp holds the whole PCH
    # stretch ~66 px south of where the sheet draws it.
    #   - There is a "5" on the drawn PCH, at the west end of the stretch, and it
    #     is the only badge the route has down here. Nothing about it is out of
    #     reach: it is 43 px from the shape, well inside the anchor gate. It
    #     attaches to the wrong stretch, which is Montebello 10's failure again —
    #     the warp's *Arlington* leg passes 43 px from that badge while the warp's
    #     own PCH leg, the one the badge is printed on, is 65 px away. So the fit
    #     pulls a point 130 px into the route down onto the badge and leaves the
    #     terminus to find its own way.
    #   - What it finds is the same grey the layover rode: the sheet draws PCH,
    #     Crenshaw and Palos Verdes Dr through here and gives Torrance no line of
    #     its own on any of them, so the stretch snapped 40 px west onto street
    #     ink and hung below the drawn corner.
    #   - A pin cannot answer this either, for the reason a pin cannot answer
    #     Montebello 10's: a point on the drawn PCH is nearer the warp's Arlington
    #     leg than its PCH one, so it attaches to the wrong stretch too.
    # Two streets and a corner, so it is drawn by hand. The box sits south and
    # east of the drawn corridor, where the warp lands the shape, and stops where
    # the snap has Arlington right on its own.
    ("torrance", "5"): {
        "box": (1405, 3106, 1470, 3210),
        "path": [
            (1396.0, 3132.5), (1408.0, 3132.5), (1421.0, 3132.5),
            (1433.0, 3132.5),
        ],   # PCH, from the Crenshaw corner the sheet ends the route at as far
             # east as the snap can be rejoined. The box stops short of the
             # corner into Arlington rather than carrying on up the avenue,
             # because the two directions do not lag by the same amount: at the
             # same point along the shape the northbound working is 64 px
             # further up Arlington than the southbound one, and a corridor run
             # out past the corner would have to seam to both at once. Along
             # PCH they agree — at the box's edge one sits at x=1436 and the
             # other at x=1430.
    },
}


DESPIKE_WIN = 3         # densified points (~12 px each side), matching path_check
DESPIKE_ANGLE = 110.0   # deg; sharper than a square street corner
DESPIKE_GAP = 10.0      # px; the path returning this close to where it was a
                        # window ago has doubled back on itself
DESPIKE_MAX = 160.0     # px of arc; a doubling-back longer than this is a real
                        # loop, not a snapping sliver, and is left alone
DESPIKE_CHORD = 34.0    # px; and its two ends must land this close together —
                        # a sliver leaves the line and returns beside where it
                        # left, so straightening it barely moves anything, while
                        # a run whose ends are far apart spans a real bend that
                        # straightening would cut a corner off


def spike_penalty(pts, step=2.0, win=12.0):
    """How un-straight a polyline is: the turning it does beyond a square corner
    where it doubles back on itself, plus what it pays for sewing between two
    streets, measured on a uniform resampling so it is blind to vertex spacing.

    This is scripts/path_check.py's score, computed the same way on the same
    grid — matching it is what lets the caller's gate promise that no shape
    comes out of a cleanup pass ranking worse than it went in. Cheap enough to
    run on every candidate for every shape, so no pass is kept on faith."""
    P = np.asarray(pts, dtype=float)
    seg = np.hypot(*np.diff(P, axis=0).T)
    cum = np.concatenate([[0], np.cumsum(seg)])
    if cum[-1] < 2 * step:
        return 0.0
    t = np.arange(0, cum[-1], step)
    R = np.c_[np.interp(t, cum, P[:, 0]), np.interp(t, cum, P[:, 1])]
    n = len(R)
    w = max(1, int(round(win / step)))
    if n < 2 * w + 1:
        return 0.0
    a = R[w:-w] - R[:-2 * w]
    b = R[2 * w:] - R[w:-w]
    na, nb = np.hypot(*a.T), np.hypot(*b.T)
    turn = np.degrees(np.arctan2(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0],
                                 a[:, 0] * b[:, 0] + a[:, 1] * b[:, 1]))
    gap = np.hypot(*(R[2 * w:] - R[:-2 * w]).T)
    # Score only where path_check would: on the drawn map, outside the ghosted
    # Downtown call-out, where the ends of the window are far enough apart to
    # have a turn at all.
    mid = R[w:-w]
    drawn = ((na > 1e-6) & (nb > 1e-6) & (mid[:, 0] >= 0) & (mid[:, 0] <= 4096)
             & (mid[:, 1] >= 708) & (mid[:, 1] <= 4139)
             & ~np.asarray(inside_callout(mid), dtype=bool))
    turn = np.where(drawn, turn, 0.0)
    mag = np.abs(turn)
    cusp = np.where(gap < 12.0, np.maximum(0.0, mag - 92.0), 0.0).sum()
    # and the zigzag: adjacent sharp turns in opposite directions, charged by
    # how sharp the tighter of the pair is, whatever either owes on its own
    sewn = (mag[1:] > 45.0) & (mag[:-1] > 45.0) & (np.sign(turn[1:]) != np.sign(turn[:-1]))
    zig = np.where(sewn, np.minimum(mag[1:], mag[:-1]) - 45.0, 0.0).sum()
    return float(cusp + zig)


def stored_penalty(full):
    """spike_penalty of exactly what a shape would be written out as: simplified
    and rounded to the tenth of a pixel schedule.json carries.

    Not a detail. The doubling-back test turns on a hard 12 px threshold, so a
    hundredth of a pixel can carry a whole run of points across it — jittering
    one Metro 202 shape by 0.02 px swings its score between 505 and 767. Score
    the geometry that ships, or the gate below can promise nothing about it."""
    return spike_penalty(np.round(simplify(full), 1))


def despike(full):
    """Straighten thin retrace spikes out of a snapped polyline.

    The snapper sometimes crushes a sharp deviation into a hairpin that darts
    out and straight back on itself within a dozen px — a shape no drawn line
    makes. Each such run is replaced by a straight line between the good points
    just outside it, which removes the dart without moving those points, so no
    new corner appears at the join. Only genuine doubling-back is touched: a
    square street corner turns without returning (its window stays open) and a
    real terminal loop keeps its two arms a block apart, so both are left as
    they are, as is any reversal longer than a snapping sliver could be."""
    n = len(full)
    w = DESPIKE_WIN
    if n < 4 * w:
        return full
    a = full[w:-w] - full[:-2 * w]
    b = full[2 * w:] - full[w:-w]
    na, nb = np.hypot(*a.T), np.hypot(*b.T)
    ang = np.abs(np.degrees(np.arctan2(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0],
                                       a[:, 0] * b[:, 0] + a[:, 1] * b[:, 1])))
    gap = np.hypot(*(full[2 * w:] - full[:-2 * w]).T)
    hit = (na > 1e-6) & (nb > 1e-6) & (ang > DESPIKE_ANGLE) & (gap < DESPIKE_GAP)
    idx = np.nonzero(hit)[0] + w          # back to full-index space
    if not len(idx):
        return full
    seg = np.hypot(*np.diff(full, axis=0).T)
    cum = np.concatenate([[0], np.cumsum(seg)])
    out = full.copy()
    runs, s, p = [], idx[0], idx[0]
    for i in idx[1:]:
        if i <= p + w:
            p = i
        else:
            runs.append((s, p)); s = p = i
    runs.append((s, p))
    for s, p in runs:
        lo, hi = max(0, s - w), min(n - 1, p + w)
        if (hi - lo < 2 or cum[hi] - cum[lo] > DESPIKE_MAX
                or np.hypot(*(full[hi] - full[lo])) > DESPIKE_CHORD):
            continue
        t = np.linspace(0, 1, hi - lo + 1)[:, None]
        out[lo:hi + 1] = full[lo] * (1 - t) + full[hi] * t
    return out


FOLD_GAP = 14.0     # px; a run that ends this close to where it began has come
                    # back to a point the line already occupied
FOLD_MIN = 24.0     # px of arc; anything shorter is despike's sliver
FOLD_MAX = 190.0    # px of arc; a longer way round is a circuit the sheet draws
                    # rather than a line laid back over itself
FOLD_OUT = 6.0      # px; the run has to go somewhere before coming back, or it
                    # is a line dawdling in place, not a fold
FOLD_REACH = 70.0   # px, and no further. However the arc is spent, this is the
                    # most route a fold can be hiding, and so the most that
                    # flattening one can cost: past it the doubling-back is more
                    # line than any sliver of snapping, and the sheet is likelier
                    # to be drawing a working that really does run out and back.
FOLD_WIDTH = 12.0   # px; and it has to come back *along itself*. Twice the area
                    # a run encloses over its own length is the mean distance
                    # between its two arms: a fold's arms lie on top of each
                    # other, while a route going round something holds them a
                    # block apart.
FOLD_WARP = 4.0     # px of that same gap, in the warp the fold was crushed from
                    # — enough that the route went somewhere over the stretch
                    # rather than out and back down one street. Under it the
                    # doubling-back is the route's own and the snapper only
                    # inherited it.


def arm_gap(P, i, j):
    """Mean distance between the two arms of the run P[i:j+1] — twice the area
    it encloses over its own length. Near zero where the run doubles back along
    itself, a block's width where it goes round something."""
    run = P[i:j + 1]
    length = np.hypot(*np.diff(run, axis=0).T).sum()
    if length <= 0:
        return 0.0
    x, y = run[:, 0], run[:, 1]
    return abs(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y)) / length


def strands_badge(before, after, badges):
    """Whether reshaping a line from `before` to `after` leaves one of the route's
    own printed badges with no path near it any more.

    A badge stands on the line it names, so a detour that is the only thing
    reaching one is a detour the sheet draws — Metro 601's run down to the badge
    on Burbank Blvd doubles back on itself the same way the snapper's folds do,
    and is the route. Gated at the distance a badge is read from its line to
    begin with, so a badge the shape still passes doesn't count."""
    for b in badges:
        if (np.hypot(*(before - b).T).min() <= BADGE_NEAR_INK
                < np.hypot(*(after - b).T).min()):
            return True
    return False


def folds(full, base):
    """The runs of a snapped polyline that double back to where they started —
    and that the warp they came from does not, so the fold is the snapper's
    doing and not the route's.

    Each is the longest run from its start that returns within FOLD_GAP without
    exceeding FOLD_MAX of arc; where two overlap, the one doubling more line
    over itself wins, since it is the one with more to be rid of."""
    n = len(full)
    cum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(full, axis=0).T))])
    cands = []
    for i in range(n):
        lo = int(np.searchsorted(cum, cum[i] + FOLD_MIN, side="left"))
        hi = min(int(np.searchsorted(cum, cum[i] + FOLD_MAX, side="right")) - 1, n - 1)
        if lo > hi:
            continue
        back = np.nonzero(np.hypot(*(full[lo:hi + 1] - full[i]).T) <= FOLD_GAP)[0]
        if not len(back):
            continue
        j = lo + int(back[-1])
        if not FOLD_OUT <= np.hypot(*(full[i:j + 1] - full[i]).T).max() <= FOLD_REACH:
            continue
        if arm_gap(full, i, j) > FOLD_WIDTH or arm_gap(base, i, j) <= FOLD_WARP:
            continue
        cands.append((cum[j] - cum[i], i, j))
    taken, used = [], np.zeros(n, bool)
    for _, i, j in sorted(cands, reverse=True):
        if used[i:j + 1].any():
            continue
        used[i:j + 1] = True
        taken.append((i, j))
    return sorted(taken)


def unfold(full, base, badges=()):
    """Take the folds out of a snapped polyline.

    Where the sheet draws a route the GTFS detours off — a bus pulling round a
    transit centre, a jog through an office park, a terminal loop the schematic
    ends in a stub — the detour has no ink of its own, and the snapper crushes
    it onto the line it does have. Nothing there is drawn twice, so the line
    runs out along that ink and straight back down it, and the fold is what
    path_check charges for.

    Only the snapper's folds go, and two things speak for the route against
    taking one out. `base` is the warp the snap displaced point for point, so it
    says what the route does over the same stretch: where the warp doubles back
    too, the retracing is the route's own. `badges` are the chips the sheet
    prints for this route, and one the line would no longer reach is the sheet
    saying it draws the route out there. Either way the fold stays, however the
    ranking scores it. The badges are read against the line as it stands after
    the folds already taken, not against the snap, so two that each look
    harmless can't between them strand a badge neither would have alone.

    The ink test `undetour` asks (see `_ink_vouches`) does *not* belong here,
    though it looks as though it should. Measured over every ink-snapped shape
    on the sheet it left total drift from the drawn lines unchanged and cost 594
    points of path_check, all of it on LADOT. The asymmetry is in what replaces
    a fold. An interior fold collapses to a chord between two points already
    within FOLD_GAP of each other, so the replacement is a few px long and sits
    on whatever ink its ends sit on — the test can never fire there. An end
    fold's replacement is a whole leg, and one leg reads as marginally further
    from the strokes than the doubled-over pair it replaces, which is enough to
    trip a 5 px threshold and veto precisely the fixes this pass exists to make.

    An interior fold is replaced by the straight line between the points either
    side, which are left where they are, so nothing outside the fold moves. A
    fold at an end is different: what it doubles over is the run out to the
    terminus, and collapsing it would leave the route stopping short of the end
    the sheet draws. That one keeps the leg that reaches the terminus and drops
    the other, stretched over the same indices, so the vehicle starts at its
    drawn end and drives in."""
    out = full.copy()
    n = len(full)
    B = np.asarray(badges, dtype=float).reshape(-1, 2)
    for i, j in folds(full, base):
        cand = out.copy()
        if i == 0 or j == n - 1:
            e = j if i == 0 else i
            m = int(np.hypot(*(full[i:j + 1] - full[e]).T).argmax()) + i
            leg = resample(full[m:j + 1] if i == 0 else full[i:m + 1], j - i + 1)
            cand[i:j + 1] = leg if len(leg) == j - i + 1 else full[m]
        else:
            t = np.linspace(0, 1, j - i + 1)[:, None]
            cand[i:j + 1] = full[i] * (1 - t) + full[j] * t
        if not strands_badge(out, cand, B):
            out = cand
    return out


# ---- detours: the snapper riding a neighbour's ink and coming back ----------
# The mask a bus route snaps onto is the whole *agency's* drawn lines — one
# undifferentiated blob of ink per legend colour (see `agency_tree` in main).
# It cannot tell Montebello 20 from Montebello 10. That is fine while a route's
# own line is drawn, because its own line is the nearest ink. It stops being
# fine where the sheet paints something over that line: a place label, a station
# marker, another route crossing on top. The ink under the label is simply
# missing from the mask, so the nearest ink for that stretch is a *sibling
# route*, and the smoothed displacement field walks the path over onto it and
# back — off the line by the Montebello/Commerce label, off it again in the
# label field by Whittier Narrows.
#
# Nothing already in the build sees this. `maskable` covers the opposite case,
# where the sheet drew nothing at all and the warp is rightly kept; here there
# is plenty of ink and it belongs to the wrong line. And the cleanup ballot is
# scored by `spike_penalty`, which charges only turning that doubles back
# within 12 px — the snapper's 61-point smoothing turns an occlusion into a
# *smooth bulge*, which has no sharp turn anywhere in it. Foothill 493 scores a
# flat 0 there while visibly leaving its line.
#
# What does see it is the warp the snapper started from. `base` and `full` are
# the same points before and after snapping, index for index, and the global
# poly2 fit is good to ~11 px median. So a stretch where `full` leaves `base`
# far and comes back is the snapper having found ink that the warp says is not
# this route's. The discriminators against a *legitimate* correction are that
# it returns (a real fix to a bad warp stays moved), that it is bounded in arc
# (Metro 690 is correctly carried 190 px onto Foothill Blvd for a third of its
# length), and that no badge vouches for it — a route-number chip inside the
# excursion means the sheet really does print the line out there.
DETOUR_MIN = 13.0     # px *past the sustained correction*; under this the
                      # snapper is refining, not relocating
DETOUR_ENDS = 7.0     # px; the run has to start and finish back on the
                      # correction, which separates an excursion from a fix
DETOUR_PEAK = 21.0    # px; and reach this far past it at its worst, or it is
                      # inside the warp's own error and not worth charging
DETOUR_ARC = 650.0    # px of arc. Longer than this the snapper is not detouring
                      # round an obstruction, it is tracking something for a
                      # sustained stretch, and the badges are the better judge
DETOUR_WEIGHT = float(os.environ.get("DETOUR_WEIGHT", 6.0))
                      # degrees of spurious turning that one px of excursion is
                      # worth, for the ballot in main(). The two measures are in
                      # different units and the ballot has to add them: a spike
                      # score runs to the hundreds while an excursion is tens of
                      # px, so unweighted the detour term is noise and the pass
                      # that fixes it never wins — Montebello 20's undetour took
                      # the excursion to zero and lost by 14 spike points. A
                      # detour is also the worse defect of the two: it puts the
                      # vehicle on the wrong street, where a kink only makes the
                      # right one look untidy.
DETOUR_BADGE = 40.0   # px; how near a chip has to be for the path to count as
                      # sitting on it at all
DETOUR_VOUCH = 9.0    # px. A chip near the excursion is not evidence *for* it:
                      # the sheet prints them every 50-100 px, so a route is
                      # within a chip's length of one almost everywhere along
                      # itself, and a plain proximity test vetoed every detour
                      # there was — Montebello 20's peak has a "20" 32 px away
                      # and so did the rest of the route. A chip only speaks for
                      # the excursion if *taking it out* would walk the path
                      # this much further from it than it is now.
DETOUR_INK = 5.0      # px, at INK_QUANTILE over the run. Where the drawing is
                      # to be had it gets the last word over the chips: a
                      # flattening that would stand this much further from the
                      # drawn line than the excursion does is not a flattening,
                      # it is the removal of the line.
INK_QUANTILE = float(os.environ.get("INK_QUANTILE", 0.85))
                      # of the run, sorted by distance from the drawing. The
                      # ends of a widened run agree whatever is done to its
                      # middle, so a median mostly reports that agreement; this
                      # reads the far end of the run instead. Set at the most a
                      # label can knock out of a mask — see `_ink_vouches`.


def _flatten_run(full, base, lo, hi):
    """The run with its excursion taken out — see undetour, which applies it."""
    d = full - base
    t = np.linspace(0, 1, hi - lo + 1)[:, None]
    return base[lo:hi + 1] + d[lo] * (1 - t) + d[hi] * t


def _badge_vouches(full, base, lo, hi, B):
    """Whether a route-number chip speaks for this excursion — that is, whether
    flattening it would carry the path away from a chip it is currently on."""
    R = full[lo:hi + 1]
    cur = float(np.sqrt(((B[:, None, :] - R[None]) ** 2).sum(-1)).min())
    if cur > DETOUR_BADGE:
        return False                       # not on a chip to begin with
    F = _flatten_run(full, base, lo, hi)
    new = float(np.sqrt(((B[:, None, :] - F[None]) ** 2).sum(-1)).min())
    return new > cur + DETOUR_VOUCH


def _ink_vouches(full, base, lo, hi, tree):
    """Whether the drawn line itself speaks for this excursion — that is,
    whether flattening it would carry the path off the artwork altogether.

    The badge test above is a proxy for this, and a coarse one. Chips are
    printed every 50-100 px, so a run a few hundred px long can hold two of
    them, and a flattening that trades one for the other passes: Metro 180 is
    drawn along Broadway and steps south onto Colorado at Eagle Rock while the
    warp runs straight through. The snapper follows the step exactly, to a
    median 0.8 px of the ink. The step then reads as a 40 px excursion over the
    sustained correction — it is one, geometrically — and the flattened version
    cuts the corner across blank page, yet lands 4.5 px from the "180" chip
    printed north of the bend where the correct path is 8.3 away. The badges
    vouched for the cut, and the ballot took it.

    Where the PDF's own strokes are to be had they are the better witness: they
    are the drawing itself, whole underneath every label painted over them. But
    the agencies that most need this test have no vector ink of their own and
    snap onto a colour mask, and the mask was written off here as unusable — it
    has label-shaped holes in it, and the stretch a genuine detour should be
    flattened back onto is exactly the stretch a label knocked out, so the
    flattening would read as leaving the artwork and every real fix would be
    vetoed. That is only true of a test that reads the hole. A word covers a
    short piece of a run and the drawing resumes either side of it, so the
    comparison has only to be made somewhere the hole cannot reach.

    Which is why the two versions are compared at `INK_QUANTILE` of their
    distance rather than at the median. The widened run reaches out to where the
    displacement has come back, so over its ends the flattening barely moves the
    path and both versions sit on the same ink; a median is mostly reporting
    that agreement, and it reported it loudly enough to lose a real corner. Big
    Blue Bus 14 comes down Centinela and turns east along Bluff Creek, a corner
    the warp cuts across. The snapper follows it to within a median 1.7 px of
    the drawn grey, and flattening it lays the route over blank page — yet the
    medians read 1.7 against 4.4, under the threshold, because the half of the
    run before the corner is on Centinela either way. At the 85th percentile the
    same pair reads 5.9 against 20.1 and the corner stays.

    Reading further out than that is worse, not better, and in the way that
    tells you the quantile is doing the intended work: at the maximum a single
    stray point decides, and the ink-snapped routes — whose strokes have no
    holes to forgive in the first place — start losing genuine fixes, Metrolink's
    Antelope Valley line and Metro 487 among them. Swept from the median to the
    maximum, 0.85 is the floor of total drift from the drawn lines: 15180 px
    committed, 14428 at the median, 13776 here, 14368 at the maximum, for two
    tenths of a percent of path_check and 50 routes improved against 2 made
    worse. Together with handing each mask-snapped shape its own agency mask it
    takes Foothill 492, 286 and 486 to no drift at all.

    What the drawing cannot answer for is the ground it is deliberately kept
    off. `pdf_ink` drops every stroke inside the Downtown call-out and the other
    regions the masks skip, so *any* flattening that lands in the panel reads as
    leaving the artwork, and the test would vouch for whatever the snapper did
    on the way in. Metro 14 comes down Beverly and finishes inside the call-out
    with only the warp to go on; the snapper takes it 33 px onto Alvarado's
    orange instead, and unqualified this test called that the drawn line and
    kept it, reversing the route at its own badge. So the comparison is made
    only where a mask could have held something, and a run flattening mostly
    into the panel is left to the badges as before."""
    R = full[lo:hi + 1]
    F = np.asarray(_flatten_run(full, base, lo, hi))
    keep = maskable(R) & maskable(F)
    if keep.sum() < max(4, len(F) // 2):
        return False
    q = 100 * INK_QUANTILE
    cur = float(np.percentile(tree.query(R[keep])[0], q))
    new = float(np.percentile(tree.query(F[keep])[0], q))
    return new > cur + DETOUR_INK


def detour_runs(full, base, badges=(), ink=None):
    """Stretches where `full` leaves `base` a long way and returns to it.

    Measured against the *sustained* part of the correction, not against the
    warp itself. A shape that the badges have rightly carried bodily onto its
    street sits at a steady offset from the warp for its whole length —
    Montebello 20 runs 13 px off it everywhere — and against an absolute
    threshold that baseline either swamps every excursion or, worse, never lets
    one close, because the displacement never returns to zero for the run to end
    at. A median over a window wider than any detour recovers that baseline
    without being dragged up by the detour sitting inside it, and what is left
    over it is the excursion.

    Yields (lo, hi, peak): the index span, widened out to where the path is
    genuinely back on the sustained correction, and how far past it it got."""
    off = np.hypot(*(full - base).T)
    n = len(off)
    if n < 16:
        return []
    cum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(base, axis=0).T))])
    step = max(1e-6, cum[-1] / max(1, n - 1))
    w = int(min(n // 2 * 2 - 1, max(9, DETOUR_ARC / step))) | 1
    off = off - ndi.median_filter(off, size=w, mode="nearest")
    B = np.asarray(badges, dtype=float) if len(badges) else None
    out, i = [], 0
    while i < n:
        if off[i] <= DETOUR_MIN:
            i += 1
            continue
        j = i
        while j + 1 < n and off[j + 1] > DETOUR_MIN:
            j += 1
        nxt = j + 1
        # widen to where the displacement has actually come back to the warp
        lo, hi = i, j
        while lo > 0 and off[lo] > DETOUR_ENDS:
            lo -= 1
        while hi < n - 1 and off[hi] > DETOUR_ENDS:
            hi += 1
        i = nxt
        # An excursion that runs off the end of the shape never came back, so
        # there is nothing to say it is an excursion rather than a correction.
        if off[lo] > DETOUR_ENDS or off[hi] > DETOUR_ENDS:
            continue
        if cum[hi] - cum[lo] > DETOUR_ARC:
            continue
        k = lo + int(np.argmax(off[lo:hi + 1]))
        peak = float(off[k])
        if peak < DETOUR_PEAK:
            continue
        if B is not None and _badge_vouches(full, base, lo, hi, B):
            continue
        if ink is not None and _ink_vouches(full, base, lo, hi, ink):
            continue
        out.append((lo, hi, peak))
    return out


def detour_penalty(full, base, badges=(), ink=None):
    """How much of a shape is off its line and back. The measure `spike_penalty`
    cannot make, and the reason the ballot in main() scores both."""
    return float(sum(pk - DETOUR_PEAK
                     for _, _, pk in detour_runs(full, base, badges, ink)))


def undetour(full, base, badges=(), ink=None):
    """Put a detoured stretch back on the warp.

    The correction either side of the excursion is real — it is what put the
    line on its own ink — so it is kept and interpolated across, rather than
    dropping the stretch flat onto the warp and leaving a step at each join.
    What the stretch loses is only the part of the displacement that took it to
    a neighbour's ink, which is the part with nothing to vouch for it."""
    runs = detour_runs(full, base, badges, ink)
    if not runs:
        return full
    out, d = full.copy(), full - base
    for lo, hi, _ in runs:
        t = np.linspace(0, 1, hi - lo + 1)[:, None]
        out[lo:hi + 1] = base[lo:hi + 1] + d[lo] * (1 - t) + d[hi] * t
    return out


DETOUR_AUDIT = []     # (peak, feed, route, x, y) for the report main() prints


def apply_override(full, base, spec):
    """Splice a hand-drawn corridor into a snapped shape.

    `full` (snapped) and `base` (warped) are equal-length and index-aligned, so
    stops keep projecting onto the warp and carrying over. `spec["box"]` (warp
    px) brackets the stretch to replace; the run of the shape inside it is
    swapped for `spec["path"]`, resampled to the same point count and oriented
    to the shape's direction of travel, so the alignment — and the stop timing —
    hold. A shape that doesn't enter the box is left untouched."""
    B = np.asarray(base, dtype=float)
    x0, y0, x1, y1 = spec["box"]
    inside = np.where((B[:, 0] >= x0) & (B[:, 0] <= x1)
                      & (B[:, 1] >= y0) & (B[:, 1] <= y1))[0]
    if len(inside) < 2:
        return full
    lo, hi = int(inside[0]), int(inside[-1])
    path = np.asarray(spec["path"], dtype=float)
    # Orient by the course the shape holds across the box, not by which end of
    # the corridor its entry point is nearer. The two agree wherever the box is
    # entered near one end of the drawn stretch, and that is every corner here
    # but Torrance 5's: the sheet ends that route halfway along the corridor's
    # length from where the warp leaves the shape's own terminus, which stands
    # 59 px from one end of the path and 60 from the other. A distance that
    # close to tied decides nothing, while the direction of travel is not close
    # at all.
    seg = (path if np.dot(B[hi] - B[lo], path[-1] - path[0]) >= 0
           else path[::-1])                # run the corridor the way the shape does
    d = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(seg, axis=0).T))])
    t = np.linspace(0, d[-1], hi - lo + 1)
    res = np.c_[np.interp(t, d, seg[:, 0]), np.interp(t, d, seg[:, 1])]
    out = np.array(full, dtype=float)
    out[lo:hi + 1] = res
    return [tuple(p) for p in out]


BRANCH_FLOOR = 24.0    # px further than the nearest variant a badge may sit and
                       # still be shared, about the width of a drawn street


def branch_anchors(anchors, sid, sids, kd_for):
    """The badges that belong to *this* variant of a route.

    route_anchors finds every badge the sheet prints for a route, and a shape
    gets all of them. But a shape is one variant, and where a route forks, the
    badges standing on one fork still fall inside the anchor gate of a variant
    that takes the other. The fit then drags that variant bodily across: Metro
    487's Rosemead Blvd workings were pulled 143 px west onto the San Gabriel
    branch, which is the chord cutting across San Gabriel — its own warp was
    on the drawn line before the badges got hold of it.

    A badge is printed on one line, so it speaks for whichever variant passes
    nearest it. Keep it for this shape while this shape comes within a street's
    width of as near, and drop it when another explains it better than that. On
    the trunk, where every variant runs the same street and is equally close,
    that keeps the badge for all of them; it only bites where the route forks.
    A route with one shape has nothing to compare against and keeps the lot.

    The comparison is additive, and it used to be a ratio besides — twice the
    nearest variant's distance was near enough to share. That reading fails in
    exactly the places badges are needed most. Where the warp is good every
    variant is a few px from the badge and the ratio is meaningless; where it is
    bad the distances are all inflated by the same local error, and doubling it
    buys slack measured in the error rather than in streets. Out at Sunset and
    the 405 the warp is 66 px off, so a "602" printed on the Brentwood stretch
    was 96 px from the short working that turns back at Church — half again as
    far as the workings that actually run past it, and inside the ratio. The
    badge sat 6 px of arc past the one pinning the turn, and pulled the last
    stop of the working 163 px down Sunset into Brentwood, where the vehicle
    sprinted the last leg at 235 km/h and vanished a mile short of the corner
    the sheet ends it at.

    Additive, at 24 px — under the 30 px that separates parallel drawn lines,
    so a badge on the next street over can't be shared. Swept against the whole
    map: 20 and 22 come out level with 24 and 26, and 28 gives the ratio's
    failure back. At 24 the sheet's routes read 1191 better on path_check and
    672 on drift_check, with the six worst hairpins on the map (LADOT 573,
    Metro 222) gone and Long Beach Transit's whole network pulled in."""
    if not anchors or len(sids) < 2:
        return anchors
    A = np.asarray(anchors, dtype=float)
    dists = [kd_for(s).query(A)[0] for s in sids]
    best = np.min(np.vstack(dists), axis=0)
    mine = dists[sids.index(sid)]
    return [a for a, k in zip(anchors, mine <= best + BRANCH_FLOOR) if k]


TRACE_STEP = 4.0          # px; lattice pitch for walking the drawn line
TRACE_PAD = 80.0          # px of slack around the two badges, for a bowed line —
                          # and for a corridor that leaves their box entirely, which
                          # a route turning a corner between two badges does: Metro
                          # 182 runs 63 px east of the eastern badge before turning
                          # back, so at 60 the lattice cut the corner off the map and
                          # the walk came back "no path" for a corridor that is drawn
TRACE_REACH = 5.0         # px; how close a lattice cell must be to drawn pixels,
                          # loose enough to step over the glyphs crossing a line
TRACE_SPAN = (float(os.environ.get("TRACE_MIN", 60.0)), 700.0)   # px between badges
                          # worth walking between
TRACE_DETOUR = (float(os.environ.get("TRACE_LO", 0.75)),
                float(os.environ.get("TRACE_HI", 1.35)))  # trusted band of
                          # walked length / shape length; sweepable, since what it
                          # costs is measured in routes rather than argued
TRACE_SAMPLE = 30.0       # px of walked line per intermediate anchor
TRACE_LIMIT = float(os.environ.get("TRACE_LIMIT", 4.0))   # times the shape's arc a
                          # walk may run to be worth aligning at all; past this it
                          # is not a rate difference, it is a different journey
BRIDGE_MAX = 40.0         # px of interruption a walk may step across
BRIDGE_HOOD = 7.0         # px of drawing read to say which way a line runs there
BRIDGE_INTO = 0.5         # cos 60 deg: how squarely each side must run into
                          # the hole, which allows a corner inside it

_PATHS = {}   # keyed by tree identity, safe because _TREES holds every tree
              # for the life of the process, so no id is ever reused


def line_headings(P, tree, hood=BRIDGE_HOOD):
    """Unit direction the drawing runs in at each of the points P, or (0,0)
    where there isn't enough of it nearby to say.

    The principal axis of the drawn pixels around a point: a line's pixels lie
    along it, so the largest eigenvector of their covariance is the direction
    it runs. Sign is arbitrary and every caller treats it that way."""
    out = np.zeros((len(P), 2))
    for i, nb in enumerate(tree.query_ball_point(P, hood)):
        if len(nb) < 4:
            continue
        Q = np.asarray(tree.data)[nb]
        Q = Q - Q.mean(0)
        w, v = np.linalg.eigh(Q.T @ Q)
        if w[1] > 3 * max(w[0], 1e-9):        # a line, not a blob or a corner
            out[i] = v[:, 1]
    return out


def bridges(C, free, tree, step, gap=BRIDGE_MAX):
    """Edges that step across an interruption in the drawing, as (rows, cols,
    weights) for the lattice graph.

    The sheet interrupts its own lines. It knocks the stroke out to make room
    for the chips it prints on them — Metro 182's corridor stops either side of
    the "81"/"182" pair on Figueroa, a 28 px hole — and a label crossing a line
    takes a bite out of any mask of it. Either way the corridor walk stops
    dead, `trace_anchors` gets nothing, and the stretch is interpolated
    straight: the chord across whatever corner the route turns there. That is
    Metro 182 cutting the corner at St George, Metro 134 leaving the coast
    road, and it is the root cause these notes have been describing since
    Montebello 20 without fixing.

    A hole is not a shortcut, and the difference is legible: at a hole the line
    stops and starts again *on the same heading*, while a shortcut leaves one
    line for another that runs some other way. So from every cell where the
    drawing runs in a definite direction, look along that direction: if the
    lattice goes unfree and then free again within `gap`, and the drawing at
    the far side runs the same way, connect them. The edge costs what it spans,
    so a bridged walk is still judged on its length by the band above.

    This is the directed bridge the notes reached for and ruled the isotropic
    version out of: a closing operation hangs word-shaped blobs off the
    underside of every street it touches, because it fills in all directions at
    once. This one fills along the line and nowhere else."""
    nx, ny = free.shape
    P = C.reshape(-1, 2)
    cand = np.flatnonzero(free)
    if not len(cand):
        return np.empty(0, int), np.empty(0, int), np.empty(0)
    H = line_headings(P[cand], tree)
    known = np.abs(H).sum(1) > 0
    cand, H = cand[known], H[known]
    if not len(cand):
        return np.empty(0, int), np.empty(0, int), np.empty(0)

    # Where the drawing stops: one step on along its own heading is off it.
    # `out` is the way it was running when it stopped, so it points into the
    # hole. Only these can carry a bridge, which is most of the safety in this:
    # a line crossing another, or running past it, is not an end and is never a
    # candidate, so nothing here can hop between two lines that merely meet.
    ends, out = [], []
    for sign in (1.0, -1.0):
        nxt = P[cand] + sign * H * step
        gx = np.clip(np.rint((nxt[:, 0] - C[0, 0, 0]) / step).astype(int), 0, nx - 1)
        gy = np.clip(np.rint((nxt[:, 1] - C[0, 0, 1]) / step).astype(int), 0, ny - 1)
        for i in np.flatnonzero(~free[gx, gy]):
            ends.append(int(i))
            out.append(sign * H[i])
    if not ends:
        return np.empty(0, int), np.empty(0, int), np.empty(0)
    ends, out = np.asarray(ends), np.asarray(out)

    # Two ends bridge when each runs into the hole the other runs into. A
    # straight line interrupted mid-run is the easy case, both pointing along
    # the same axis; the case that matters more is a corner whose *junction* is
    # what got knocked out — Metro 182's diagonal and the horizontal it turns
    # onto both stop at the Highland Park station marker, 24 px apart, and no
    # test of "same heading" can join them because the drawing turns inside the
    # hole. Facing each other is what they still do, so that is what is asked.
    # Anything perpendicular — the gap between two parallel streets, which is
    # the failure to avoid — fails it flat.
    kd = cKDTree(P[cand[ends]])
    rows, cols, w = [], [], []
    for i in range(len(ends)):
        best, bestd = None, np.inf
        for k in kd.query_ball_point(P[cand[ends[i]]], gap):
            d = P[cand[ends[k]]] - P[cand[ends[i]]]
            n = float(np.hypot(*d))
            if n <= step or n >= bestd:
                continue
            u = d / n
            if u @ out[i] < BRIDGE_INTO or -u @ out[k] < BRIDGE_INTO:
                continue                    # not the two sides of one hole
            best, bestd = k, n
        if best is not None:
            rows.append(cand[ends[i]]); cols.append(cand[ends[best]]); w.append(bestd)
    return (np.asarray(rows, int), np.asarray(cols, int), np.asarray(w, float))


def mask_path(a, b, tree, step=TRACE_STEP, pad=TRACE_PAD, reach=TRACE_REACH):
    """(polyline, length) of the shortest route from a to b that stays on the
    drawn mask, or (None, None) if the mask doesn't connect them.

    A coarse lattice over the two points' bounding box, cells kept where drawn
    pixels are within `reach`, 8-connected, Dijkstra. The lattice is
    deliberately blunt: drawn lines are ~8 px wide, so a 4 px pitch keeps every
    corridor connected while leaving only a few thousand nodes to search. The
    walk is only ever used to aim the snap, which then refines onto the pixels.

    The drawing is interrupted, and the walk steps across the interruptions it
    can justify — see `bridges` below. Widening `reach` instead is the wrong
    tool and these notes have said so since BBB 14: at 6 px the lattice steps
    onto the glyphs beside a line and comes back with a shortcut through the
    words. A bridge crosses blank page only where the line resumes on the same
    heading, which is what an interruption looks like and a shortcut doesn't."""
    key = (id(tree), round(a[0]), round(a[1]), round(b[0]), round(b[1]))
    if key in _PATHS:                  # a route's variants share their badges
        return _PATHS[key]
    a, b = np.asarray(a, float), np.asarray(b, float)
    lo, hi = np.minimum(a, b) - pad, np.maximum(a, b) + pad
    nx, ny = (int(np.ceil((hi[k] - lo[k]) / step)) + 1 for k in (0, 1))
    C = np.stack(np.meshgrid(lo[0] + step * np.arange(nx),
                             lo[1] + step * np.arange(ny), indexing="ij"), -1)
    free = (tree.query(C.reshape(-1, 2))[0] < reach).reshape(nx, ny)
    ia, ib = (int(np.abs(C - p).sum(2).argmin()) for p in (a, b))
    free.flat[[ia, ib]] = True         # the badges themselves are on the line
    idx = np.arange(nx * ny).reshape(nx, ny)
    rows, cols, w = [], [], []
    for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
        s0 = idx[max(0, -dx):nx - max(0, dx), max(0, -dy):ny - max(0, dy)]
        s1 = idx[max(0, dx):nx - max(0, -dx), max(0, dy):ny - max(0, -dy)]
        ok = free.flat[s0] & free.flat[s1]
        rows.append(s0[ok])
        cols.append(s1[ok])
        w.append(np.full(int(ok.sum()), step * math.hypot(dx, dy)))

    def solve(extra=None):
        r, c, ww = list(rows), list(cols), list(w)
        if extra is not None:
            r.append(extra[0]); c.append(extra[1]); ww.append(extra[2])
        G = sparse.coo_matrix((np.concatenate(ww),
                               (np.concatenate(r), np.concatenate(c))),
                              shape=(nx * ny, nx * ny))
        return dijkstra(G + G.T, indices=ia, return_predecessors=True)

    dist, pred = solve()
    if not np.isfinite(dist[ib]):
        # Only now, and this ordering is the whole safety of it. A bridge is for
        # a corridor the drawing does not connect at all; where it does connect,
        # the drawn way round is the one to take. Offered as an ordinary edge
        # instead, a bridge cuts corners that are drawn: Big Blue Bus 7 turns
        # from Pico onto Crenshaw and the two ends either side of that corner
        # face each other across it, so the shortcut won on length and the route
        # left the grey it had been sitting on to a median 0.5 px.
        dist, pred = solve(bridges(C, free, tree, step))
    if not np.isfinite(dist[ib]):
        _PATHS[key] = (None, None)
        return _PATHS[key]
    walk = [ib]
    while walk[-1] != ia:
        walk.append(pred[walk[-1]])
    _PATHS[key] = (C.reshape(-1, 2)[walk[::-1]], float(dist[ib]))
    return _PATHS[key]


ALIGN_SAMPLES = 24     # points each curve is read at when the two are aligned
_ALIGN_OFF = False     # set while the same shape is fitted without aligned walks
_ALIGN_USED = 0        # aligned walks taken since the count was last read


def align_used():
    """How many aligned walks the last fit took, and reset. The caller fits the
    shape a second time without them when this is non-zero, and keeps whichever
    result is better — nothing here is trusted, only measured."""
    global _ALIGN_USED
    n, _ALIGN_USED = _ALIGN_USED, 0
    return n


_LAST_SNAP = None      # the arguments of the last snap, so it can be repeated


def snap_recording(*args, **kw):
    """snap_coherent, remembering how it was called so the same fit can be run
    again with the aligned walks refused."""
    global _LAST_SNAP
    _LAST_SNAP = (args, kw)
    return snap_coherent(*args, **kw)


def resnap_without_alignment():
    """The last snap, fitted again with out-of-band walks left out."""
    global _ALIGN_OFF
    if _LAST_SNAP is None:
        return None
    _ALIGN_OFF = True
    try:
        return snap_coherent(*_LAST_SNAP[0], **_LAST_SNAP[1])
    finally:
        _ALIGN_OFF = False
        align_used()


def ink_offset(P, tree, q=0.85):
    """How far a path sits from the drawing it should be on, read high enough
    up the distribution that a hole in a mask cannot answer for the whole of
    it — the same reading `_ink_vouches` takes, and for the same reason."""
    if tree is None or P is None or not len(P):
        return 0.0
    return float(np.quantile(tree.query(np.asarray(P, float))[0], q))
ALIGN_SPACING = float(os.environ.get("ALIGN_SPACING", 10.0))   # px of shape
                       # between two aligned anchors, so the field stays smooth
ALIGN_DEG = float(os.environ.get("ALIGN_DEG", 70.0))   # mean heading
                       # disagreement, deg, an alignment may leave. Loose on
                       # purpose: what decides an alignment is the measurement
                       # afterwards, not this, and swept from 20 to 999 the
                       # results stop changing at 70 — so it bounds the absurd
                       # and nothing else. Tightening it to 20 costs 6 % of
                       # the drift and 19 % of the hairpin score


def _headings(P, k):
    """Unit-tangent angles at k evenly spaced steps along a polyline."""
    P = np.asarray(P, float)
    cum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(P, axis=0).T))])
    if cum[-1] <= 0:
        return None, None
    t = np.linspace(0, cum[-1], k + 1)
    Q = np.c_[np.interp(t, cum, P[:, 0]), np.interp(t, cum, P[:, 1])]
    d = np.diff(Q, axis=0)
    if (np.hypot(*d.T) > 1e-9).sum() < 3:
        return None, None
    return np.arctan2(d[:, 1], d[:, 0]), t


def align_walk(walk, seg):
    """Where each point of `walk` sits along `seg`, as a fraction of `seg`.

    The walk and the shape's own stretch run over the same ground at different
    rates — that is the whole difficulty. The rate is not constant either: the
    sheet compresses one street and stretches the next, so no single scale
    relates them. What survives is the *order* of the turns, because the warp is
    smooth: it can put a corner in the wrong place but not in the wrong order.

    So align the two by their headings, letting either run slow or fast
    (Dynamic Time Warping over the wrapped angle difference), and read each
    walk sample's place along the shape off the alignment. Returns None when the
    two courses cannot be aligned at all, which is the honest reading of "this
    walk is not this stretch of route"."""
    ka, kb = ALIGN_SAMPLES, ALIGN_SAMPLES
    wa, wt = _headings(walk, ka)
    sa, st = _headings(seg, kb)
    if wa is None or sa is None:
        return None, None
    d = wa[:, None] - sa[None, :]
    cost = np.abs(np.arctan2(np.sin(d), np.cos(d)))
    D = np.full((ka + 1, kb + 1), np.inf)
    D[0, 0] = 0.0
    for i in range(1, ka + 1):
        prev, cur = D[i - 1], D[i]
        for j in range(1, kb + 1):
            cur[j] = cost[i - 1, j - 1] + min(prev[j], cur[j - 1], prev[j - 1])
    i, j, pairs = ka, kb, []
    while i > 0 and j > 0:
        pairs.append((i - 1, j - 1))
        k = int(np.argmin([D[i - 1, j - 1], D[i - 1, j], D[i, j - 1]]))
        i, j = (i - 1, j - 1) if k == 0 else (i - 1, j) if k == 1 else (i, j - 1)
    pairs.reverse()
    if not pairs:
        return None, None
    mean_deg = math.degrees(sum(cost[p] for p in pairs) / len(pairs))
    # each walk sample's place along the shape, in [0,1], made non-decreasing
    frac = np.zeros(ka)
    seen = np.zeros(ka, bool)
    for a, b in pairs:
        frac[a] = max(frac[a], (b + 0.5) / kb)
        seen[a] = True
    for a in range(1, ka):
        if not seen[a]:
            frac[a] = frac[a - 1]
        frac[a] = max(frac[a], frac[a - 1])
    return frac, mean_deg


def trace_anchors(s, D, A, P, cum, tree):
    """Add intermediate anchors by walking the drawn line between badges.

    Two badges of a route bracket a stretch of its drawn line, but the
    displacement between them is interpolated straight — and where the map is
    schematic, that straight guess lands on the wrong street. Metro 2's warp
    runs up to 60 px south of the drawn Sunset through West Hollywood, wider
    than the block, so the interpolation settled it onto Santa Monica. Walking
    the mask from badge to badge recovers the corridor itself; sampling that
    walk pins the stretch to it, close enough for the snap passes to finish.

    The walk is trusted only when it comes out about as long as the shape says
    the stretch should be. The drawn lines are one connected web, so a walk
    that cuts a corner the route actually turns shows up as conspicuously
    shorter than the shape; anything outside the band keeps the straight
    interpolation.

    "As long as the shape says" is measured on the shape as it stands, which on
    the first pass is the warp — so where the warp is bad enough to need this,
    the yardstick is bad too, and the walk that would fix it can read as a
    detour. That is why the count of walks taken comes back with the anchors:
    the caller re-fits while it keeps rising, and a stretch the warp talked it
    out of is walked on a later pass, once the badges have brought the arc
    length to something like the truth."""
    out_s, out_D, walked, believed = [s[:1]], [D[:1]], 0, 0
    for i in range(len(s) - 1):
        ds = s[i + 1] - s[i]
        if ds > 0 and TRACE_SPAN[0] < math.dist(A[i], A[i + 1]) < TRACE_SPAN[1]:
            walk, length = mask_path(A[i], A[i + 1], tree)
            if walk is None:
                pass
            elif TRACE_DETOUR[0] * ds < length < TRACE_DETOUR[1] * ds:
                # Comparable lengths: run at the same rate, as before.
                wcum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(walk, axis=0).T))])
                k = max(1, round(length / TRACE_SAMPLE))
                t = np.arange(1, k) / k
                q = np.c_[np.interp(t * wcum[-1], wcum, walk[:, 0]),
                          np.interp(t * wcum[-1], wcum, walk[:, 1])]
                sv = s[i] + t * ds
                out_s.append(sv)
                out_D.append(q - np.c_[np.interp(sv, cum, P[:, 0]),
                                       np.interp(sv, cum, P[:, 1])])
                walked += 1
                believed += 1
            elif not _ALIGN_OFF and length < TRACE_LIMIT * ds:
                # Out of band, which until now threw the walk away. But the band
                # is not a test of whether the corridor is the route's — it is
                # the range over which "same rate" holds, and outside it the
                # anchors land at the wrong points and saw the line into
                # hairpins. Align the two courses instead and place the anchors
                # where they actually correspond; if they cannot be aligned,
                # then it really is the wrong corridor and it goes.
                seg = P[max(0, int(np.searchsorted(cum, s[i]))):
                        int(np.searchsorted(cum, s[i + 1])) + 1]
                frac, deg = align_walk(walk, seg)
                if frac is None or deg > ALIGN_DEG:
                    out_s.append(s[i + 1:i + 2]); out_D.append(D[i + 1:i + 2])
                    continue
                wcum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(walk, axis=0).T))])
                k = max(1, round(length / TRACE_SAMPLE))
                t = np.arange(1, k) / k
                q = np.c_[np.interp(t * wcum[-1], wcum, walk[:, 0]),
                          np.interp(t * wcum[-1], wcum, walk[:, 1])]
                # each sampled walk point's place along the shape, off the alignment
                ft = np.interp(t, (np.arange(len(frac)) + 0.5) / len(frac), frac)
                ft = np.maximum.accumulate(np.clip(ft, 0.0, 1.0))
                sv = s[i] + ft * ds
                # Spaced along the *shape*, not along the walk. A walk three
                # times the arc packs three anchors into every px of shape it
                # corresponds to, and neighbouring anchors a px apart carrying
                # displacements tens of px apart is a cliff in the field the fit
                # interpolates — which comes out as the hairpins that made the
                # widened band look like a bad idea in the first place.
                keep = np.diff(sv, prepend=s[i] - ALIGN_SPACING) >= ALIGN_SPACING
                if keep.sum() < 1:
                    out_s.append(s[i + 1:i + 2]); out_D.append(D[i + 1:i + 2])
                    continue
                sv, q = sv[keep], q[keep]
                out_s.append(sv)
                out_D.append(q - np.c_[np.interp(sv, cum, P[:, 0]),
                                       np.interp(sv, cum, P[:, 1])])
                walked += 1
                global _ALIGN_USED
                _ALIGN_USED += 1
        out_s.append(s[i + 1:i + 2])
        out_D.append(D[i + 1:i + 2])
    return np.concatenate(out_s), np.concatenate(out_D), walked, believed


ANCHOR_PASSES = 3     # times the anchor fit may be re-run to pick up more badges
ANCHOR_PITCH = (8.0, 2.0)   # px; the slide is searched coarse, then refined
ANCHOR_DRAG = 0.15    # px of residual charged per px of slide, per badge
ANCHOR_GAIN = 0.5     # of the badge residual a slide must clear to be believed
CROSSED_APART = 20.0  # px between two badges before they are different streets
CROSSED_SPAN = 30.0   # px along the shape within which they'd be the same stretch

PASS_SLACK = 4.0      # px. A second place the shape comes this near a badge is
                      # the same drawn line run twice, not a parallel one: half
                      # a line width, where two parallel streets are a street.
PASS_RISE = 40.0      # px. And it is a second *pass* only if the shape left the
                      # badge in between — went at least this far from it and
                      # came back. A route running alongside a badge holds much
                      # the same distance for hundreds of px, and every point of
                      # that run is within the slack of every other.
PASS_BACK = -0.5      # cos 120 deg: and it has to be running back the other way
PASS_HOOD = 12.0      # px of shape read either side of a pass, for its heading


def _pass_heading(P, cum, k, hood=PASS_HOOD):
    """Unit course the shape holds where it passes a badge."""
    lo = int(np.searchsorted(cum, cum[k] - hood))
    hi = min(len(P) - 1, int(np.searchsorted(cum, cum[k] + hood)))
    v = P[hi] - P[lo]
    n = float(np.hypot(*v))
    return v / n if n > 1e-9 else np.zeros(2)


def badge_passes(P, cum, A, gate, slack=PASS_SLACK, rise=PASS_RISE):
    """(badge index, shape index) for every pass the shape makes at each badge.

    A badge belongs to the point of the shape nearest it, and for almost every
    route that is the whole story. A route that runs the same drawn corridor
    twice is nearest it twice, at two places far apart along its own length,
    and pinning only the nearer leaves the other pass unanchored.

    Foothill 861 is the case. It loops out of Duarte up Highland Av, east along
    Royal Oaks Dr, round Encanto Pkwy, and comes back the same way, and the
    sheet prints one "861" on Royal Oaks at the Highland corner. That chip
    attached to the outbound pass, so on the way home the fit had nothing
    between the Encanto chip and the one at Duarte & Buena Vista — one 262 px
    span, which the corridor walk crossed by dropping down Las Lomas Rd onto
    Foothill's own 187 along Huntington Dr and running west on that instead:
    209 px against the drawn corridor's 250-odd, a ratio of 0.80 and inside the
    band by a hair. The return leg rode Huntington for 130 px, a block south of
    the Royal Oaks it is drawn along, and read as no drift at all — the 187's
    ink is the same evergreen.

    Three conditions, and the last two are what keep this from firing
    everywhere. A pass counts only if the shape comes back within `slack` of
    the distance the nearest one stands at, which is under a line width. It has
    to have *left* in between — gone `rise` px from the badge and returned — or
    a route running alongside a chip for a few hundred px, every point of it
    within a hair of every other, would be pinned to it over and over, each
    anchor demanding that its own point be the one on the badge. And it has to
    be running back the other way, which is what doubling along one drawn line
    means: Montebello 20 comes up Greenwood Av, west along Whittier Bl to
    Garfield and back east, then north up Montebello Bl, and the warp lays
    Greenwood near enough to the "20" printed on Montebello for the slack to
    take it — two parallel streets a block apart, both northbound, and the case
    `crossed_badges` and `anchor_slide` exist for rather than this one. Pinning
    both dragged the Whittier spur 24 px north off its line.

    The nearest pass is kept whatever its course; the test is on the others,
    against it."""
    out = []
    for i, a in enumerate(A):
        d = np.hypot(P[:, 0] - a[0], P[:, 1] - a[1])
        best = float(d.min())
        if best >= gate:
            continue
        # One visit per run of the shape that stays inside `rise` of the badge;
        # the shape has to go further than that for the next run to be a second
        # pass rather than more of the same approach.
        near = d <= best + rise
        edges = np.flatnonzero(np.diff(near.astype(np.int8)))
        starts = np.r_[0 if near[0] else [], edges[near[edges + 1]] + 1].astype(int)
        hits = []
        for s in starts:
            e = s
            while e + 1 < len(near) and near[e + 1]:
                e += 1
            k = s + int(np.argmin(d[s:e + 1]))
            if d[k] <= best + slack:
                hits.append(k)
        if not hits:
            continue
        first = min(hits, key=lambda k: d[k])
        h0 = _pass_heading(P, cum, first)
        out += [(i, k) for k in hits
                if k == first or float(h0 @ _pass_heading(P, cum, k)) <= PASS_BACK]
    return out


def crossed_badges(A, cum, j):
    """Whether two badges on different streets are claiming the same stretch.

    A badge is read as belonging to whichever point of the shape is nearest it,
    which asks the warp to be closer to the truth than the streets are to each
    other. Where it isn't, a route running out and back on parallel streets
    hands both streets' badges to whichever leg the warp favours — and the
    shape carries one line through that stretch, so at most one of them can be
    right. Two badges a street apart on the sheet claiming the same few px
    along the shape is that failure, visible without knowing which is wrong."""
    if len(A) < 2:
        return False
    order = np.argsort(cum[j])
    s, pos = cum[j][order], A[order]
    return bool(((np.diff(s) < CROSSED_SPAN) &
                 (np.hypot(*np.diff(pos, axis=0).T) > CROSSED_APART)).any())


def anchor_slide(P, A, gate):
    """The translation of the whole shape that best explains its badges.

    GTrans 2 is a loop north on Normandie and Vermont and back south on
    Western, three drawn lines 30 and 56 px apart, and the warp puts all three
    25-40 px east of their ink: every badge on a northbound street came out
    nearest the southbound leg instead, and the fit dragged each leg across the
    loop onto the other one's street.

    The warp's error varies slowly over a few miles of map, so a shape's legs
    are all out by much the same vector — slide the shape by it and each badge
    is nearest its own leg again. The slide is searched on a coarse grid, then
    refined, and is charged for its length, so the smallest slide that sorts
    the badges out is the one taken. It only decides which point of the shape
    each badge speaks for; the displacement fitted afterwards is still measured
    from the unslid shape, so the correction it carries is the whole error,
    slide included.

    Two ways of refusing: a slide that wants to run past the search bound is
    one the badges don't agree on a direction for, and a slide that leaves most
    of the residual behind hasn't explained the badges, it has only nudged
    them. Only a slide that takes the shape from missing its badges to running
    through them is worth re-reading them against.

    The bound is the anchor gate itself, because past it there is nothing to
    slide onto: a badge further from the shape than the gate does not count for
    it at any offset, so a longer reach can only chase badges the fit will
    ignore. It used to be a separate 48 px, which is shorter than the warp's
    own error in the places the slide exists for. Through the west Valley the
    sheet holds Devonshire ~87 px south of where the warp puts it and Tampa ~57
    px east, so Metro 242 wanted a bodily (58, 92) — 109 px — and every badge
    printed along Tampa attached to a point half the route away. One direction
    came out cutting diagonally across blank page onto Winnetka; the other kept
    the street but ran 51% longer than the ground it covers, which is what put
    four of its segments over 120 km/h.

    Reaching further is only safe with the gain read tighter, and 0.7 was too
    loose to be a test at all once the base residual was hundreds of px: a
    slide could leave a badge 40 px off its line and still "clear" it. At 0.5
    the marginal slides are refused and the decisive ones — Metro 242's cuts a
    352 px residual to 9 — are kept. Swept together over the whole map against
    a 48 px bound: path_check 31148 -> 30322, drift_check 10920 -> 9984, and
    segments over 120 km/h 61 -> 42 on the sheet and 13 -> 7 in the call-out.
    Torrance R3 is the shape of the win — 961 -> 219, its flat hairpin beside
    the Mary K. Giordano Regional Transit Center replaced by the loop into the
    hub the sheet actually draws it serving.

    Thirteen routes score worse on path_check and drift says none of them is
    worse to look at. The DASH Wilmington is the loudest, 274 -> 856, and it is
    the case to understand: it now sits on its drawn olive everywhere — 8 px of
    it over 12 px from the ink, down to 0 — and what it scores for is running
    out to the "WM" beside Watson and back down the same stroke. The sheet
    draws that stub, so the retracing is the artwork's, not the snapper's;
    RETRACE only spares a turnaround whose two arms stay a block apart. Long
    Beach 182 (72 px of drift -> 24), Metro 169 (180 -> 44) and Torrance 4X
    (80 -> 56) are the same trade, and the rest are the terminus slivers Metro
    33 has: a scar traded for a smaller one path_check charges more for."""
    kd = cKDTree(P)
    base = np.minimum(kd.query(A)[0], gate).sum()
    t, resid = np.zeros(2), base
    span = gate
    for pitch in ANCHOR_PITCH:
        g = np.arange(-span, span + pitch / 2, pitch)
        T = t + np.stack(np.meshgrid(g, g, indexing="ij"), -1).reshape(-1, 2)
        d = kd.query((A[:, None, :] - T[None, :, :]).reshape(-1, 2))[0]
        r = np.minimum(d.reshape(len(A), len(T)), gate).sum(0)
        k = int((r + ANCHOR_DRAG * np.hypot(*T.T) * len(A)).argmin())
        t, resid = T[k], r[k]
        span = pitch
    if np.hypot(*t) >= gate or resid > ANCHOR_GAIN * base:
        return np.zeros(2)
    return t


# The Downtown call-out, as the PDF draws it: a cream panel laid over downtown
# at an angle, carrying a ghosted schematic in place of the network it covers.
# Not one route that crosses downtown is drawn inside it, so it is a hole in
# every mask — the panel is the reason EXCLUDE can't say so, being the one
# region of the sheet that isn't square with the page.
CALLOUT = [(1731, 1774), (1609, 1993), (1731, 2061), (1853, 1841)]


def inside_callout(P):
    """Which points fall inside the Downtown call-out panel."""
    Q = np.asarray(CALLOUT, dtype=float)
    E = np.roll(Q, -1, axis=0) - Q
    D = P[:, None, :] - Q[None, :, :]
    side = E[None, :, 0] * D[:, :, 1] - E[None, :, 1] * D[:, :, 0]
    return (side >= 0).all(1) | (side <= 0).all(1)   # same side of every edge


def maskable(P, region="main"):
    """Which points lie where a mask could have artwork to offer: on the sheet,
    outside the regions the masks deliberately skip. Points under the title
    banner or a call-out box have nothing to snap to no matter how well the
    route is drawn, so they must not count against it."""
    x, y = P[:, 0], P[:, 1]
    if region == "inset":
        x0, y0, x1, y1 = INSET_RECT
        lx0, ly0, lx1, ly1 = INSET_LEGEND
        return ((x >= x0) & (x < x1) & (y >= y0) & (y < y1) &
                ~((x >= lx0) & (x < lx1) & (y >= ly0) & (y < ly1)))
    h, w = map_image()[1].shape
    ok = (x >= 0) & (x < w) & (y >= 0) & (y < h)
    for ex0, ey0, ex1, ey1 in EXCLUDE:
        ok &= ~((x >= ex0) & (x < ex1) & (y >= ey0) & (y < ey1))
    return ok & ~inside_callout(P)


SOLID_R = 2.5      # px; radius a mask pixel must be crowded within
SOLID_MIN = 8      # neighbours inside it before the pixel counts as artwork
_SOLID = {}


def solid_pixels(tree):
    """Which of a mask's pixels sit in real artwork rather than in speckle.

    The muted agency colors sit close to the gray the sheet antialiases its
    text and line casings with, so every mask carries a rim of stray pixels
    around the labels: half of Torrance's is blobs under ten pixels, and the
    median blob across every agency is a single pixel.

    The coarse passes are unbothered — they move the line by a displacement
    smoothed over tens of points, so a stray pixel is outvoted. The final pass
    is not: it sets each point *onto* the mask pixel nearest it, and where
    that pixel is a speck, neighbouring points get pinned to different specks
    and the line arrives jagged. So that last pass only lands on a pixel with
    company. A drawn line is at least a few pixels wide, so its pixels are
    crowded; a speck's are not.

    Only masks read out of the raster speckle; a tree of PDF strokes is the
    drawn line itself, sampled along its length rather than across its width,
    and would fail this test everywhere — see snap_coherent's `speckled`."""
    key = id(tree)
    if key not in _SOLID:
        # DirectionalTree.query wants the points in line order; crowding is a
        # question about the mask alone, so ask its undirected tree.
        base = getattr(tree, "plain", tree)
        n = min(SOLID_MIN, len(base.data))
        _SOLID[key] = base.query(base.data, k=n)[0][:, -1] <= SOLID_R
    return _SOLID[key]


def snap_coherent(pts, tree, caps=None, win=61, anchors=None,
                  anchor_gate=120.0, min_frac=0.5, tail=(10.0, 11), region="main",
                  speckled=True, sole=False):
    """Snap a warped polyline onto a drawn-line mask. The displacement field is
    smoothed along the line so whole stretches move to the same drawn street
    instead of individual points grabbing different parallels. Returns None if
    the line isn't substantially drawn on the map.

    anchors: points known to lie on this route's drawn line (its map badges).
    The global warp's local error can exceed the spacing of parallel drawn
    streets, so first shift the polyline by a displacement field interpolated
    between anchors — `trace_anchors` filling in the stretches between them by
    walking the drawn line — then snap with tighter caps so the corrected line
    can't wander back onto a neighboring route.

    The anchor fit is re-run while it keeps learning something the last pass
    didn't have — a badge it couldn't reach, or a corridor it couldn't walk.
    A badge only counts for a shape it passes within `anchor_gate` of, and
    where the warp is poor the badges that would fix it start out beyond that:
    Metro 690 runs 190 px north of drawn Foothill Blvd in the far valley, out
    of reach of the two badges at the ends of the error. One fit on the badges
    it can see brings the rest within reach, and the second pass lands every
    one of them. Re-fitting is self-limiting in a way that simply widening the
    gate is not — a badge on another branch of the route stays far from the
    corrected line and never joins in.

    The walks need the re-fit for the same reason, and are the half of it the
    badge count cannot see. `trace_anchors` trusts a badge-to-badge walk only
    when it comes out about as long as the shape says that stretch is, and on
    the first pass the shape saying so is the warp. Metro 240 comes down Reseda
    and turns east along Ventura with the warp 94 px north of the corner, so
    the badge printed above the turn is nearest a warp point already past it,
    the arc between that badge and the next reads 219 px against the drawn
    corridor's 299, and the walk round the corner is thrown out as a detour —
    leaving the straight interpolation, which is the chord across it. Both
    badges were in reach the whole time, so a pass counting only badges stops
    there. Once the fit has put the shape on them the same arc reads 234 px,
    the walk is believed, and the corner comes back.

    tail: (cap, window) for one last pass with a short smoothing window. The
    wide window that keeps whole stretches together also averages the
    correction across junctions, leaving the line sagging a line-width or two
    off the artwork wherever parallel routes crowd it (Sunset through West
    Hollywood). The cap is kept below the spacing of neighboring drawn streets
    so this can only refine within the corridor the wide passes chose, never
    hop to the next one.

    Raising that cap is a trap, and drift_check will recommend it. Swept at 14,
    18 and 22, every value cuts total distance from the drawn lines — 18 takes
    it down 6% — because most stretches really are a few px short of their ink
    and a longer reach closes the gap. It also breaks routes outright. The cap
    bounds which points may *contribute* a displacement, not how far the pass
    may carry one: the field is interpolated across the points that contribute
    nothing and then smoothed, so admitting more contributors lets a stretch
    with no ink of its own be dragged by its neighbours. At 18, Metro 169 comes
    off Saticoy and runs 70 px north of it across blank page for a third of the
    Valley — a worse defect than the 24 px diagonal the change was aimed at,
    and invisible in an aggregate that improved. Judge a change like this on
    routes, not on the total.

    speckled: whether the tree came out of the raster, and so needs the final
    landing guarded against stray pixels. A tree of PDF strokes does not.

    sole: the mask holds this one route's drawn line and nothing else, so
    whatever it finds is this route's. Where that holds, the regions the mask
    skips stop being a reason to leave a point where it is. Ordinarily they
    are: a point the sheet drew nothing under has no correction of its own, and
    interpolating one into it carries the line off into blank page and piles it
    against whatever is nearest the far side. But that failure is a point being
    dragged onto a *neighbour's* line, and on a mask of one line there is no
    neighbour to be dragged onto — only its own line, drawn a little way off.
    The Downtown call-out needs it. Its legend is a box printed over the corner
    of a panel that redraws the whole downtown network, and the A Line's warp
    crosses that box running 96 px north of the Washington Blvd it is drawn
    along: with the box vetoing every point under it, the line could not be
    pulled down onto blue that was well inside the coarse pass's reach, and ran
    diagonally across the legend instead. `min_frac` still counts only what the
    mask could cover, so a route the panel doesn't draw still keeps its warp."""
    P = np.array(densify(pts, 4.0), dtype=float)
    n = len(P)
    if n < 8 or tree is None:
        return None
    default_caps = caps is None
    if default_caps:
        caps = (40.0, 26.0, 14.0)
    if anchors:
        A = np.asarray(anchors, dtype=float)
        used = walked = believed = 0
        for _ in range(ANCHOR_PASSES):
            cum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(P, axis=0).T))])
            # (badge, shape point): one per badge in reach, and one per *pass*
            # where the shape runs its drawn corridor twice — see badge_passes
            hit = badge_passes(P, cum, A, anchor_gate)
            ai = np.array([h[0] for h in hit], dtype=int)
            j = np.array([h[1] for h in hit], dtype=int)
            if len(ai) and crossed_badges(A[ai], cum, j):
                # the warp is out by more than the streets are apart; read the
                # badges again against a shape slid onto them
                S = A - anchor_slide(P, A, anchor_gate)
                hit = badge_passes(P, cum, S, anchor_gate)
                ai = np.array([h[0] for h in hit], dtype=int)
                j = np.array([h[1] for h in hit], dtype=int)
            if not len(ai):
                break
            order = np.argsort(cum[j], kind="stable")
            s = cum[j][order]
            D = (A[ai] - P[j])[order]
            s, D, w, b = trace_anchors(s, D, A[ai][order], P, cum, tree)
            # nothing the last fit didn't already have: no badge it couldn't
            # reach, and no corridor it couldn't walk — nor one it could only
            # walk by *aligning*, which is the fallback for a corridor whose
            # length the shape cannot yet vouch for, and which the next pass
            # may well be able to believe outright. Counting the two together
            # made converting one into the other look like standing still.
            # Metro 246's turn off Pacific Coast Hwy onto Figueroa is that
            # case: on the first pass the shape reads 26 px between the two
            # badges against the drawn corridor's 87, three times out of band,
            # so the corner went in as a single aligned node. On the second the
            # same span reads 71 px, the walk is believed and lays three nodes
            # round the corner — but the walk *count* was nine both times, the
            # loop called it no progress, and the better fit was computed and
            # thrown away. The corner stayed 23 px inside the drawn turn.
            if len(ai) <= used and w <= walked and b <= believed:
                break
            used, walked = max(used, len(ai)), max(walked, w)
            believed = max(believed, b)
            P = P + np.c_[np.interp(cum, s, D[:, 0]), np.interp(cum, s, D[:, 1])]
        if used and default_caps:
            caps = (26.0, 14.0)            # anchors pin the street; stay tight
    idx = np.arange(n)
    passes = [(cap, win) for cap in caps] + ([tail] if tail else [])
    for ci, (cap, pwin) in enumerate(passes):
        pwin = min(pwin, max(3, (n // 2) * 2 - 1))
        is_tail = bool(tail) and ci == len(passes) - 1
        d, j = tree.query(P)
        # A point where no mask could hold artwork is not a point that failed to
        # find any: it is one the sheet never drew. Interpolating a correction
        # into it carries the last one the line had off into blank page, and
        # the stretch piles up against whatever is nearest the far side —
        # LADOT 534's downtown end was dragged 110 px across the call-out and
        # crushed onto the 409's Figueroa. Those points keep the warp, and the
        # smoothing below ramps the correction down to them.
        cov = maskable(P, region)
        ok = (d < cap) & (cov | sole)
        if ci == 0:
            # "mostly undrawn" is judged only over the stretch a mask could
            # cover. Metro 690 runs a third of its length under the title
            # banner, which every mask excludes; counting that against it
            # failed the whole route out of snapping and left it on a warp
            # that strays 190 px north of the Foothill Blvd it is drawn on.
            if ok.sum() < max(1, cov.sum()) * min_frac:
                return None                # keep the warp
        if ok.sum() < 4:
            if is_tail:
                break                      # nothing close enough to refine; keep it
            return None
        disp = np.full((n, 2), np.nan)
        disp[ok] = tree.data[j[ok]] - P[ok]
        k = np.ones(pwin) / pwin
        for c in (0, 1):
            col = np.interp(idx, idx[~np.isnan(disp[:, c])], disp[:, c][~np.isnan(disp[:, c])])
            if not sole:
                col[~cov] = 0.0
            disp[:, c] = np.convolve(np.pad(col, pwin // 2, mode="edge"), k, "valid")
        P = P + disp
    d, j = tree.query(P)                   # final tight snap + light smoothing
    ok = (d < 8) & (maskable(P, region) | sole)
    if speckled:
        ok &= solid_pixels(tree)[j]        # onto artwork, never onto a speck
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


def chaikin(pts, iters=2):
    """Round a polyline's corners (Chaikin corner cutting), keeping endpoints.
    Collinear runs stay straight, so only turns get curved."""
    P = np.asarray(pts, dtype=float)
    for _ in range(iters):
        if len(P) < 3:
            break
        a, b = P[:-1], P[1:]
        cut = np.empty((2 * len(a), 2))
        cut[0::2] = 0.75 * a + 0.25 * b
        cut[1::2] = 0.25 * a + 0.75 * b
        P = np.vstack([P[0], cut, P[-1]])
    return P


TAIL_LIMIT = 90.0    # px of drawn track a terminus may sit beyond the warp's end
TAIL_STEP = 3.0      # px; lattice pitch for the walk out
TAIL_REACH = 8.0     # px a lattice cell may sit from drawn pixels
TAIL_MARKER = 13.0   # px; a platform bridges the gap it cuts in its own ribbon
TAIL_BLOCK = 16.0    # px around the stretch already covered, kept off the walk
TAIL_FREE = 30.0     # px of line by the end left unblocked, so the walk can start
TAIL_AHEAD = 0.75    # cosine of the widest cone off the line's heading a
                     # terminus may be found in
TRIM_LIMIT = 60.0    # px an end may overshoot the drawn line and still be cut
                     # back to it, rather than read as artwork that isn't there


def rail_heading(pts, end):
    """Unit vector out of one end of a line, along the way it was running when
    it got there, or None if the line doubles back on itself in TAIL_FREE px."""
    D = np.asarray(pts, dtype=float)
    if end == 0:
        D = D[::-1]
    cum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(D, axis=0).T))])
    out = D[-1] - D[np.searchsorted(cum, cum[-1] - TAIL_FREE)]
    n = np.hypot(*out)
    return out / n if n > 1e-6 else None


def rail_trim(P, tree, end, limit=TRIM_LIMIT):
    """How many points to drop from one end of a snapped rail line that
    overshoots the artwork.

    The snap smooths its displacement along the line and pads that smoothing at
    the ends, so the last points inherit the shift of their neighbours and slide
    on along the line's own heading — past the end of the drawn line, out into
    blank page. The B line ran 24 px beyond North Hollywood that way. Points
    lying on neither ribbon nor platform are cut back to the artwork.

    Only a short overrun, though: where a line crosses the Downtown call-out
    nothing is drawn for 200 px and the warp is all there is to go on, so a long
    run off the ink is the artwork's absence rather than the snap's error."""
    D = P[::-1] if end == 0 else P
    off = tree.query(D)[0] >= TAIL_REACH
    mt = marker_tree()
    if mt is not None:
        off &= mt.query(D)[0] >= TAIL_MARKER
    n = 0
    while n < len(off) - 1 and off[-1 - n]:
        n += 1
    return n if n and np.hypot(*(D[-1] - D[-1 - n])) <= limit else 0


def rail_platform(P, end):
    """Finish one end in the middle of the platform the map draws there.

    A drawn platform interrupts its own ribbon, so a line laid along the ink
    stops at the marker's edge, and one cut back off a blank-page overshoot
    stops at the other — but the middle of the marker is where the map says the
    train stands, so whichever side it finished on, the points inside the
    marker give way to its centre."""
    mt = marker_tree()
    if mt is None:
        return P
    D = P[::-1] if end == 0 else P
    md, mj = mt.query(D[-1])
    c = mt.data[mj]
    if md >= TAIL_MARKER:
        return P
    k = len(D)
    while k > 2 and np.hypot(*(D[k - 1] - c)) < TAIL_MARKER:
        k -= 1
    D = np.vstack([D[:k], c])
    return D[::-1] if end == 0 else D


def centre_on_ink(pts, tree, r=7.0, need=4):
    """Slide each point sideways to the middle of the ribbon beneath it.

    The walk runs on a lattice whose cells only have to be *near* the drawn
    line, so it comes out a few px off centre and wanders across the ribbon.
    Nearest-pixel snapping would pin it to whichever edge is closer; averaging
    the ink either side puts it down the middle instead."""
    P = np.asarray(pts, dtype=float)
    if len(P) < 2:
        return P
    t = np.gradient(P, axis=0)
    t /= np.maximum(np.hypot(*t.T), 1e-9)[:, None]
    for i, (p, n) in enumerate(zip(P, np.c_[-t[:, 1], t[:, 0]])):
        q = tree.data[tree.query_ball_point(p, r)]
        if len(q) >= need:
            P[i] = p + n * float(((q - p) @ n).mean())
    return P


def rail_tail(pts, tree, end, limit=TAIL_LIMIT):
    """The drawn track running on past one end of a snapped rail line, if any.

    Snapping only ever moves a point sideways onto the artwork, so where the
    warp lands a terminus short of where the sheet draws it the line simply
    stops early and the last stretch of track is left bare — the E line gave up
    at East LA Civic Center with Atlantic 70 px further on, and at 17th St/SMC
    with Downtown Santa Monica 50 px behind it. This walks the mask outward
    from the endpoint and returns the piece to append.

    The lattice is the one mask_path() walks on, with two differences. Cells
    within TAIL_BLOCK of the stretch already covered are cut, all but a short
    window by the end itself, so the walk can only head away from the line
    rather than doubling back along it; and a drawn platform counts as track,
    since the white marker interrupts its own ribbon (16 px of it at East LA
    Civic Center) and the walk has to cross that to reach the terminus behind
    it. The farthest inked cell ahead of the endpoint is the target; standing
    the line in the platform there is rail_platform()'s job.

    Two gates keep the walk from inventing track. Only ink inside a narrow cone
    off the heading the line arrived on counts as the line carrying on, so
    track that turns away is not followed — the A line's terminal loop at Long
    Beach, where the warp lands the shape on the wrong side of the block and
    the drawn line runs both ways round it. And a walk longer than `limit` says
    this isn't a terminus at all: the line runs on and the end is a short-turn.
    Either way the answer is empty and the end stays where the warp put it."""
    D = np.array(densify([tuple(p) for p in pts], 2.0), dtype=float)
    if end == 0:
        D = D[::-1]
    a = D[-1]
    cum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(D, axis=0).T))])
    out = rail_heading(pts, end)
    if out is None:
        return np.zeros((0, 2))

    span = limit + 30.0
    lo, hi = a - span, a + span
    nx, ny = (int(np.ceil((hi[k] - lo[k]) / TAIL_STEP)) + 1 for k in (0, 1))
    C = np.stack(np.meshgrid(lo[0] + TAIL_STEP * np.arange(nx),
                             lo[1] + TAIL_STEP * np.arange(ny), indexing="ij"), -1)
    cells = C.reshape(-1, 2)
    ink = tree.query(cells)[0] < TAIL_REACH
    free = ink & ((cells - a) @ out > -TAIL_BLOCK)
    mt = marker_tree()
    if mt is not None:
        free |= mt.query(cells)[0] < TAIL_MARKER
    covered = cum <= cum[-1] - TAIL_FREE
    if covered.any():
        free &= cKDTree(D[covered]).query(cells)[0] > TAIL_BLOCK
    ia = int(np.abs(cells - a).sum(1).argmin())
    free[ia] = True

    idx = np.arange(nx * ny).reshape(nx, ny)
    rows, cols, w = [], [], []
    for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
        s0 = idx[max(0, -dx):nx - max(0, dx), max(0, -dy):ny - max(0, dy)]
        s1 = idx[max(0, dx):nx - max(0, -dx), max(0, dy):ny - max(0, -dy)]
        ok = free.flat[s0] & free.flat[s1]
        rows.append(s0[ok])
        cols.append(s1[ok])
        w.append(np.full(int(ok.sum()), TAIL_STEP * math.hypot(dx, dy)))
    G = sparse.coo_matrix((np.concatenate(w),
                           (np.concatenate(rows), np.concatenate(cols))),
                          shape=(nx * ny, nx * ny))
    dist, pred = dijkstra(G + G.T, indices=ia, return_predecessors=True)

    reach = np.hypot(*(cells - a).T)
    ahead = np.isfinite(dist) & ink & ((cells - a) @ out > TAIL_AHEAD * reach)
    if not ahead.any():
        return np.zeros((0, 2))
    ib = int(np.argmax(np.where(ahead, reach, -1.0)))
    if dist[ib] > limit:
        return np.zeros((0, 2))
    walk = [ib]
    while walk[-1] != ia:
        walk.append(pred[walk[-1]])
    return centre_on_ink(cells[walk[-2::-1]], tree)


def snap_rail(pts, tree, caps=(45.0, 24.0), wins=(15, 9), max_gap=45, rnd=2):
    """Snap a warped rail polyline onto its drawn-track mask, coherently.

    Each pass pins points within `cap` of the track to it, interpolates that
    displacement across shorter unsnapped runs, and smooths it along the line,
    so a stretch where the schematic warp drifts off the drawn track is pulled
    back as a whole rather than a few points snapping and the rest sagging.
    Snapping each point to its own nearest track pixel (the previous approach)
    left a westward hook on the B line by Universal City; the passes tighten
    the cap so the second pass reels in what the first left bulging. Runs
    longer than `max_gap` densified points keep the raw warp instead — the
    ghosted downtown call-out has no track to snap onto. Corners are rounded
    with Chaikin so turns read as curves, not right angles."""
    P = np.array(densify(pts, 4.0), dtype=float)
    n = len(P)
    idx = np.arange(n)
    for pass_i, (cap, win) in enumerate(zip(caps, wins)):
        dist, j = tree.query(P)
        ok = dist < cap
        if ok.sum() < 4:
            if pass_i == 0:
                return [tuple(p) for p in P]    # nothing drawn here: keep the warp
            break
        disp = np.full((n, 2), np.nan)
        disp[ok] = tree.data[j[ok]] - P[ok]
        i = 0                                    # keep the warp through long gaps
        while i < n:
            if not ok[i]:
                k = i
                while k < n and not ok[k]:
                    k += 1
                if k - i > max_gap:
                    disp[i:k] = 0.0
                i = k
            else:
                i += 1
        known = ~np.isnan(disp[:, 0])
        kern = np.ones(win) / win
        for c in (0, 1):
            col = np.interp(idx, idx[known], disp[known, c])
            disp[:, c] = np.convolve(np.pad(col, win // 2, mode="edge"), kern, "valid")
        P = P + disp
    dist, j = tree.query(P)                      # final tight re-snap on the track
    close = dist < 8
    P[close] = tree.data[j[close]]
    return [tuple(p) for p in chaikin(simplify(square_ends(P, tree), 1.0), rnd)]


def resample(P, n):
    """`P` redrawn as `n` points evenly spaced along its own length."""
    P = np.asarray(P, dtype=float)
    cum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(P, axis=0).T))])
    if len(P) < 2 or cum[-1] <= 0:
        return P
    t = np.linspace(0, cum[-1], n)
    return np.c_[np.interp(t, cum, P[:, 0]), np.interp(t, cum, P[:, 1])]


def square_ends(P, tree):
    """Line both ends of a snapped line up with where the map stops drawing it:
    cut back an overshoot, run out to a terminus the warp fell short of, then
    stand the end in the middle of the platform it finished at.

    Snapping only moves a point sideways, and it pads its smoothing at the ends,
    so an end finishes wherever the warp left it along the line rather than
    where the artwork stops — which is a different place whenever the warp's
    error runs along the line instead of across it."""
    lo, hi = (rail_trim(P, tree, e) for e in (0, -1))
    if lo + hi + 4 < len(P):
        P = P[lo:len(P) - hi]
    head, tail = (rail_tail(P, tree, e) for e in (0, -1))
    P = np.vstack([head[::-1], P, tail])
    for e in (0, -1):
        P = rail_platform(P, e)
    return P


def simplify(pts, tol=1.2, mask=False):
    """Douglas-Peucker. With mask=True also returns which points survived, so
    a caller can carry a parameterization of the original through it."""
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
    return (pts[keep], keep) if mask else pts[keep]


def project_stops(shape_px, cum, stop_px):
    """Distance along shape for each stop: minimum-cost monotone assignment.

    Projects every stop onto every shape segment, then a DP picks the
    non-decreasing (by segment) sequence minimizing total squared stop-to-shape
    distance. A greedy nearest-point ratchet fails on loops and overlapping
    out/back legs: one stop matching the wrong leg jams all later stops at the
    end of the shape, freezing the whole pattern.
    """
    P = np.asarray(shape_px, dtype=float)
    S = len(stop_px)
    if len(P) < 2:
        return [0.0] * S
    A, Bv = P[:-1], P[1:] - P[:-1]
    L2 = (Bv ** 2).sum(1)
    L2[L2 == 0] = 1e-9
    stops = np.asarray(stop_px, dtype=float)
    rel = stops[:, None, :] - A[None, :, :]              # S x N x 2
    t = np.clip((rel * Bv).sum(2) / L2, 0.0, 1.0)
    proj = A + Bv * t[..., None]
    d2 = ((proj - stops[:, None, :]) ** 2).sum(2)        # S x N
    along = cum[:-1] + t * np.sqrt(L2)
    N = d2.shape[1]
    idx = np.arange(N)
    pmarg = np.empty((S, N), dtype=np.int32)             # backtrack pointers
    best = d2[0]
    for i in range(1, S):
        m = np.minimum.accumulate(best)
        arg = np.where(best <= m, idx, 0)
        np.maximum.accumulate(arg, out=arg)              # argmin of best[:k+1]
        pmarg[i] = arg
        best = d2[i] + m
    ks = np.empty(S, dtype=np.int64)
    ks[-1] = int(np.argmin(best))
    for i in range(S - 1, 0, -1):
        ks[i - 1] = pmarg[i][ks[i]]
    dists = along[np.arange(S), ks]
    np.maximum.accumulate(dists, out=dists)              # order ties within a segment
    return [float(v) for v in dists]


def project_onto(P, cum, pts):
    """(distance along P, distance off it) of each point's foot on P."""
    Q = np.asarray(pts, dtype=float)
    A, Bv = P[:-1], P[1:] - P[:-1]
    L2 = (Bv ** 2).sum(1)
    L2[L2 == 0] = 1e-9
    rel = Q[:, None, :] - A[None, :, :]
    t = np.clip((rel * Bv).sum(2) / L2, 0.0, 1.0)
    d2 = (((A + Bv * t[..., None]) - Q[:, None, :]) ** 2).sum(2)
    k = d2.argmin(1)
    i = np.arange(len(Q))
    return (cum[:-1] + t * np.sqrt(L2))[i, k], np.sqrt(d2[i, k])


PLATFORM_NEAR = 14.0    # px a drawn platform may sit off the line it serves
PLATFORM_GATE = 120.0   # px along the line a stop may be moved to reach its own


def platform_stops(shape_px, cum, stop_px, gate=PLATFORM_GATE):
    """Move each rail stop onto the platform the map draws for it.

    Rail platforms are drawn: the white shape with the black stroke, a circle
    alone or conjoined where lines meet. A stop projected from its warped
    position lands beside one rather than on it, so the train eases to a halt
    short of the platform or past it.

    Matching each stop to its nearest marker independently isn't enough,
    because near the sheet's schematic corners the warp lags the artwork by
    more than the stops are apart: at the E line's east end it left Maravilla
    and East LA Civic Center sharing one platform and put Atlantic on East LA
    Civic Center's, so a train reached the map's terminus a station early. The
    platforms along a line *are* its stop sequence, in order, so the two are
    aligned as sequences instead — a monotone, one-to-one match, which pins
    each stop to its own platform however far the warp has slid. Stops whose
    platform the sheet doesn't draw fall in the gaps and keep the warp; that is
    most of downtown, where the call-out panel covers the markers.
    """
    P = np.asarray(shape_px, dtype=float)
    M = station_markers()
    if len(P) < 2 or not len(M):
        return stop_px
    ms, moff = project_onto(P, cum, M[:, :2])
    on = np.nonzero(moff < PLATFORM_NEAR)[0]
    if not len(on):
        return stop_px
    on = on[np.argsort(ms[on])]
    s, plat = ms[on], M[on, :2]
    u = project_onto(P, cum, stop_px)[0]
    S, K = len(u), len(s)

    # Needleman-Wunsch: match, leave the stop on the warp, or leave the
    # platform to another line. Leaving a platform out is free — a shape may
    # run past platforms its pattern doesn't call at — while a stop that finds
    # no platform costs the gate, so any match inside the gate beats skipping.
    cost = np.abs(u[:, None] - s[None, :])
    cost[cost >= gate] = np.inf
    cost **= 2
    F = np.zeros((S + 1, K + 1))
    src = np.zeros((S + 1, K + 1), dtype=np.int32)   # which g the row min came from
    hit = np.zeros((S + 1, K + 1), dtype=bool)       # ...and whether it was a match
    ks = np.arange(K + 1)
    for i in range(1, S + 1):
        g = np.empty(K + 1)
        g[0] = F[i - 1, 0] + gate ** 2
        match, skip = F[i - 1, :K] + cost[i - 1], F[i - 1, 1:] + gate ** 2
        g[1:] = np.minimum(match, skip)
        hit[i, 1:] = match <= skip
        F[i] = np.minimum.accumulate(g)
        src[i] = np.maximum.accumulate(np.where(g <= F[i], ks, 0))

    out = list(stop_px)
    i, k = S, K
    while i > 0:
        kk = int(src[i, k])
        if kk and hit[i, kk]:
            out[i - 1] = tuple(plat[kk - 1])
            k = kk - 1
        else:
            k = kk
        i -= 1
    return out


INSET_SLACK = 25.0   # inset px a shape may stray outside the frame and still
                     # count as inside — ~4x the inset fit's median residual


def outside_inset(ix, iy, ll):
    """How far each point falls outside what the inset depicts, in inset px:
    past the frame rect, or past the geographic bounds converted at the
    inset's own scale."""
    x0, y0, x1, y1 = INSET_RECT
    sx = (x1 - x0) / (INSET_GEO[2] - INSET_GEO[0])
    sy = (y1 - y0) / (INSET_GEO[3] - INSET_GEO[1])
    return np.maximum.reduce([
        x0 - ix, ix - x1, y0 - iy, iy - y1,
        (INSET_GEO[0] - ll[:, 0]) * sx, (ll[:, 0] - INSET_GEO[2]) * sx,
        (INSET_GEO[1] - ll[:, 1]) * sy, (ll[:, 1] - INSET_GEO[3]) * sy,
        np.zeros(len(ix))])


# The cap ladder and smoothing window the call-out snaps on. A mask of one
# route's colour can reach as far there as rail's does on the main map, and
# needs to: the panel magnifies downtown about fourfold, so the same warp error
# is four times the pixels, and the A Line comes into the frame's south-east
# corner 96 px off its drawn Washington Blvd. A mask that holds every Metro bus
# line in the panel at once gets the short reach it always had, since a longer
# one would only find a neighbour sooner. Both take the shorter window — it is
# the magnified grid's right-angle turns that want it, not the livery.
INSET_CAPS = (60.0, 30.0, 14.0)
INSET_SOLE_CAPS = (120.0, 60.0, 30.0, 14.0)
INSET_WIN = 15


def inset_runs(ll, main_dist, snap_tree=None, anchors=None, sole=False):
    """Portions of a shape inside the DTLA inset, as runs of inset-px
    polyline. Motion in the inset is computed natively in inset space (the
    schematic main map collapses downtown, so main-shape distance cannot
    parameterize it): each run carries its own cumulative distance, and
    stops are later projected onto it. d0/d1 (distance range on the main
    shape) only route each stop to the right run, and come from `main_dist`
    — the same measure the stops themselves are placed by, so the two agree
    however far the snap moved the shape out from under the warp."""
    ll = np.asarray(ll, dtype=float)
    if TR_INSET is None or len(ll) < 2:
        return None
    ix, iy = to_inset_px(ll[:, 0], ll[:, 1])
    x0, y0, x1, y1 = INSET_RECT
    inside = ((ix > x0) & (ix < x1) & (iy > y0) & (iy < y1) &
              (ll[:, 0] > INSET_GEO[0]) & (ll[:, 0] < INSET_GEO[2]) &
              (ll[:, 1] > INSET_GEO[1]) & (ll[:, 1] < INSET_GEO[3]))
    if not inside.any():
        return None
    # Both tests above read the raw warp, which is only good to a few pixels,
    # so a stretch drawn well inside the frame can measure as outside it and
    # split the shape into two runs. That costs the vehicle its inset sprite
    # entirely: the renderer wants the stops either side of it to agree on a
    # run, and when they disagree it draws nothing — the A line disappeared
    # between Union Station and Little Tokyo, where the warp grazes 7 px past
    # the frame's right edge. So fill in gaps the route never really left
    # through. A genuine exit from downtown misses by hundreds of px, not tens,
    # and still splits the runs, which is what should happen.
    slack = outside_inset(ix, iy, ll)
    known = np.nonzero(inside)[0]
    i = known[0]
    while i < known[-1]:
        if inside[i]:
            i += 1
            continue
        j = i
        while not inside[j]:
            j += 1
        if slack[i:j].max() <= INSET_SLACK:
            inside[i:j] = True
        i = j
    spans, i, n = [], 0, len(ll)
    while i < n:
        if not inside[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and inside[j + 1]:
            j += 1
        a, b = max(0, i - 1), min(n - 1, j + 1)   # one point past each edge
        spans.append((a, b))
        i = j + 1
    out = []
    for a, b in spans:
        if b - a < 1:
            continue
        mx, my = to_px(ll[a:b+1, 0], ll[a:b+1, 1])
        d = main_dist(list(zip(mx, my)))
        pts = np.c_[ix[a:b+1], iy[a:b+1]]
        if snap_tree is not None:
            # same coherent snap + badge anchors as the main map, but scaled
            # for the magnified inset: a short smoothing window keeps the
            # grid's right-angle turns square, and a tight anchor gate stops
            # chips on the other street of a one-way couplet from matching
            sc = snap_coherent([tuple(p) for p in pts], snap_tree,
                               caps=INSET_SOLE_CAPS if sole else INSET_CAPS,
                               win=INSET_WIN, anchors=anchors, anchor_gate=75.0,
                               min_frac=0.35, region="inset", sole=sole)
            if sc is not None:
                pts = np.asarray(sc)
        # drop edge-hugging slivers that never meaningfully enter the frame
        vis = ((pts[:, 0] > x0 + 8) & (pts[:, 0] < x1 - 8) &
               (pts[:, 1] > y0 + 8) & (pts[:, 1] < y1 - 8))
        if not vis.any():
            continue
        icum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))])
        if icum[-1] < 10:
            continue
        out.append({"pts": pts, "icum": icum, "d0": d[0], "d1": d[-1]})
    return out or None


def inset_stop_map(runs, stop_d, stop_ipx):
    """Per stop: (run index or -1, distance along that run's inset polyline).
    A stop belongs to the run whose main-shape distance range contains its
    pattern distance; its inset position is then projected onto that run."""
    ir = [-1] * len(stop_d)
    idist = [0.0] * len(stop_d)
    x0, y0, x1, y1 = INSET_RECT
    for r, run in enumerate(runs):
        members = [k for k, sd in enumerate(stop_d)
                   if run["d0"] - 5 <= sd <= run["d1"] + 5 and ir[k] < 0
                   and x0 - 40 < stop_ipx[k][0] < x1 + 40
                   and y0 - 40 < stop_ipx[k][1] < y1 + 40]
        if not members:
            continue
        proj = project_stops(run["pts"], run["icum"], [stop_ipx[k] for k in members])
        for k, v in zip(members, proj):
            ir[k] = r
            idist[k] = v
    return ir, idist


def settle(full, base, anc, line_ink):
    """The best of the cleanup candidates for a snapped shape.

    Straightening one spike can leave a sharper residual where it met a bend,
    and simplify() can turn a helped dense path into a worse stored one, so
    neither cleanup is taken on faith. Every candidate is scored on the *stored*
    geometry the animation actually plays — by the very measure path_check ranks
    on — and the best of them wins, the snapper's own shape taking ties, so no
    shape comes out worse than it went in. Taking a fold out is what leaves the
    residual despike files off, so the two together usually win; but not always,
    and a pass run unconditionally ahead of the other can rob it of a better
    answer, so each stands on the ballot alone as well.

    The ballot is scored on two measures, not one. `spike_penalty` charges only
    turning that doubles back inside 12 px, and the snapper's 61-point smoothing
    turns an occluded stretch into a smooth bulge with no sharp turn anywhere in
    it: Foothill 493 scores a flat 0 while visibly off its line. Scored on that
    alone, `undetour` can never win a shape it is the only fix for — it ties,
    and the tie goes to the snapper. So the excursion is priced too, and the
    winner minimises both.

    The old promise still holds, and is explicit: a candidate that would rank
    worse on `spike_penalty` than the snapper's own shape is thrown out before
    it can be scored, so nothing buys a straighter line at the cost of a
    hairpin."""
    as_snapped = full
    spike0 = stored_penalty(as_snapped)
    best = spike0 + DETOUR_WEIGHT * detour_penalty(as_snapped, base, anc, line_ink)
    unfolded = unfold(as_snapped, base, anc)
    undet = undetour(as_snapped, base, anc, line_ink)
    for cand in (despike(as_snapped), unfolded, despike(unfolded),
                 undet, despike(undet), unfold(undet, base, anc)):
        if np.array_equal(cand, as_snapped):
            continue
        spike = stored_penalty(cand)
        if spike > spike0:
            continue
        penalty = spike + DETOUR_WEIGHT * detour_penalty(cand, base, anc, line_ink)
        if penalty < best:
            full, best = cand, penalty
    return full


def main():
    rail_trees = load_masks()

    routes, route_idx = [], {}      # route_idx[(feed, route_id)]
    systems, system_idx = [], {}    # per-feed display names, routes point in
    shapes_raw = {}                 # (feed, shape_id) -> [(x,y)...] px
    shape_ll = {}                   # shape key -> [(lon,lat)...] original
    shape_isnap = {}                # (feed, shape_id) -> (colors, tol, tokens)
    stops_name = {}                 # (feed, stop_id) -> printed name
    route_stops = {}                # (feed, route_id) -> {stop_id}
    shape_param = {}                # (feed, shape_id) -> pre-snap polyline,
                                    # its cumulative dist, and that sampled at
                                    # the points simplify() kept
    trips_out = []
    patterns, pattern_idx = [], {}  # key (feed, shape_id, stop_seq)
    stops_px = {}                   # (feed, stop_id) -> (x, y)
    stops_ll = {}                   # (feed, stop_id) -> (lon, lat)
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
                stops_name[(feed, r["stop_id"])] = r.get("stop_name", "")
                stops_ll[(feed, r["stop_id"])] = (float(r["stop_lon"]), float(r["stop_lat"]))

        rmeta, badge_tokens, sheet_tokens, is_dash = {}, {}, {}, {}
        for row in read_csv(feed, "routes.txt"):
            label = MAP_LABELS.get((feed, row["route_id"])) or route_label(
                row.get("route_short_name", ""), row.get("route_long_name", ""))
            extra = set()          # designations the sheet prints that the
                                   # feed's short name doesn't carry — see 910
            color = (row.get("route_color") or "").strip()
            text = (row.get("route_text_color") or "").strip()
            is_dash[row["route_id"]] = "DASH" in (row.get("route_long_name") or "")
            if not color:
                color, text = (METRO_BUS_COLOR, METRO_BUS_TEXT) if is_metro else (FALLBACK_COLOR, FALLBACK_TEXT)
            if color == "000000" and is_metro:
                # GTFS says black; the map draws 720/754/761 in its Rapid-red
                # ribbon, so the sprite takes that ribbon's own ink — the same
                # red these shapes now snap onto (RAPID_RED_INK).
                color = "%02X%02X%02X" % tuple(round(v * 255) for v in RAPID_RED_INK[0])
            if row["route_id"].split("-")[0] in ("910", "950"):
                color, text = BUSWAY_GRAY, "FFFFFF"   # J Line rides the gray busway
                # And it is badged by number, not by letter. Metro's
                # route_short_name is "Metro J Line (Silver) 910/950", whose
                # first token is short enough that route_label takes it and
                # stops — so every one of these vehicles carried a "J" that
                # appears nowhere on the sheet: badge_words() finds the token
                # zero times on the main map and zero times in the call-out,
                # against gray "910" over "950" chips at four places along the
                # transitway and a "950" on its own where only that working
                # runs, down Pacific Ave in San Pedro. This is the case
                # route_label's own docstring describes and cannot reach here,
                # the designation being four characters already.
                #
                # "910" of the pair, for the same reason it returns "14" from
                # "14/37": the first part the sheet prints. It is also the
                # working most of these trips are — 211 of 291 turn back at
                # Harbor Gateway — though the choice does cost the San Pedro
                # trips a badge, since down there the sheet prints only "950".
                label = "910"
                # It costs them the *sprite*, at least. As an anchor the sheet's
                # other number is wanted whatever the vehicles are labelled, and
                # nothing else here can supply it: Metro leaves
                # route_short_name empty on the busways and writes the numbers
                # into the long name, so a shape's tokens would be its label
                # and nothing besides. The "950" at the 22nd St loop is the one
                # thing on the sheet that says where this line goes once the
                # freeway ends — see the busway snap below.
                extra = {"950"}
            if row["route_id"].split("-")[0] == "901":
                color, text = "%02X%02X%02X" % BUSWAY_ORANGE, "FFFFFF"   # G Line's own
            rail = row.get("route_type") in ("0", "1", "2")
            rmeta[row["route_id"]] = (label, color, text or "FFFFFF", rail)
            # tokens as printed on map badges, for anchor lookup
            short = (row.get("route_short_name") or "").strip()
            badge_tokens[row["route_id"]] = (
                set(short.replace("/", " ").split()) | {label} | extra)
            # ...and, apart, the ones read off the artwork rather than out of
            # the feed. A DASH is anchored on those alone; see ladot_livery.
            sheet_tokens[row["route_id"]] = (
                {MAP_LABELS[(feed, row["route_id"])]}
                if (feed, row["route_id"]) in MAP_LABELS else set())

        trip_info = {}
        for row in trip_rows:
            if row["service_id"] not in active:
                continue
            sid = row.get("shape_id", "")
            if feed == "metrolink":
                sid = METROLINK_SHAPES.get((row["route_id"], row.get("direction_id", "")), sid)
            trip_info[row["trip_id"]] = (row["route_id"], sid)

        stop_times = defaultdict(list)
        for ti, seq, at, dt, sid_ in read_cols(
                feed, "stop_times.txt",
                ("trip_id", "stop_sequence", "arrival_time", "departure_time", "stop_id")):
            if ti in trip_info and at.strip():
                # keep both times: a stop is a dwell [arrival, departure], and at
                # the origin that dwell is a layover we must not draw (see below)
                stop_times[ti].append((int(seq), parse_time(at),
                                       parse_time(dt) if dt.strip() else parse_time(at),
                                       sid_))

        n_before = len(trips_out)
        used_shapes = set()
        for ti, sts in stop_times.items():
            if len(sts) < 2:
                continue
            rid, sid = trip_info[ti]
            sts.sort()
            route_stops.setdefault((feed, rid), set()).update(s for _, _, _, s in sts)
            # A bus laying over at its origin before it enters service is not yet
            # a vehicle anyone can ride, and drawing it parked there for the
            # length of the layover is what pooled Foothill's buses at Pomona,
            # Montclair and El Monte: those terminals time the first stop with an
            # arrival_time a median 15 minutes — up to two hours — before its
            # departure_time, and timing the trip from the arrival left the bus
            # sitting on the terminal until it pulled out. The trip starts when it
            # departs, so the origin is timed by its departure; every later stop
            # keeps its arrival (arrival and departure are equal there anyway, so
            # this changes nothing downstream). Clamped so a malformed feed whose
            # departure trails the next arrival can't make the clock run backward.
            times = [t for _, t, _, _ in sts]
            if len(times) > 1:
                times[0] = min(sts[0][2], times[1])
            stop_seq = tuple(s for _, _, _, s in sts)
            rkey = (feed, rid)
            if rkey not in route_idx:
                label, color, text, rail = rmeta[rid]
                if feed not in system_idx:
                    system_idx[feed] = len(systems)
                    systems.append(FEED_NAMES.get(feed, feed))
                route_idx[rkey] = len(routes)
                routes.append({"n": label, "c": "#" + color, "t": "#" + text,
                               "rail": rail, "sy": system_idx[feed]})
            pkey = (feed, sid, stop_seq)
            if pkey not in pattern_idx:
                pattern_idx[pkey] = len(patterns)
                patterns.append(pkey)
            trips_out.append((route_idx[rkey], pkey, times))
            used_shapes.add(sid)

        # load shapes used by this feed
        tmp = defaultdict(list)
        for sid_, seq, lon, lat in read_cols(
                feed, "shapes.txt",
                ("shape_id", "shape_pt_sequence", "shape_pt_lon", "shape_pt_lat")):
            if sid_ in used_shapes:
                tmp[sid_].append((int(seq), float(lon), float(lat)))
        route_by_shape = {row.get("shape_id", ""): row["route_id"] for row in trip_rows}
        if feed == "metrolink":
            # trips.txt leaves shape_id empty here, so the line above keys every
            # Metrolink route under "" and no shape can find the route it
            # belongs to. METROLINK_SHAPES already paired the two; trip_info
            # carries that pairing.
            route_by_shape = {s: r for r, s in trip_info.values()}
        warped = {}
        for sid, p in tmp.items():
            p.sort()
            x, y = to_px(np.array([q[1] for q in p]), np.array([q[2] for q in p]))
            warped[sid] = list(zip(x, y))

        # the shapes each route runs, and their warps as trees, so a badge can
        # be handed to the variant that actually passes it (branch_anchors)
        route_sids = defaultdict(list)
        for sid in warped:
            route_sids[route_by_shape.get(sid)].append(sid)
        # Where the sheet designates a whole agency rather than each route, one
        # symbol covers several routes and branch_anchors has to range over all
        # of their shapes, not just one route's variants: every "BC" on the
        # sheet is a candidate anchor for both Beach Cities routes at once, and
        # the one printed on the 102's leg across Redondo Beach was pulling the
        # 109 15 px off the PCH it runs on.
        label_sids = defaultdict(list)
        for sid in warped:
            r = route_by_shape.get(sid)
            label_sids[rmeta[r][0] if r in rmeta else r].append(sid)
        _kd = {}

        def kd_for(sid):
            if sid not in _kd:
                _kd[sid] = cKDTree(np.asarray(densify(warped[sid], 4.0), dtype=float))
            return _kd[sid]

        # snap shapes onto the drawn lines of this system where they exist
        agency_tree, sprite_cols = None, None
        if feed in LEGEND_SEEDS and warped:
            seeds = LEGEND_SEEDS[feed]
            # The seed refined against the artwork the routes lie over, and —
            # where there isn't enough of that to refine from — the same
            # reading taken on the sheet's own strokes instead. Without the
            # fallback an agency the refinement can't reach has no mask at all
            # and every one of its shapes keeps the warp, which is what left
            # BurbankBus's two workings off their drawn teal.
            refined = [refine_color(list(warped.values()), s) for s in seeds]
            good = [c for c in refined if c] or [c for c in [stroke_color(feed)] if c]
            if good:
                agency_tree = mask_tree(good, 30.0)
            # Sprites take the color the sheet actually prints: the PDF's ink
            # where the feed's lines are read from it, and otherwise the tiles,
            # falling back to map.png's washed-out reading then the legend
            # swatch.
            sprite_cols = ([INK_SPRITES[feed]] if feed in INK_SPRITES else
                           [drawn_color(list(warped.values()), s) or c or tuple(s)
                            for c, s in zip(refined, seeds)])
            print(f"  {feed} drawn color(s): {good}")
        elif feed in DRAWN_COLORS:
            sprite_cols = [DRAWN_COLORS[feed]]

        # recolor this agency's sprites to match the line color the map draws
        # (GTFS route_color is the agency's own branding, not the map's)
        if sprite_cols:
            for (f, rid), ridx in route_idx.items():
                if f != feed:
                    continue
                routes[ridx]["c"] = "#%02X%02X%02X" % tuple(sprite_cols[0])
                routes[ridx]["t"] = "#FFFFFF"

        snapped = anchored = 0
        for sid, pts in warped.items():
            out_pts, anc, can_refit = None, [], False
            # The drawing this shape was snapped on: the PDF's strokes where it
            # has them, its agency's colour mask where it does not. It is the
            # arbiter of whether a detour is really the drawn line — see
            # _ink_vouches, which reads it far enough out along the run that a
            # mask's label-shaped holes cannot answer for the whole of it.
            line_ink = None
            rid = route_by_shape.get(sid)
            toks = badge_tokens.get(rid, set())
            pins = PINNED_ANCHORS.get((feed, (rid or "").split("-")[0]), [])
            cuts = pins + TRIM_TERMINI.get((feed, (rid or "").split("-")[0]), [])
            if cuts:
                pts = trim_terminus(pts, cuts)   # end at the drawn hub, not past it
            if feed == "gtfs_rail":
                tree = rail_trees.get(rid)
                if tree is not None:
                    out_pts = snap_rail(pts, tree)
                if rid in ROUTE_COLORS:
                    shape_isnap[(feed, sid)] = ([ROUTE_COLORS[rid]], TOL, toks)
            elif feed == "gtfs_bus":
                rid0 = (rid or "").split("-")[0]
                # Both ribbons the sheet draws in an ink of their own, and both
                # snapped the same way: on that ink, pinned by the station names
                # printed beside it. The J Line used to be the one Metro bus
                # route that snapped onto nothing at all — its drawn gray is a
                # rounding from the freeway's in map.png, so a color mask would
                # have taken every freeway on the sheet with it — and what stood
                # instead was the raw warp. Out at the end of the line that is a
                # block and a half: through San Pedro the 950 ran down Figueroa
                # and Anaheim instead of the Harbor Freeway beside them, and its
                # south end sat 70 px past the 22nd St loop the sheet draws,
                # down by Shepard St. The PDF has no such collision — see
                # JLINE_INK — so there was never anything to snap it onto except
                # the wrong thing to read it from.
                busway = rid0 in ("901", "910")
                if rid0 in ("720", "754", "761"):
                    cols = [RAPID_RED]
                elif rid0 == "910":
                    cols = [JLINE_GRAY]
                else:
                    cols = [BUSWAY_ORANGE] if busway else [ORANGE]
                if cols is not None:
                    # Snap on the strokes, anchor on the pixels. The badges are
                    # chips filled with the line color, not strokes of it, so
                    # they stand on the mask and not on the ink.
                    ink = ink_tree(JLINE_INK if rid0 == "910" else
                                   BUSWAY_INK if busway else
                                   RAPID_RED_INK if cols == [RAPID_RED] else ORANGE_INK)
                    # The busway is pinned by station names printed beside the
                    # ribbon, not by badges standing on it — the sheet gives the
                    # G Line no badge at all — so it has no use for a mask, and
                    # every use for being rid of one. Its ribbon reads as a hotter
                    # orange than the streets only on the sheet itself; map.png's
                    # downscale mixes it with the page until it sits 24 from
                    # ordinary Metro orange, so the mask was read off the tiles
                    # instead, where the two stay 57 apart. That kept whole
                    # parallel streets out but not the orange badge chips beside
                    # the line, whose fringe pixels blend to within tolerance of
                    # the ribbon: single stray mask points under the "154" and
                    # "237" chips bent the line 40 px north of Burbank Blvd
                    # between Valley College and Laurel Canyon, and the chips
                    # around De Soto pulled a Canoga working diagonally across
                    # three blocks of blank page. The ink has one stroke per
                    # drawn line and no chips at all.
                    tree = (ink or tile_tree(cols, BUSWAY_TOL)) if busway else mask_tree(cols)
                    snap_tree = ink or tree
                    line_ink = ink
                    # no color gate: Metro's orange badges render with variable
                    # fade (crisp ~1 px from orange, faded ~70), overlapping
                    # muted foreign badge colors, so a color test drops genuine
                    # ones. Metro's number badges are dense and its anchoring
                    # was already tuned without it.
                    if busway:
                        # Station names, and — where the sheet numbers the
                        # ribbon — the numbers printed along it. The names run
                        # out where the stations do: the J Line's last one is
                        # Harbor Gateway, and the 700 px the 950 runs on past
                        # it into San Pedro had nothing pinning it at all. That
                        # is the stretch where the sheet stops going straight.
                        # It brings the busway off the 110 where the freeway
                        # ends, west along Channel, back east along Ocean and
                        # south down Pacific to the 22nd St loop — a switchback
                        # a snap cannot invent, since every point of the chord
                        # across it is already sitting on some part of it, and
                        # the warp drew exactly that chord. Only a walk between
                        # two anchors recovers a corridor (`trace_anchors`),
                        # and there were no two anchors to walk between. The
                        # sheet prints a "950" at the loop and another where
                        # the 950 leaves the 910 at Harbor Gateway; those two
                        # bracket it.
                        #
                        # Numbers only. The G Line's designation is a letter,
                        # and the sole "G" the sheet sets is the one in "See G
                        # Line detour inset" — a caption, but one standing 4 px
                        # from the ghosted ribbon it captions, which is inside
                        # any reach a badge test can use.
                        anc = (station_anchors(
                                   route_stops.get((feed, rid), ()), tree,
                                   {s: stops_name.get((feed, s), "")
                                    for s in route_stops.get((feed, rid), ())},
                                   {s: stops_px.get((feed, s))
                                    for s in route_stops.get((feed, rid), ())})
                               + route_anchors({t for t in toks if any(c.isdigit()
                                                                      for c in t)},
                                               tree, near=BADGE_NEAR_INK))
                    else:
                        anc = route_anchors(toks, tree) + pins
                    # A badge is printed on one line, and the "950" at the loop
                    # is on the one variant that gets there — the workings that
                    # turn back at Harbor Gateway must not be dragged 640 px
                    # south onto it.
                    anc = branch_anchors(anc, sid, route_sids[rid], kd_for)
                    anchored += bool(anc)
                    can_refit = not busway
                    out_pts = snap_recording(pts, snap_tree, anchors=anc,
                                            caps=(BUSWAY_CAPS if busway else
                                                  INK_CAPS if ink else None),
                                            win=BUSWAY_WIN if busway else 61,
                                            speckled=ink is None)
                    # The busway is drawn the way a rail line is — its own
                    # ribbon, ending at a platform the sheet draws — so its ends
                    # are squared against that ribbon like a rail line's. They
                    # need it for the same reason and worse: the warp is a
                    # median 50 px out through the Valley, and out there the
                    # error runs *along* the busway as much as across it, which
                    # a sideways snap cannot answer for. Every end landed
                    # somewhere other than the terminus it should be at — 47 px
                    # short of North Hollywood on the Canoga workings, and,
                    # before the ink, 50 px past it and away down the B line's
                    # red toward Universal City.
                    #
                    # Squaring trims and extends, so the result is resampled
                    # back to the point count it came in with. That keeps it
                    # index-aligned with the warp, which is what carries the
                    # stops over (see below) — and with both lines now running
                    # platform to platform, the two ends pin the parameterization
                    # and leave the stations between them a median 10 px from
                    # their drawn platforms, down from 42. Handing those stops
                    # to platform_stops instead, as rail does, was worse rather
                    # than better: the warp lags by half the distance between
                    # stations here, so the alignment that minimizes total
                    # offset is the one that puts every station on the platform
                    # *before* its own, and that is the one it found — 14 stops
                    # each one station early.
                    if busway and out_pts is not None:
                        out_pts = resample(
                            square_ends(np.asarray(out_pts, dtype=float), snap_tree),
                            len(out_pts))
                    # Not the J Line: the call-out redraws every Metro bus line
                    # in orange (see runs_for), and this one is not a Metro bus
                    # line down there — it is the gray transitway, drawn in the
                    # panel the same as it is drawn outside it. Registering it
                    # here would snap its downtown run onto the mask of every
                    # other route in the panel. It stays unsnapped there,
                    # exactly as it was.
                    if rid0 != "910":
                        shape_isnap[(feed, sid)] = (cols, 38.0, toks)
            elif feed == "metrolink":
                # The railroad ink holds railroads and nothing else, which is
                # enough to put a line on track but not to say *which* track
                # where two run together. The sheet's own name for the line
                # says that, so it anchors like a numbered route's badges.
                tree = line_ink = rail_line_tree()
                if tree is not None:
                    # `+ pins` as every other branch does it. A railroad's name
                    # is written along it a handful of times and nowhere else,
                    # so a line can run a quarter of its length on the sheet
                    # before the first one — and where the warp has to be told
                    # which of two parallel tracks is which, that head is the
                    # stretch with nothing to tell it. See the Riverside Line.
                    anc = line_name_anchors(rid or "", tree) + pins
                    anchored += bool(anc)
                    can_refit = True
                    out_pts = snap_recording(pts, tree, anchors=anc, caps=RAIL_CAPS,
                                            win=RAIL_WIN, speckled=False)
            elif feed == "ladot":
                # LADOT's two liveries are two stroke styles of one olive ink —
                # DASH solid, Commuter Express dashed — so each snaps to its own
                # network and neither can be dragged onto the other's streets.
                # Which style a route is drawn in is settled by its badges
                # rather than by its name; see ladot_livery.
                #
                # Commuter Express needs pinning where the warp is worst: at
                # Marina del Rey it is 130 px out, further than the run down
                # Via Marina is long, and the leg had no way to tell which end
                # of the drawn line was which. A DASH is pinned only by the
                # designation MAP_LABELS reads off the sheet, the feed's own
                # name for it being no designation the sheet prints.
                tree, anc = ladot_livery(toks, bool(is_dash.get(rid)),
                                         sheet_tokens.get(rid, set()))
                line_ink = tree
                # `+ pins` as every other branch does it. LADOT was one of the
                # networks PINNED_ANCHORS could not reach, which is a gap rather
                # than a policy: a hand-placed point on the drawn line serves
                # here exactly as it does elsewhere, and a DASH — badged once or
                # twice on the whole sheet, if at all — is the case with least
                # else to go on.
                anc = branch_anchors(anc + pins, sid, route_sids[rid], kd_for)
                anchored += bool(anc)
                if tree is not None:
                    can_refit = True
                    out_pts = snap_recording(pts, tree, anchors=anc, caps=LADOT_CAPS,
                                            win=LADOT_WIN, speckled=False)
                shape_isnap[(feed, sid)] = (good, 30.0, toks)
            elif feed in SYMBOL_FEEDS:
                # Snapped and anchored on the sheet's own strokes — see
                # SYMBOL_FEEDS for why the mask can hold neither. Beach Cities
                # shares its evergreen with Foothill Transit, which the legend
                # is explicit about and the geography makes harmless: the
                # nearest Foothill stroke is most of the county away, further
                # than any cap here can reach.
                tree = line_ink = ink_tree([LEGEND_INK[feed]])
                anc = route_anchors(toks, tree, near=BADGE_NEAR_INK)
                owned = SYMBOL_OWNERS.get((feed, rid))
                if owned is None:
                    anc = branch_anchors(
                        anc, sid,
                        label_sids[rmeta[rid][0] if rid in rmeta else rid], kd_for)
                else:
                    # Named by hand, so `branch_anchors` has nothing left to
                    # decide — see SYMBOL_OWNERS, which exists because the
                    # distances it decides by are the ones that went wrong.
                    anc = [p for p in anc
                           if min(math.hypot(p[0] - q[0], p[1] - q[1])
                                  for q in owned) <= SYMBOL_OWNER_NEAR]
                anchored += bool(anc)
                if tree is not None:
                    can_refit = True
                    out_pts = snap_recording(pts, tree, anchors=anc,
                                             caps=INK_CAPS, win=61, speckled=False)
                shape_isnap[(feed, sid)] = (good, 30.0, toks)
            elif agency_tree is not None:
                anchor_tree = agency_tree
                anchor_cols = list(good)
                if feed in BADGE_FILLS:
                    anchor_cols = good + [BADGE_FILLS[feed]]
                    anchor_tree = mask_tree(anchor_cols, 30.0)
                # The colour gate below rejects a badge whose chip is better
                # explained by another agency's colour than by this one's — but
                # an agency's *own* colours must never play that rival. `good` is
                # the green refined off the drawn lines, and it drifts: foothill's
                # came out (77,102,85), a dozen px from the (62,100,78) seed it
                # started at. That seed is still in the rival palette, and it sits
                # 6 px from the "SS" chip while `good` sits 22 px away — so it out-
                # explains `good` and the gate throws the badge out. It cost the
                # Silver Streak 16 of its 18 badges, and with nothing left to pin
                # its 57-px-north warp the line sat up on Valley Blvd instead of on
                # its own busway. Folding the seeds into the own-set keeps them off
                # the rival list; a genuinely foreign chip is still far from every
                # one of them and still rejected.
                gate_cols = anchor_cols + LEGEND_SEEDS.get(feed, [])
                anc = branch_anchors(
                    route_anchors(toks, anchor_tree, colors=gate_cols) + pins,
                    sid, route_sids[rid], kd_for)
                anchored += bool(anc)
                can_refit = True
                # Snap on the strokes where the sheet's vectors can be trusted
                # to hold this agency's whole drawing, anchor on the pixels
                # either way — the same split Metro's branch makes above, and
                # for the same reason: a chip is filled with the line colour
                # rather than stroked in it, so it stands on the mask and not
                # on the ink. The ink is the centreline alone, which is what
                # INK_CAPS' coarse-to-fine ladder is for; a mask smears each
                # line across its casing and its badges, and the two tight
                # passes anchored mask snapping settles for cannot answer for
                # the distance. `speckled` goes with the mask: the ink has no
                # stray pixels to shrug off. See INK_SNAP.
                ink = ink_tree([LEGEND_INK[feed]]) if feed in INK_SNAP else None
                out_pts = snap_recording(pts, ink or agency_tree, anchors=anc,
                                         caps=INK_CAPS if ink else None,
                                         speckled=ink is None)
                line_ink = ink or agency_tree
                # The call-out keeps the mask whatever the main map does:
                # pdf_ink drops every stroke inside the panel, so there is no
                # ink down there to snap a downtown run onto.
                shape_isnap[(feed, sid)] = (good, 30.0, toks)
            elif feed in STREET_SNAP:
                # No livery, so no anchors either: the sheet prints "PT" beside
                # the street, never a route number, so there is nothing to tell
                # 31 from 32 where they part. The snap is unanchored and short-
                # reaching by design — it refines within the corridor the warp
                # already chose rather than choosing one.
                out_pts = snap_coherent(pts, street_tree(), caps=STREET_CAPS,
                                        speckled=False)
            # An aligned walk is a corridor the length band would have thrown
            # away, taken because the two courses could be aligned. That is a
            # judgement, so it is checked rather than trusted: fit the shape
            # again with those walks refused, and keep them only if what they
            # produce is no worse on the hairpin measure and no further from the
            # drawing. The corridors are usually right — this is where the drift
            # win comes from — but a handful of routes came out kinked when
            # every alignment was believed, and this is what stops them.
            aligned_fit = bool(align_used()) and can_refit and out_pts is not None
            if out_pts is not None:
                snapped += 1
            # Keep the pre-snap polyline alongside the stored one. Stops are
            # warped, not snapped, so projecting them onto a snapped shape asks
            # them to find themselves on a line that has moved out from under
            # them — half a kilometre, at the median. Where the offset rivals
            # the spacing between stops the monotone projection scrambles, and
            # the vehicle sprints between the stops it piles up. snap_coherent
            # displaces the densified polyline point by point, so the two agree
            # index for index and a stop's place on one is its place on the
            # other. snap_rail resamples, and falls back to projecting on the
            # stored shape — rail tracks the artwork closely enough not to care.
            base = np.array(densify(pts, 4.0), dtype=float)
            full = np.asarray(out_pts, dtype=float) if out_pts is not None else base
            if out_pts is not None and len(full) == len(base):
                # Straightening one spike can leave a sharper residual where it
                # met a bend, and the simplify() below can turn a helped dense
                # path into a worse stored one, so neither cleanup is taken on
                # faith. Every candidate is scored on the *stored* geometry the
                # animation actually plays — by the very measure path_check
                # ranks on — and the best of them wins, the snapper's own shape
                # taking ties, so no shape comes out worse than it went in.
                # Taking a fold out is what leaves the residual despike files
                # off, so the two together usually win; but not always, and a
                # pass run unconditionally ahead of the other can rob it of a
                # better answer, so each stands on the ballot alone as well.
                #
                # The ballot is scored on two measures, not one. `spike_penalty`
                # charges only turning that doubles back inside 12 px, and the
                # snapper's 61-point smoothing turns an occluded stretch into a
                # smooth bulge with no sharp turn anywhere in it: Foothill 493
                # scores a flat 0 while visibly off its line. Scored on that
                # alone, `undetour` can never win a shape it is the only fix
                # for — it ties, and the tie goes to the snapper. So the
                # excursion is priced too, and the winner minimises both.
                #
                # The old promise still holds, and is now explicit: a candidate
                # that would rank worse on `spike_penalty` than the snapper's
                # own shape is thrown out before it can be scored, so nothing
                # buys a straighter line at the cost of a hairpin.
                full = settle(full, base, anc, line_ink)
                # An aligned walk is a corridor the length band would have
                # thrown away, taken because the two courses could be aligned
                # instead. That is a judgement, so it is checked rather than
                # trusted: fit the shape again with those walks refused, put
                # that through the same ballot, and keep the alignment only if
                # what ships is no worse on the hairpin measure and no further
                # from the drawing. The corridors are usually right — this is
                # where the drift comes from — but a handful of routes came out
                # kinked when every alignment was believed, and this is what
                # stops them, on the geometry that ships rather than on the one
                # the ballot was handed.
                if aligned_fit:
                    plain = resnap_without_alignment()
                    if plain is not None and len(plain) == len(base):
                        pf = settle(np.asarray(plain, float), base, anc, line_ink)
                        if (stored_penalty(full) > stored_penalty(pf)
                                or ink_offset(full, line_ink) > ink_offset(pf, line_ink) + 0.5):
                            full = pf
                            stats["align_refused"] += 1
                        else:
                            stats["align_kept"] += 1
            if len(full) == len(base) and os.environ.get("DETOUR_TRACE") == f"{feed}:{rid}":
                _o = np.hypot(*(full - base).T)
                _c = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(base, axis=0).T))])
                _st = max(1e-6, _c[-1] / max(1, len(_o) - 1))
                _w = int(min(len(_o) // 2 * 2 - 1, max(9, DETOUR_ARC / _st))) | 1
                _x = _o - ndi.median_filter(_o, size=_w, mode="nearest")
                _B = np.asarray(anc, dtype=float) if len(anc) else None
                print(f"  TRACE {feed}:{rid} sid={sid} n={len(_o)} arc={_c[-1]:.0f} "
                      f"medwin={_w} anchors={len(anc)} off p50={np.median(_o):.1f} "
                      f"max={_o.max():.1f} excess max={_x.max():.1f} "
                      f"runs={len(detour_runs(full, base, anc, line_ink))}")
                for _s in range(0, len(_o), max(1, len(_o) // 22)):
                    _e = min(len(_o), _s + max(1, len(_o) // 22))
                    _k = _s + int(np.argmax(_x[_s:_e]))
                    _bd = np.hypot(*(_B - full[_k]).T).min() if _B is not None else -1
                    print(f"    arc {_c[_k]:7.0f}  off {_o[_k]:6.1f}  exc {_x[_k]:6.1f}"
                          f"  badge {_bd:6.1f}  at ({full[_k][0]:.0f},{full[_k][1]:.0f})")
            if len(full) == len(base):
                for _lo, _hi, _pk in detour_runs(full, base, anc, line_ink):
                    _k = _lo + int(np.argmax(np.hypot(*(full - base).T)[_lo:_hi + 1]))
                    DETOUR_AUDIT.append((_pk, feed, rid or "?",
                                         int(full[_k][0]), int(full[_k][1])))
            override = OVERRIDE_PATHS.get((feed, (rid or "").split("-")[0]))
            if override is not None and len(full) == len(base):
                full = np.asarray(apply_override(full, base, override), dtype=float)
            if len(full) == len(base):
                stored, keep = simplify(full, mask=True)
                cb = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(base, axis=0).T))])
                shape_param[(feed, sid)] = (base, cb, cb[keep])
            else:
                stored = simplify(full)
            shapes_raw[(feed, sid)] = stored
            p = tmp[sid]
            shape_ll[(feed, sid)] = [(q[1], q[2]) for q in p]
        n_trips = len(trips_out) - n_before
        stats[feed] = n_trips
        print(f"{feed}: {n_trips} trips on {day} "
              f"({snapped}/{len(warped)} shapes snapped, {anchored} anchored)")

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

    def main_dist(key, si, px):
        """Distance along the stored shape of each map-px point.

        Where the snap displaced the warp point by point the two agree index
        for index, so a point is placed on the warp — which it was measured
        against — and carried over. Everything that has to speak about a
        position along a shape goes through here, because two callers using
        different rules is a mismatch that only shows up when the snap moves:
        the DTLA inset runs used to project onto the snapped shape while the
        stops carried over, and a Metrolink shape shifting 28 px inside the
        Downtown call-out was enough to put Union Station outside its own run
        and drop every Metrolink line out of the inset panel."""
        prm = shape_param.get(key)
        if prm is None:
            return project_stops(shapes_raw[key], cums[si], px)
        base, cb, cb_kept = prm
        return np.interp(project_stops(base, cb, px), cb_kept, cums[si])

    # DTLA inset: per-shape downtown runs in inset px, computed on demand
    shape_runs = {}                 # si -> runs or None

    def runs_for(key, si):
        if si not in shape_runs:
            ll = shape_ll.get(key)
            runs = None
            if ll is not None and TR_INSET is not None:
                cols, tol, toks = shape_isnap.get(key, (None, 0, set()))
                if key[0] == "gtfs_bus" and cols:
                    cols = [ORANGE]   # inset draws ALL Metro bus lines orange
                # Rail is the one network whose printed colors are known
                # exactly rather than sampled off the artwork, so it is the one
                # that can be masked on the pyramid — see inset_tile_tree.
                # Everything else is masked on the reading it was refined from.
                tree = (inset_tile_tree(cols) if cols and key[0] == "gtfs_rail"
                        else mask_tree(cols, tol, region="inset") if cols else None)
                gate = None if key[0] in ("gtfs_bus", "gtfs_rail") else cols
                anc = route_anchors(toks, tree, region="inset", colors=gate)
                runs = inset_runs(ll, lambda px: main_dist(key, si, px), tree, anc,
                                  sole=key[0] == "gtfs_rail")
            shape_runs[si] = runs
        return shape_runs[si]

    patterns_out = []
    for feed, sid, stop_seq in patterns:
        spx = [stops_px[(feed, s)] for s in stop_seq]
        key = (feed, sid)
        if key not in shape_index:            # no shape in feed: polyline through stops
            # key per pattern — distinct stop sequences must not share one
            # pseudo-shape (all Metrolink lines once collided on an empty sid)
            key = (feed, sid, stop_seq)
            if key not in shape_index:
                if len(set(spx)) < 2:
                    patterns_out.append(None)
                    stats["skipped_no_shape"] += 1
                    continue
                shapes_raw[key] = spx
                shape_ll[key] = [stops_ll[(feed, s)] for s in stop_seq]
                add_shape(key, spx)
        si = shape_index[key]
        if feed == "gtfs_rail":
            spx = platform_stops(shapes_raw[key], cums[si], spx)
        d = main_dist(key, si, spx)
        entry = {"s": si, "d": [round(v) for v in d]}
        runs = runs_for(key, si)
        if runs:
            sll = np.array([stops_ll[(feed, s)] for s in stop_seq], dtype=float)
            sx, sy = to_inset_px(sll[:, 0], sll[:, 1])
            ir, idist = inset_stop_map(runs, d, list(zip(sx, sy)))
            if any(r >= 0 for r in ir):
                entry["ir"] = ir
                entry["id"] = [round(v) for v in idist]
        patterns_out.append(entry)

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

    # inset run geometry, only for shapes some pattern actually mapped onto
    used_si = {p["s"] for p in patterns_out if p and "ir" in p}
    insets_out = [None] * len(shapes_out)
    for si in used_si:
        insets_out[si] = [[round(v, 1) for xy in r["pts"] for v in xy]
                          for r in shape_runs[si]]
    stats["inset_shapes"] = len(used_si)

    out = {"date": TARGET.strftime("%Y%m%d"), "systems": systems,
           "routes": routes, "shapes": shapes_out,
           "patterns": patterns_out, "trips": trips_final,
           "insets": insets_out, "insetRect": list(INSET_RECT)}
    with open("schedule.json", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    stats["routes"] = len(routes)
    stats["shapes"] = len(shapes_out)
    stats["patterns"] = len(patterns_out)
    stats["trips_total"] = len(trips_final)
    print(dict(stats))
    if DETOUR_AUDIT:
        worst = sorted(DETOUR_AUDIT, reverse=True)
        print(f"detours: {len(worst)} on {len({(f, r) for _, f, r, _, _ in worst})} routes"
              f" (worst {worst[0][0]:.0f} px)")
        for pk, feed, rid, x, y in worst[:15]:
            print(f"  {pk:6.1f} px  {feed:<12} {rid:<8} at ({x},{y})")
        with open("scratch/detours.tsv", "w") as f:
            for pk, feed, rid, x, y in worst:
                f.write(f"{pk:.1f}\t{feed}\t{rid}\t{x}\t{y}\n")
    print(f"built {datetime.now().isoformat(timespec='seconds')}")


if __name__ == "__main__":
    main()
