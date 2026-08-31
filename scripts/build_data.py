"""Build schedule.json from all cached GTFS feeds in data/gtfs/.

- Gathers every trip active on each day of the target week (Wed 2026-07-22's);
  feeds whose calendar doesn't cover a day fall back to their busiest date of
  that same weekday.
- Projects shapes into map pixel space via data/transform.json (poly2 warp).
- Metro rail shapes are additionally snapped onto the drawn line pixels.
- Stops are projected onto shapes to get distance-along-shape per stop.
- Emits compact JSON: routes, shapes (px polylines), patterns (stop dists),
  trips (route, pattern, stop arrival times) and, per trip, a bitmask of the
  weekdays it runs on.

Trips crossing midnight (times >= 24:00) are also emitted shifted by -24h onto
the following day, so the after-midnight portion of yesterday's service appears
at the start of the day.

--only fits one feed, or one route of it, and writes those shapes alone to a
stub debug_line.py can draw with --schedule. Seconds instead of two minutes,
for looking at what a table entry did:

    scripts/build_data.py --only bigbluebus:9
    scripts/debug_line.py 9 --schedule scratch/refit_bigbluebus.json --no-stops

It fits nothing else and writes no schedule.json, so the checks — drift_check,
path_check, speed_check — still want a full build before you commit.
"""
import argparse, colorsys, csv, hashlib, inspect, json, math, os, re, sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from functools import lru_cache
from operator import itemgetter

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
PDF = "26-1720_blt_system_map_47x47.5-2.pdf"   # the sheet itself, read for both
                                               # its strokes and its knockouts
# gtfs_rail first so Metro trains draw config (snap) is applied; order otherwise cosmetic
# Norwalk Transit publishes only from its own site; the Mobility Database entry
# catalogued as "us-california-norwalk-transit-system-nts" is Connecticut's
# Norwalk Transit District, and warps a quarter of a million px off the sheet.
FEEDS = ["gtfs_rail", "gtfs_bus", "bigbluebus", "culvercity", "ladot", "longbeach",
         "foothill", "torrance", "montebello", "gtrans", "pasadena",
         "burbank", "beachcities", "norwalk", "metrolink"]
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
    "norwalk": "Norwalk Transit", "metrolink": "Metrolink",
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
TR_INSET = _TRJ.get("inset", {}).get("grid")
if "geo" in _TRJ.get("inset", {}):
    INSET_GEO = tuple(_TRJ["inset"]["geo"])   # fitted frame coverage wins


def to_px(lon, lat):
    L, T = lon - TR["lon0"], lat - TR["lat0"]
    B = np.c_[np.ones_like(L), L, T, L * L, L * T, T * T]
    return B @ TR["cx"], B @ TR["cy"]


def _grid_axis(v, table):
    """One of the call-out's two axes: ground coordinate to drawn pixel,
    through the streets the fit named, straight on past either end."""
    c = np.array([r[0] for r in table]), np.array([r[1] for r in table])
    out = np.interp(v, c[0], c[1])
    lo, hi = v < c[0][0], v > c[0][-1]
    if lo.any():
        out[lo] = c[1][0] + (v[lo] - c[0][0]) * (c[1][1] - c[1][0]) / (c[0][1] - c[0][0])
    if hi.any():
        out[hi] = c[1][-1] + (v[hi] - c[0][-1]) * (c[1][-1] - c[1][-2]) / (c[0][-1] - c[0][-2])
    return out


def to_inset_px(lon, lat):
    """The call-out is a rectified drawing of a rotated grid, so the transform
    is separable in that grid's own axes rather than a polynomial in lon/lat —
    see georef_inset."""
    e = np.asarray(TR_INSET["along"])
    g = np.c_[(np.asarray(lon) - TR_INSET["lon0"]) * TR_INSET["lon_scale"],
              np.asarray(lat) - TR_INSET["lat0"]]
    return (_grid_axis(g @ e, TR_INSET["x"]),
            _grid_axis(g @ np.array([-e[1], e[0]]), TR_INSET["y"]))


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
        # One C call per row rather than a generator frame per field, which is
        # worth having on a file of two million. The slow path stands behind it
        # for the rows it cannot index: a column the file doesn't carry, or a
        # short last field the reader dropped.
        lo, hi = min(idx), max(idx)
        wide = itemgetter(*idx) if len(idx) > 1 and lo >= 0 else None
        out = []
        for row in r:
            if len(row) < n - 1:
                continue
            out.append(wide(row) if wide is not None and len(row) > hi
                       else tuple(row[i] if 0 <= i < len(row) else "" for i in idx))
        return out


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


def pick_date(feed, trips_per_service, target):
    """`target` if it has service; else the busiest same-weekday date the feed
    covers. Staying on the weekday matters: the fallback stands in for the day
    it replaces, and a Sunday's service is not a Wednesday's."""
    def score(d):
        return sum(trips_per_service.get(s, 0) for s in active_services(feed, d))
    if score(target) > 0:
        return target
    wd = target.weekday()
    cands = set()
    for row in read_csv(feed, "calendar.txt"):
        d0 = datetime.strptime(row["start_date"], "%Y%m%d").date()
        d1 = datetime.strptime(row["end_date"], "%Y%m%d").date()
        d = d0 + timedelta(days=(wd - d0.weekday()) % 7)   # first one of that weekday
        while d <= d1 and len(cands) < 400:
            cands.add(d)
            d += timedelta(days=7)
    for row in read_csv(feed, "calendar_dates.txt"):
        d = datetime.strptime(row["date"], "%Y%m%d").date()
        if d.weekday() == wd and row["exception_type"] == "1":
            cands.add(d)
    best = max(cands, key=lambda d: (score(d), -abs((d - target).days)), default=None)
    return best if best and score(best) > 0 else None


def pick_dates(feed, trips_per_service):
    """A service date per weekday, indexed the way JS reads one: 0 = Sunday.
    Taken from TARGET's own week, so the seven are one week's service rather
    than seven days gathered from wherever each is busiest."""
    sunday = TARGET - timedelta(days=(TARGET.weekday() + 1) % 7)
    return [pick_date(feed, trips_per_service, sunday + timedelta(days=i)) for i in range(7)]


@lru_cache(maxsize=None)   # called once per stop time — millions of them, over
                           # a few tens of thousands of distinct clock readings
def parse_time(s):
    parts = s.split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2] if len(parts) > 2 else 0)


# Designations the sheet prints that the feed never says. Getting this wrong
# costs twice over: a rider sees a designation the map never prints, *and* the
# badges are the anchors, so the shape has nothing pinning it to its own drawn
# line and wanders onto whichever of the agency's lines runs nearest.
#
# Three cases. A route branded rather than numbered carries the brand nowhere in
# its GTFS. A whole agency can be designated by one symbol for the operator, with
# no route number printed anywhere. And a named route (a DASH) has prose in
# route_short_name, which route_label initialises its own way — often onto a code
# the sheet prints for some other route entirely, which is worse than landing on
# nothing. Only a designation read off the artwork can settle any of them.
#
# Both directions of a loop share one badge; the sheet draws them as one line.
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
    ("ladot", "4867"): "BE",        # DASH Boyle Heights
    ("ladot", "4868"): "SC",        # DASH El Sereno/City Terrace
    ("ladot", "1757"): "SE",        # DASH Southeast, clockwise
    ("ladot", "1758"): "SE",        # DASH Southeast, counterclockwise
    ("ladot", "573"): "CR",         # DASH Crenshaw, clockwise
    ("ladot", "589"): "CR",         # DASH Crenshaw, counter-clockwise
    ("ladot", "6768"): "PA",        # DASH Pacoima, clockwise
    ("ladot", "6770"): "PA",        # DASH Pacoima, counter-clockwise
    ("ladot", "801"): "PV",         # DASH Panorama City/Van Nuys, clockwise
    ("ladot", "804"): "PV",         # DASH Panorama City/Van Nuys, counterclockwise
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

    A lettered working of a numbered route goes the same way: where the feed
    suffixes a number and the sheet draws one line under the bare number, the
    suffix costs twice over, exactly as a split designation does — a label no
    rider can find, and no badges to anchor the shape with."""
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
    A muted agency line color can sit within mask tolerance of one of these, so
    masks exclude pixels that match an infrastructure color better than the
    agency's own."""
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
FADE_MARGIN = 6.0   # how much better this agency's blend must fit than a rival's;
                    # under a knockout panel, scaled by the ink the panel leaves


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


_PANELS = None
PANEL_MAX = 1e5     # px²; above this a page-colored fill is the page, not a panel


def knockout_panels():
    """A mask of the page-colored panels the sheet lays over its own artwork.

    A place name set across the drawing gets one of these behind it, and what
    it covers survives at a fraction of its ink rather than at the ~40% a
    label's halo leaves. They are a few hundred small fills of the page's own
    color in the PDF — which is named by the fill color the page reads as,
    rather than by a constant of ours — and the page itself is the one fill of
    it too big to be a panel. Without pymupdf there are no panels and the
    deep-knockback rule below simply never fires."""
    global _PANELS
    if _PANELS is None:
        im, _ = map_image()
        h, w = im.shape[:2]
        _PANELS = np.zeros((h, w), dtype=bool)
        try:
            import fitz
            page = fitz.open(PDF)[0]
            s = w / page.rect.width
            paper = np.asarray(bg_palette()[-1], dtype=float)
            fills = []
            for it in page.get_drawings():
                if it["type"] != "f" or it.get("fill") is None:
                    continue
                r = it["rect"]
                fills.append((tuple(c * 255 for c in it["fill"]),
                              (r.x0 * s, r.y0 * s, r.x1 * s, r.y1 * s)))
            if fills:
                page_fill = min(set(c for c, _ in fills),
                                key=lambda c: ((np.asarray(c) - paper) ** 2).sum())
                for fill, (x0, y0, x1, y1) in fills:
                    if fill != page_fill or (x1 - x0) * (y1 - y0) > PANEL_MAX:
                        continue
                    _PANELS[max(0, int(y0)):int(y1) + 1,
                            max(0, int(x0)):int(x1) + 1] = True
        except Exception as e:                      # missing pdf / pymupdf
            print(f"knockout panels unavailable: {e}")
    return _PANELS


