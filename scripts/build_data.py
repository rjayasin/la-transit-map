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
    line's name."""
    s = (short or long_name or "?").strip()
    for pre in ("Metro ", "Metrolink "):
        if s.startswith(pre):
            s = s[len(pre):]
    if s.endswith(" Line"):
        s = s[:-5]
    tok = s.split()[0]
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
STATION_REACH = 45.0       # px a stop may sit from its drawn platform


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
# The busway mask holds that one ribbon and nothing else, so the snap can
# reach much further than it dares on the shared orange, and can follow the
# line closely instead of smoothing whole stretches together.
BUSWAY_CAPS = (100.0, 50.0, 25.0, 12.0)
BUSWAY_WIN = 9

# Drawn colors for feeds whose lines can't be color-masked, sampled from the
# map, so vehicle sprites still match the artwork they ride on.
DRAWN_COLORS = {
    "pasadena": (204, 193, 184),   # plain gray, same as street art
    "metrolink": (120, 124, 126),  # crosshatched railroad gray
}

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
    when there's no fill to read."""
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


def route_anchors(tokens, tree, region="main", colors=None, margin=8.0):
    """Badge positions for any of the route's number tokens that lie on the
    agency's drawn-line mask (rejects same-number badges of other agencies,
    highway shields, street labels). The agency color must be present AT the
    word itself (badge fill / colored glyph strokes), not merely nearby —
    another agency's badge drawn against this agency's line must not match.

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
            # >=10: a real badge fill / glyph has dozens of mask pixels here;
            # stray antialiased fringes near a foreign badge have a few
            if len(tree.query_ball_point([cx, cy], 6.0)) < 10:
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


TRACE_STEP = 4.0          # px; lattice pitch for walking the drawn line
TRACE_PAD = 60.0          # px of slack around the two badges, for a bowed line
TRACE_REACH = 5.0         # px; how close a lattice cell must be to drawn pixels,
                          # loose enough to step over the glyphs crossing a line
TRACE_SPAN = (60.0, 700.0)   # px between badges worth walking between
TRACE_DETOUR = (0.75, 1.35)  # trusted band of walked length / shape length
TRACE_SAMPLE = 30.0       # px of walked line per intermediate anchor

_PATHS = {}   # keyed by tree identity, safe because _TREES holds every tree
              # for the life of the process, so no id is ever reused


def mask_path(a, b, tree, step=TRACE_STEP, pad=TRACE_PAD, reach=TRACE_REACH):
    """(polyline, length) of the shortest route from a to b that stays on the
    drawn mask, or (None, None) if the mask doesn't connect them.

    A coarse lattice over the two points' bounding box, cells kept where drawn
    pixels are within `reach`, 8-connected, Dijkstra. The lattice is
    deliberately blunt: drawn lines are ~8 px wide, so a 4 px pitch keeps every
    corridor connected while leaving only a few thousand nodes to search. The
    walk is only ever used to aim the snap, which then refines onto the pixels."""
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
    G = sparse.coo_matrix((np.concatenate(w),
                           (np.concatenate(rows), np.concatenate(cols))),
                          shape=(nx * ny, nx * ny))
    dist, pred = dijkstra(G + G.T, indices=ia, return_predecessors=True)
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
    interpolation."""
    out_s, out_D = [s[:1]], [D[:1]]
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
        out_s.append(s[i + 1:i + 2])
        out_D.append(D[i + 1:i + 2])
    return np.concatenate(out_s), np.concatenate(out_D)


ANCHOR_PASSES = 3     # times the anchor fit may be re-run to pick up more badges


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
    return ok


