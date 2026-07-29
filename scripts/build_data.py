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
from georef import (EXCLUDE, MASK_LEVEL, ROUTE_COLORS, TILE, TILES, TOL,  # noqa: E402
                    load_masks, tile_scan)
from georef_inset import GEO as INSET_GEO, LEGEND as INSET_LEGEND, RECT as INSET_RECT  # noqa: E402

TARGET = date(2026, 7, 22)  # a Wednesday inside the Metro JUNE26 calendar window
GTFS = "data/gtfs"
# gtfs_rail first so Metro trains draw config (snap) is applied; order otherwise cosmetic
FEEDS = ["gtfs_rail", "gtfs_bus", "bigbluebus", "culvercity", "ladot", "longbeach",
         "foothill", "torrance", "norwalk", "montebello", "gtrans", "pasadena",
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
    "norwalk": "Norwalk Transit", "montebello": "Montebello Bus Lines",
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
MAP_LABELS = {
    ("foothill", "20707"): "SS",   # Silver Streak
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
BADGE_FILLS = {
    "foothill": (118, 140, 120),
}

LEGEND_SEEDS = {
    "culvercity": [(215, 215, 157)],
    "gtrans": [(198, 165, 188)],
    "ladot": [(175, 170, 141), (154, 150, 117)],   # DASH + Commuter Express olives
    "longbeach": [(136, 88, 92)],
    "norwalk": [(162, 208, 207)],   # badge-fill sampled; legend swatch too pale
    "bigbluebus": [(143, 135, 136)],
    "foothill": [(62, 100, 78)],    # dark evergreen lines; legend swatch too pale
    "montebello": [(172, 186, 153)],
    "torrance": [(137, 139, 174)],
    "burbank": [(132, 168, 155)],
    "beachcities": [(170, 181, 169)],
}

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
LADOT_INK = [(0.409, 0.398, 0.173), (0.419, 0.4, 0.164)]   # DASH + Commuter Express

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
    seg = (path if np.hypot(*(B[lo] - path[0])) <= np.hypot(*(B[lo] - path[-1]))
           else path[::-1])                # start the corridor where the shape enters
    d = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(seg, axis=0).T))])
    t = np.linspace(0, d[-1], hi - lo + 1)
    res = np.c_[np.interp(t, d, seg[:, 0]), np.interp(t, d, seg[:, 1])]
    out = np.array(full, dtype=float)
    out[lo:hi + 1] = res
    return [tuple(p) for p in out]