def unfade(m, sub, d2a, tol, colors):
    """Re-add drawn-line pixels that a place-name label has dimmed.

    Labels sit on top of the artwork, and a color mask breaks wherever a name
    crosses a line — a long place name can knock a ~45 px hole in one, and the
    snap then locks onto whichever parallel street stays unbroken. But the label
    isn't painting the line out: under its halo the map knocks the artwork back
    toward the page, and a place name set over the artwork adds a page-colored
    panel that knocks it back further still, to a quarter or a third of the
    ink. The line is still there, just too pale for the mask's tolerance.

    So inside the halo, take a pixel that reads as this agency's color painted
    over the page at partial opacity: near the segment from the page color to
    the line color, and at least FADE_MIN of the way along it. Muted line
    colors dim into ordinary map grays, so — as in the mask itself — a pixel
    counts only when this agency's blend explains it better than any background
    or rival agency's does; without that test a gray livery claims every light
    gray on the sheet. Under a knockout panel that margin has to be read
    against the ink that is left: every blend line converges on the page color,
    so where the panel leaves a third of the ink the distance between this
    agency's blend and a rival's shrinks with it, and a fixed margin in RGB
    units becomes impossible to meet however distinct the two colors are.
    Scaling it by the fade there asks the same separation of a pale stretch as
    of a solid one; outside the panels, where a halo leaves enough ink to
    judge, the margin stays absolute and the reading stays as strict as it was.
    Recovery stays within LABEL_REACH of real artwork, since the point is to
    close gaps in drawn lines, not to find new ones.

    Glyphs still interrupt what's recovered, but only by a stroke width at a
    time, which nearest-pixel snapping rides straight over. Bridging those too
    (a morphological closing kept where text is) was tried and removed: it
    cannot span a hole where the line turns its corner inside the label, and
    it pulls the line low: dilating the word into the mask hangs a word-sized
    blob of text off the underside of the street."""
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

    own, own_a = np.full(len(P), np.inf), np.zeros(len(P))
    for c in colors:
        d2, a = fit(c)
        take = d2 < own
        own, own_a = np.where(take, d2, own), np.where(take, a, own_a)
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
    margin = np.where(knockout_panels()[ys, xs], FADE_MARGIN * own_a, FADE_MARGIN)
    ok = ((own_a > FADE_MIN) & (own < (tol * 0.6) ** 2)
          & (np.sqrt(own) + margin < np.sqrt(rival)))
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
            ("mask", key, art_stamp("map.png", PDF), EXCLUDE,
             code_stamp(mask_pixels, unfade, knockout_panels, box_dilate,
                        bg_palette, rival_palette)),
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
# to the line, so a route dives into its own chip and comes back out. A drawn
# line is longer than a chip is wide whichever way it runs, though: every ribbon
# component in the panel spans at least 19 map px along its length while the
# chips all measure 8-9 square. So
# a component that fits inside a chip's own footprint in both axes is not a line.
INSET_CHIP_SPAN = 12.0     # map px


def inset_tile_tree(colors, tol=MASK_TOL, level=MASK_LEVEL):
    """A mask of the Downtown call-out read off the tile pyramid, where the
    printed color is faithful, rather than off map.png's blend of it.

    Rail on the main map has always been masked this way. The call-out was not,
    and it is where it matters most, because the panel is the one place the
    sheet redraws every downtown line at a legible size — so a line that misses
    its mask there misses it in the only view that shows the difference.
    The 4096 px reduction shifts a printed colour far enough that a line can
    land exactly on the rail tolerance and match not one pixel of itself in the
    whole panel, keeping its raw warp and cutting diagonally across the blocks
    it is drawn along. On the pyramid the same colour sits 0.0 from its own.

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
# Pasadena Transit has no livery at all: the sheet prints "PT" beside the street
# the route runs on and the line under that label *is* the street. For any other
# agency that would be fatal; here it isn't, because a PT bus runs on those
# streets. The grid is the right thing to snap to and the only question is which
# street of it — which the warp already answers nearly everywhere (median 3 px,
# i.e. on the correct street and a line-width off it). The fault is in the
# excursions, where it drifts into the white between two streets for a few
# hundred px, and a short reach closes those without ever choosing a street.
#
# A grid runs both ways at once, and that is the difficulty: a route crossing it
# is *on* ink at every intersection. A run parallel to its own street but a few
# px off it touches ink only at the crossings, which contribute a displacement of
# zero and, smoothed over the 61-point window, outvote every point that wants to
# move. Widening the cap does nothing, because reach was never the problem — the
# problem is a north-south street claiming a point travelling east-west.
#
# So the ink is binned by the direction it runs, and a point may only be claimed
# by ink going roughly its own way. For a coloured livery this would be pointless;
# for the grid it is what makes the mask usable at all.
DIR_BINS = 6                # 30 deg apart; a line has no sense, so 0..180
DIR_SLACK = 1               # bins either side: accept within ~45 deg


# The street layer, taken from the PDF's strokes rather than from the raster.
#
# A raster mask cannot be used for this one. Every other agency's mask is keyed
# on a colour only that agency's lines are drawn in, so stray lettering inside
# the tolerance is a rim of speckle that solid_pixels() deals with. The street
# gray has no such luck: the sheet sets its labels in a gray of the same hue, and
# a word's antialiased strokes are indistinguishable from a one-px street. No
# luminance band separates them, and no direction test can either, since a
# horizontal word is horizontal.
#
# get_drawings() returns strokes, and text is not a stroke. It also gives the
# direction exactly, from the segment itself, rather than estimated off a raster.
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
# the next street over. The excursions this closes run 15-30 px against a 3 px
# median, and the coherent field does most of the work: the points either side of
# an excursion are already on the right street and hold it there. 34 is wide
# enough to reach a street the warp sits ~28 px off and still too short to reach
# past it to the next one.
STREET_CAPS = (34.0, 20.0, 10.0)

# Feeds with no drawn line of their own, snapped to the grid instead. Metrolink
# is deliberately not here: it has a livery (a crosshatched railroad gray) and
# its own anchors in the names the sheet writes along each track.
STREET_SNAP = {"pasadena"}

# Per-agency drawn-line color seeds, sampled from the map's legend swatches.
# Thin dashes sample washed-out, so each seed is refined against pixels found
# along the agency's actual routes before masking. Pasadena Transit's color is
# plain gray (identical to street art), so it keeps the polynomial warp.
# Badge fill colors that differ from the drawn line color: used only for anchor
# detection (the words sit on light chips), never for line snapping.
#
# An agency needs an entry here when its chips are its legend ink saturated while
# its lines are that ink laid thin over a cream page. The two can be further
# apart than the chip is from a rival agency's colour, so the gate that asks
# which agency best explains a chip answers with the rival and throws out every
# badge the agency has, leaving the whole network unanchored.
BADGE_FILLS = {
    "foothill": (118, 140, 120),
    "longbeach": (126, 33, 58),
    "ladot": (105, 103, 55),
}

LEGEND_SEEDS = {
    "culvercity": [(215, 215, 157)],
    "gtrans": [(198, 165, 188)],
    "ladot": [(175, 170, 141), (154, 150, 117)],   # DASH + Commuter Express olives
    "longbeach": [(136, 88, 92), (98, 38, 53)],
    "bigbluebus": [(143, 135, 136)],
    "foothill": [(62, 100, 78)],    # dark evergreen lines; legend swatch too pale
    "montebello": [(172, 186, 153)],
    "torrance": [(137, 139, 174)],
    "burbank": [(132, 168, 155)],
    "beachcities": [(62, 100, 78)],
    "norwalk": [(168, 208, 208)],
}

# The two operators the sheet symbolises by *agency* rather than by route, and
# so the two that snap on their legend ink rather than on a colour mask. Neither
# has a route number printed anywhere — one code beside every line the operator
# runs, set as plain text rather than on a chip, so the mask's presence test (the
# agency's own pixels *under* the word) finds only antialiased glyph strokes and
# rejects them, leaving both agencies with no anchors at all. The legend strokes
# answer both halves: they are where the line is, and distance to them is a test
# a word standing beside a line can pass. See `route_anchors`'s `near`.
SYMBOL_FEEDS = {"beachcities", "burbank"}

# Which route each of those symbols stands for, where the sheet's own answer is
# "the operator" and the routes cannot be told apart any other way.
#
# One code for the whole agency makes every one of its words a candidate anchor
# for every one of its routes, and `branch_anchors` divides them by distance: a
# word speaks for whichever variant passes nearest it. That holds only while the
# warp is nearer the truth than the routes are to each other. Where it isn't, the
# distances come out backwards and one route is fitted bodily onto the other's
# drawing — so the assignment is made by hand instead.
#
# A route listed here takes exactly the words listed for it and nothing else; one
# left out is divided by `branch_anchors` as before. An agency whose routes run
# further apart than the warp is wrong does not need an entry.
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
# Montebello is here because a colour mask cannot hold its sage at all. The
# corridors two or three routes share are drawn thick and mask solidly; the
# stretches one route runs alone are drawn thin, and at 4096 px a thin sage line
# is a blend with the cream page — 35% of the agency's strokes have no mask pixel
# on them. What *is* in the mask is the sheet's grey street lettering, blending
# into range from the other side, so a badge-to-badge walk bridges along the
# words onto whatever thick corridor they reach.
#
# Long Beach's answer, naming the thin stroke as a second seed, is no use here:
# its thin lines have a dark core to name, and Montebello's have none — the
# missed readings smear pale with no cluster in them, and a seed wide enough to
# cover the strokes takes the page with it (167k mask pixels to 977k).
#
# The PDF has the same lines as vectors, thin and thick alike, complete under
# every label painted over them and with no chips or lettering in them at all.
# drift_check measures these agencies on those strokes for the same reason.
INK_SNAP = {"montebello", "bigbluebus"}


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
#    page or vanish under a heavier line drawn alongside, so that none of the
#    route reaches the raster at all.
#
# But the sheet is a vector PDF, and every route on it is a stroke in its
# agency's ink: no tolerance, no rival colors, no rendering to recover it from.
# Where two liveries share one ink they are two stroke styles of it — the
# railroad's centreline under its dashed ticks, LADOT's solid DASH against its
# dashed Commuter Express — so the dash pattern selects between them too.

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
# whatever is drawn beside it, so a shape can sit on the mask while its own line
# is a couple of dozen px away, resting against a neighbour's ribbon. Ink is the
# centreline alone, and answering for that distance takes the coarse-to-fine
# ladder rather than the two tight passes anchored mask snapping settles for.
INK_CAPS = (40.0, 26.0, 14.0)

# Each LADOT livery's ink holds that livery and nothing else, so its snap can
# reach as far as the railroad's. It needs to: the warp is a median 20 px off
# the drawn line and locally as much as 100, and a shorter reach leaves the
# worst stretches with no ink in range at all.
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

# A route the sheet doesn't name rides one it does: where two railroads share a
# track for the whole of their length here, the sheet draws them as one line and
# writes only the one name along it. Borrowing that name is the only thing
# holding the unnamed one to its own track. A railroad that shares no track with
# a named one has nothing to borrow and keeps its warp.
SHARED_RAIL_LABEL = {"91 Line": "Orange County Line"}


def line_name_anchors(name, tree, near=LABEL_NEAR):
    """Anchors from the name the sheet writes along a railroad.

    Metrolink prints no badge anywhere on the map, and its lines share one ink
    and one crosshatched livery, so where two of them run parallel the artwork
    alone cannot say which is which — two tracks drawn a couple of dozen px
    apart on the same heading are near enough that the warp lands one line's
    schedule closer to the other's track, and the snap leaves it there.

    The sheet does say which is which, though, and in the very words GTFS names
    the route with: metrolink's route_id is the line's printed name. Each is
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
    puts that stop. Neither test alone is enough: proximity to the ribbon alone
    matches a stop to a neighbouring station's label, and the warp is too far
    out to be trusted on its own."""
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
    badge itself but for the named ones is prose, yielding one token per word.
    Those are street and place names,
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
    agencies at once, and where two such lines run bundled a foreign badge clips
    this agency's mask and passes the presence test above. So also read the
    badge's own chip color and drop it when some *other* agency's color
    explains it better than this one's. This is relative, not a fixed
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
    keeps a DASH loop and a Commuter Express off each other's streets. Which
    style a route is drawn in is not the route's *name* to say, though: dashes
    mark part-time service, which is what a Commuter Express usually is and a
    DASH never is, so the name predicts the style — and predicts it wrongly for
    the one Commuter Express that runs all day, drawn solid like a DASH. Looked
    for among the dashes it finds nothing within any cap and keeps the warp.

    So the name only proposes a livery and the printed badges settle it. A DASH
    is left with the name's answer, having nothing to settle with: it is named
    rather than numbered, and the designation the feed's name yields is one the
    sheet doesn't print.

    Which is not to say a DASH cannot be anchored, only that the feed cannot say
    what to anchor it on. `sheet_tokens` is the designation read off the artwork
    instead, by hand, in MAP_LABELS — the sheet's own word for this route, so it
    names badges printed along this route's line and no other. Nothing else the
    feed offers may anchor a DASH: an initialism is a guess, and a guess landing
    on a code the sheet *does* print is worse than one landing on nothing."""
    prefer = ink_tree(LADOT_INK, dashed=not is_dash)
    if is_dash:
        # On the chip, not merely near the ink. A Commuter Express number is
        # set as plain text beside its line, so distance to the ink is all there
        # is to go on; a DASH designation is printed on a chip, and needs to be,
        # because two capitals is also the shape of half the words on the sheet
        # — a street label abbreviated to the same two letters will sit inside
        # the anchor gate, on this agency's own olive, and drag the shape across
        # the map to reach it. The chip is a flat olive (BADGE_FILLS) and street
        # labels are the teal the sheet letters streets in, so the colour under
        # the word separates them outright.
        return prefer, route_anchors(set(sheet_tokens), prefer,
                                     near=BADGE_NEAR_INK,
                                     colors=[BADGE_FILLS["ladot"]])
    anc = route_anchors(tokens, prefer, near=BADGE_NEAR_INK)
    other = ink_tree(LADOT_INK, dashed=is_dash)
    alt = route_anchors(tokens, other, near=BADGE_NEAR_INK)
    return (other, alt) if len(alt) > len(anc) else (prefer, anc)


