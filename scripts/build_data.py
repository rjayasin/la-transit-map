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
import csv, json, math, os, sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import numpy as np
from PIL import Image
from scipy import ndimage as ndi
from scipy.spatial import cKDTree

sys.path.insert(0, "scripts")
from georef import EXCLUDE, ROUTE_COLORS, TOL, load_masks  # noqa: E402
from georef_inset import GEO as INSET_GEO, LEGEND as INSET_LEGEND, RECT as INSET_RECT  # noqa: E402

TARGET = date(2026, 7, 22)  # a Wednesday inside the Metro JUNE26 calendar window
GTFS = "data/gtfs"
# gtfs_rail first so Metro trains draw config (snap) is applied; order otherwise cosmetic
FEEDS = ["gtfs_rail", "gtfs_bus", "bigbluebus", "culvercity", "ladot", "longbeach",
         "foothill", "torrance", "norwalk", "montebello", "gtrans", "pasadena",
         "burbank", "beachcities", "metrolink"]
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


def route_label(short, long_name):
    s = (short or long_name or "?").strip()
    for pre in ("Metro ", "Metrolink "):
        if s.startswith(pre):
            s = s[len(pre):]
    if s.endswith(" Line"):
        s = s[:-5]
    tok = s.split()[0]
    if len(tok) <= 4:
        return tok
    t2 = tok.replace("-", "").replace("/", "")
    if len(t2) <= 4:
        return t2
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

LABEL_BRIDGE = 28   # px; half-width of the largest label gap worth closing


def bridge_label_gaps(m, sub, d2a, tol, radius=LABEL_BRIDGE):
    """Re-add drawn-line pixels that a place-name label is painted over.

    Labels sit on top of the artwork, so a color mask breaks wherever a name
    crosses a line — "WEST HOLLYWOOD" puts a ~40 px hole in Metro 2's Sunset
    line. The snap then locks onto whichever parallel street stays unbroken
    (Metro 2 was landing a block south). Morphologically close the mask and
    keep the filled pixels only where label text actually is, so gaps get
    bridged but genuine line ends stay ends.

    Label text is gray, and so are several agencies' drawn lines (Torrance,
    Big Blue Bus, Beach Cities). Pixels anywhere near the color being masked
    are therefore not eligible as "text", or an agency's own artwork would be
    read as a label and dilated into the mask wholesale."""
    mx, mn = sub.max(axis=2), sub.min(axis=2)
    text = ndi.binary_dilation((mx - mn) < 26, np.ones((5, 5), bool)) & (mx < 215)
    text &= d2a > (tol * 1.6) ** 2
    k = 2 * radius + 1
    closed = m
    for st in (np.ones((1, k), bool), np.ones((k, 1), bool)):
        closed = ndi.binary_dilation(closed, st)
    for st in (np.ones((1, k), bool), np.ones((k, 1), bool)):
        closed = ndi.binary_erosion(closed, st)
    return m | (closed & text)


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
        for bg in bg_palette():
            if min(((np.array(c) - bg) ** 2).sum() for c in colors) < 24 * 24:
                continue                       # bg IS this agency's color; keep
            m &= d2a < ((sub - bg) ** 2).sum(axis=2)
        # after the background filter: bridged pixels are label gray, which
        # every background test would reject
        m = bridge_label_gaps(m, sub, d2a, tol)
        ys, xs = np.nonzero(m & keep)
        _TREES[key] = cKDTree(np.c_[xs + x0, ys + y0]) if len(xs) > 300 else None
    return _TREES[key]

# Metro's drawn line colors (sampled from the map)
ORANGE = (217, 129, 83)     # Metro Local/Rapid orange
RAPID_RED = (180, 51, 61)   # 720/754/761
BUSWAY_GRAY = "969CA0"      # J Line 910/950 freeway busway ribbon

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
    for t in tokens:
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


def snap_coherent(pts, tree, caps=None, win=61, anchors=None,
                  anchor_gate=120.0, min_frac=0.5, tail=(10.0, 11)):
    """Snap a warped polyline onto a drawn-line mask. The displacement field is
    smoothed along the line so whole stretches move to the same drawn street
    instead of individual points grabbing different parallels. Returns None if
    the line isn't substantially drawn on the map.

    anchors: points known to lie on this route's drawn line (its map badges).
    The global warp's local error can exceed the spacing of parallel drawn
    streets, so first shift the polyline by a displacement field interpolated
    between anchors, then snap with tighter caps so the corrected line can't
    wander back onto a neighboring route.

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
        cum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(P, axis=0).T))])
        A = np.asarray(anchors, dtype=float)
        d2 = ((P[None, :, :] - A[:, None, :]) ** 2).sum(2)
        j = d2.argmin(1)
        near = np.sqrt(d2[np.arange(len(A)), j]) < anchor_gate  # badge serves this shape
        if near.any():
            order = np.argsort(cum[j[near]])
            s = cum[j[near]][order]
            D = (A[near] - P[j[near]])[order]
            P = P + np.c_[np.interp(cum, s, D[:, 0]), np.interp(cum, s, D[:, 1])]
            if default_caps:
                caps = (26.0, 14.0)        # anchors pin the street; stay tight
    idx = np.arange(n)
    passes = [(cap, win) for cap in caps] + ([tail] if tail else [])
    for ci, (cap, pwin) in enumerate(passes):
        pwin = min(pwin, max(3, (n // 2) * 2 - 1))
        is_tail = bool(tail) and ci == len(passes) - 1
        d, j = tree.query(P)
        ok = d < cap
        if ci == 0 and ok.sum() < n * min_frac:
            return None                    # mostly undrawn: keep the warp
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


def simplify(pts, tol=1.2):
    """Douglas-Peucker."""
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
    return pts[keep]


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
                               anchor_gate=75.0, min_frac=0.35)
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
            # sprites take the drawn color even when refinement failed
            sprite_cols = [c or tuple(s) for c, s in zip(refined, seeds)]
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
                if rid0 in ("720", "754", "761"):
                    cols = [RAPID_RED]
                elif rid0 == "910":
                    cols = None   # J/Silver: drawn color collides with freeway gray
                else:
                    cols = [ORANGE]
                if cols is not None:
                    tree = mask_tree(cols)
                    # no color gate: Metro's orange badges render with variable
                    # fade (crisp ~1 px from orange, faded ~70), overlapping
                    # muted foreign badge colors, so a color test drops genuine
                    # ones. Metro's number badges are dense and its anchoring
                    # was already tuned without it.
                    anc = route_anchors(toks, tree)
                    anchored += bool(anc)
                    out_pts = snap_coherent(pts, tree, anchors=anc)
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
            shapes_raw[(feed, sid)] = simplify(out_pts if out_pts is not None else pts)
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
        d = project_stops(shapes_raw[key], cums[si], spx)
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