BRANCH_SLACK = 2.0     # times the nearest variant's distance a badge may sit
BRANCH_FLOOR = 20.0    # px of slack under that, so near-ties stay shared


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
    nearest it. Keep it for this shape while this shape is about as near as the
    nearest variant gets, and drop it when another explains it far better. On
    the trunk, where every variant runs the same street and is equally close,
    that keeps the badge for all of them; it only bites where the route forks.
    A route with one shape has nothing to compare against and keeps the lot."""
    if not anchors or len(sids) < 2:
        return anchors
    A = np.asarray(anchors, dtype=float)
    dists = [kd_for(s).query(A)[0] for s in sids]
    best = np.min(np.vstack(dists), axis=0)
    mine = dists[sids.index(sid)]
    keep = mine <= np.maximum(best * BRANCH_SLACK, best + BRANCH_FLOOR)
    return [a for a, k in zip(anchors, keep) if k]


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
    out_s, out_D, walked = [s[:1]], [D[:1]], 0
    for i in range(len(s) - 1):
        ds = s[i + 1] - s[i]
        if ds > 0 and TRACE_SPAN[0] < math.dist(A[i], A[i + 1]) < TRACE_SPAN[1]:
            walk, length = mask_path(A[i], A[i + 1], tree)
            if walk is not None and TRACE_DETOUR[0] * ds < length < TRACE_DETOUR[1] * ds:
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
        out_s.append(s[i + 1:i + 2])
        out_D.append(D[i + 1:i + 2])
    return np.concatenate(out_s), np.concatenate(out_D), walked


ANCHOR_PASSES = 3     # times the anchor fit may be re-run to pick up more badges
ANCHOR_SLIDE = 48.0   # px the shape may be slid bodily before its badges are read
ANCHOR_PITCH = (8.0, 2.0)   # px; the slide is searched coarse, then refined
ANCHOR_DRAG = 0.15    # px of residual charged per px of slide, per badge
ANCHOR_GAIN = 0.7     # of the badge residual a slide must clear to be believed
CROSSED_APART = 20.0  # px between two badges before they are different streets
CROSSED_SPAN = 30.0   # px along the shape within which they'd be the same stretch


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
    through them is worth re-reading them against."""
    kd = cKDTree(P)
    base = np.minimum(kd.query(A)[0], gate).sum()
    t, resid = np.zeros(2), base
    span = ANCHOR_SLIDE
    for pitch in ANCHOR_PITCH:
        g = np.arange(-span, span + pitch / 2, pitch)
        T = t + np.stack(np.meshgrid(g, g, indexing="ij"), -1).reshape(-1, 2)
        d = kd.query((A[:, None, :] - T[None, :, :]).reshape(-1, 2))[0]
        r = np.minimum(d.reshape(len(A), len(T)), gate).sum(0)
        k = int((r + ANCHOR_DRAG * np.hypot(*T.T) * len(A)).argmin())
        t, resid = T[k], r[k]
        span = pitch
    if np.hypot(*t) >= ANCHOR_SLIDE or resid > ANCHOR_GAIN * base:
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
                  speckled=True):
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
    landing guarded against stray pixels. A tree of PDF strokes does not."""
    P = np.array(densify(pts, 4.0), dtype=float)
    n = len(P)
    if n < 8 or tree is None:
        return None
    default_caps = caps is None
    if default_caps:
        caps = (40.0, 26.0, 14.0)
    if anchors:
        A = np.asarray(anchors, dtype=float)
        used = walked = 0
        for _ in range(ANCHOR_PASSES):
            cum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(P, axis=0).T))])
            d2 = ((P[None, :, :] - A[:, None, :]) ** 2).sum(2)
            j = d2.argmin(1)
            near = np.sqrt(d2[np.arange(len(A)), j]) < anchor_gate  # badge serves this shape
            if crossed_badges(A[near], cum, j[near]):
                # the warp is out by more than the streets are apart; read the
                # badges again against a shape slid onto them
                S = A - anchor_slide(P, A, anchor_gate)
                d2 = ((P[None, :, :] - S[:, None, :]) ** 2).sum(2)
                j = d2.argmin(1)
                near = np.sqrt(d2[np.arange(len(A)), j]) < anchor_gate
            order = np.argsort(cum[j[near]], kind="stable")
            s = cum[j[near]][order]
            D = (A[near] - P[j[near]])[order]
            s, D, w = trace_anchors(s, D, A[near][order], P, cum, tree)
            # nothing the last fit didn't already have: no badge it couldn't
            # reach, and no corridor it couldn't walk
            if near.sum() <= used and w <= walked:
                break
            used, walked = max(used, int(near.sum())), max(walked, w)
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
        ok = (d < cap) & cov
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
            col[~cov] = 0.0
            disp[:, c] = np.convolve(np.pad(col, pwin // 2, mode="edge"), k, "valid")
        P = P + disp
    d, j = tree.query(P)                   # final tight snap + light smoothing
    ok = (d < 8) & maskable(P, region)
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


def inset_runs(ll, main_dist, snap_tree=None, anchors=None):
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
                               caps=(60.0, 30.0, 14.0), win=25, anchors=anchors,
                               anchor_gate=75.0, min_frac=0.35, region="inset")
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

        rmeta, badge_tokens, is_dash = {}, {}, {}
        for row in read_csv(feed, "routes.txt"):
            label = MAP_LABELS.get((feed, row["route_id"])) or route_label(
                row.get("route_short_name", ""), row.get("route_long_name", ""))
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
            if row["route_id"].split("-")[0] == "901":
                color, text = "%02X%02X%02X" % BUSWAY_ORANGE, "FFFFFF"   # G Line's own
            rail = row.get("route_type") in ("0", "1", "2")
            rmeta[row["route_id"]] = (label, color, text or "FFFFFF", rail)
            # tokens as printed on map badges, for anchor lookup
            short = (row.get("route_short_name") or "").strip()
            badge_tokens[row["route_id"]] = set(short.replace("/", " ").split()) | {label}

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
        _kd = {}

        def kd_for(sid):
            if sid not in _kd:
                _kd[sid] = cKDTree(np.asarray(densify(warped[sid], 4.0), dtype=float))
            return _kd[sid]

        # snap shapes onto the drawn lines of this system where they exist
        agency_tree, sprite_cols = None, None
        if feed in LEGEND_SEEDS and warped:
            seeds = LEGEND_SEEDS[feed]
            refined = [refine_color(list(warped.values()), s) for s in seeds]
            good = [c for c in refined if c]
            if good:
                agency_tree = mask_tree(good, 30.0)
            # Sprites take the color the sheet actually prints, off the tiles,
            # falling back to map.png's washed-out reading then the legend
            # swatch. Where an agency has two seeds they are two liveries, not
            # two guesses at one, so each is sampled over only the routes drawn
            # in it — sampling both over everything collapses them onto
            # whichever is more common, and LADOT lost DASH vs Commuter Express.
            groups = [list(warped.values())] * len(seeds)
            if len(seeds) > 1:
                groups = [[p for sid, p in warped.items()
                           if is_dash.get(route_by_shape.get(sid, "")) == (i == 0)]
                          for i in range(len(seeds))]
            sprite_cols = [drawn_color(g, s) or c or tuple(s)
                           for g, c, s in zip(groups, refined, seeds)]
            print(f"  {feed} drawn color(s): {good}")
        elif feed in DRAWN_COLORS:
            sprite_cols = [DRAWN_COLORS[feed]]

        # recolor this agency's sprites to match the line color the map draws
        # (GTFS route_color is the agency's own branding, not the map's)
        if sprite_cols:
            for (f, rid), ridx in route_idx.items():
                if f != feed:
                    continue
                c = sprite_cols[0]
                if len(sprite_cols) > 1 and not is_dash.get(rid):
                    c = sprite_cols[1]        # ladot: [DASH, Commuter Express]
                routes[ridx]["c"] = "#%02X%02X%02X" % tuple(c)
                routes[ridx]["t"] = "#FFFFFF"

        snapped = anchored = 0
        for sid, pts in warped.items():
            out_pts, anc = None, []
            # The drawing this shape was snapped on: the PDF's strokes where it
            # has them, its agency's colour mask where it does not. It is the
            # arbiter of whether a detour is really the drawn line — see
            # _ink_vouches, which reads it far enough out along the run that a
            # mask's label-shaped holes cannot answer for the whole of it.
            line_ink = None
            rid = route_by_shape.get(sid)
            toks = badge_tokens.get(rid, set())
            pins = PINNED_ANCHORS.get((feed, (rid or "").split("-")[0]), [])
            if pins:
                pts = trim_terminus(pts, pins)   # end at the drawn hub, not past it
            if feed == "gtfs_rail":
                tree = rail_trees.get(rid)
                if tree is not None:
                    out_pts = snap_rail(pts, tree)
                if rid in ROUTE_COLORS:
                    shape_isnap[(feed, sid)] = ([ROUTE_COLORS[rid]], TOL, toks)
            elif feed == "gtfs_bus":
                rid0 = (rid or "").split("-")[0]
                busway = rid0 == "901"
                if rid0 in ("720", "754", "761"):
                    cols = [RAPID_RED]
                elif rid0 == "910":
                    cols = None   # J/Silver: drawn color collides with freeway gray
                else:
                    cols = [BUSWAY_ORANGE] if busway else [ORANGE]
                if cols is not None:
                    # Snap on the strokes, anchor on the pixels. The badges are
                    # chips filled with the line color, not strokes of it, so
                    # they stand on the mask and not on the ink.
                    ink = ink_tree(BUSWAY_INK if busway else
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
                    anc = (station_anchors(route_stops.get((feed, rid), ()), tree,
                                           {s: stops_name.get((feed, s), "")
                                            for s in route_stops.get((feed, rid), ())},
                                           {s: stops_px.get((feed, s))
                                            for s in route_stops.get((feed, rid), ())})
                           if busway else branch_anchors(
                               route_anchors(toks, tree) + pins,
                               sid, route_sids[rid], kd_for))
                    anchored += bool(anc)
                    out_pts = snap_coherent(pts, snap_tree, anchors=anc,
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
                    shape_isnap[(feed, sid)] = (cols, 38.0, toks)
            elif feed == "metrolink":
                # The railroad ink holds railroads and nothing else, which is
                # enough to put a line on track but not to say *which* track
                # where two run together. The sheet's own name for the line
                # says that, so it anchors like a numbered route's badges.
                tree = line_ink = rail_line_tree()
                if tree is not None:
                    anc = line_name_anchors(rid or "", tree)
                    anchored += bool(anc)
                    out_pts = snap_coherent(pts, tree, anchors=anc, caps=RAIL_CAPS,
                                            win=RAIL_WIN, speckled=False)
            elif feed == "ladot":
                # LADOT's two liveries are two stroke styles of one olive ink —
                # DASH solid, Commuter Express dashed — so each snaps to its own
                # network and neither can be dragged onto the other's streets.
                dash = bool(is_dash.get(rid))
                tree = line_ink = ink_tree(LADOT_INK, dashed=not dash)
                # Commuter Express needs pinning where the warp is worst: at
                # Marina del Rey it is 130 px out, further than the run down
                # Via Marina is long, and the leg had no way to tell which end
                # of the drawn line was which. DASH doesn't get anchors — its
                # loops are drawn continuously and the ink alone puts them on
                # the street, and its designations are single letters the sheet
                # also gives Metro's rail lines.
                anc = ([] if dash else
                       branch_anchors(route_anchors(toks, tree, near=BADGE_NEAR_INK),
                                      sid, route_sids[rid], kd_for))
                anchored += bool(anc)
                if tree is not None:
                    out_pts = snap_coherent(pts, tree, anchors=anc, caps=LADOT_CAPS,
                                            win=LADOT_WIN, speckled=False)
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
                out_pts = snap_coherent(pts, agency_tree, anchors=anc)
                line_ink = agency_tree
                shape_isnap[(feed, sid)] = (good, 30.0, toks)
            elif feed in STREET_SNAP:
                # No livery, so no anchors either: the sheet prints "PT" beside
                # the street, never a route number, so there is nothing to tell
                # 31 from 32 where they part. The snap is unanchored and short-
                # reaching by design — it refines within the corridor the warp
                # already chose rather than choosing one.
                out_pts = snap_coherent(pts, street_tree(), caps=STREET_CAPS,
                                        speckled=False)
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
                as_snapped = full
                spike0 = stored_penalty(as_snapped)
                best = spike0 + DETOUR_WEIGHT * detour_penalty(as_snapped, base,
                                                               anc, line_ink)
                unfolded = unfold(as_snapped, base, anc)
                undet = undetour(as_snapped, base, anc, line_ink)
                for cand in (despike(as_snapped), unfolded, despike(unfolded),
                             undet, despike(undet), unfold(undet, base, anc)):
                    if np.array_equal(cand, as_snapped):
                        continue
                    spike = stored_penalty(cand)
                    if spike > spike0:
                        continue
                    penalty = spike + DETOUR_WEIGHT * detour_penalty(cand, base,
                                                                     anc, line_ink)
                    if penalty < best:
                        full, best = cand, penalty
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
                tree = mask_tree(cols, tol, region="inset") if cols else None
                gate = None if key[0] in ("gtfs_bus", "gtfs_rail") else cols
                anc = route_anchors(toks, tree, region="inset", colors=gate)
                runs = inset_runs(ll, lambda px: main_dist(key, si, px), tree, anc)
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