# A point on the *drawn* line that serves as a badge does, for a stretch the
# sheet prints no badge over. Keyed by (feed, route), in map px.
#
# Three things bring it to this: a shared transit hub prints each route once in
# the municipal gray, so a chip there belongs to another agency and the colour
# gate rightly refuses it; the badge-to-badge corridor walk needs a continuous
# mask, which fails where another agency's ink crosses; and the badges can
# simply run out before the route does, leaving a whole limb unanchored past the
# last one, where the interpolation clamps.
#
# A pin near an end of a shape also cuts an overshoot back to itself, for the
# schematic that ends a route at its hub while the GTFS runs on to a layover the
# map omits. That doubles as a hazard — see trim_terminus and TRIM_TERMINI.
PINNED_ANCHORS = {
    ("gtfs_bus", "2"): [(1001, 1801)],
    ("bigbluebus", "4061"): [(1138, 2215), (1090, 2271)],
    ("bigbluebus", "4056"): [(991.6, 2006.5), (1092.0, 1949.6)],
    ("bigbluebus", "4051"): [(830.0, 1962.0)],
    ("bigbluebus", "4058"): [(705.0, 2085.7), (775.3, 2166.4)],
    ("longbeach", "2"): [(1610, 3044)],
    ("longbeach", "91"): [(2288, 3262)],
    ("longbeach", "92"): [(2288, 3262)],
    ("longbeach", "93"): [(2288, 3262)],
    ("longbeach", "94"): [(2288, 3262)],
    ("gtfs_bus", "55"): [(1746.5, 2120)],
    ("gtfs_bus", "222"): [(1334.8, 1040.3)],
    ("foothill", "20270"): [(2684.3, 1426.0)],
    ("foothill", "20284"): [(3209.0, 1712.0), (3236.0, 1695.5)],
    ("longbeach", "131"): [(2113, 3320)],
    ("longbeach", "111"): [(2146, 2880)],
    ("longbeach", "61"): [(1930, 2752.2)],
    ("longbeach", "51"): [(1893, 2751)],
    ("torrance", "6"): [(1378, 2822), (1575, 2785)],
    ("gtfs_bus", "217"): [(1802, 1499)],
    ("gtfs_bus", "233"): [(1040.9, 1755.1), (1004.7, 1810.7)],
    ("foothill", "10495"): [(1878.2, 1918.7), (1950.0, 1889.6), (2022.8, 1860.2),
                            (2699.5, 2045.3), (2788.5, 2101.0),
                            (3207.0, 2166.0), (3216.0, 2098.0)],
    ("foothill", "20498"): [(1878.2, 1918.7), (1950.0, 1889.6), (2022.8, 1860.2)],
    ("foothill", "10490"): [(2260.0, 1860.0), (2400.0, 1860.0)],
    ("foothill", "10499"): [(1878.2, 1918.7), (1950.0, 1889.6), (2022.8, 1860.2)],
    ("foothill", "10699"): [(1878.2, 1918.7), (1950.0, 1889.6), (2022.8, 1860.2)],
    ("foothill", "20707"): [(1878.2, 1918.7), (1950.0, 1889.6), (2022.8, 1860.2)],
    ("foothill", "20493"): [(1878.2, 1918.7), (1950.0, 1889.6), (2022.8, 1860.2),
                            (2699.5, 2045.3), (2788.5, 2101.0)],
    ("foothill", "20272"): [(2826.0, 1656.0)],
    ("metrolink", "Riverside Line"): [(1860, 2077.4)],
    ("ladot", "573"): [(1299, 2080), (1310, 2103), (1313, 2117), (1322, 2130),
                       (1336, 2133), (1348, 2112), (1370, 2124), (1387, 2134),
                       (1417, 2134)],
    ("ladot", "589"): [(1299, 2080), (1310, 2103), (1313, 2117), (1322, 2130),
                       (1336, 2133), (1348, 2112), (1370, 2124), (1387, 2134),
                       (1417, 2134)],
    ("ladot", "798"): [(645, 1236), (652, 1140)],
    ("ladot", "799"): [(1032, 1472), (999, 1336), (1180, 1507)],
    ("ladot", "800"): [(1032, 1472), (999, 1336), (1180, 1507)],
    ("gtrans", "7X"): [(1400.0, 2489.5), (1440.0, 2514.5), (1500.0, 2514.5),
                       (1560.0, 2514.5), (1584.0, 2552.0), (1584.0, 2590.0),
                       (1569.0, 2640.0), (1569.0, 2680.0), (1569.0, 2748.0)],
}


# Termini given in *warp* px instead of on the drawing, for trimming only — they
# are never read as anchors. A pin in PINNED_ANCHORS does both jobs at once, and
# that works only where the warp lands near enough the drawn terminus for one
# point to serve as both.
#
# Where it doesn't, distance is not what defeats the pin: the trim cuts to the
# *nearest* point of the shape, so at a schematic corner the layover's own start
# point can be nearer the drawn terminus than the terminus is, and every point of
# the drawn corridor answers the same way. So the terminus is named where the
# warp puts it instead.
TRIM_TERMINI = {
    ("torrance", "5"): [(1412, 3189)],   # PCH & Crenshaw, in warp px
}