def snap_coherent(pts, tree, caps=None, win=61, anchors=None,
                  anchor_gate=120.0, min_frac=0.5, tail=(10.0, 11), region="main"):
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

    The anchor fit is re-run while it keeps reaching new badges. A badge only
    counts for a shape it passes within `anchor_gate` of, and where the warp is
    poor the badges that would fix it start out beyond that: Metro 690 runs 190
    px north of drawn Foothill Blvd in the far valley, out of reach of the two
    badges at the ends of the error. One fit on the badges it can see brings the
    rest within reach, and the second pass lands every one of them. Re-fitting
    is self-limiting in a way that simply widening the gate is not — a badge on
    another branch of the route stays far from the corrected line and never
    joins in.

    tail: (cap, window) for one last pass with a short smoothing window. The
    wide window that keeps whole stretches together also averages the
    correction across junctions, leaving the line sagging a line-width or two
    off the artwork wherever parallel routes crowd it (Sunset through West
    Hollywood). The cap is kept below the spacing of neighboring drawn streets
    so this can only refine within the corridor the wide passes chose, never
    hop to the next one."""
    P = np.array(densify(pts, 4.0), dtype=float)
    n = len(P)
    if n < 8 or tree is None:
        return None
    default_caps = caps is None
    if default_caps:
        caps = (40.0, 26.0, 14.0)
    if anchors:
        A = np.asarray(anchors, dtype=float)
        used = 0
        for _ in range(ANCHOR_PASSES):
            cum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(P, axis=0).T))])
            d2 = ((P[None, :, :] - A[:, None, :]) ** 2).sum(2)
            j = d2.argmin(1)
            near = np.sqrt(d2[np.arange(len(A)), j]) < anchor_gate  # badge serves this shape
            if near.sum() <= used:             # no badge the last fit couldn't reach
                break
            used = int(near.sum())
            order = np.argsort(cum[j[near]], kind="stable")
            s = cum[j[near]][order]
            D = (A[near] - P[j[near]])[order]
            s, D = trace_anchors(s, D, A[near][order], P, cum, tree)
            P = P + np.c_[np.interp(cum, s, D[:, 0]), np.interp(cum, s, D[:, 1])]
        if used and default_caps:
            caps = (26.0, 14.0)            # anchors pin the street; stay tight
    idx = np.arange(n)
    passes = [(cap, win) for cap in caps] + ([tail] if tail else [])
    for ci, (cap, pwin) in enumerate(passes):
        pwin = min(pwin, max(3, (n // 2) * 2 - 1))
        is_tail = bool(tail) and ci == len(passes) - 1
        d, j = tree.query(P)
        ok = d < cap
        if ci == 0:
            # "mostly undrawn" is judged only over the stretch a mask could
            # cover. Metro 690 runs a third of its length under the title
            # banner, which every mask excludes; counting that against it
            # failed the whole route out of snapping and left it on a warp
            # that strays 190 px north of the Foothill Blvd it is drawn on.
            cov = maskable(P, region)
            if (ok & cov).sum() < max(1, cov.sum()) * min_frac:
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
            disp[:, c] = np.convolve(np.pad(col, pwin // 2, mode="edge"), k, "valid")
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
    return [tuple(p) for p in chaikin(simplify(P, 1.0), rnd)]


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


def inset_runs(ll, stored_pts, cum, snap_tree=None, anchors=None):
    """Portions of a shape inside the DTLA inset, as runs of inset-px
    polyline. Motion in the inset is computed natively in inset space (the
    schematic main map collapses downtown, so main-shape distance cannot
    parameterize it): each run carries its own cumulative distance, and
    stops are later projected onto it. d0/d1 (distance range on the main
    shape) only route each stop to the right run."""
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
        d = project_stops(stored_pts, cum, list(zip(mx, my)))
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
            label = route_label(row.get("route_short_name", ""), row.get("route_long_name", ""))
            color = (row.get("route_color") or "").strip()
            text = (row.get("route_text_color") or "").strip()
            is_dash[row["route_id"]] = "DASH" in (row.get("route_long_name") or "")
            if not color:
                color, text = (METRO_BUS_COLOR, METRO_BUS_TEXT) if is_metro else (FALLBACK_COLOR, FALLBACK_TEXT)
            if color == "000000" and is_metro:
                color = "B4333D"  # map's Rapid red (GTFS says black; map draws red)
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
        for ti, seq, at, sid_ in read_cols(
                feed, "stop_times.txt",
                ("trip_id", "stop_sequence", "arrival_time", "stop_id")):
            if ti in trip_info and at.strip():
                stop_times[ti].append((int(seq), parse_time(at), sid_))

        n_before = len(trips_out)
        used_shapes = set()
        for ti, sts in stop_times.items():
            if len(sts) < 2:
                continue
            rid, sid = trip_info[ti]
            sts.sort()
            route_stops.setdefault((feed, rid), set()).update(s for _, _, s in sts)
            times = [t for _, t, _ in sts]
            stop_seq = tuple(s for _, _, s in sts)
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
        warped = {}
        for sid, p in tmp.items():
            p.sort()
            x, y = to_px(np.array([q[1] for q in p]), np.array([q[2] for q in p]))
            warped[sid] = list(zip(x, y))

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
            out_pts = None
            rid = route_by_shape.get(sid)
            toks = badge_tokens.get(rid, set())
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
                    # The G Line's own busway ribbon is a hotter orange than the
                    # streets, but only on the sheet: map.png's downscale mixes
                    # it with the page until it sits 24 from ordinary Metro
                    # orange, inside the mask tolerance, so the line had every
                    # parallel street to choose from and took Vanowen. Read it
                    # off the tiles, where the two stay 57 apart.
                    tree = tile_tree(cols, BUSWAY_TOL) if busway else mask_tree(cols)
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
                           if busway else route_anchors(toks, tree))
                    anchored += bool(anc)
                    out_pts = snap_coherent(pts, tree, anchors=anc,
                                            caps=BUSWAY_CAPS if busway else None,
                                            win=BUSWAY_WIN if busway else 61)
                    shape_isnap[(feed, sid)] = (cols, 38.0, toks)
            elif agency_tree is not None:
                anchor_tree = agency_tree
                anchor_cols = list(good)
                if feed in BADGE_FILLS:
                    anchor_cols = good + [BADGE_FILLS[feed]]
                    anchor_tree = mask_tree(anchor_cols, 30.0)
                anc = route_anchors(toks, anchor_tree, colors=anchor_cols)
                anchored += bool(anc)
                out_pts = snap_coherent(pts, agency_tree, anchors=anc)
                shape_isnap[(feed, sid)] = (good, 30.0, toks)
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
                runs = inset_runs(ll, shapes_raw[key], cums[si], tree, anc)
            shape_runs[si] = runs
        return shape_runs[si]

    patterns_out = []
    marker_tree = None
    if len(station_markers()):
        marker_tree = cKDTree(station_markers()[:, :2])

    for feed, sid, stop_seq in patterns:
        spx = [stops_px[(feed, s)] for s in stop_seq]
        if feed == "gtfs_rail" and marker_tree is not None:
            # Rail platforms are drawn: the white shape with the black stroke,
            # a circle alone or conjoined where lines meet. A stop projected
            # from its warped position lands beside one rather than on it, so
            # the train eases to a halt short of the platform or past it. Move
            # each stop onto its own marker first; the projection below then
            # measures to where the map says the platform is. Stops with no
            # marker in reach — under the Downtown panel's own geometry, or a
            # platform the sheet doesn't draw — keep the warp.
            dist, j = marker_tree.query(spx)
            spx = [tuple(marker_tree.data[jj]) if dd < STATION_REACH else p
                   for p, dd, jj in zip(spx, dist, j)]
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
        prm = shape_param.get(key)
        if prm is None:
            d = project_stops(shapes_raw[key], cums[si], spx)
        else:                     # project before the snap, then carry it over
            base, cb, cb_kept = prm
            d = np.interp(project_stops(base, cb, spx), cb_kept, cums[si])
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
    print(f"built {datetime.now().isoformat(timespec='seconds')}")


if __name__ == "__main__":
    main()