TERMINUS_REACH = 35.0   # px a shape must pass within of a pin to be cut to it
TERMINUS_TAIL = 110.0   # px of overshoot past the pin that gets trimmed off
CIRCUIT_GAP = 14.0      # px between a shape's two ends before it is a circuit
                        # rather than a line. Nothing sits between: a route that
                        # closes comes back within a few px, and the nearest one
                        # that merely starts and finishes nearby is 250 away


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
    the cuts compound: a first cut brings a pin further into the route — one
    that is no kind of terminus — inside the tail limit as measured from the new
    end, and the next pass cuts the route back to that as well.

    A shape that finishes where it started has no overshoot to cut: its two ends
    are one point of a circuit, passed through mid-journey as much as at either
    end, and the stretch either side of them is the route rather than a layover
    tail. Cutting there would take a piece out of the circuit and leave the last
    point short of the first. It also puts the whole of a circulator's first and
    last `TERMINUS_TAIL` px of arc — a quarter of a small loop — beyond what a
    pin can hold, which is the difference between anchoring one and needing an
    override for it."""
    P = np.asarray(densify(pts, 4.0), dtype=float)
    if len(P) < 2 or float(np.hypot(*(P[0] - P[-1]))) <= CIRCUIT_GAP:
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
# A last resort: every entry here is a stretch where the badges cannot bracket
# the fault, the corridor walk cannot cross it, and no pin can be placed that
# attaches to the right part of the shape. See implementation_notes.md.
OVERRIDE_PATHS = {
    ("gtrans", "7X"): {
        "box": (1300, 2380, 1400, 2532),
        "path": [
            (1374.4, 2389.7), (1355, 2389.7), (1335, 2389.7), (1324.5, 2389.7),
            (1320.5, 2391), (1318, 2394), (1317.5, 2398), (1317.5, 2420),
            (1317.5, 2440), (1317.5, 2460), (1317.5, 2478), (1317.5, 2484.8),
            (1319, 2488), (1322.6, 2489.8), (1390, 2489.8),
        ],
    },
    ("ladot", "868"): {
        "box": (720, 850, 900, 1000),
        "path": [
            (922, 995), (920, 1001), (910, 1004), (890, 1004), (882, 1002),
            (881, 995), (881, 989), (876, 988), (840, 988), (800, 988),
            (791, 993), (787, 1002),
        ],
    },
    ("gtfs_bus", "134"): {
        "box": (674, 2040, 812, 2135),
        "path": [
            (660.2, 2055.6), (715.5, 2150.8), (716.6, 2152.2), (718.0, 2153.1),
            (719.6, 2153.7), (721.2, 2153.8), (722.9, 2153.4), (723.7, 2153.0),
            (732.3, 2148.2), (709.9, 2108.3), (709.1, 2104.4), (709.8, 2101.6),
            (711.0, 2100.0), (712.5, 2098.8), (715.3, 2097.2), (722.2, 2093.3),
            (743.0, 2081.5),
        ],
    },
    ("gtfs_bus", "761"): {
        "box": (930, 1786, 1002, 1861),
        "path": [
            (930, 1744), (985, 1744), (1010, 1744), (1022, 1745), (1030, 1750),
            (1035, 1755), (1038, 1765), (1038, 1778), (1037, 1789), (1035, 1795),
            (1029, 1800), (1019, 1804), (1010, 1806), (1003, 1808), (1001, 1816),
            (1001, 1830), (1001, 1845), (1001, 1858),
        ],
    },
    ("foothill", "20188"): {
        "box": (3758, 1648, 3845, 1730),
        "path": [
            (3761, 1656), (3770, 1656), (3780, 1656), (3788, 1656),
            (3791, 1657), (3793.5, 1659), (3795, 1662), (3795.5, 1668),
            (3795.5, 1690), (3795.5, 1712), (3795.5, 1728), (3795, 1733),
            (3793, 1736.5), (3790, 1738), (3782, 1738), (3772, 1738),
            (3761, 1738),
        ],
    },
    ("foothill", "10197"): {
        "box": (3496, 1730, 3596, 1834),
        "path": [
            (3515, 1833), (3515, 1825), (3515, 1818), (3516, 1812), (3518, 1808),
            (3521, 1804), (3524, 1801), (3528, 1800), (3532, 1800), (3537, 1802),
            (3541, 1804), (3546, 1807), (3550, 1810), (3555, 1811), (3559, 1810),
            (3561, 1806), (3559, 1798), (3559, 1788), (3559, 1775), (3559, 1766),
            (3560, 1756), (3562, 1751), (3566, 1750), (3575, 1751), (3584, 1757),
            (3593, 1762),
        ],
    },
    ("montebello", "10"): {
        "box": (2085, 1966, 2158, 2078),
        "path": [
            (2177, 1961), (2177, 1976), (2177, 1992), (2177, 2007), (2176, 2019),
            (2175, 2027), (2173, 2034), (2170, 2040), (2166, 2046), (2162, 2052),
            (2159, 2058), (2157, 2064), (2157, 2071),
        ],
    },
    ("gtfs_bus", "501"): {
        "box": (1180, 1320, 1345, 1440),
        "path": [
            (1289.5, 1382.5), (1303.0, 1393.5), (1310.6, 1406.0),
            (1318.5, 1419.8), (1326.8, 1432.4), (1333.0, 1443.1),
            (1338.7, 1452.9), (1343.6, 1455.7),
        ],
    },
    ("gtfs_bus", "251"): {
        "box": (1780, 1430, 1875, 1580),
        "path": [
            (1745.0, 1489.5), (1757.0, 1489.5), (1769.0, 1489.5),
            (1777.0, 1489.6), (1780.5, 1490.2), (1782.5, 1492.0),
            (1783.6, 1494.5), (1784.0, 1497.5), (1784.0, 1510.0),
            (1784.0, 1525.0), (1784.0, 1545.0), (1784.0, 1570.0),
        ],
    },
    ("ladot", "870"): {
        "box": (1560, 3380, 1700, 3500),
        "path": [
            (1617.5, 3404.5), (1617.5, 3390.0), (1617.5, 3378.9),
            (1616.0, 3375.6), (1614.2, 3375.6), (1580.0, 3375.6),
            (1555.1, 3375.6), (1551.8, 3372.4), (1551.8, 3350.0),
            (1551.8, 3326.0), (1554.9, 3322.9), (1600.0, 3322.9),
            (1682.0, 3322.9),
        ],
    },
    ("montebello", "20"): {
        "box": (2186, 2080, 2276, 2146),
        "path": [
            (2271.0, 2136.5), (2260.0, 2129.4), (2252.0, 2124.9),
            (2244.0, 2120.4), (2236.0, 2115.8), (2228.0, 2111.2),
            (2220.0, 2106.6), (2212.0, 2103.0), (2220.0, 2106.6),
            (2228.0, 2111.2), (2236.0, 2115.8), (2244.0, 2120.4),
            (2252.0, 2124.9), (2260.0, 2129.4), (2271.0, 2136.5),
        ],
    },
    ("torrance", "5"): {
        "box": (1405, 3106, 1470, 3210),
        "path": [
            (1396.0, 3132.5), (1408.0, 3132.5), (1421.0, 3132.5),
            (1433.0, 3132.5),
        ],
    },
    ("longbeach", "1"): {
        "box": (1655, 2800, 1790, 2925),
        "path": [
            (1716.0, 2779.0), (1708.0, 2778.7), (1700.0, 2778.6),
            (1693.0, 2778.5), (1689.8, 2779.6), (1688.2, 2782.0),
            (1687.6, 2786.0), (1687.5, 2800.0), (1687.5, 2820.0),
            (1687.5, 2840.0), (1687.5, 2860.0), (1687.6, 2872.0),
            (1688.2, 2877.0), (1690.0, 2880.2), (1693.5, 2881.3),
            (1700.0, 2881.5), (1720.0, 2881.5), (1740.0, 2881.5),
            (1765.0, 2881.5), (1791.0, 2881.2),
        ],
    },
    ("longbeach", "2"): {
        "box": (1660, 2805, 1765, 2916),
        "path": [
            (1716.0, 2779.0), (1721.0, 2779.4), (1724.5, 2781.0),
            (1726.5, 2783.2), (1734.0, 2783.5), (1742.0, 2783.5),
            (1746.0, 2784.5), (1748.0, 2787.0), (1748.5, 2800.0),
            (1748.5, 2820.0), (1748.5, 2840.0), (1748.5, 2860.0),
            (1748.4, 2872.0), (1747.8, 2877.5), (1746.0, 2880.4),
            (1742.5, 2881.4), (1730.0, 2881.5), (1710.0, 2881.5),
            (1698.0, 2881.5), (1694.0, 2882.0), (1692.5, 2884.5),
            (1692.5, 2900.0), (1692.5, 2925.0), (1692.5, 2948.0),
        ],
    },
    ("torrance", "6"): {
        "box": (1320, 2950, 1400, 3010),
        "path": [
            (1321.0, 2941.0), (1335.0, 2941.0), (1350.0, 2941.0),
            (1362.0, 2941.0), (1366.5, 2939.0), (1368.3, 2934.0),
            (1368.5, 2925.0), (1368.5, 2910.5),
        ],
    },
    ("gtfs_bus", "344"): {
        "box": (1105, 3262, 1225, 3450),
        "path": [
            (1162.7, 3271.7), (1152.2, 3289.9), (1151.1, 3294.3),
            (1163.4, 3317.9), (1160.0, 3324.6), (1139.2, 3336.6),
            (1133.1, 3337.4), (1129.3, 3334.3), (1123.3, 3323.9),
            (1118.0, 3319.3), (1103.4, 3326.9), (1102.9, 3336.8),
            (1109.6, 3348.4), (1118.6, 3351.8), (1165.8, 3351.8),
            (1170.3, 3351.0), (1197.5, 3335.4), (1170.3, 3351.0),
            (1165.8, 3351.8), (1118.6, 3351.8),
        ],
    },
    ("bigbluebus", "4058"): {
        "box": (600, 1930, 740, 2072),
        "path": [
            (714.6, 2085.2), (657.1, 1983.5), (656.8, 1982.6),
            (656.8, 1981.8), (657.1, 1981.2), (657.9, 1980.6),
            (673.7, 1971.5), (674.4, 1970.9), (674.8, 1970.2),
            (674.8, 1969.5), (674.4, 1968.6), (666.2, 1954.5),
            (665.7, 1953.7), (665.0, 1953.4), (664.2, 1953.3),
            (663.4, 1953.7), (647.3, 1963.0), (645.9, 1964.0),
            (644.9, 1965.2), (644.4, 1966.7), (644.2, 1968.4),
            (644.2, 1986.6), (644.0, 1988.3), (643.4, 1989.8),
            (642.4, 1991.0), (641.0, 1992.1), (628.6, 1999.3),
            (628.1, 1999.4), (627.8, 1999.4), (627.6, 1999.1),
            (627.5, 1998.6), (627.5, 1921.9), (627.4, 1921.0),
            (627.0, 1920.3), (626.3, 1920.0), (625.4, 1919.8),
            (607.0, 1919.8),
        ],
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
    hundredth of a pixel can carry a whole run of points across it — jittering a
    shape by 0.02 px can swing its score by half. Score the geometry that ships,
    or the gate below can promise nothing about it."""
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
FOLD_KEEP = 0.75    # of the snapped line's arc, the least a cleanup may leave.
                    # The tests above read one fold at a time, and a route whose
                    # arms run a block apart in the warp — a one-way pair, a
                    # circulator drawn as a single line — offers a fold at every
                    # point along it, each of them ordinary. Taken together they
                    # are half the route. Stop distances are carried through this
                    # geometry, so a line that comes out short does not merely
                    # look wrong: the stops stack up on what is left of it, and
                    # the vehicle stands at one of them for the leg it should
                    # have spent driving.


def arc_length(P):
    """Total length along a polyline, px."""
    P = np.asarray(P, dtype=float)
    return float(np.hypot(*np.diff(P, axis=0).T).sum()) if len(P) > 1 else 0.0


def arm_gap(P, i, j):
    """Mean distance between the two arms of the run P[i:j+1] — twice the area
    it encloses over its own length. Near zero where the run doubles back along
    itself, a block's width where it goes round something."""
    run = P[i:j + 1]
    length = np.hypot(*np.diff(run, axis=0).T).sum()
    if length <= 0:
        return 0.0
    x, y = run[:, 0], run[:, 1]
    xr, yr = np.empty_like(x), np.empty_like(y)      # np.roll(-1), without the
    xr[:-1], xr[-1] = x[1:], x[0]                    # gather and the concatenate
    yr[:-1], yr[-1] = y[1:], y[0]
    return abs(np.sum(x * yr - xr * y)) / length


def strands_badge(before, after, badges):
    """Whether reshaping a line from `before` to `after` leaves one of the route's
    own printed badges with no path near it any more.

    A badge stands on the line it names, so a detour that is the only thing
    reaching one is a detour the sheet draws — a run out to a badge and back
    doubles over exactly the way the snapper's folds do, and is the route.
    Gated at the distance a badge is read from its line to begin with, so a
    badge the shape still passes doesn't count."""
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
    lo = np.searchsorted(cum, cum + FOLD_MIN, side="left")
    hi = np.minimum(np.searchsorted(cum, cum + FOLD_MAX, side="right") - 1, n - 1)
    # Which points come back within FOLD_GAP of which, asked once of a tree
    # rather than scanned window by window: the pairs are a handful per point,
    # and finding them by hand cost more than everything else here. The tree
    # only narrows the candidates — each is still measured with the same
    # np.hypot on the same two points, so the answer is the scan's, and the
    # slack on the radius keeps a pair sitting exactly on FOLD_GAP from turning
    # on which of the two computes the distance.
    pairs = cKDTree(full).query_pairs(FOLD_GAP * (1 + 1e-9), output_type="ndarray")
    far = np.full(n, -1)
    if len(pairs):
        pi, pj = pairs[:, 0], pairs[:, 1]
        keep = ((np.hypot(*(full[pj] - full[pi]).T) <= FOLD_GAP)
                & (pj >= lo[pi]) & (pj <= hi[pi]))
        pi, pj = pi[keep], pj[keep]
        if len(pi):
            order = np.lexsort((pj, pi))
            pi, pj = pi[order], pj[order]
            last = np.append(pi[1:] != pi[:-1], True)   # each point's furthest partner
            far[pi[last]] = pj[last]
    cands = []
    for i in np.nonzero(far >= 0)[0]:
        i, j = int(i), int(far[i])
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
    ends in a stub — the detour has no ink of its own and the snapper crushes it
    onto the line it does have. Nothing there is drawn twice, so the line runs
    out along that ink and straight back down it.

    Only the snapper's folds go, and two things speak for the route against
    taking one out. `base` is the warp the snap displaced point for point, so
    where the warp doubles back too, the retracing is the route's own. `badges`
    are the chips the sheet prints for this route, and one the line would no
    longer reach is the sheet saying it draws the route out there. The badges are
    read against the line as it stands after the folds already taken, so two that
    each look harmless can't between them strand a badge neither would have alone.

    The ink test `undetour` asks (see `_ink_vouches`) does *not* belong here,
    though it looks as though it should — it leaves total drift unchanged and
    costs path_check. The asymmetry is in what replaces a fold: an interior fold
    collapses to a chord between two points already within FOLD_GAP of each
    other, so the test can never fire, while an end fold's replacement is a whole
    leg that reads as marginally further from the strokes than the doubled-over
    pair it replaces — enough to veto precisely the fixes this pass exists for.

    An interior fold is replaced by the straight line between the points either
    side, which don't move. A fold at an *end* doubles over the run to the
    terminus, and collapsing it would leave the route stopping short of the end
    the sheet draws — so that one keeps the leg reaching the terminus and drops
    the other, stretched over the same indices."""
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
# It cannot tell one route of an agency from another. That is fine while a
# route's own line is drawn, since its own line is the nearest ink. It stops being
# fine where the sheet paints something over that line: a place label, a station
# marker, another route crossing on top. The ink under the label is missing from
# the mask, so the nearest ink for that stretch is a *sibling route*, and the
# smoothed displacement walks the path onto it and back.
#
# Nothing else in the build sees this. `maskable` covers the opposite case, where
# the sheet drew nothing at all and the warp is rightly kept; here there is
# plenty of ink and it belongs to the wrong line. And `spike_penalty` charges
# only turning that doubles back within 12 px, while the 61-point smoothing makes
# an occlusion a *smooth bulge* with no sharp turn in it.
#
# What does see it is the warp. `base` and `full` are the same points before and
# after snapping, index for index, so a stretch leaving `base` far and returning
# is the snapper having found ink the warp says is not this route's. The
# discriminators against a legitimate correction: it returns (a real fix to a bad
# warp stays moved), it is bounded in arc, and no badge vouches for it — a chip
# inside the excursion means the sheet really does print the line out there.
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
                      # that fixes it never wins, even where it takes the
                      # excursion to zero. A
                      # detour is also the worse defect of the two: it puts the
                      # vehicle on the wrong street, where a kink only makes the
                      # right one look untidy.
DETOUR_BADGE = 40.0   # px; how near a chip has to be for the path to count as
                      # sitting on it at all
DETOUR_VOUCH = 9.0    # px. A chip near the excursion is not evidence *for* it:
                      # the sheet prints them every 50-100 px, so a route is
                      # within a chip's length of one almost everywhere along
                      # itself, so a plain proximity test vetoes every detour
                      # there is. A chip only speaks for
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

    The badge test above is a coarse proxy for this: chips are printed every
    50-100 px, so a run a few hundred px long can hold two of them, and a
    flattening that trades one for the other passes even when it cuts a genuine
    corner across blank page.

    Where the PDF's own strokes are to be had they are the better witness, being
    the drawing itself, whole underneath every label painted over it. The
    agencies that most need this test have no vector ink and snap onto a colour
    mask, which has label-shaped holes in it — and the stretch a genuine detour
    should flatten back onto is exactly the stretch a label knocked out. That
    only defeats a test that reads the hole: a word covers a short piece of a run
    and the drawing resumes either side of it, so the comparison need only be
    made somewhere the hole cannot reach.

    Which is why the two versions are compared at `INK_QUANTILE` of their
    distance rather than at the median. The widened run reaches out to where the
    displacement has come back, so over its ends both versions sit on the same
    ink and a median mostly reports that agreement — loudly enough to flatten
    real corners. Reading further out is worse rather than better: at the maximum
    a single stray point decides, and the ink-snapped routes, whose strokes have
    no holes to forgive, start losing genuine fixes. 0.85 is the swept floor of
    total drift.

    What the drawing cannot answer for is ground it is deliberately kept off.
    `pdf_ink` drops every stroke inside the Downtown call-out and the other
    regions the masks skip, so *any* flattening landing there reads as leaving
    the artwork and the test would vouch for whatever the snapper did on the way
    in. So the comparison is made only where a mask could have held something,
    and a run flattening mostly into the panel is left to the badges as before."""
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
    warp itself. A shape the badges have rightly carried bodily onto its street
    sits at a steady offset from the warp for its whole length, and against an
    absolute threshold that baseline either swamps every excursion or, worse,
    never lets
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
    # entered near one end of the drawn stretch, but where the sheet ends a route
    # partway along the corridor the entry point can stand all but equidistant
    # from the path's two ends. A distance that close to tied decides nothing,
    # while the direction of travel is not close at all.
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
SLIDE_SPAN = 800.0     # px across a route's badges that one slide can stand for:
                       # the warp's error holds over a few miles of map, and a
                       # route badged over more ground than this is out by a
                       # different vector at each end


def branch_anchors(anchors, sid, sids, kd_for, slide_for):
    """The badges that belong to *this* variant of a route.

    route_anchors finds every badge the sheet prints for a route, and a shape
    gets all of them. But a shape is one variant, and where a route forks, the
    badges on one fork still fall inside the anchor gate of a variant taking the
    other, and the fit drags that variant bodily across.

    A badge is printed on one line, so it speaks for whichever variant passes
    nearest it: keep it while this shape comes within a street's width of as
    near, drop it when another explains it better. On the trunk every variant is
    equally close and they all keep it, so this only bites at a fork. A route
    with one shape has nothing to compare against and keeps the lot.

    The comparison is additive rather than a ratio, which fails in exactly the
    places badges are needed most: where the warp is good every variant is a few
    px away and a ratio is meaningless, and where it is bad every distance is
    inflated by the same local error, so the slack is measured in the error
    rather than in streets. 24 px is under the 30 that separates parallel drawn
    lines, so a badge on the next street over can't be shared.

    That slack is what the badges are read against the *slid* route for. Where
    the warp is out by about half the distance between two parallel drawn lines
    — a feed pairing two lines under one route puts a variant on each, so the
    pair's badges are printed a street apart — a badge sitting between them is
    nearer the wrong variant by less than the slack, and every variant keeps
    every badge. The error is common to the variants, so taking it off first
    leaves the comparison to be decided by the distance between the drawn lines,
    which is wider than the slack."""
    if not anchors or len(sids) < 2:
        return anchors
    A = np.asarray(anchors, dtype=float)
    A = A - slide_for(sids, A)
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
    data = np.asarray(tree.data)
    rows, mats = [], []
    for i, nb in enumerate(tree.query_ball_point(P, hood, workers=-1)):
        if len(nb) < 4:
            continue
        Q = data[nb]
        Q = Q - Q.mean(0)
        rows.append(i)
        mats.append(Q.T @ Q)
    if not rows:
        return out
    # One call for every neighbourhood rather than one per point: each
    # covariance is still built from its own pixels the same way, and eigh on a
    # stack is the same 2x2 decomposition of each of them.
    w, v = np.linalg.eigh(np.stack(mats))
    line = w[:, 1] > 3 * np.maximum(w[:, 0], 1e-9)   # a line, not a blob or a corner
    out[np.array(rows)[line]] = v[line][:, :, 1]
    return out


def bridges(C, free, tree, step, gap=BRIDGE_MAX):
    """Edges that step across an interruption in the drawing, as (rows, cols,
    weights) for the lattice graph.

    The sheet interrupts its own lines. It knocks the stroke out to make room
    for the chips it prints on them, leaving a hole a few tens of px wide, and a
    label crossing a line takes a bite out of any mask of it. Either way the
    corridor walk stops dead, `trace_anchors` gets nothing, and the stretch is
    interpolated straight: the chord across whatever corner the route turns
    there. It is the root cause behind most corners cut across blank page.

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
    # what got knocked out, where the two arms stop a couple of dozen px apart
    # and no test of "same heading" can join them, the drawing having turned
    # inside the hole. Facing each other is what they still do, so that is asked.
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


def mask_path(a, b, tree, step=TRACE_STEP, pad=TRACE_PAD, reach=TRACE_REACH,
              over_holes=False):
    """(polyline, length) of the shortest route from a to b that stays on the
    drawn mask, or (None, None) if the mask doesn't connect them.

    A coarse lattice over the two points' bounding box, cells kept where drawn
    pixels are within `reach`, 8-connected, Dijkstra. The lattice is
    deliberately blunt: drawn lines are ~8 px wide, so a 4 px pitch keeps every
    corridor connected while leaving only a few thousand nodes to search. The
    walk is only ever used to aim the snap, which then refines onto the pixels.

    The drawing is interrupted, and the walk steps across the interruptions it
    can justify — see `bridges` below. Widening `reach` instead is the wrong
    tool: at 6 px the lattice steps onto the glyphs beside a line and comes back
    with a shortcut through the words. A bridge crosses blank page only where
    the line resumes on the same
    heading, which is what an interruption looks like and a shortcut doesn't.

    over_holes offers them from the start, for a caller that can judge the
    answer's length: where a block closes round a hole the drawing still
    connects, so the ordering above never asks for a bridge."""
    key = (id(tree), round(a[0]), round(a[1]), round(b[0]), round(b[1]), over_holes)
    if key in _PATHS:                  # a route's variants share their badges
        return _PATHS[key]
    a, b = np.asarray(a, float), np.asarray(b, float)
    lo, hi = np.minimum(a, b) - pad, np.maximum(a, b) + pad
    nx, ny = (int(np.ceil((hi[k] - lo[k]) / step)) + 1 for k in (0, 1))
    C = np.stack(np.meshgrid(lo[0] + step * np.arange(nx),
                             lo[1] + step * np.arange(ny), indexing="ij"), -1)
    # Only whether each cell is within `reach` of the drawing, never how far
    # past it — so the query is bounded, which lets the tree stop descending on
    # the cells over blank page, and spread over the cores.
    free = (tree.query(C.reshape(-1, 2), distance_upper_bound=reach,
                       workers=-1)[0] < reach).reshape(nx, ny)
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

    dist, pred = solve(bridges(C, free, tree, step) if over_holes else None)
    if not np.isfinite(dist[ib]) and not over_holes:
        # Only now, and this ordering is the whole safety of it. A bridge is for
        # a corridor the drawing does not connect at all; where it does connect,
        # the drawn way round is the one to take. Offered as an ordinary edge
        # instead, a bridge cuts corners that are drawn: the two ends either
        # side of a drawn corner face each other across it, so the shortcut wins
        # on length and the route leaves the ink it was sitting on.
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
    schematic, that straight guess lands on the wrong street: a warp that is out
    by more than the width of a block settles the interpolation onto the
    neighbouring one. Walking
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
            if walk is not None and not (TRACE_DETOUR[0] * ds < length
                                         < TRACE_DETOUR[1] * ds):
                # Out of band may be a walk round a hole rather than along the
                # drawing. Ask again across the holes; the band is what keeps
                # the corner-cutting bridges out.
                w2, l2 = mask_path(A[i], A[i + 1], tree, over_holes=True)
                if w2 is not None and TRACE_DETOUR[0] * ds < l2 < TRACE_DETOUR[1] * ds:
                    walk, length = w2, l2
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


ANCHOR_GATE = 120.0   # px a badge may stand from a shape and still count for it,
                      # and so also the furthest a slide is worth searching
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
    route that is the whole story. A route running the same drawn corridor twice
    is nearest it twice, at two places far apart along its own length, and
    pinning only the nearer leaves the other pass unanchored — free to be walked
    onto a neighbouring route drawn in the same ink.

    Three conditions, and the last two keep this from firing everywhere. A pass
    counts only if the shape comes back within `slack` of the distance the
    nearest one stands at, which is under a line width. It has to have *left* in
    between — gone `rise` px from the badge and returned — or a route running
    alongside a chip for a few hundred px would be pinned to it over and over,
    each anchor demanding its own point be the one on the badge. And it has to be
    running back the other way, which is what doubling along one drawn line
    means; two legs on *parallel* streets running the same way are a street apart
    at the badge and stay one leg's, which is `crossed_badges` and `anchor_slide`'s
    case rather than this one.

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

    A route that runs out and back on parallel streets asks the warp to be nearer
    the truth than the streets are to each other, and where it isn't, every badge
    on one leg comes out nearest the other and the fit drags each leg across onto
    the other's street.

    The warp's error varies slowly over a few miles of map, so a shape's legs are
    all out by much the same vector — slide the shape by it and each badge is
    nearest its own leg again. The slide is searched on a coarse grid, then
    refined, and charged for its length, so the smallest slide that sorts the
    badges out is taken. It only decides which point of the shape each badge
    speaks for; the displacement fitted afterwards is still measured from the
    unslid shape, so the correction carries the whole error, slide included.

    Two ways of refusing: a slide wanting to run past the search bound is one the
    badges don't agree on a direction for, and a slide leaving most of the
    residual behind has only nudged them. Only a slide taking the shape from
    missing its badges to running through them is worth re-reading against.

    The bound is the anchor gate itself, because past it there is nothing to
    slide onto — a badge further from the shape than the gate does not count for
    it at any offset, so a longer reach only chases badges the fit will ignore.
    A shorter bound is worse than useless where the warp's own error exceeds it,
    which is exactly where the slide exists. Reaching that far is only safe with
    the gain read tight (0.5): looser is not a test at all once the base residual
    is hundreds of px, since a slide could leave a badge well off its line and
    still "clear" it.

    A handful of routes score worse on path_check for this and are not worse to
    look at: a shape now sitting on its drawn line is charged for retracing a
    stub the sheet actually draws, and terminus scars get traded for smaller ones
    path_check prices higher. Read drift alongside it."""
    kd = cKDTree(P)
    # A badge no offset in the search can bring within the gate scores the gate
    # in `base` and in every candidate alike. It cannot say which slide is
    # better, and the constant it adds to both sides is enough on its own to
    # fail the gain test — one such badge, and no slide is believed however
    # squarely it puts every other badge on its line. So only the badges a slide
    # could reach are scored, and the bound is twice the gate: the gate itself,
    # plus the furthest the search may carry the shape.
    A = np.asarray(A, dtype=float)
    A = A[kd.query(A)[0] < 2 * gate]
    if len(A) < 2:
        return np.zeros(2)
    base = np.minimum(kd.query(A)[0], gate).sum()
    t, resid = np.zeros(2), base
    span = gate
    for pitch in ANCHOR_PITCH:
        g = np.arange(-span, span + pitch / 2, pitch)
        T = t + np.stack(np.meshgrid(g, g, indexing="ij"), -1).reshape(-1, 2)
        # Everything past the gate scores the gate whatever its distance, so
        # the search never has to find it: bounding the query returns inf for
        # exactly those and lets the tree stop looking, which is most of the
        # coarse grid's thousand offsets.
        d = kd.query((A[:, None, :] - T[None, :, :]).reshape(-1, 2),
                     distance_upper_bound=gate, workers=-1)[0]
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
                  anchor_gate=ANCHOR_GATE, min_frac=0.5, tail=(10.0, 11), region="main",
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
    didn't have — a badge it couldn't reach, *or* a corridor it couldn't walk.
    A badge only counts for a shape it passes within `anchor_gate` of, and where
    the warp is poor the badges that would fix it start out beyond that; one fit
    on the badges it can see brings the rest within reach. This is self-limiting
    in a way that widening the gate is not, since a badge on another branch stays
    far from the corrected line and never joins in. The walks need it for the
    same reason and are the half a badge count cannot see: `trace_anchors`
    believes a walk only when its length matches what the shape says, and on the
    first pass the shape saying so is the warp.

    tail: (cap, window) for one last pass with a short smoothing window. The wide
    window that keeps whole stretches together also averages the correction
    across junctions, leaving the line sagging a px or two off the artwork where
    parallel routes crowd it. The cap is kept below the spacing of neighbouring
    drawn streets, so this can only refine within the corridor the wide passes
    chose.

    Raising that cap is a trap, and drift_check will recommend it: every wider
    value cuts total distance from the drawn lines, because most stretches really
    are a few px short of their ink. It also breaks routes outright. The cap
    bounds which points may *contribute* a displacement, not how far the pass may
    carry one — the field is interpolated across non-contributing points and then
    smoothed, so more contributors let a stretch with no ink of its own be
    dragged by its neighbours, out across blank page. Judge a change like this on
    routes, not on the total.

    speckled: whether the tree came out of the raster, and so needs the final
    landing guarded against stray pixels. A tree of PDF strokes does not.

    sole: the mask holds this one route's drawn line and nothing else, so
    whatever it finds is this route's, and the regions the mask skips stop being
    a reason to leave a point where it is. Ordinarily they are — a point the
    sheet drew nothing under has no correction of its own, and interpolating one
    carries the line off into blank page. But that failure is a point dragged
    onto a *neighbour's* line, and a one-line mask has no neighbour. The Downtown
    call-out needs this, its legend box otherwise vetoing every point under it.
    `min_frac` still counts only what the mask could cover, so a route the panel
    doesn't draw keeps its warp."""
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
            # made converting one into the other look like standing still: a
            # corner can go in as one aligned node on the first pass and be
            # believed outright on the second, laying several — with the walk
            # *count* unchanged, so the loop stopped and threw away the better
            # fit it had just computed.
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
        # the stretch piles up against whatever is nearest the far side. Those
        # points keep the warp, and the smoothing below ramps the correction
        # down to them.
        cov = maskable(P, region)
        ok = (d < cap) & (cov | sole)
        if ci == 0:
            # "mostly undrawn" is judged only over the stretch a mask could
            # cover. A route running a large part of its length under the title
            # banner, or any other excluded region, is otherwise failed out of
            # snapping altogether and left on a warp that may be far off its
            # drawn line.
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


def span_points(P, span):
    """`span` px of line, as an odd number of points of the polyline `P`.

    A window over a fitted shape is a length of line, but the arrays are
    indexed by point, and how much line a point stands for is the feed's
    business rather than ours: densify() puts a ceiling on the step and nothing
    under it, so a feed drawing its shapes finely keeps its own spacing — under
    a px for some feeds, three for others. A window counted in points is
    therefore a different window on every feed."""
    if len(P) < 3:
        return 3
    step = float(np.hypot(*np.diff(P, axis=0).T).mean())
    w = int(round(span / max(step, 1e-6))) | 1
    return max(3, min(w, (len(P) // 2) * 2 - 1))


JITTER_SPAN = 28.0    # px of line a fitted shape is averaged over. Long enough
                      # to outrun the tracing wander, short enough to leave the
                      # corner between two straight runs where the sheet put it.


JITTER_KEEP = 6.0     # px of drawing the smoothing may give up before it is
                      # charged for it. Above the wander's own amplitude, so
                      # removing the wander is never what gets vetoed, and well
                      # under the legs of a drawn detour, so a corner is.


def unjitter(P, span=JITTER_SPAN, tree=None):
    """A fitted shape with the feed's own tracing wander taken out of it.

    A GTFS shape is a driven trace, and at map scale its vertices wander a
    couple of px either side of the street. That is inside the width of the
    line the sheet draws, so the fit carries the whole wander onto the drawn
    line rather than flattening it, and a corridor drawn dead straight ships as
    a zigzag. Nothing else takes it out and no check reports it: every reversal
    is wide enough for simplify() to keep, and far too shallow for path_check,
    which scores turning past a square corner.

    Averaging the line over a stretch longer than the wander is what removes
    it. The span is a length rather than a count of points — see span_points.

    `tree` is the drawing this shape was fitted on, and it is what tells a
    wander from a corner: a span long enough to outrun the one is longer than
    the legs of a detour the schematic draws tight — a circulator's few
    blocks — and averaging across those rounds the corners off the artwork.
    So the average is taken as far as it does not cost the line its drawing:
    each point keeps whatever share of the correction leaves it no further
    from the ink than the wander it removes, and a corner where the smoothed
    line would leave the drawing keeps its own place instead. The share falls
    off smoothly rather than switching, since a step in the correction is a
    kink in the line. Off the drawing entirely — a stretch the sheet doesn't
    draw — there is nothing to give up and the average stands."""
    P = np.asarray(P, dtype=float)
    w = span_points(P, span)
    k = np.ones(w) / w
    out = np.empty_like(P)
    for c in (0, 1):
        out[:, c] = np.convolve(np.pad(P[:, c], w // 2, mode="edge"), k, "valid")
    if tree is None:
        return out
    lost = tree.query(out)[0] - np.maximum(tree.query(P)[0], JITTER_KEEP)
    share = 1.0 / (1.0 + np.maximum(lost, 0.0) / JITTER_KEEP)
    return P + share[:, None] * (out - P)


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
    blank page. Points lying on neither ribbon nor platform are cut back to the
    artwork.

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
    stops early, a station or so before the end, and the last stretch of track
    is left bare. This walks the mask outward from the endpoint and returns the
    piece to append.

    The lattice is the one mask_path() walks on, with two differences. Cells
    within TAIL_BLOCK of the stretch already covered are cut, all but a short
    window by the end itself, so the walk can only head away from the line
    rather than doubling back along it; and a drawn platform counts as track,
    since the white marker interrupts its own ribbon by a dozen px or so and the
    walk has to cross that to reach the terminus behind it. The farthest inked
    cell ahead of the endpoint is the target; standing
    the line in the platform there is rail_platform()'s job.

    Two gates keep the walk from inventing track. Only ink inside a narrow cone
    off the heading the line arrived on counts as the line carrying on, so track
    that turns away is not followed — a terminal loop the drawn line runs both
    ways round would otherwise be walked into. And a walk longer than `limit` says
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
    Snapping each point to its own nearest track pixel leaves hooks where the
    track curves; the passes tighten the cap so the second reels in what the
    first left bulging. Runs
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
    more than the stops are apart, so two stops double-book one platform and
    everything past them shifts a station early. The
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
# is four times the pixels, and the corner by Union Station is still tens of px
# out. A mask holding every bus line in the panel at once gets the short reach
# it always had, since a longer one would only find a neighbour sooner. Both take the shorter window — it is the magnified grid's
# right-angle turns that want it, not the livery.
INSET_CAPS = (60.0, 30.0, 14.0)
INSET_SOLE_CAPS = (120.0, 60.0, 30.0, 14.0)
INSET_WIN = 15


INSET_COLORS = {"ladot": [(107, 103, 61), (128, 126, 85)]}

# Metro's bus orange as the call-out prints it. ORANGE is that colour after
# map.png's reduction has blended it with the page, which is 30 away and what
# every mask read off the raster has to match; the pyramid has the ink itself.
INSET_ORANGE = (245, 132, 70)


# Diversions the call-out doesn't draw, in inset px. A feed sometimes routes
# some workings of a route off the line the sheet gives it — round a closure,
# usually — and the panel is the one view that draws the difference, on streets
# it never badges the route along. The box brackets the stretch; the run of the
# shape inside it is replaced by its own chord, so a variant that keeps to the
# corridor is left exactly as it was and only the diverted one is straightened.
# Which workings divert, and why, belongs to the feed and changes with it.
INSET_DIVERSIONS = {
    ("gtfs_bus", "18"): [(3550, 2980, 3710, 3100)],
}
DIVERSION_BULGE = 25.0   # px off its own chord before a run counts as diverted


def undivert(pts, boxes):
    """`pts` with any boxed run that bulges off its chord flattened onto it."""
    P = np.asarray(pts, dtype=float)
    for x0, y0, x1, y1 in boxes:
        k = np.nonzero((P[:, 0] >= x0) & (P[:, 0] <= x1)
                       & (P[:, 1] >= y0) & (P[:, 1] <= y1))[0]
        if len(k) < 3:
            continue
        lo, hi = int(k[0]), int(k[-1])
        a, b = P[lo], P[hi]
        n = b - a
        L = float(np.hypot(*n))
        if L < 1e-6:
            continue
        u, V = n / L, P[lo:hi + 1] - a
        off = np.abs(u[0] * V[:, 1] - u[1] * V[:, 0])
        if off.max() < DIVERSION_BULGE:
            continue
        t = np.linspace(0, 1, hi - lo + 1)[:, None]
        P[lo:hi + 1] = a + t * n
    return P


def inset_runs(ll, main_dist, snap_tree=None, anchors=None, sole=False,
               boxes=()):
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
    # run, and when they disagree it draws nothing, so a line whose warp grazes
    # a few px past the frame edge vanishes from the panel for that stretch.
    # So fill in gaps the route never really left
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
        if boxes:
            pts = undivert(pts, boxes)
        if snap_tree is not None:
            # the same coherent snap as the main map, scaled for the magnified
            # inset: a short smoothing window keeps the grid's right-angle
            # turns square
            sc = snap_coherent([tuple(p) for p in pts], snap_tree,
                               caps=INSET_SOLE_CAPS if sole else INSET_CAPS,
                               win=INSET_WIN, anchors=anchors, anchor_gate=75.0,
                               min_frac=0.35, region="inset", sole=sole)
            if sc is not None:
                pts = unjitter(np.asarray(sc), tree=snap_tree)
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
    it, scoring a flat 0 while visibly off its line. Scored on that alone,
    `undetour` can never win a shape it is the only fix for — it ties,
    and the tie goes to the snapper. So the excursion is priced too, and the
    winner minimises both.

    The old promise still holds, and is explicit: a candidate that would rank
    worse on `spike_penalty` than the snapper's own shape is thrown out before
    it can be scored, so nothing buys a straighter line at the cost of a
    hairpin. A candidate that has lost more than FOLD_KEEP of the line's arc
    goes the same way, and for the same reason: the folds each read as the
    snapper's, but a route drawn as one line and driven as two offers one at
    every point along it, and flattening the lot leaves a line too short for
    the timetable it carries."""
    as_snapped = full
    spike0 = stored_penalty(as_snapped)
    floor = FOLD_KEEP * arc_length(as_snapped)
    best = spike0 + DETOUR_WEIGHT * detour_penalty(as_snapped, base, anc, line_ink)
    unfolded = unfold(as_snapped, base, anc)
    undet = undetour(as_snapped, base, anc, line_ink)
    # Both hand back the shape they were given where there was nothing to take
    # out, and the ballot then asks for the same cleanup of the same values two
    # or three times over. A candidate equal to the snapper's own shape loses
    # below in any case, and one equal to an earlier candidate cannot beat it
    # on a strict improvement, so neither is built.
    cands = [despike(as_snapped)]
    if not np.array_equal(unfolded, as_snapped):
        cands += [unfolded, despike(unfolded)]
    if not np.array_equal(undet, as_snapped):
        cands += [undet, despike(undet), unfold(undet, base, anc)]
    for cand in cands:
        if np.array_equal(cand, as_snapped):
            continue
        if arc_length(cand) < floor:
            continue
        spike = stored_penalty(cand)
        if spike > spike0:
            continue
        penalty = spike + DETOUR_WEIGHT * detour_penalty(cand, base, anc, line_ink)
        if penalty < best:
            full, best = cand, penalty
    return full


# One agency's — or one route's — shapes, fitted the way the full build fits
# them and written as a `schedule.json`-shaped stub `debug_line.py --schedule`
# can draw. A full build is around two minutes, and almost all of it is shapes
# the change under test cannot reach; a refit is seconds. It runs the build's
# own code rather than a copy, so the fast path cannot answer a question the
# slow one wouldn't.
REFIT = None            # (feed, route token or None, output path) while refitting
SHAPE_CACHE = "scratch/shape-cache"

# Which shapes a feed actually runs is settled by the timetable — a trip with
# fewer than two timed stops contributes none — and the colour a feed is masked
# on is refined off the first twenty shapes it does run (`refine_color`). So a
# refit that guessed the set from trips.txt alone could mask on a different
# colour and answer a question the build never asked. The set is cached
# instead: every full build writes it, a refit reads it, and a cold or stale
# one falls back to reading the stop times as usual.


def used_shapes_stamp(feed):
    """What a feed's set of live shape ids depends on: its trips, its stop
    times, its calendar, and the week being built."""
    h = hashlib.sha1(str(TARGET).encode())
    for name in ("trips.txt", "stop_times.txt", "calendar.txt", "calendar_dates.txt"):
        path = f"{GTFS}/{feed}/{name}"
        st = os.stat(path) if os.path.exists(path) else None
        h.update(f"{name}:{st.st_size if st else 0}:{st.st_mtime_ns if st else 0}"
                 .encode())
    return h.hexdigest()


def cached_used_shapes(feed):
    path = f"{SHAPE_CACHE}/{feed}.json"
    try:
        with open(path) as f:
            blob = json.load(f)
    except (OSError, ValueError):
        return None
    return set(blob["shapes"]) if blob.get("stamp") == used_shapes_stamp(feed) else None


def store_used_shapes(feed, used):
    os.makedirs(SHAPE_CACHE, exist_ok=True)
    with open(f"{SHAPE_CACHE}/{feed}.json", "w") as f:
        json.dump({"stamp": used_shapes_stamp(feed), "shapes": sorted(used)}, f)


def write_refit(path, feed, shapes_raw, route_by_shape, route_idx, routes,
                systems, trip_counts):
    """The refitted shapes as the keys `debug_line.py` reads, and no others: a
    refit has no timetable, so the stop distances are empty and the call-out
    runs are absent. Trip counts come from trips.txt, which is enough to keep
    the variants in the order the full build lists them."""
    keys = [k for k in shapes_raw if k[0] == feed]
    shapes, patterns, trips = [], [], []
    for i, key in enumerate(keys):
        P = np.asarray(shapes_raw[key])
        shapes.append([round(v, 1) for xy in P for v in xy])
        patterns.append({"s": i, "d": []})
        ridx = route_idx.get((feed, route_by_shape.get(key[1])))
        if ridx is not None:
            trips += [[ridx, i, 0]] * max(1, trip_counts.get(key[1], 1))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump({"date": TARGET.strftime("%Y%m%d"), "systems": systems,
                   "routes": routes, "shapes": shapes, "patterns": patterns,
                   "trips": trips, "tripDays": [0] * len(trips),
                   "insets": [None] * len(shapes),
                   "insetRect": list(INSET_RECT)}, f, separators=(",", ":"))
    print(f"refit -> {path}  ({len(shapes)} shapes)\n"
          f"  scripts/debug_line.py <line> --schedule {path} --no-stops")


def main():
    refit_feed, refit_route, refit_out = REFIT or (None, None, None)
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
    shape_route = {}                # (feed, shape_id) -> route id, sans suffix
    stats = defaultdict(int)

    for feed in FEEDS:
        if refit_feed and feed != refit_feed:
            continue
        if not os.path.isdir(f"{GTFS}/{feed}"):
            print(f"{feed}: missing, skipped")
            continue
        is_metro = feed in ("gtfs_rail", "gtfs_bus")

        trip_rows = read_csv(feed, "trips.txt")
        tps = defaultdict(int)
        for row in trip_rows:
            tps[row["service_id"]] += 1
        days = pick_dates(feed, tps)
        if not any(days):
            print(f"{feed}: no usable service date, skipped")
            continue
        # Which weekdays each service_id runs on, as a bitmask over the seven
        # dates above. A trip is emitted once and carried by every day it runs.
        dow_of = defaultdict(int)
        for i, d in enumerate(days):
            if d is None:
                continue
            for s in active_services(feed, d):
                dow_of[s] |= 1 << i

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
                # And badged by number, not by letter: route_label takes the
                # first token of the long name and stops, which leaves these
                # vehicles carrying a letter the sheet prints nowhere. This is
                # the case route_label's docstring describes and cannot reach
                # here, the designation being four characters already.
                #
                # "910" of the pair, for the reason route_label prefers the
                # first part of a split designation — it is what the sheet
                # prints, and the working most of these trips are. It costs the
                # longer working a badge, the sheet printing only "950" there.
                label = "910"
                # It costs them the *sprite*, at least. As an anchor the sheet's
                # other number is wanted whatever the vehicles are labelled, and
                # nothing else here can supply it: Metro leaves
                # route_short_name empty on the busways and writes the numbers
                # into the long name, so a shape's tokens would be its label
                # and nothing besides. The other number is the only thing on the
                # sheet that says where this line goes past the point the
                # station names run out — see the busway snap below.
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

        trip_info, trip_dow = {}, {}
        for row in trip_rows:
            if row["service_id"] not in dow_of:
                continue
            sid = row.get("shape_id", "")
            if feed == "metrolink":
                sid = METROLINK_SHAPES.get((row["route_id"], row.get("direction_id", "")), sid)
            trip_info[row["trip_id"]] = (row["route_id"], sid)
            trip_dow[row["trip_id"]] = dow_of[row["service_id"]]

        def route_index(rid):
            """This feed's route, registered on first sight."""
            key = (feed, rid)
            if key not in route_idx:
                label, color, text, rail = rmeta[rid]
                if feed not in system_idx:
                    system_idx[feed] = len(systems)
                    systems.append(FEED_NAMES.get(feed, feed))
                route_idx[key] = len(routes)
                routes.append({"n": label, "c": "#" + color, "t": "#" + text,
                               "rail": rail, "sy": system_idx[feed]})
            return route_idx[key]

        # The busways are pinned by the station names printed beside them, and
        # those come out of the timetable; every other fit reads nothing the
        # stop times carry, so a refit can take the shape set from the cache
        # and leave the largest file in the feed unread.
        cached = None
        if refit_feed and not (feed == "gtfs_bus" and (
                refit_route is None or refit_route.split("-")[0] in ("901", "910"))):
            cached = cached_used_shapes(feed)

        stop_times = defaultdict(list)
        if cached is None:
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
        used_shapes = set(cached or ())
        for rid, sid in trip_info.values() if cached else ():
            if sid in used_shapes and rid in rmeta:
                route_index(rid)
        for ti, sts in stop_times.items():
            if len(sts) < 2:
                continue
            rid, sid = trip_info[ti]
            sts.sort()
            route_stops.setdefault((feed, rid), set()).update(s for _, _, _, s in sts)
            # A bus laying over at its origin before it enters service is not yet
            # a vehicle anyone can ride, and drawing it parked there for the
            # length of the layover pools whole fleets motionless on the
            # terminals that time an origin early — some feeds give the first
            # stop an arrival_time a median 15 minutes, and up to two hours,
            # before its departure_time. The trip starts when it departs, so the
            # origin is timed by its departure; every later stop
            # keeps its arrival (arrival and departure are equal there anyway, so
            # this changes nothing downstream). Clamped so a malformed feed whose
            # departure trails the next arrival can't make the clock run backward.
            times = [t for _, t, _, _ in sts]
            if len(times) > 1:
                times[0] = min(sts[0][2], times[1])
            stop_seq = tuple(s for _, _, _, s in sts)
            ridx = route_index(rid)
            pkey = (feed, sid, stop_seq)
            if pkey not in pattern_idx:
                pattern_idx[pkey] = len(patterns)
                patterns.append(pkey)
            trips_out.append((ridx, pkey, times, trip_dow[ti]))
            used_shapes.add(sid)
        if cached is None:
            store_used_shapes(feed, used_shapes)

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
        # Which routes a `--only feed:route` refit fits. The token is matched
        # against the route id, the id without its variant suffix, and the
        # designation the sheet prints, since a feed's own ids are opaque.
        refit_ids = None
        if refit_route is not None:
            want = refit_route.lower()
            refit_ids = {r for r in set(route_by_shape.values())
                         if want in {r.lower(), r.split("-")[0].lower(),
                                     rmeta[r][0].lower() if r in rmeta else ""}}
            if not refit_ids:
                sys.exit(f"--only {feed}:{refit_route}: no route of that name")
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
        # of their shapes, not just one route's variants: every instance of the
        # symbol is a candidate anchor for every route the operator runs, so one
        # printed on a neighbour's leg will drag this route off its own line.
        label_sids = defaultdict(list)
        for sid in warped:
            r = route_by_shape.get(sid)
            label_sids[rmeta[r][0] if r in rmeta else r].append(sid)
        _kd, _slide = {}, {}

        def kd_for(sid):
            if sid not in _kd:
                _kd[sid] = cKDTree(np.asarray(densify(warped[sid], 4.0), dtype=float))
            return _kd[sid]

        def slide_for(sids, A):
            """The warp's local error where a route's badges are, as the one
            translation that best puts its whole drawing on them — the reading
            branch_anchors weighs the variants against.

            Fitted over every variant at once, so it is the error they share
            rather than one of them pulled onto another's badges, and taken only
            where one vector can speak for the error at all (SLIDE_SPAN).
            anchor_slide refuses a slide the badges don't agree on a direction
            for, so a route whose variants disagree keeps its warp and is read
            as it stands."""
            key = tuple(sids)
            if key not in _slide:
                span = np.hypot(*(A.max(0) - A.min(0))) if len(A) else 0.0
                _slide[key] = (anchor_slide(np.vstack([kd_for(s).data for s in sids]),
                                            A, ANCHOR_GATE)
                               if span <= SLIDE_SPAN else np.zeros(2))
            return _slide[key]

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

        snapped = anchored = fitted = 0
        for sid, pts in warped.items():
            # Only the fit is narrowed, never `warped`: the colour this feed is
            # masked on is refined off the shapes it runs (`refine_color`), so
            # a route fitted alone has to be fitted against the whole agency's
            # reading of its own ink.
            if refit_ids is not None and route_by_shape.get(sid) not in refit_ids:
                continue
            fitted += 1
            out_pts, anc, can_refit = None, [], False
            # The drawing this shape was snapped on: the PDF's strokes where it
            # has them, its agency's colour mask where it does not. It is the
            # arbiter of whether a detour is really the drawn line — see
            # _ink_vouches, which reads it far enough out along the run that a
            # mask's label-shaped holes cannot answer for the whole of it.
            line_ink = None
            rid = route_by_shape.get(sid)
            shape_route[(feed, sid)] = (rid or "").split("-")[0]
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
                # snap the same way: on that ink, pinned by the station names
                # printed beside it. A colour mask is no use for the J Line —
                # its drawn gray is a rounding from the freeway's in map.png, so
                # the mask would take every freeway on the sheet with it, and
                # the raw warp stood instead. The PDF has no such collision, so
                # there was never anything to snap onto except the wrong thing
                # to read it from. See JLINE_INK.
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
                    # The busway is pinned by station names beside the ribbon
                    # rather than by badges on it, so it has no use for a mask
                    # and every use for being rid of one: on the raster its
                    # orange is nearly the streets', and even read off the tiles
                    # (where the two stay 57 apart) the badge chips' antialiased
                    # fringe blends into tolerance and bends the line. The ink
                    # has one stroke per drawn line and no chips at all.
                    tree = (ink or tile_tree(cols, BUSWAY_TOL)) if busway else mask_tree(cols)
                    snap_tree = ink or tree
                    line_ink = ink
                    # no color gate: Metro's orange badges render with variable
                    # fade (crisp ~1 px from orange, faded ~70), overlapping
                    # muted foreign badge colors, so a color test drops genuine
                    # ones. Metro's number badges are dense and its anchoring
                    # was already tuned without it.
                    if busway:
                        # Station names, plus any numbers printed along the
                        # ribbon. The names run out where the stations do, and
                        # past the last one a snap cannot recover a switchback
                        # on its own — every point of the chord across one is
                        # already sitting on some part of it. Only a walk
                        # between two anchors does (`trace_anchors`), so the
                        # numbers are what bracket that stretch.
                        #
                        # Numbers only: the G Line's designation is a letter,
                        # and the sole "G" the sheet sets is a caption standing
                        # 4 px from the ribbon it captions.
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
                    anc = branch_anchors(anc, sid, route_sids[rid], kd_for,
                                         slide_for)
                    anchored += bool(anc)
                    can_refit = not busway
                    out_pts = snap_recording(pts, snap_tree, anchors=anc,
                                            caps=(BUSWAY_CAPS if busway else
                                                  INK_CAPS if ink else None),
                                            win=BUSWAY_WIN if busway else 61,
                                            speckled=ink is None)
                    # The busway is drawn the way a rail line is — its own
                    # ribbon, ending at a drawn platform — so its ends are
                    # squared against that ribbon the same way. It needs it more
                    # than rail does: out in the Valley the warp's error runs
                    # *along* the busway as much as across it, which a sideways
                    # snap cannot answer for, so every end landed somewhere
                    # other than its terminus.
                    #
                    # Squaring trims and extends, so the result is resampled
                    # back to the point count it came in with, keeping it
                    # index-aligned with the warp — which is what carries the
                    # stops over (see below). Handing those stops to
                    # platform_stops instead, as rail does, is worse: the warp
                    # lags by half the distance between stations here, so the
                    # alignment minimising total offset is the one putting every
                    # station on the platform *before* its own.
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
                # Commuter Express needs pinning where the warp is worst — out
                # at the coast it exceeds the length of the leg it is displacing,
                # so the leg has no way to tell which end of the drawn line is
                # which. A DASH is pinned only by the
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
                anc = branch_anchors(anc + pins, sid, route_sids[rid], kd_for,
                                     slide_for)
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
                        label_sids[rmeta[rid][0] if rid in rmeta else rid], kd_for,
                        slide_for)
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
                # an agency's *own* colours must never play that rival. `good`
                # is refined off the drawn lines and drifts a dozen px from the
                # legend seed it started at — and that seed is still in the rival
                # palette, sitting nearer the agency's own chip than the refined
                # colour does, so it out-explains `good` and the gate throws out
                # the agency's own badges. Folding the seeds into the own-set
                # keeps them off the rival list; a genuinely foreign chip is
                # still far from every one of them and still rejected.
                gate_cols = anchor_cols + LEGEND_SEEDS.get(feed, [])
                anc = branch_anchors(
                    route_anchors(toks, anchor_tree, colors=gate_cols) + pins,
                    sid, route_sids[rid], kd_for, slide_for)
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
                # No cleanup is taken on faith: straightening one spike can
                # leave a sharper residual where it met a bend, and simplify()
                # can turn a helped dense path into a worse stored one. Every
                # candidate is scored on the *stored* geometry the animation
                # plays, by the measure path_check ranks on, and the best wins
                # with the snapper's own shape taking ties — so no shape comes
                # out worse than it went in. Each pass stands on the ballot
                # alone as well as combined, since one run unconditionally ahead
                # of another can rob it of a better answer.
                #
                # Scored on two measures. `spike_penalty` charges only turning
                # that doubles back inside 12 px, and the 61-point smoothing
                # makes an occluded stretch a smooth bulge with no sharp turn in
                # it — so on that alone `undetour` could never win a shape it is
                # the only fix for. The excursion is priced too, and the winner
                # minimises both. A candidate ranking worse than the snapper's
                # own shape on `spike_penalty` is thrown out before scoring, so
                # nothing buys a straighter line at the cost of a hairpin.
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
            # The ballot above picks between fits on how sharply they turn,
            # and a smoothed line reads differently enough there to change
            # which one ships, so it smooths after. An override's path is drawn
            # by hand, so it goes on after this rather than through it.
            if out_pts is not None:
                full = unjitter(full, tree=line_ink)
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
        picked = [d for d in days if d]
        if refit_feed:
            print(f"{feed}: {snapped}/{fitted} shapes snapped, {anchored} anchored")
            write_refit(refit_out, feed, shapes_raw, route_by_shape, route_idx,
                        routes, systems,
                        Counter(s for _, s in trip_info.values()))
            return
        print(f"{feed}: {n_trips} trips over {min(picked)}..{max(picked)} "
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
        stops carried over, and a shape shifting a few tens of px inside the
        call-out — where nothing is drawn and the geometry is the warp's own
        noise — was enough to put a stop outside its own run and drop a whole
        network out of the panel."""
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
                    # one orange for every Metro bus line down there. The Rapid
                    # does keep a red ribbon of its own, but drawn beside the
                    # orange on the same street rather than instead of it, and
                    # the orange is the denser thing to snap on.
                    cols = [INSET_ORANGE]
                elif cols and key[0] in INSET_COLORS:
                    cols = INSET_COLORS[key[0]]
                # Metro's networks are masked on the pyramid, where the panel
                # prints its colours faithfully and its badge chips come away
                # from the lines — see inset_tile_tree. Every other agency is
                # masked on the reading its colour was refined from.
                tree = (inset_tile_tree(cols)
                        if cols and key[0] in ("gtfs_rail", "gtfs_bus")
                        else mask_tree(cols, tol, region="inset") if cols else None)
                # No badge anchors down here. They exist to pull a shape onto
                # its street where the warp is out by more than the streets are
                # apart, and the panel's is not: a chip is printed *beside* its
                # line, so anchoring on one now drags the line off the ink it
                # was already sitting on.
                runs = inset_runs(ll, lambda px: main_dist(key, si, px), tree,
                                  sole=key[0] == "gtfs_rail",
                                  boxes=INSET_DIVERSIONS.get(
                                      (key[0], shape_route.get(key)), ()))
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

    # Every trip of the week, once, with the weekdays it runs on as a bitmask
    # (bit 0 = Sunday) alongside; the client picks a day by filtering on it.
    # Rows identical in route, pattern and every time are merged, so a working
    # that keeps to one timetable all week carries several day bits instead of
    # costing several rows — feeds give each day its own trip ids, and without
    # the merge the file would be most of seven whole timetables.
    trips_final, trip_days = [], []
    seen = {}

    def emit(row, dows):
        i = seen.setdefault(tuple(row), len(trips_final))
        if i == len(trips_final):
            trips_final.append(row)
            trip_days.append(0)
        trip_days[i] |= dows

    for ridx, pkey, times, dows in trips_out:
        pi = pattern_idx[pkey]
        if patterns_out[pi] is None:
            continue
        t0 = times[0]
        deltas = [times[k] - times[k - 1] for k in range(1, len(times))]
        emit([ridx, pi, t0] + deltas, dows)
        if times[-1] > 86400:
            # The tail of a trip that crosses midnight belongs to the *next*
            # day: the vehicles running at 01:00 are yesterday's last workings.
            emit([ridx, pi, t0 - 86400] + deltas, (dows << 1 | dows >> 6) & 0x7F)
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
           "tripDays": trip_days,
           "insets": insets_out, "insetRect": list(INSET_RECT)}
    with open("schedule.json", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    stats["routes"] = len(routes)
    stats["shapes"] = len(shapes_out)
    stats["patterns"] = len(patterns_out)
    stats["trips_total"] = len(trips_final)
    print(dict(stats))
    print("per weekday: " + ", ".join(
        f"{n} {sum(1 for m in trip_days if m >> i & 1)}" for i, n in enumerate(
            ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"])))
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
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", metavar="FEED[:ROUTE]",
                    help="fit one feed, or one of its routes, and write the "
                         "shapes to a debug_line stub instead of a full build")
    ap.add_argument("-o", "--out", metavar="PATH",
                    help="where --only writes (default scratch/refit_<feed>.json)")
    a = ap.parse_args()
    if a.only:
        _feed, _, _route = a.only.partition(":")
        if _feed not in FEEDS:
            sys.exit(f"--only: no feed {_feed!r}; one of {', '.join(FEEDS)}")
        REFIT = (_feed, _route or None, a.out or f"scratch/refit_{_feed}.json")
    elif a.out:
        sys.exit("--out goes with --only; a full build writes schedule.json")
    main()
