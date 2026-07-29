"use strict";
// ---- which build this is ----
// Replaced at deploy time by scripts/stamp_build.mjs; left as the placeholder
// when the file is served straight from a checkout, which is how a dev copy
// tells itself apart from a published one.
//
// Pages serves every file with Cache-Control: max-age=600 and offers no way to
// change it, so a stale index.html is possible for ten minutes after any deploy
// and a tab left open is stale for as long as it stays open. Neither is fixable
// in the headers. What is fixable is not being able to *tell*: the build id
// travels in every snapshot, so a freeze record names the exact code that
// produced it, and the data files carry it in their URLs, so a cached page can
// never pair with a newer schedule.json than the one it was built against.
const BUILD = "__BUILD__";
const V_SCHEDULE = "__V_SCHEDULE__", V_MAP = "__V_MAP__", V_TILES = "__V_TILES__";
const DEPLOYED = !BUILD.startsWith("__");     // false in a working copy
// Set by checkVersion() below, and reported in every snapshot. Declared up here
// because resourceStats() reads it and is defined long before the poll is.
let staleBuild = null;

const cv = document.getElementById("c"), ctx = cv.getContext("2d");
// A canvas past a certain size stops being accelerated and falls back to
// software, and it is a cliff rather than a slope. Measured on this page at a
// fixed view, same window height, same everything else:
//
//     canvas 2560x1900   0.5 ms a frame
//     canvas 4096x1900   0.5 ms a frame
//     canvas 5120x1900   9.7 ms a frame     <- twenty times, for 25% more pixels
//
// The reporter's window in this investigation is 2560 CSS px wide, so at a
// device-pixel ratio of 2 it lands at 5120 and has been paying that on every
// frame of every session — the ~43 fps that looked like a large-window tax, and
// nine tenths of the main thread's frame budget spent on a blit.
//
// So the backing store is capped and the ratio follows it: at 2560 CSS px that
// is 1.6 rather than 2. Well above 1, and the tile pyramid supplies the detail
// either way, so what it costs is a little sharpness on very wide windows.
//
// DPR is read at call time everywhere (transforms, sprite sizes, the tile level,
// the zoom limit), so it can change with the window; it is no longer a constant.
const MAX_CANVAS_PX = 4096;
let DPR = Math.min(devicePixelRatio || 1, 2);
let W = 0, H = 0;
function resize() {
  W = Math.max(1, innerWidth); H = Math.max(1, innerHeight);
  const raw = Math.min(devicePixelRatio || 1, 2);
  const fit = Math.min(raw, MAX_CANVAS_PX / W, MAX_CANVAS_PX / H);
  cv.width = Math.max(1, Math.round(W * fit));
  DPR = cv.width / W;                  // the ratio the buffer actually has
  cv.height = Math.max(1, Math.round(H * DPR));
  cv.style.width = W + "px"; cv.style.height = H + "px";
}
addEventListener("resize", () => { resize(); });
resize();

// ---- view (map px -> screen) ----
const view = { x: 0, y: 0, k: 0.2 };   // screen = (map - x,y) * k
function fitView(mw, mh) {
  view.k = Math.min(W / mw, H / mh);
  view.x = (mw - W / view.k) / 2;
  view.y = (mh - H / view.k) / 2;
}

// ---- state ----
let map = null, data = null;
let shapes = [];        // {pts: Float32Array, cum: Float32Array}
let insetRuns = [];     // per shape: null or [{d, p}] mirroring it into the DTLA inset
let insetRect = null;   // [x0, y0, x1, y1] inset frame in map px
let trips = [];         // {r, p, times:Int32Array, t0, t1}
let vAlpha = null;      // per-trip opacity, eased in real time so vehicles fade in/out
const FADE_SEC = 0.16;  // how long a vehicle takes to fade on appear/disappear
let sprites = [];       // per route: {img, w, h}
let simT = 0;           // seconds since midnight
let playing = true;
let pathTrip = -1;      // index into trips whose path is shown, or -1
// The sheet already draws every route in its own ink, so tracing a path in that
// same color loses it in the line underneath — and which line the vehicle is on
// is the whole question. A fixed bright stroke reads against the map's muted
// palette instead; magenta is what scripts/debug_line.py reaches for first, so
// the browser and the offline tool draw a path the same way.
const PATH_INK = "#FF00FF";
let lastFrame = performance.now();
const speedSel = document.getElementById("speed");
const scrub = document.getElementById("scrub");
const playBtn = document.getElementById("play");
const stats = document.getElementById("stats");

// URL params: ?t=HH:MM start time, ?speed=N, ?paused=1
const qp = new URLSearchParams(location.search);
if (qp.get("t")) { const [h, m] = qp.get("t").split(":").map(Number); simT = h * 3600 + (m || 0) * 60; }
if (qp.get("speed")) speedSel.value = qp.get("speed");
if (qp.get("paused")) { playing = false; playBtn.textContent = "▶"; }

function togglePlay() {
  playing = !playing; playBtn.textContent = playing ? "⏸" : "▶";
}
playBtn.onclick = togglePlay;
scrub.oninput = () => { simT = +scrub.value; };
// space always plays/pauses — never re-activates the last-clicked control
addEventListener("keydown", e => {
  if (e.code !== "Space" || e.metaKey || e.ctrlKey || e.altKey) return;
  e.preventDefault();
  togglePlay();
});

// ---- system filter popover ----
const bar = document.getElementById("bar");
const filtersEl = document.getElementById("filters");
const sysBtn = document.getElementById("sys");
let sysOn = [];
sysBtn.onclick = () => {
  if (filtersEl.classList.toggle("open")) {
    // center over the button, clamped to the viewport; aim the caret at it
    const cx = sysBtn.getBoundingClientRect().left + sysBtn.offsetWidth / 2;
    const w = filtersEl.offsetWidth;
    const left = Math.min(Math.max(cx - w / 2, 8), W - w - 8);
    filtersEl.style.left = left + "px";
    filtersEl.style.setProperty("--caret-x", (cx - left) + "px");
  }
};
function buildFilters(systems) {
  sysOn = systems.map(() => true);
  const allCb = document.createElement("input");
  allCb.type = "checkbox"; allCb.checked = true;
  const head = document.createElement("label");
  head.className = "head";
  head.append(allCb, "All systems");
  const grid = document.createElement("div");
  grid.className = "grid";
  const boxes = systems.map((name, i) => {
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = true;
    cb.onchange = () => {
      sysOn[i] = cb.checked;
      const on = sysOn.filter(Boolean).length;
      allCb.checked = on === sysOn.length;
      allCb.indeterminate = on > 0 && on < sysOn.length;
    };
    const lab = document.createElement("label");
    lab.append(cb, name);
    grid.append(lab);
    return cb;
  });
  allCb.onchange = () => {
    allCb.indeterminate = false;
    sysOn.fill(allCb.checked);
    boxes.forEach(b => { b.checked = allCb.checked; });
  };
  filtersEl.append(head, grid);
}

// ---- auto-hide the control bar; wake on activity near the bottom edge ----
let barTimer = 0;
function bumpBar() {
  bar.classList.remove("faded");
  clearTimeout(barTimer);
  barTimer = setTimeout(() => {
    if (filtersEl.classList.contains("open")) { bumpBar(); return; }
    bar.classList.add("faded");
  }, 5000);
}
addEventListener("mousemove", e => { if (H - e.clientY < 140) bumpBar(); });
addEventListener("touchstart", e => {
  const t = e.touches[0];
  if (t && H - t.clientY < 140) bumpBar();
}, { passive: true });
for (const el of [bar, filtersEl])
  for (const ev of ["pointerdown", "input"]) el.addEventListener(ev, bumpBar);
bumpBar();

// ---- hi-res tile pyramid (pre-rendered from the PDF; levels 2x and 4x the base PNG) ----
const TILE = 512;
// Decoded tiles are this page's largest graphics allocation: 512x512x4 is 1 MB
// apiece and the pyramid holds 5488 of them, so only a fraction is ever
// resident. What matters is not only how many the cache holds but whether
// letting one go actually gives the memory back.
//
// It didn't. Tiles used to be <img> elements, and an <img> does not own its
// decoded surface — it names one in the browser's image cache, which is keyed
// by URL, lives in the content process, and is not tied to the document.
// Dropping the element and clearing src releases a reference, nothing more: the
// surface returns to a cache that decides for itself when to let go, and at
// panning speed it never keeps up. A session that evicted 2359 tiles had handed
// back 2.3 GB that way, on top of the 282 MB it was holding live, and froze
// with a render loop that was still running clean — no slow frames, no
// exceptions. It also explains the symptom that otherwise makes no sense:
// reloading didn't help because those surfaces were never the document's to
// free, and only closing the tab took the content process down with them.
//
// ImageBitmap is the one decoded image whose lifetime this page controls.
// close() releases the surface then and there, so an eviction is a free and the
// live set is what the cache says it is. Fetching the bytes here also means the
// only thing left in a browser cache is the compressed WebP — 44 MB for the
// entire pyramid, versus 4.1 GB if every tile in it were decoded at once.
//
// The budget still must never fall below one frame's working set. The cascade
// can draw three levels at once, so a deep zoom on a wide display wants 200+
// tiles in a single frame; a smaller budget evicts tiles that same frame is
// still drawing, so every frame re-fetches hundreds of them and the ones that
// haven't decoded don't paint at all. That is not a slow cache, it is one that
// misses every time, and it reads as stutter. A fixed 160 did exactly this
// against a measured demand of 215 — 800k evictions and visible juddering.
//
// So the budget follows demand rather than being guessed: twice the recent
// peak, floored so a small viewport still caches usefully, and capped in the
// megabytes the cap is actually protecting — the old ceiling of 640 tiles read
// like a count and meant 640 MB. Eviction is least-recently-*used*.
//
// TILE_CEIL is now a cap that actually holds. It used to be overridden whenever
// demand passed it — `max(capped, demand * 1.25)` — on the reasoning that a
// budget under the working set is worse than a big one. That reasoning is
// sound and the conclusion was still wrong: it meant the ceiling stopped
// applying at exactly the zoom where memory was tightest. Measured on the
// window that froze (2560x1331, DPR 2), demand peaked at 304 tiles and took the
// budget to 380 MB; on a 3440-wide display it asks for 565 MB. The way out is
// not to raise the cap or to clamp it and thrash, but to stop generating that
// much demand — see levelsFor(), which drops a tier that doesn't fit. Demand is
// bounded there, so the budget never has to choose between the cap and the
// working set, and honouring the cap here costs nothing.
const TILE_FLOOR = 192;        // tiles; a small viewport still caches usefully
const TILE_CEIL = 256;         // tiles, and 1 MB each: the live-memory ceiling
const TILE_HEADROOM = 1.25;    // budget over working set; a frame must never
                               // evict a tile it is itself still drawing
const TILE_INFLIGHT = 12;      // concurrent decodes — see pumpTiles
const tileCache = new Map();   // "level/c_r" -> tile, held in LRU order
const tileQueue = [];          // tiles waiting for a decode slot, oldest first
let tilesDrawn = 0, tileEvictions = 0, tileDemand = 0;
let tileDecodes = 0, tileDiscards = 0, tileErrors = 0, tileInflight = 0;
let tilePx = 0, peakTileMB = 0;   // live decoded tile pixels, and the high-water mark

function tileBudget() {
  // levelsFor() keeps demand at or under TILE_CEIL / TILE_HEADROOM, so this is
  // always comfortably above one frame's working set without the cap giving way.
  return Math.min(TILE_CEIL, Math.max(TILE_FLOOR, Math.ceil(tileDemand * 2)));
}

// Release a tile's decoded surface now, and abandon a fetch still on its way.
function dropTile(t) {
  t.dead = true;
  t.state = "dead";
  if (t.bmp) { tilePx -= t.bmp.width * t.bmp.height; t.bmp.close(); t.bmp = null; }
  if (t.ctl) { t.ctl.abort(); t.ctl = null; }
}

function evictTiles(budget) {
  if (tileCache.size < budget) return;
  const keep = Math.floor(budget * 0.75);  // still comfortably above one frame
  for (const [k, t] of tileCache) {        // insertion order, so the front of the
    tileCache.delete(k);                   // map is the least recently used tile
    dropTile(t);
    tileEvictions++;
    if (tileCache.size <= keep) break;
  }
}

// A tile that failed used to stay failed for as long as the view held it, and
// that is far more expensive than the missing square it looks like. levelReady()
// is all-or-nothing, so one stuck tile makes its level permanently un-ready, and
// two things follow every single recompose: tilesCover() stays false, so the
// 17-megapixel base PNG is redrawn underneath — scaled by DPR*view.k, which is
// 7.3x at the zoom this froze at — and drawTiles() never collapses to the finest
// tier, so all three levels are drawn instead of one. None of that shows up as a
// slow frame, because canvas draws are recorded on the main thread and
// rasterized off it: the loop stays fast and clean while the compositor wears
// it, which is exactly the signature these freezes report (frameErrors 0,
// slowFrames 0, fps 0). And it cannot heal, because getTile() re-inserts the
// tile on every access, making the broken one the *most* recently used tile in
// the cache and so the last thing eviction would ever reach.
//
// So a failure is retried, with a delay and a small ceiling on attempts — which
// was the whole point of not retrying, and is kept: a genuinely missing file
// gives up after TILE_TRIES and cannot become a fetch storm.
const TILE_RETRY_MS = 4000;    // before a failed tile is asked for again
const TILE_TRIES = 3;          // attempts before it is left alone for good

// Nothing is fetched for a view that is still moving.
//
// A zoom gesture walks through every level on the way, and each stop of it asks
// for a full screen of tiles it will have left before they arrive. Measured on
// the freeze of 2026-07-28 13:21: 26 seconds of hard zooming, k swinging between
// 0.15 and 4 over and over, produced **1237 decodes and 1322 evictions** — about
// fifty ImageBitmaps a second created and fifty closed, none of which was on
// screen long enough to be seen. The tile budget bounds what is *resident* and
// says nothing about that: the cache sat at ~230 of 256 the whole time and every
// snapshot read healthy.
//
// A tile asked for mid-gesture is waste twice over, so the ask is simply
// deferred: the view is drawn from whatever is already cached — which is what
// the coarse levels are for, they back-fill exactly this — and the moment it
// holds still the tiles for where it actually landed are fetched. This is how a
// map app behaves anyway; it is only visible as slightly softer artwork during
// the gesture itself.
//
// TILE_SETTLE_MS is under a frame at 60 Hz times ten, so a deliberate slow zoom
// still streams continuously and only a flung one is held back.
const TILE_SETTLE_MS = 140;
let viewMovedAt = 0, viewSeen = "";
let tilesMayLoad = true;      // set once a frame, from the view's own stillness
let tileHoldFrames = 0;       // how many frames were drawn with loading held off
// A tile that was never asked for. Returned instead of a cache entry on a miss
// while the view moves, because *recording* the miss is itself the damage:
// inserting a placeholder counts against the budget and evicts a decoded tile
// that is still on screen, to reserve room for one nobody has asked for.
const COLD = { state: "idle", bmp: null };

function wantTile(t) {
  // "idle" is a tile the cache knows it needs and has deliberately not asked
  // for yet. It becomes "queued" on the first look after the view settles.
  if (t.state !== "idle") return;
  t.state = "queued";
  tileQueue.push(t);
  pumpTiles();
}

function getTile(level, c, r) {
  const key = `${level}/${c}_${r}`;
  let t = tileCache.get(key);
  if (t) {                                // re-insert: Map iterates in insertion
    tileCache.delete(key);                // order, so this is the LRU bookkeeping
    tileCache.set(key, t);
    if (tilesMayLoad) wantTile(t);
    if (t.state === "error" && t.tries < TILE_TRIES
        && performance.now() - t.errAt >= TILE_RETRY_MS) {
      t.state = "queued";
      tileQueue.push(t);
      pumpTiles();
    }
    return t;
  }
  if (!tilesMayLoad) return COLD;
  evictTiles(tileBudget());
  t = { level, c, r, key, bmp: null, ctl: null, state: "idle", dead: false,
        tries: 0, errAt: 0 };
  tileCache.set(key, t);
  wantTile(t);
  return t;
}

// A pan asks for tiles far faster than they decode, and every decode in flight
// is another megabyte allocated at once — the peak, not the resident set, is
// what runs a process out of image memory. So decodes are rationed, and the
// queue is served newest-first: the newest request is what the current view is
// waiting on, while anything still queued from a view two gestures ago has
// usually been evicted already and is skipped on sight.
function pumpTiles() {
  if (tileQueue.length > 4 * TILE_CEIL) {   // compact away the evicted backlog
    let n = 0;
    for (const t of tileQueue) if (t.state === "queued") tileQueue[n++] = t;
    tileQueue.length = n;
  }
  while (tileInflight < TILE_INFLIGHT && tileQueue.length) {
    const t = tileQueue.pop();
    if (t.state === "queued") fetchTile(t);
  }
}

async function fetchTile(t) {
  t.state = "loading";
  t.ctl = new AbortController();
  tileInflight++;
  try {
    const res = await fetch(`tiles/${t.level}/${t.c}_${t.r}.webp?v=${V_TILES}`, { signal: t.ctl.signal });
    if (!res.ok) throw new Error(`tile ${t.key}: HTTP ${res.status}`);
    const bmp = await createImageBitmap(await res.blob());
    if (t.dead) { bmp.close(); tileDiscards++; return; }   // evicted mid-flight
    t.bmp = bmp; t.state = "ready";
    tilePx += bmp.width * bmp.height;
    const liveMB = tilePx * 4 / 1048576;
    if (liveMB > peakTileMB) peakTileMB = +liveMB.toFixed(1);
    tileDecodes++;
    bgDirty = true;                       // a newly arrived tile must repaint
  } catch (err) {
    // An evicted tile aborts its own fetch, so only a real failure counts. The
    // tile stays in the cache as an error and getTile() retries it a few times
    // on a delay, so a blip heals instead of pinning the base PNG under every
    // recompose for as long as the view holds the tile.
    if (!t.dead) { t.state = "error"; t.errAt = performance.now(); t.tries++; tileErrors++; }
  } finally {
    tileInflight--;
    t.ctl = null;
    pumpTiles();
  }
}

// These surfaces are the page's to free, so a reload starts from nothing
// instead of stacking a second set on top of the first.
addEventListener("pagehide", () => {
  for (const t of tileCache.values()) dropTile(t);
  tileCache.clear();
  tileQueue.length = 0;
});
// the tile column/row range covering the viewport, for a tile size in map px
function tileRange(ts) {
  return [
    Math.max(0, Math.floor(view.x / ts)),
    Math.max(0, Math.floor(view.y / ts)),
    Math.min(Math.ceil(map.width / ts) - 1, Math.floor((view.x + W / view.k) / ts)),
    Math.min(Math.ceil(map.height / ts) - 1, Math.floor((view.y + H / view.k) / ts)),
  ];
}

// how many tiles this level needs to cover the viewport
function levelCost(level) {
  const [c0, r0, c1, r1] = tileRange(TILE / level);
  return Math.max(0, c1 - c0 + 1) * Math.max(0, r1 - r0 + 1);
}

// The cascade to draw for a given sharpness: every level up to the one that is
// sharp enough, coarsest first, so coarser tiles back-fill while finer ones load.
//
// Two things bound it, and both exist because the naive rule — take the finest
// level that isn't too soft — is what ran the tab out of graphics memory.
//
// Level L supplies L device px per map px, and costs 4x the level below it. The
// old test, `level / 2 >= want`, reached for the finer tier the instant the
// coarser one fell short by *any* margin: at want = 4.02 it paid 4x the tiles
// and 4x the fill for a 0.5% gain in sharpness nobody can see. SHARP_ENOUGH
// says don't trade up for less than a tenth, which is still under what reads as
// soft when the tile is upscaled to fit.
//
// The switch-on is also where a tier is most expensive: it engages while the
// viewport is at its widest in map px, so the first frame that wants level 8
// wants ~250 of it. That cost is 4*W*H*DPR^2/TILE^2 whichever tier it is, so it
// scales with the window and no fixed cascade is safe on every display — 304
// tiles on the 2560x1331 window that froze, 452 on a 3440-wide one. So a tier
// that doesn't fit the memory ceiling is simply not used, and the view stays on
// the tier below until zooming further in shrinks the footprint enough to
// afford it. The result is a slightly softer picture for a few tenths of zoom
// on a large window, instead of 500+ MB of live tiles and a dead compositor.
const SHARP_ENOUGH = 0.9;      // accept a tier within this fraction of sharp
function levelsFor(want) {
  const levels = [];
  for (const level of [2, 4, 8]) {
    if (level / 2 >= want * SHARP_ENOUGH) break;
    levels.push(level);
  }
  // Drop the finest tier while a single frame's tiles wouldn't fit under the
  // ceiling with headroom. Always keep one: the coarsest is cheap, and drawing
  // nothing would show the base PNG through a view that has zoomed past it.
  let cost = levels.reduce((n, l) => n + levelCost(l), 0);
  while (levels.length > 1 && cost * TILE_HEADROOM > TILE_CEIL) {
    cost -= levelCost(levels.pop());
  }
  return levels;
}

// whether every tile this level needs for the viewport has finished decoding
function levelReady(level) {
  const [c0, r0, c1, r1] = tileRange(TILE / level);
  for (let r = r0; r <= r1; r++)
    for (let c = c0; c <= c1; c++)
      if (getTile(level, c, r).state !== "ready") return false;   // also queues the load
  return true;
}

function drawTiles(g) {
  tilesDrawn = 0;
  if (view.k * DPR <= 1.05 || !map) return;         // base PNG is sharp enough
  const want = view.k * DPR;                         // needed px per map px
  let levels = levelsFor(want);
  if (!levels.length) return;
  // Size the budget from what this frame needs *before* touching the cache. A
  // budget that lags demand by a frame spends that frame evicting tiles it is
  // about to ask for again, which is a whole frame of thrash on every zoom.
  let wanted = 0;
  for (const level of levels) wanted += levelCost(level);
  tileDemand = Math.max(wanted, tileDemand * 0.99);  // decaying peak
  // Once the finest level has every tile it needs, the levels beneath it are
  // covered pixel for pixel: drawing them is wasted fill, and keeps their tiles
  // hot in a cache that has better uses for the room.
  const fine = levels[levels.length - 1];
  if (levels.length > 1 && levelReady(fine)) levels = [fine];
  for (const level of levels) {
    const ts = TILE / level;                         // tile size in map px
    const [c0, r0, c1, r1] = tileRange(ts);
    for (let r = r0; r <= r1; r++)
      for (let c = c0; c <= c1; c++) {
        const t = getTile(level, c, r);
        if (t.state === "ready") {
          const b = t.bmp;    // its own size: the last row and column are cropped
          g.drawImage(b, c * ts, r * ts, b.width / level, b.height / level);
          tilesDrawn++;
        }
      }
  }
}

// ---- background compositing ----
// The background — base PNG plus tile pyramid — changes only when the view moves
// or a tile finishes loading. Vehicles move every frame, so the canvas is
// repainted 60 times a second, and recomposing 200+ scaled tile draws (plus a
// 4096x4139 base image) inside each of those repaints is what makes a large
// window stutter. It is fill cost, not cache cost: the same zoom is smooth in a
// smaller window purely because there is less to fill, which is exactly the
// shape of the measurements — 209 tiles drawn against 143, at one zoom, with no
// evictions on either side.
//
// The pyramid also oversamples hardest right here. Levels go 2x, 4x, 8x, so at
// view.k*DPR = 2.2 level 2 is a hair too soft and level 4 — nearly double what
// is needed — gets drawn instead, downscaled on the way. That is inherent to a
// power-of-2 pyramid; what is not inherent is paying for it every frame.
//
// So compose into an offscreen canvas and blit that. A still view then pays for
// the background once instead of 60 times a second, and panning costs one extra
// full-canvas blit on top of work it was doing anyway.
const bg = document.createElement("canvas");
const bgCtx = bg.getContext("2d");
let bgKey = "", bgDirty = true, bgComposes = 0;
// What the last compose actually asked the compositor for. The base PNG is the
// largest single draw this page makes and it is drawn whenever the tiles do not
// yet cover — scaled by DPR*view.k, which at the deepest zoom is 8, putting a
// 4096x4139 image on a 32768 px destination rect. Whether that was happening
// when a freeze hit has been guessed at for five rounds and never recorded.
let baseDrawn = false, baseScale = 0;

// Whether the browser took the canvases away.
//
// Every freeze so far ends the same way: the page is visible, its timers run,
// its input handlers run, and `document.timeline.currentTime` stops — the
// browser has stopped updating the rendering for this document. That is the
// browser's side of the line, and the page has had no way to say *why*. There
// is one thing it can still be told: a 2D context is lost when the process
// holding its surfaces goes away (the GPU process dying is the usual reason),
// and the browser says so with an event, on the main thread, which every one of
// these freezes has left running. If a freeze is a lost context, this catches
// it outright; if `ctxLost` is still 0 in the next record, that is ruled out
// for good rather than argued about.
//
// The event is not cancelled, so the browser restores the context on its own.
// What it cannot restore is what was drawn: the composed background and every
// sprite bitmap come back blank, so both caches are invalidated here. Without
// that a survived loss would leave the map painting nothing over nothing, which
// would look exactly like the freeze it just recovered from.
let ctxLost = 0, ctxRestored = 0, ctxLostAt = 0;
for (const c of [cv, bg]) {
  c.addEventListener("contextlost", () => {
    ctxLost++; ctxLostAt = Math.round(performance.now());
    noteEvent("contextlost");
    console.error("[transit] canvas context lost — the browser took the drawing "
                  + "surfaces away. Recorded; transitFreeze() reads it back.");
  });
  c.addEventListener("contextrestored", () => {
    ctxRestored++;
    noteEvent("contextrestored");
    bgDirty = true;                                  // recompose from scratch
    for (const s of spriteBmp) if (s) s.px = -1;      // and re-raster the fleet
  });
}

function composeBackground() {
  const key = `${view.x}|${view.y}|${view.k}`;
  if (!bgDirty && key === bgKey && bg.width === cv.width && bg.height === cv.height) return;
  bgKey = key; bgDirty = false; bgComposes++;
  if (bg.width !== cv.width || bg.height !== cv.height) {
    bg.width = cv.width; bg.height = cv.height;      // resizing also clears it
  }
  bgCtx.setTransform(DPR, 0, 0, DPR, 0, 0);
  bgCtx.fillStyle = "#cfe3ec";
  bgCtx.fillRect(0, 0, W, H);
  bgCtx.setTransform(DPR * view.k, 0, 0, DPR * view.k,
                     -view.x * DPR * view.k, -view.y * DPR * view.k);
  // Draw the base PNG first so tiles land on top of it while they stream in,
  // then skip it entirely once the tiles are shown to cover the viewport —
  // scaling a 17-megapixel image under an opaque layer is pure waste.
  if (map) {
    const probe = tilesCover();
    baseDrawn = !probe;
    baseScale = +(DPR * view.k).toFixed(2);
    if (!probe) bgCtx.drawImage(map, 0, 0);
    drawTiles(bgCtx);
  }
}

// Whether the tile pyramid will completely hide the base PNG this frame. It has
// to ask about the same cascade drawTiles will actually draw — this used to
// repeat the level choice inline, and any disagreement between the two skips the
// base PNG under tiles that were never drawn, leaving bare canvas.
function tilesCover() {
  if (view.k * DPR <= 1.05 || !map) return false;
  const levels = levelsFor(view.k * DPR);
  if (!levels.length) return false;
  return levelReady(levels[levels.length - 1]);
}

// ---- pan / zoom ----
let dragging = false, lx = 0, ly = 0, downX = 0, downY = 0, pressOnMap = false;
// any press on the map dismisses the filter popover
cv.addEventListener("pointerdown", () => filtersEl.classList.remove("open"));
cv.addEventListener("mousedown", e => {
  dragging = true; lx = e.clientX; ly = e.clientY;
  downX = e.clientX; downY = e.clientY; pressOnMap = true;
  cv.classList.add("drag");
});
addEventListener("mouseup", e => {
  dragging = false; cv.classList.remove("drag");
  // a press that didn't turn into a drag is a click: show/hide a vehicle's path
  if (pressOnMap && Math.hypot(e.clientX - downX, e.clientY - downY) < 5) handleTap(e.clientX, e.clientY);
  pressOnMap = false;
});
addEventListener("mousemove", e => {
  if (!dragging) return;
  view.x -= (e.clientX - lx) / view.k; view.y -= (e.clientY - ly) / view.k;
  lx = e.clientX; ly = e.clientY;

});
function zoomAt(cx, cy, f) {
  const mx = view.x + cx / view.k, my = view.y + cy / view.k;
  view.k = Math.min(8 / DPR, Math.max(0.08, view.k * f));  // cap at deepest tile level's 1:1
  view.x = mx - cx / view.k; view.y = my - cy / view.k;
}
// Trackpad gestures follow Maps.app: a two-finger swipe pans, a pinch zooms
// about the pointer. macOS keeps sending wheel events through the momentum
// phase after the fingers lift, so panning off them glides to a stop on its
// own — a hand-rolled easing would only fight the one the OS already applies.
//
// A pinch arrives as a wheel event with ctrlKey set (Chrome, Firefox, Edge);
// Safari sends its own gesture events instead, handled below, and no ctrl
// wheel, so the two paths can't both fire. A real mouse wheel still zooms, as
// it does in Maps.app — but telling one from a two-finger swipe takes a
// different tell in each browser:
//   - Firefox delivers a physical wheel in *line* units (deltaMode 1) and a
//     trackpad in pixels, so the delta unit alone settles it.
//   - Chrome and Safari deliver both in pixel units, and a mouse notch is not
//     the coarse whole number it is elsewhere: macOS ramps the wheel with
//     acceleration, so the first event of a scroll can be as small and
//     fractional as any trackpad delta. What stays constant is the legacy
//     wheelDeltaY — a physical notch steps in multiples of 120, where a
//     trackpad reports arbitrary pixel-derived values.
// The test is only applied to the first event of a gesture and then held for
// the rest of it, because mid-swipe a hard flick can throw a delta as coarse
// as any mouse notch, and a swipe that changed its mind halfway would be far
// worse than one misread from the start. A pause ends the gesture; macOS runs
// the momentum tail straight on from the fingers, well inside the gap.
const WHEEL_NOTCH = 40;      // px of deltaY below which it can't be a mouse notch
const WHEEL_GAP = 150;       // ms of quiet that ends a gesture
let wheelMode = null, wheelAt = 0;
function isMouseWheel(e) {
  if (e.deltaMode !== 0) return true;            // Firefox sends a wheel in lines
  const wd = e.wheelDeltaY;                      // Blink/WebKit legacy delta: a
  if (wd) return Math.abs(wd) % 120 === 0;       // notch steps by 120, a trackpad
                                                 // reports arbitrary pixels
  return e.deltaX === 0 && Number.isInteger(e.deltaY) && Math.abs(e.deltaY) >= WHEEL_NOTCH;
}
cv.addEventListener("wheel", e => {
  e.preventDefault();
  const now = performance.now();
  if (now - wheelAt > WHEEL_GAP) wheelMode = null;
  wheelAt = now;
  if (e.ctrlKey) {                                   // a pinch identifies itself
    wheelMode = null;
    zoomAt(e.clientX, e.clientY, Math.exp(-e.deltaY * 0.01));
    return;
  }
  if (wheelMode === null) wheelMode = isMouseWheel(e) ? "zoom" : "pan";
  if (wheelMode === "zoom") zoomAt(e.clientX, e.clientY, Math.exp(-e.deltaY * 0.0015));
  else { view.x += e.deltaX / view.k; view.y += e.deltaY / view.k; }
}, { passive: false });

// Safari: trackpad pinch as WebKit gesture events, scale being cumulative
let pinchScale = 1;
cv.addEventListener("gesturestart", e => { e.preventDefault(); pinchScale = e.scale || 1; });
cv.addEventListener("gesturechange", e => {
  e.preventDefault();
  if (pinchScale > 0 && e.scale > 0) zoomAt(e.clientX, e.clientY, e.scale / pinchScale);
  pinchScale = e.scale;
});
cv.addEventListener("gestureend", e => e.preventDefault());

// touch: one finger pans, two fingers pinch-zoom around the pinch midpoint
let touches = new Map();
let tapStart = null;   // {x, y} of a candidate single-finger tap, else null
function touchPts(e) { for (const t of e.changedTouches) touches.set(t.identifier, [t.clientX, t.clientY]); }
cv.addEventListener("touchstart", e => {
  e.preventDefault(); touchPts(e);
  tapStart = e.touches.length === 1 ? { x: e.touches[0].clientX, y: e.touches[0].clientY } : null;
}, { passive: false });
cv.addEventListener("touchmove", e => {
  e.preventDefault();
  const prev = new Map(touches);
  touchPts(e);
  const ids = [...touches.keys()];
  if (ids.length === 1 && prev.has(ids[0])) {
    const [px, py] = prev.get(ids[0]), [cx, cy] = touches.get(ids[0]);
    view.x -= (cx - px) / view.k; view.y -= (cy - py) / view.k;
  } else if (ids.length >= 2 && prev.has(ids[0]) && prev.has(ids[1])) {
    const [ax0, ay0] = prev.get(ids[0]), [bx0, by0] = prev.get(ids[1]);
    const [ax1, ay1] = touches.get(ids[0]), [bx1, by1] = touches.get(ids[1]);
    const d0 = Math.hypot(bx0 - ax0, by0 - ay0), d1 = Math.hypot(bx1 - ax1, by1 - ay1);
    const mx = (ax1 + bx1) / 2, my = (ay1 + by1) / 2;
    view.x -= (mx - (ax0 + bx0) / 2) / view.k; view.y -= (my - (ay0 + by0) / 2) / view.k;
    if (d0 > 0) zoomAt(mx, my, d1 / d0);
  }
  // any second finger or meaningful drift disqualifies the tap
  if (tapStart) {
    const c = touches.get(ids[0]);
    if (ids.length !== 1 || (c && Math.hypot(c[0] - tapStart.x, c[1] - tapStart.y) > 8)) tapStart = null;
  }
}, { passive: false });
const endTouch = e => {
  if (tapStart && touches.size <= 1) {
    const t = e.changedTouches[0];
    if (t && Math.hypot(t.clientX - tapStart.x, t.clientY - tapStart.y) < 8) handleTap(tapStart.x, tapStart.y);
  }
  tapStart = null;
  for (const t of e.changedTouches) touches.delete(t.identifier);
};
cv.addEventListener("touchend", endTouch);
cv.addEventListener("touchcancel", endTouch);

// ---- sprites ----
// Drawn at exactly the size they will occupy in device pixels, so drawImage
// copies 1:1 instead of resampling.
//
// A sprite rendered once at a fixed size and then scaled to fit is only clean
// where that scale lands on a whole ratio. This one used to be a (R+2)*4 px
// bitmap drawn at (R+2)*2*DPR*min(2.2, sqrt(k)) device px, which is 1:1 only at
// k = 1 and k = 4; at every zoom in between the ratio is a fraction like
// 46 -> 65.05, and as a vehicle translates sub-pixel the filter keeps
// reshuffling which source pixels land on which screen pixels. That reads as
// the sprite twitching rather than gliding, and it comes and goes with zoom —
// smooth at the whole ratios, jittery between them. Rendering to the size
// actually needed removes the resample entirely; motion stays sub-pixel, which
// is what makes it look smooth.
function drawSprite(g, route, px) {
  const rail = route.rail, R = rail ? 12 : 9.5, half = R + 2;
  const k = px / (half * 2);          // device px per map-unit of sprite
  g.scale(k, k);
  g.beginPath(); g.arc(half, half, R, 0, 7);
  g.fillStyle = route.c; g.fill();
  g.lineWidth = rail ? 2 : 1.2; g.strokeStyle = "rgba(255,255,255,.9)"; g.stroke();
  g.fillStyle = route.t;
  const label = route.n;
  const fs = label.length <= 2 ? 12 : label.length === 3 ? 9.5 : 7;
  g.font = `bold ${fs}px -apple-system, Arial`;
  g.textAlign = "center"; g.textBaseline = "middle";
  g.fillText(label, half, half + 0.5);
}

// One reused bitmap per route, resized in place when the size it needs changes.
//
// Keyed on the integer pixel size `spriteSize` rounds to — NOT on `view.k`.
// Keying on `view.k` meant any change to that float dropped the whole set and
// built a fresh <canvas> per visible route, even when the rounded size was
// identical: a trackpad zoom's momentum tail nudges `view.k` every frame for a
// second after the fingers lift, so a single gesture churned thousands of
// canvas elements. Firefox frees canvas backing on GC, and a canvas looks tiny
// to the JS heap while its buffer is large, so the buffers outran collection
// until an allocation failed inside the render loop and killed it. The tab
// froze, and the exhausted graphics memory lived in the content process, so a
// reload didn't clear it but closing the tab did. (A failed allocation no
// longer ends the loop — see the diagnostics below — but it is still the tile
// cache and this one that decide whether the memory runs out at all.)
//
// Reusing the element and resizing only on a real size change (a cache hit
// through the whole momentum tail, and through playback, where the size is
// constant) frees the old buffer synchronously and caps the live canvases at
// one per route. Rail and bus sizes coexist without thrashing
// because each route keeps its own bitmap and checks its own size.
const spriteBmp = [];

function spriteAt(i, px) {
  let s = spriteBmp[i];
  if (!s) s = spriteBmp[i] = { cv: document.createElement("canvas"), px: -1 };
  if (s.px !== px) {
    s.cv.width = s.cv.height = px;          // resize in place; frees the old buffer
    drawSprite(s.cv.getContext("2d"), data.routes[i], px);
    s.px = px;
  }
  return s.cv;
}

// geometry only — the bitmap is made on demand at the size the frame needs
function makeSprite(route) {
  const R = route.rail ? 12 : 9.5;
  return { half: R + 2 };
}

// The device-pixel size a sprite occupies this frame, and the map-unit size
// that lands on exactly that many device pixels.
function spriteSize(half, s) {
  const px = Math.max(6, Math.round(half * 2 * s * DPR * view.k));
  return [px, px / (DPR * view.k)];
}

// ---- load ----
Promise.all([
  new Promise(res => { const im = new Image(); im.onload = () => res(im); im.src = `map.png?v=${V_MAP}`; }),
  fetch(`schedule.json?v=${V_SCHEDULE}`).then(r => r.json()),
]).then(([im, d]) => {
  map = im; data = d;
  fitView(im.width, im.height);
  if (qp.get("k")) view.k = +qp.get("k");
  if (qp.get("x")) view.x = +qp.get("x");
  if (qp.get("y")) view.y = +qp.get("y");

  function toShape(flat) {
    const pts = Float32Array.from(flat);
    const n = flat.length / 2;
    const cum = new Float32Array(n);
    for (let i = 1; i < n; i++) {
      const dx = pts[2*i] - pts[2*i-2], dy = pts[2*i+1] - pts[2*i-1];
      cum[i] = cum[i-1] + Math.hypot(dx, dy);
    }
    return { pts, cum };
  }
  shapes = d.shapes.map(toShape);
  sprites = d.routes.map(makeSprite);
  buildFilters(d.systems || []);
  insetRect = d.insetRect;
  insetRuns = (d.insets || []).map(runs => runs && runs.map(toShape));
  // GTFS times are minute-quantized, so consecutive stops can share a
  // timestamp (or sit 1s apart — a scheduler trick to force ordering) while
  // the bus travels between them, teleporting the vehicle. Spread each run
  // of (near-)tied times over the adjacent gap, proportional to distance.
  function detie(times, dist) {
    const n = times.length;
    let i = 0;
    while (i < n - 1) {
      if (times[i+1] - times[i] > 1) { i++; continue; }
      let j = i;
      while (j + 1 < n && times[j+1] - times[j] <= 1) j++;
      if (j + 1 < n) {           // spread [i..j+1] over the gap that follows
        const T = times[i], U = times[j+1], D = dist[j+1] - dist[i];
        if (D > 0) for (let m = i + 1; m <= j; m++)
          times[m] = T + (U - T) * (dist[m] - dist[i]) / D;
      } else if (i > 0) {        // run ends the trip: use the preceding gap
        const T = times[i-1], U = times[j], D = dist[j] - dist[i-1];
        if (D > 0) for (let m = i; m < j; m++)
          times[m] = T + (U - T) * (dist[m] - dist[i-1]) / D;
      }
      i = j;
    }
  }
  // distance for de-tying: downtown the main map is so compressed that stop
  // distances plateau, which would starve inset-moving segments of time —
  // weigh inset movement (at ~1/5 scale, the inset's magnification) too
  function effDist(pat) {
    const dd = pat.d, ir = pat.ir, id = pat.id;
    if (!ir) return dd;
    const eff = new Float64Array(dd.length);
    for (let i = 1; i < dd.length; i++) {
      let step = dd[i] - dd[i-1];
      if (ir[i] >= 0 && ir[i] === ir[i-1]) step = Math.max(step, Math.abs(id[i] - id[i-1]) / 5);
      eff[i] = eff[i-1] + step;
    }
    return eff;
  }
  trips = d.trips.map(t => {
    const times = new Float64Array(t.length - 2);
    times[0] = t[2];
    for (let i = 3; i < t.length; i++) times[i-2] = times[i-3] + t[i];
    const pat = d.patterns[t[1]];
    if (pat) detie(times, effDist(pat));
    return { r: t[0], p: t[1], times, t0: times[0], t1: times[times.length-1] };
  });
  // draw rail last so trains sit on top of the bus swarm
  trips.sort((a, b) => (d.routes[a.r].rail ? 1 : 0) - (d.routes[b.r].rail ? 1 : 0));
  vAlpha = new Float32Array(trips.length);   // all start hidden, fade in on first frame
  requestAnimationFrame(frame);
});

// average speed (map px / sec) of segment i -> i+1; 0 if degenerate
function segSpeed(times, d, i) {
  const span = times[i+1] - times[i];
  return span > 0 ? (d[i+1] - d[i]) / span : 0;
}

// distance along shape at time t within segment [lo, hi], using a monotone
// cubic Hermite over distance-vs-time. Endpoint slopes are the average of the
// adjacent segments' speeds, so velocity is continuous across stop times —
// per-segment easing (the old smoothstep) made every vehicle pulse to a halt
// at each stop, which read as rhythmic jerking at low speed multipliers.
// `halt` makes the vehicle come to rest at each stop instead of carrying its
// speed through: zero tangents turn the cubic into a smoothstep, so it pulls
// away, runs, and brakes to a standstill at the platform, all within the
// scheduled time. Trains stop at stations you can see on the map — the white
// circles — and gliding through one at line speed reads as wrong; buses call
// at unmarked kerbside stops every couple of blocks, where halting at each
// would just look like a stutter.
function distAt(times, d, lo, hi, t, halt) {
  const span = times[hi] - times[lo];
  if (span <= 0) return d[lo];
  const f = (t - times[lo]) / span;
  const delta = (d[hi] - d[lo]) / span;
  if (delta === 0) return d[lo];
  let m0 = 0, m1 = 0;
  if (!halt) {
    m0 = lo > 0 ? (segSpeed(times, d, lo - 1) + delta) / 2 : delta;
    m1 = hi < times.length - 1 ? (delta + segSpeed(times, d, hi)) / 2 : delta;
    // Fritsch–Carlson clamp keeps the cubic monotone (no backing up / overshoot)
    m0 = Math.min(Math.max(m0 / delta, 0), 3) * delta;
    m1 = Math.min(Math.max(m1 / delta, 0), 3) * delta;
  }
  const f2 = f * f, f3 = f2 * f;
  return (2*f3 - 3*f2 + 1) * d[lo] + (f3 - 2*f2 + f) * span * m0
       + (3*f2 - 2*f3) * d[hi] + (f3 - f2) * span * m1;
}

function posAlong(shape, dist) {
  const { pts, cum } = shape;
  let lo = 0, hi = cum.length - 1;
  if (dist <= 0) return [pts[0], pts[1]];
  if (dist >= cum[hi]) return [pts[2*hi], pts[2*hi+1]];
  while (hi - lo > 1) { const m = (lo + hi) >> 1; (cum[m] <= dist ? lo = m : hi = m); }
  const f = (dist - cum[lo]) / (cum[hi] - cum[lo] || 1);
  return [pts[2*lo] + (pts[2*hi] - pts[2*lo]) * f,
          pts[2*lo+1] + (pts[2*hi+1] - pts[2*lo+1]) * f];
}

// trace a shape's polyline into the current path (used by the path inspector)
function strokeShape(shape) {
  const pts = shape.pts, n = pts.length >> 1;
  if (n < 2) return;
  ctx.beginPath();
  ctx.moveTo(pts[0], pts[1]);
  for (let i = 1; i < n; i++) ctx.lineTo(pts[2*i], pts[2*i+1]);
  ctx.stroke();
}

// on-screen sprite size factor: constant when zoomed out, grows gently with deep zoom
function spriteScale() {
  return 1 / view.k * Math.min(1, view.k * 2.3) * Math.min(2.2, Math.max(1, Math.sqrt(view.k)));
}

// map-px position of a trip's vehicle at time t, or null if it isn't running
function vehiclePos(tr, t) {
  if (t < tr.t0 || t > tr.t1) return null;
  const pat = data.patterns[tr.p];
  if (!pat) return null;
  const times = tr.times, d = pat.d;
  let lo = 0, hi = times.length - 1;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; (times[m] <= t ? lo = m : hi = m); }
  return posAlong(shapes[pat.s], distAt(times, d, lo, hi, t, data.routes[tr.r].rail));
}

// index of the running vehicle under a screen point, or -1 if none is close enough
function pickVehicle(cx, cy) {
  if (!data) return -1;
  const mx = view.x + cx / view.k, my = view.y + cy / view.k;
  const s = spriteScale(), tol = 6 / view.k;   // a few screen px of slack for touch
  let best = -1, bestD = Infinity;
  for (let i = 0; i < trips.length; i++) {
    const tr = trips[i];
    if (!sysOn[data.routes[tr.r].sy]) continue;
    const p = vehiclePos(tr, simT);
    if (!p) continue;
    const rad = (sprites[tr.r].half + 2) * s + tol;
    const dx = p[0] - mx, dy = p[1] - my, dd = dx*dx + dy*dy;
    if (dd <= rad*rad && dd < bestD) { bestD = dd; best = i; }
  }
  return best;
}

// path inspector: tapping a vehicle shows the line it runs; tapping another
// switches to it, tapping empty space hides the path. The selection outlives a
// pause in both directions — pressing play keeps the path up, which is what
// makes it useful for watching a vehicle run its variant rather than only for
// reading a frozen one. Only another tap (or filtering the system out) clears it.
function handleTap(cx, cy) {
  pathTrip = pickVehicle(cx, cy);
}

// ---- diagnostics ----
// The render loop used to end at its first exception: requestAnimationFrame was
// the last statement of frame(), so anything that threw before it — most
// plausibly a failed allocation once graphics memory is tight — stopped the
// simulation for good, with no message left behind. The next frame is now
// scheduled whatever happens, so a bad frame is a dropped frame rather than the
// end of the animation, and the reason is reported here.
//
// What to look for when it does fire: this page's memory is almost entirely
// decoded images. Tiles dominate (see the cache above), map.png holds ~65 MB,
// and the sprite bitmaps are a rounding error beside them.
//
// A freeze also doesn't have to come through here at all. The one that
// prompted this reported zero frame errors and zero slow frames, because the
// loop really was fine — it was the content process underneath that had run
// out of image surfaces, holding every tile the page thought it had evicted.
// So the snapshot leads with fps and with live megabytes: a log still arriving
// while fps reads 0 means the loop stopped, and one arriving at 60 over a
// frozen picture means it didn't. resourceStats() is on window as
// transitDebug() so the numbers can be read from the console at any time.
const DEBUG = qp.has("debug");
const SLOW_FRAME_MS = 500;   // a frame this long is thrashing, not drawing
let frameErrors = 0, slowFrames = 0, lastSlowLog = -1e9;
let frames = 0, lastFrameAt = performance.now();
// `fps: 0` has now been reported for two different failures and one non-failure,
// and the snapshot could not tell them apart, because a stalled compositor and a
// backgrounded tab produce the identical reading: rAF stops, timers keep firing.
// Both were guessed at from the surrounding numbers instead of being stated. The
// gap between rAF callbacks and the visibility of the document say it outright,
// and neither costs anything, so both are kept always rather than only under
// ?trace — where they existed already and were no use, the freeze never having
// been caught under a flag someone had to set in advance.
let lastRafAt = 0, rafGapMax = 0;
let visibleSince = performance.now(), hiddenInWindow = document.hidden;
// Those flags were still not enough, and the third report is why: it came back
// `hidden: true`, which the rule above reads as "no fault, the tab was
// backgrounded" — and there is no way to tell from it whether the freeze was
// missed or whether the freeze is *what happens* around a hide. Reading a
// snapshot means going to the browser to read it, and on macOS a window that
// ends up behind another is reported hidden, so the act of collecting the
// evidence is one of the things that produces this reading. A flag that the
// observer trips by observing cannot decide anything.
//
// It also disagreed with itself: `visibilityAgeMs` 50499 against `sinceFrameMs`
// 39078, i.e. a frame 11 s after the tab was supposedly hidden. That gap dates
// the last visibility *change*, and at load it dates the load instead, so it
// answers a different question depending on what has happened — which is not a
// number to reason from either.
//
// What can decide it is a clock that only advances while the document is
// visible. rAF is called when the page is visible and not otherwise, so time
// spent visible since the last frame is exactly "how long the page has been
// asked to draw and hasn't". A tab hidden for an hour contributes nothing to it
// and cannot fake a stall; a page that is visible and not drawing cannot hide
// one. Everything below hangs off that instead of off document.hidden.
let visClock = 0;                                   // ms spent visible, banked
let visSince = document.hidden ? 0 : performance.now();   // 0 while hidden
let frameAtVis = 0;                                 // visibleMs() at the last frame
let stalls = 0, firstStallAt = 0;                   // what the watchdog below found

function visibleMs() {
  return visClock + (visSince ? performance.now() - visSince : 0);
}

// The browser's own frame clock, and the one number that says whose fault a
// stall is.
//
// `document.timeline.currentTime` is the timestamp rAF callbacks are handed. It
// is set once per "update the rendering" pass and does not move between them, so
// reading it from a *timer* asks the browser directly: are you still producing
// frames? It is not the page's bookkeeping — nothing this file does can advance
// it or hold it back.
//
// Against `stalledVisibleMs` it splits the freeze in two, and the split is the
// whole question:
//
//   climbing, while the page has no frames  — the browser is rendering and this
//     page's rAF registration is gone. A page bug, and re-arming recovers it.
//   frozen at the same value                — the browser stopped updating the
//     rendering for a document that is visible and asking. Nothing the page
//     draws can reach the screen, because the compositing pass is the one that
//     stopped. Not fixable from in here; survivable, and reported as such.
function renderTick() {
  const t = document.timeline && document.timeline.currentTime;
  return typeof t === "number" ? Math.round(t) : -1;
}

addEventListener("visibilitychange", () => {
  const now = performance.now();
  if (document.hidden) {
    if (visSince) { visClock += now - visSince; visSince = 0; }
  } else if (!visSince) {
    visSince = now;
    // Coming back is not a stall, however long the tab was away: the loop was
    // stopped on purpose and the next callback is due within a frame.
    frameAtVis = visibleMs();
  }
  visibleSince = now;
  // A tab hidden and shown again *between* two snapshots reads as a stall in
  // both fps and rafGapMax while being visible at both ends, so the flag has to
  // survive the round trip rather than being sampled at the end of it.
  if (document.hidden) hiddenInWindow = true;
}, true);
// Baselines for the per-second rates below, refreshed by every snapshot: the
// rates a snapshot reports are since the *previous* snapshot, whoever took it.
let statsAt = 0, statsFrames = 0, statsComposes = 0, statsEvictions = 0, statsDecodes = 0;
let peakDecodeRate = 0, peakEvictRate = 0;
// Reset with the rate baselines below, so these describe the window a snapshot
// covers rather than the whole session.
let frameCostSum = 0, frameCostN = 0, frameCostMax = 0;
// And what the frame spent it on. One number said the canvas was too big for
// the machine and the cap fixed it; the two freezes after the cap say the
// number is not flat — it sits at 3-4 ms zoomed out and 10-16 ms past k≈3, and
// both stalls happened at the top of that. A single average cannot say which
// part of the frame grows, and the three candidates fail differently: the
// compose is one huge draw of the base PNG or the tile cascade and only runs
// when the view moves, the blit is the whole canvas every frame and scales with
// its size, and the sprites are thousands of small draws whose *on-screen* size
// grows as sqrt(k) — so at deep zoom each one covers five times the pixels it
// does at k=1, over a fleet that also changes size with the simulated clock.
// Timed separately, a rising cost names its own cause.
let costComposeSum = 0, costBlitSum = 0, costSpriteSum = 0, costComposeMax = 0;
let frameComposeMs = 0, frameBlitMs = 0, spriteDraws = 0;

function resourceStats() {
  let spriteCanvases = 0, spritePx = 0;
  for (const s of spriteBmp) if (s && s.px > 0) { spriteCanvases++; spritePx += s.px * s.px; }
  const mb = px => +(px * 4 / 1048576).toFixed(1);
  const now = performance.now();
  const secs = statsAt ? (now - statsAt) / 1000 : 0;
  const rate = n => secs > 0 ? Math.round(n / secs) : 0;
  const s = {
    // Read fps first. The render loop and this log run off separate clocks, so
    // a log that keeps arriving while fps sits at 0 means frames stopped, and
    // one arriving at 60 fps over a picture that isn't moving means they
    // didn't — the frames are being drawn and something below the page is
    // failing to put them on screen. The old log couldn't tell those apart:
    // it reported no errors and no slow frames for both.
    fps: rate(frames - statsFrames), sinceFrameMs: Math.round(now - lastFrameAt),
    // Then read this one, and settle the question on it alone: milliseconds the
    // document has spent *visible* since the last frame was drawn. rAF runs when
    // the page is visible and not otherwise, so anything here beyond a frame or
    // two is the loop failing to be called while the page was asking to draw —
    // no interpretation, and nothing an observer can trip by observing. Seconds
    // of it is the freeze; `sinceFrameMs` far larger than it is a backgrounded
    // tab and no fault at all. STALL_VISIBLE_MS of it fires the watchdog below,
    // which records the snapshot at the time rather than leaving it to be read
    // afterwards. Zero until the first frame: the page spends its load visible
    // and not drawing, which is not a stall but would read as the largest one
    // in the session to anyone who took a snapshot during it.
    stalledVisibleMs: frames ? Math.round(visibleMs() - frameAtVis) : 0,
    framesTotal: frames, stalls, firstStallAt,
    // The rest is context for that number, not a substitute for it. rafGapMax is
    // the longest gap between consecutive frames since the last snapshot, so it
    // catches a stall the page recovered from, which the live figure above no
    // longer shows; slowFrames is the other failure, a loop that ran but ran
    // slowly. `hidden` and `hiddenSinceLast` say what the browser was doing to
    // the loop on purpose, and visibilityAgeMs dates the last change — with the
    // caveat that before the first change it dates the page load.
    hidden: document.hidden, hiddenSinceLast: hiddenInWindow,
    visibilityAgeMs: Math.round(now - visibleSince),
    visibleMs: Math.round(visibleMs()),
    rafGapMax: Math.round(rafGapMax),
    // Whether the *browser* is still producing frames, independent of whether
    // this page is getting any — see renderTick(). Read it second, right after
    // stalledVisibleMs, and it names the culprit outright.
    renderTick: renderTick(),
    // The window against the canvas cut for it. These agree unless a resize
    // event went undelivered, and one that did is worth as much as renderTick:
    // resize steps and animation-frame callbacks are dispatched by the same
    // "update the rendering" pass, so a canvas still cut for a window that has
    // since changed size is the same finding by another route.
    winW: innerWidth, winH: innerHeight, cvW: cv.width, cvH: cv.height,
    // tilesDrawn/tileDemand are per frame: if demand ever approaches the budget
    // the cache is thrashing, and evictions will be climbing by thousands/sec.
    tilesCached: tileCache.size, tilesDrawn, tileDemand: Math.round(tileDemand),
    tileBudget: tileBudget(), tileQueued: tileQueue.length, tileInflight,
    // Every decode is a megabyte allocated, so decodes are the churn that used
    // to run away invisibly — before tiles owned their surfaces, each one also
    // stayed allocated. Over a still view both rates should fall to zero; if
    // decodesPerSec tracks evictsPerSec instead, the cache is re-fetching tiles
    // it just dropped and the budget is under the working set.
    tileDecodes, decodesPerSec: rate(tileDecodes - statsDecodes),
    tileEvictions, evictsPerSec: rate(tileEvictions - statsEvictions),
    // The rates above are since the last snapshot, so at a freeze they read 0 —
    // nothing is happening, which is the point. These are the worst the session
    // ever saw, and are what the churn actually looked like on the way in: the
    // 13:21 freeze was ~48 decodes and ~50 evictions a second, sustained for 26
    // seconds of zooming, while every instantaneous reading looked healthy.
    peakDecodeRate, peakEvictRate,
    // Time inside drawFrame, averaged over this snapshot's window, and the worst
    // one in it. Against fps this says whether the page is the bottleneck.
    frameCostAvg: frameCostN ? +(frameCostSum / frameCostN).toFixed(1) : 0,
    frameCostMax: +frameCostMax.toFixed(1),
    // and where it went: one compose (only when the view moved), one
    // full-canvas blit, and the fleet. costSprites climbing with `zoom` while
    // the other two sit still is the sqrt(k) sprite growth; costCompose is the
    // base PNG or the tile cascade and shows up as a spike in the max rather
    // than in the average, since most frames skip it.
    costCompose: frameCostN ? +(costComposeSum / frameCostN).toFixed(1) : 0,
    costComposeMax: +costComposeMax.toFixed(1),
    costBlit: frameCostN ? +(costBlitSum / frameCostN).toFixed(1) : 0,
    costSprites: frameCostN ? +(costSpriteSum / frameCostN).toFixed(1) : 0,
    spriteDraws,
    // Whether the last compose drew the base PNG, and at what scale — see
    // composeBackground. tileHoldFrames counts frames drawn while tile loading
    // was deliberately held off because the view was still moving.
    baseDrawn, baseScale, tileHoldFrames,
    tileDiscards, tileErrors,
    // bgComposes should sit still while the view is still; climbing at frame
    // rate means the blit cache is missing and every frame recomposites.
    bgComposes, composesPerSec: rate(bgComposes - statsComposes),
    bgMB: mb(bg.width * bg.height),
    tileMB: mb(tilePx), peakTileMB,
    mapMB: map ? mb(map.width * map.height) : 0,
    spriteCanvases, spriteMB: mb(spritePx),
    canvasMB: mb(cv.width * cv.height),
    zoom: +view.k.toFixed(3), dpr: DPR, frameErrors, slowFrames,
    // The two ways a rendering pass dies that are not this page's doing: the
    // browser taking the drawing surfaces away, and the process being suspended
    // rather than wedged (see the canvas listeners and CLOCK_SKEW0).
    ctxLost, ctxRestored, ctxLostAt,
    driftMs: Math.round(Date.now() - performance.now() - CLOCK_SKEW0),
    // Which copy of this file the tab is running. Twice now a freeze has been
    // reported minutes after a fix went live and there was no way to tell from
    // the record whether the frozen tab had it — a tab open since before the
    // deploy keeps running the old script, and Pages serves this HTML with
    // max-age=600, so even a reload can be served a stale copy for ten minutes.
    // document.lastModified is the served file's Last-Modified, so it dates the
    // code rather than the session, and costs nothing and no build step. `build`
    // names it exactly, once the deploy has stamped one in.
    build: BUILD, docModified: document.lastModified, staleBuild,
  };
  // The one number that matters for a freeze, and now a true one: every decoded
  // surface here is held by this page and released when the page releases it.
  s.imageMB = +(s.tileMB + s.bgMB + s.mapMB + s.spriteMB + s.canvasMB).toFixed(1);
  if (s.decodesPerSec > peakDecodeRate) peakDecodeRate = s.decodesPerSec;
  if (s.evictsPerSec > peakEvictRate) peakEvictRate = s.evictsPerSec;
  s.peakDecodeRate = peakDecodeRate; s.peakEvictRate = peakEvictRate;
  statsAt = now; statsFrames = frames; statsComposes = bgComposes;
  statsEvictions = tileEvictions; statsDecodes = tileDecodes;
  frameCostSum = 0; frameCostN = 0; frameCostMax = 0;
  costComposeSum = 0; costBlitSum = 0; costSpriteSum = 0; costComposeMax = 0;
  rafGapMax = 0; hiddenInWindow = document.hidden;
  return s;
}

// A freeze doesn't arrive out of nowhere: frame times climb first, as the
// process starts fighting for image memory. Saying so early gives the cause
// while the tab is still usable, rather than only in the wreckage.
function noteSlowFrame(ms, at) {
  slowFrames++;
  if (slowFrames > 6 || at - lastSlowLog < 3000) return;
  lastSlowLog = at;
  console.warn(`[transit] frame took ${ms | 0} ms. If this keeps climbing the tab is `
             + "thrashing on decoded image memory — check tileMB and decodesPerSec. "
             + "Snapshot:", resourceStats());
}
window.transitDebug = resourceStats;

function noteFrameError(err) {
  frameErrors++;
  if (frameErrors > 3) return;          // a bad frame usually repeats; don't spam
  console.error("[transit] frame aborted — the simulation would have stopped here:", err);
  console.warn("[transit] probable cause: graphics memory exhausted by decoded image "
             + "surfaces — tiles dominate, so read tileMB and peakTileMB first. "
             + "Snapshot:", resourceStats());
  if (frameErrors === 3) console.warn("[transit] further frame errors suppressed");
}

if (DEBUG) {
  console.log("[transit] debug on — transitDebug() for live resource stats. "
            + `Tile budget tracks demand, ${TILE_FLOOR}-${TILE_CEIL} tiles at 1 MB each; `
            + "if tileDemand ever reaches tileBudget the cache is thrashing and evictions "
            + "and decodes will run away together.");
  setInterval(() => console.log("[transit]", resourceStats()), 5000);
}

// Without ?debug nothing is logged at all, and running out of image memory
// doesn't announce itself — the frame loop stays clean right up to the freeze.
// So one cheap watchdog runs always and says the only thing worth saying
// beforehand: how much decoded image memory is live, and what is holding it.
// Under ?debug the 5-second log above owns the rate baselines instead.
const MEM_WARN_MB = 480;
let memWarnings = 0;
setInterval(() => {
  if (DEBUG || TRACE || memWarnings >= 3) return;
  const s = resourceStats();
  if (s.imageMB < MEM_WARN_MB) return;
  memWarnings++;
  console.warn(`[transit] ${s.imageMB} MB of decoded images live. Snapshot:`, s);
}, 10000);

// ---- am I the current build? ----
// A tab open across a deploy keeps running the script it loaded, for as long as
// it stays open. That is ordinary browser behaviour and not a fault, but it cost
// this investigation two rounds: a freeze was reported minutes after a fix went
// live, twice, and both times the tab predated it — which took reading the shape
// of the stored record to work out, after the fact.
//
// So the page asks. version.json carries the build the deploy published; the
// query string is what gets past the CDN, which would otherwise answer from the
// same ten-minute cache that made the page stale in the first place, and
// no-store keeps it out of the browser's own.
//
// It never reloads by itself. This is a simulation someone watches for a long
// time at a zoom and a clock they chose, and throwing that away to pick up a
// change they haven't asked for would be worse than being a version behind. It
// offers, in the bar, and says so on the console.
const VERSION_POLL_MS = 300000;   // 5 min; a deploy is not an urgent event
const updEl = document.getElementById("upd");
updEl.addEventListener("click", () => location.reload());

async function checkVersion() {
  if (!DEPLOYED || staleBuild || document.hidden) return;
  try {
    const res = await fetch(`version.json?ts=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) return;
    const v = await res.json();
    if (!v.build || v.build === BUILD) return;
    staleBuild = v.build;
    updEl.hidden = false;
    console.warn(`[transit] this tab is running build ${BUILD}; ${v.build} is live. `
               + "Nothing is wrong with it — but a freeze reported from this tab is a "
               + "freeze in the older code. The update button in the bar reloads.");
  } catch (e) { /* offline, or the deploy is mid-flight; ask again next time */ }
}
setInterval(checkVersion, VERSION_POLL_MS);
// and whenever the tab is picked up again, which is when a long-open one is
// most likely to have missed something
addEventListener("visibilitychange", () => { if (!document.hidden) checkVersion(); });
checkVersion();

// ---- stall watchdog (always on) ----
// Three freezes, three snapshots, and not one of them taken while the page was
// failing: each was read off the console afterwards, by which time the numbers
// describe the reading rather than the fault. The ?trace black box was built for
// exactly this and has never been running when it happened, because it takes a
// flag set in advance — and nobody sets a flag in advance for a bug that shows
// up once a session. So the page watches itself, always, and writes down the
// moment rather than waiting to be asked about it afterwards.
//
// The condition is the visible clock above: the document visible and no frame
// for STALL_VISIBLE_MS. There is no legitimate way to sit that long — a slow
// frame is still a frame and shows up as slowFrames, a hidden tab does not
// advance the clock, and a frame that threw still scheduled its successor.
//
// The record goes to localStorage because the only cure anyone has found is
// closing the tab, which throws away the console with it. transitFreeze() after
// reopening reads it back. Written once, when the stall is detected, so nothing
// touches storage on the hot path.
//
// What this cannot catch is the main thread wedged outright, since the timer
// would be wedged with it — that is the ?trace worker's case, and it stays there.
// Every freeze so far has had live timers and a dead loop, which is this one.
const STALL_VISIBLE_MS = 4000;   // visible, and not drawn, for this long
const STALL_TICK_MS = 1000;      // so the record lands within a second of that
const STALL_KEY = "transit.freeze";
const STALL_PREV_KEY = "transit.freeze.prev";
const STALL_RUNUP = 30;          // cheap liveness samples kept before the stall
// Which run of the page wrote the record. "Keep the first stall of the session"
// needs to know what the session is, and localStorage outlives it: the test used
// to be "is there a record already", which is true in every later session on any
// machine that has ever frozen. So the first freeze ever recorded was kept
// forever, every freeze after it only bumped a counter on a snapshot of some
// older run, and — worse, once there was something to amend — the verdict and
// the recovery were dropped on the floor, because neither is written unless this
// run owns the record. Caught the first time it mattered: the freeze on
// 2026-07-28 12:12, two minutes after the verdict shipped, left a record from
// 11:46 with nothing but `lastAt` changed.
const STALL_SESSION = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
let stallRunup = [], inStall = false;   // stalls/firstStallAt live with the clock

// What the page was being asked to do on the way in.
//
// Every record so far describes the page's own state and none of them says what
// the user was doing, which has left the run-up readable only by inference —
// the two freezes on 2026-07-28 were both at or just off the deepest zoom, and
// the only reason anyone knows that is that `k` happens to be sampled. A ring
// of the events themselves says it directly, and says the ones no counter
// reflects at all: a resize, a display change, the page being frozen or resumed
// by the browser's own lifecycle.
//
// Runs of one kind are coalesced — a pinch is hundreds of wheel events and
// would otherwise be the whole ring — so an entry is "wheel x214, from 41.2 s
// to 43.9 s". Registered passively and in the capture phase, so nothing here
// can change what the page's own handlers see.
const EVENT_KEEP = 24;
const EVENT_JOIN = 1000;     // ms of quiet that ends a run of one kind
const stallEvents = [];
function noteEvent(kind) {
  const t = Math.round(performance.now());
  const last = stallEvents[stallEvents.length - 1];
  if (last && last.e === kind && t - last.t1 <= EVENT_JOIN) { last.n++; last.t1 = t; return; }
  stallEvents.push({ e: kind, n: 1, t0: t, t1: t });
  if (stallEvents.length > EVENT_KEEP) stallEvents.shift();
}
for (const ev of ["pointerdown", "pointerup", "wheel", "keydown", "resize",
                  "visibilitychange", "pagehide", "pageshow", "freeze", "resume"]) {
  addEventListener(ev, () => noteEvent(ev), { passive: true, capture: true });
}

// The wall clock against the monotonic one. They diverge when the process is
// suspended rather than wedged — a laptop lid, a sleeping machine, a tab the
// browser froze — which reproduces the freeze signature exactly and is the
// first thing to rule out. The ?trace worker has reported this all along and
// the freeze record never has.
const CLOCK_SKEW0 = Date.now() - performance.now();

// One sample per tick, and deliberately not resourceStats(): that resets the
// rate baselines the ?debug log and the memory watchdog read from, so polling it
// would quietly zero everyone else's rates. These are counters already being
// kept, copied.
function stallSample() {
  return { t: Math.round(performance.now()), vis: Math.round(visibleMs()),
           frames, hidden: document.hidden, stalled: Math.round(visibleMs() - frameAtVis),
           // the browser's frame clock beside this page's frame count: the pair
           // is what dates the moment rendering stopped, and says whose it was
           tick: renderTick(), winH: innerHeight, cvH: cv.height,
           tiles: tileCache.size, decodes: tileDecodes, evictions: tileEvictions,
           composes: bgComposes, errors: frameErrors, slow: slowFrames,
           // the churn on the way in, and what the compositor was being handed:
           // `base` is the 17-megapixel PNG going down at `k`*DPR scale
           queued: tileQueue.length, base: baseDrawn, hold: tileHoldFrames,
           cost: +(frameCostN ? frameCostSum / frameCostN : 0).toFixed(1),
           // and the same millisecond split three ways, so the run-up says
           // which phase was growing rather than only that the frame was
           k: +view.k.toFixed(3), sprites: spriteDraws,
           cCompose: +(frameCostN ? costComposeSum / frameCostN : 0).toFixed(1),
           cBlit: +(frameCostN ? costBlitSum / frameCostN : 0).toFixed(1),
           cSprites: +(frameCostN ? costSpriteSum / frameCostN : 0).toFixed(1),
           // suspension, and the browser taking the surfaces away: the two
           // explanations for a dead rendering pass that are not this page's
           drift: Math.round(Date.now() - performance.now() - CLOCK_SKEW0),
           lost: ctxLost };
}

// Whether the stored record is this session's, so the verdict and the recovery
// below attach to the stall they describe rather than to an older one kept for
// its run-up.
let ownsRecord = false, verdictPending = false;
let stallTick = 0, stallFrames = 0, stallAtVis = 0;
const PAGE_TITLE = document.title;

// Amend the stored record in place. The stall is the one time storage is worth
// touching more than once: nothing is drawing, so there is no hot path to stay
// off, and what happens *after* detection is half the evidence.
function amendRecord(fields) {
  if (!ownsRecord) return;
  try {
    const rec = JSON.parse(localStorage.getItem(STALL_KEY) || "null");
    if (rec) localStorage.setItem(STALL_KEY, JSON.stringify(Object.assign(rec, fields)));
  } catch (e) { /* storage full or disabled; the console record still stands */ }
}

setInterval(() => {
  // A resize event that was never delivered leaves the canvas cut for a window
  // that no longer exists, and it stays that way until the next resize — the
  // drawing buffer permanently the wrong size for the CSS box, which stretches
  // everything drawn into it. That is not hypothetical: the freeze this
  // watchdog caught reported a 2560x1331 canvas in a 2560x1081 window, because
  // resize steps ride the same "update the rendering" pass as rAF and stopped
  // with it. Reconciling here costs two property reads a second and heals it
  // whenever frames come back, whatever ate the event.
  if (innerWidth !== W || innerHeight !== H) resize();

  if (!frames) return;                 // nothing has drawn yet; still loading
  const sample = stallSample();
  stallRunup.push(sample);
  if (stallRunup.length > STALL_RUNUP) stallRunup.shift();

  if (sample.stalled < STALL_VISIBLE_MS) {
    if (inStall) {                     // it came back
      document.title = PAGE_TITLE;
      console.warn(`[transit] frames resumed after ${Math.round(sample.vis - stallAtVis)} ms `
                 + "of visible stall.");
      amendRecord({ recovered: {
        afterVisibleMs: Math.round(sample.vis - stallAtVis),
        framesSince: frames - stallFrames, tickSince: sample.tick - stallTick,
      } });
    }
    inStall = false;
    return;
  }

  if (inStall) {
    // Keep asking for a frame, once a second, for as long as it is stalled. One
    // request at detection only covers a registration dropped at that instant;
    // if the browser stopped delivering and later starts again, whether the loop
    // comes back depends on there being a live request when it does. frame()
    // drops a duplicate callback, so the standing request and this one cannot
    // both take.
    requestAnimationFrame(frame);
    // One tick after detection, still stalled: the browser's frame clock has
    // had a full second to move, and whether it did is the whole diagnosis.
    // Recorded once — after that there is nothing further to learn by watching.
    if (verdictPending) {
      verdictPending = false;
      const browserRendering = sample.tick > stallTick;
      console.error(browserRendering
        ? "[transit] the browser is still producing frames and this page is not being "
        + "called — the rAF registration was lost. Re-armed; if this line is followed "
        + "by a resume, that was it."
        : "[transit] the browser has produced no frames for a second while this document "
        + "is visible and asking to draw. The stall is below the page: nothing drawn from "
        + "here can reach the screen. Reopen the tab — a reload keeps the same process.");
      amendRecord({ verdict: {
        browserRendering, tickAtStall: stallTick, tickAfter: sample.tick,
        framesAfter: frames - stallFrames, winH: innerHeight, cvH: cv.height,
      } });
    }
    return;                            // already recorded this one
  }

  inStall = true;
  stalls++;
  if (!firstStallAt) firstStallAt = Math.round(Date.now());
  // resourceStats() is called here and only here, so the run-up above stays
  // cheap and the snapshot below is the expensive one, taken once.
  const snap = resourceStats();
  stallTick = sample.tick; stallFrames = frames; stallAtVis = sample.vis;
  verdictPending = true;
  console.error(`[transit] the page has been visible and not drawing for `
              + `${sample.stalled} ms — this is the freeze, recorded. `
              + "transitFreeze() reads it back, and survives closing the tab.", snap);
  // The tab strip is painted by the browser, not by this document, so the title
  // still reaches the user when nothing drawn here can. It is the only channel
  // left when the rendering pass itself has stopped — an overlay would sit in
  // the same dead pass as the canvas.
  document.title = "⚠ frozen — " + PAGE_TITLE;
  // And ask for a frame again. If the loop merely lost its registration this
  // recovers it within a frame and costs one call; if the browser has stopped
  // rendering, it is ignored along with the registration already outstanding.
  // frame() drops a duplicate callback, so recovering cannot leave two loops.
  requestAnimationFrame(frame);
  try {
    const prior = JSON.parse(localStorage.getItem(STALL_KEY) || "null");
    // Keep the first stall of *this* session: it is the one with the healthy-to-
    // stalled transition in front of it. Later ones in the same session only
    // bump the count. A record from an earlier run is superseded rather than
    // preserved — a fresh freeze, carrying its own run-up and its own verdict,
    // is worth more than an old one — but it steps back one slot first, so
    // nothing that has not been read yet disappears without warning.
    if (prior && prior.session === STALL_SESSION) {
      prior.stalls = stalls;
      prior.lastAt = Date.now();
      localStorage.setItem(STALL_KEY, JSON.stringify(prior));
    } else {
      if (prior) localStorage.setItem(STALL_PREV_KEY, JSON.stringify(prior));
      localStorage.setItem(STALL_KEY, JSON.stringify({
        at: Date.now(), session: STALL_SESSION, stalls, ua: navigator.userAgent,
        screen: [innerWidth, innerHeight, DPR], snapshot: snap, runup: stallRunup,
        events: stallEvents.slice(),
      }));
      ownsRecord = true;
    }
  } catch (e) { /* storage full or disabled; the console record still stands */ }
}, STALL_TICK_MS);

// The record from the session that froze, after reopening the tab.
// transitFreeze(1) reaches the run before it, kept so that superseding a record
// nobody has read yet is recoverable rather than final.
window.transitFreeze = (back = 0) => {
  try { return JSON.parse(localStorage.getItem(back ? STALL_PREV_KEY : STALL_KEY) || "null"); }
  catch (e) { return null; }
};

// And say at load that there is one. Reading the record takes knowing to ask for
// it, and the whole reason it is in storage is that the tab it describes is
// gone — so the session after a freeze is exactly when nobody is thinking about
// the console. Announcing it also dates the code: a record from a run older than
// the fix being tested says so, instead of being read as a result of it.
{
  const rec = window.transitFreeze();
  if (rec) console.warn(
    `[transit] a freeze was recorded ${new Date(rec.at).toLocaleString()}`
    + `${rec.session === STALL_SESSION ? "" : " (an earlier run)"} — `
    + `${rec.stalls} stall${rec.stalls === 1 ? "" : "s"}, `
    + `${rec.verdict ? "verdict: " + (rec.verdict.browserRendering
        ? "the page lost its rAF registration"
        : "the browser stopped rendering")
      : "no verdict recorded"}. `
    + "transitFreeze() reads it back; the next freeze supersedes it, and "
    + "transitFreeze(1) reaches the one before.");
}

// ---- black box (?trace) ----
// Everything above reports from inside the page, which is the one place that
// stops reporting when the page is what fails. The freeze takes the tab with
// it — console gone, devtools gone, and closing the tab, the only cure, throws
// the evidence away. Twice now the last thing we had was a snapshot that looked
// healthy, which is not a diagnosis.
//
// So under ?trace the page keeps a second record, on a second thread, and ships
// it out of the process as it goes. A Worker is the whole trick: it has its own
// event loop, so it keeps running while the main thread is wedged, and it holds
// the last snapshot the main thread managed to hand it. When the snapshots stop
// arriving it says so, and keeps saying so, with that last state attached. That
// turns "the tab froze and there's nothing" into "the main thread stopped at T,
// and here is what it was doing at T minus half a second".
//
// It also tells apart the three failures that look identical from inside:
//   - the main thread is blocked      — samples stop, the worker keeps posting
//   - the main thread is fine and the compositor is not
//                                     — samples continue, rafGapMax climbs
//   - the content process is gone     — the posts stop too, and the last record
//                                       in the file is the epitaph
// A worker that goes quiet *with* the main thread is itself the finding: that
// is the process dying, not a script hanging.
//
// scripts/freeze_log.py receives the posts and writes them to
// scratch/freeze-trace.jsonl; scripts/freeze_report.py reads them back. If
// nothing is listening the records still accumulate in localStorage, so
// transitTrace() after reopening the tab gets the tail of them either way.
const TRACE = qp.has("trace");
const TRACE_KEY = "transit.trace";
const TRACE_MS = 500;         // how often the main thread proves it is alive
const TRACE_KEEP = 240;       // records held for the localStorage fallback
// One id per page load. A reload beacons its own last gasp into the same file
// as the run that follows it, so without this the reader interleaves two
// sessions whose clocks both start at zero and reads the seam as a fault.
const TRACE_SID = Math.random().toString(36).slice(2, 10);
let traceSeq = 0, traceRing = [], traceWorker = null;
let frameMs = [], rafGaps = [];   // lastRafAt lives with the diagnostics above
let longTasks = 0, longTaskMs = 0, evWheel = 0, evPointer = 0, canvasesMade = 0;
let lastWall = Date.now(), lastMono = performance.now(), lastTick = performance.now();

// Called from frame(). Two numbers, no allocation beyond the push: the cost of
// the frame itself, and the gap since the previous one — which is the only way
// to see rAF being throttled or dropped, as opposed to running slowly. Both are
// measured in frame() now, since the snapshot reports the gap too; this only
// keeps the distribution the trace records over each interval.
function traceFrame(cost, gap) {
  if (!TRACE) return;
  if (gap) rafGaps.push(gap);
  frameMs.push(cost);
}

function pct(a, p) {
  if (!a.length) return 0;
  const b = Float64Array.from(a).sort();
  return Math.round(b[Math.min(b.length - 1, Math.floor(b.length * p))]);
}

const TRACE_WORKER_SRC = `
// Runs on its own thread. Holds the newest sample, notices when they stop, and
// posts everything it has to the recorder — including, and especially, while
// the main thread is not answering.
let last = null, lastAt = 0, stalledSince = 0, queue = [], busy = false, tick = 0;
let hidden = false, throttledAt = 0;
const STALL_MS = 1600;        // silence longer than three sample periods
const THROTTLE_EVERY = 15000; // a backgrounded tab is quiet on purpose; say so rarely
function push(r) {
  if (!r) return;               // a throttle report suppressed by its rate limit
  r.sid = SID;
  queue.push(r);
  if (queue.length > 600) queue.splice(0, queue.length - 600);
}
async function flush() {
  if (busy || !queue.length) return;
  busy = true;
  const batch = queue.splice(0, queue.length);
  try {
    await fetch(ENDPOINT, { method: "POST", headers: { "content-type": "application/json" },
                            body: batch.map(r => JSON.stringify(r)).join("\\n") });
  } catch (e) { /* nobody listening; drop rather than grow without bound */ }
  busy = false;
}
onmessage = e => {
  const m = e.data;
  if (typeof m.hidden === "boolean") hidden = m.hidden;
  if (m.t === "vis") return;      // a visibility change, not a sample
  lastAt = performance.now();
  if (stalledSince) {
    push({ t: "resume", at: Date.now(), stalledMs: Math.round(lastAt - stalledSince),
           hidden });
    stalledSince = 0;
  }
  last = m;
  push(m);
  if (m.t === "final") flush();
};
setInterval(() => {
  // The worker's own lateness matters too: if this interval slips, the trouble
  // is the whole process, not one blocked script.
  const now = performance.now();
  const late = tick ? Math.round(now - tick - 1000) : 0;
  tick = now;
  const gap = lastAt ? now - lastAt : 0;
  if (lastAt && gap > STALL_MS) {
    if (!stalledSince) stalledSince = lastAt;
    // A hidden tab is quiet because the browser clamped its timers, not because
    // anything is wrong: Chrome holds a background tab to one wake a second and,
    // after a few minutes, to one a minute. Measured on this page, a backgrounded
    // tab produced 83 "stalls" up to 12.8 s long with the worker never once late
    // — every one of them an artifact. Calling that a blocked main thread would
    // bury a real freeze under noise, so it is reported as what it is.
    push(hidden
      ? (now - throttledAt < THROTTLE_EVERY ? null : (throttledAt = now,
          { t: "throttled", at: Date.now(), silentMs: Math.round(gap), hidden: true }))
      : { t: "stall", at: Date.now(), silentMs: Math.round(gap),
          workerLateMs: late, hidden: false, last });
  } else if (late > 250) {
    push({ t: "workerLate", at: Date.now(), lateMs: late });
  }
  flush();
}, 1000);
`;

function traceRecord(rec) {
  rec.sid = TRACE_SID;
  traceRing.push(rec);
  if (traceRing.length > TRACE_KEEP) traceRing.splice(0, traceRing.length - TRACE_KEEP);
  if (traceWorker) traceWorker.postMessage(rec);
}

function traceSample(kind) {
  const wall = Date.now(), mono = performance.now();
  const s = resourceStats();
  const rec = {
    t: kind, n: ++traceSeq, at: wall, up: Math.round(mono),
    // How late this very sample ran is the cheapest read on main-thread
    // contention there is: it is scheduled every TRACE_MS and nothing else
    // has to cooperate for it to be true.
    tickLateMs: Math.round(mono - lastTick - TRACE_MS),
    // Wall clock minus monotonic clock. They advance together unless the
    // process was suspended or descheduled, so a jump here is the OS or the
    // browser taking the tab away rather than anything the page did.
    driftMs: Math.round((wall - lastWall) - (mono - lastMono)),
    frames: frameMs.length, frameP50: pct(frameMs, 0.5), frameP95: pct(frameMs, 0.95),
    frameMax: Math.round(frameMs.reduce((a, b) => b > a ? b : a, 0)),
    rafGapMax: Math.round(rafGaps.reduce((a, b) => b > a ? b : a, 0)),
    longTasks, longTaskMs: Math.round(longTaskMs),
    wheel: evWheel, pointer: evPointer,
    hidden: document.hidden, playing, speed: +speedSel.value,
    // Canvases are counted as they are *made*, not as they are found. A sprite
    // canvas is never appended to the document, so the DOM knows nothing about
    // it — the DOM count read 1 on a page holding 324 of them. Creation is the
    // number that matters anyway: this page has frozen once before because the
    // sprite cache built a fresh canvas per zoom step and their buffers outran
    // collection, and that shows up here as canvasesMade climbing without end.
    canvasesMade,
    ...s,
  };
  if (performance.memory) {                 // Chrome: JS heap only, but free
    rec.heapMB = +(performance.memory.usedJSHeapSize / 1048576).toFixed(1);
    rec.heapCapMB = +(performance.memory.jsHeapSizeLimit / 1048576).toFixed(1);
  }
  lastTick = mono; lastWall = wall; lastMono = mono;
  frameMs = []; rafGaps = []; longTasks = 0; longTaskMs = 0; evWheel = 0; evPointer = 0;
  traceRecord(rec);
  return rec;
}

if (TRACE) {
  try {
    // The endpoint is baked in absolute, because a worker made from a blob: URL
    // cannot resolve a relative one: its self.location is the blob URL, and
    // resolving "/_trace" against that throws inside URL() and inside fetch().
    // The throw is caught by the worker's own try/catch, so the failure is
    // completely silent — the recorder simply records nothing, which is the one
    // way this could fail that would waste a whole reproduction.
    traceWorker = new Worker(URL.createObjectURL(new Blob(
      [`const ENDPOINT = ${JSON.stringify(location.origin + "/_trace")};\n`,
       `const SID = ${JSON.stringify(TRACE_SID)};\n`,
       TRACE_WORKER_SRC], { type: "text/javascript" })));
  } catch (e) {
    console.warn("[transit] trace worker unavailable, falling back to localStorage:", e);
  }
  // Counted with capture-phase listeners of their own, so the real handlers
  // stay exactly as they are and a gesture can still be correlated with a
  // freeze that happened during one.
  addEventListener("wheel", () => evWheel++, { passive: true, capture: true });
  addEventListener("pointermove", () => evPointer++, { passive: true, capture: true });
  const makeEl = document.createElement.bind(document);
  document.createElement = (tag, ...rest) => {
    if (String(tag).toLowerCase() === "canvas") canvasesMade++;
    return makeEl(tag, ...rest);
  };
  try {
    new PerformanceObserver(list => {       // Chrome; Firefox reports no longtask
      for (const e of list.getEntries()) { longTasks++; longTaskMs += e.duration; }
    }).observe({ entryTypes: ["longtask"] });
  } catch (e) { /* not supported: tickLateMs covers the same ground more coarsely */ }

  traceRecord({
    t: "open", at: Date.now(), url: location.href, ua: navigator.userAgent,
    dpr: DPR, w: W, h: H, isolated: self.crossOriginIsolated === true,
    cores: navigator.hardwareConcurrency, deviceMemoryGB: navigator.deviceMemory || null,
  });
  setInterval(() => traceSample("sample"), TRACE_MS);
  // Tell the worker the moment visibility changes rather than letting it learn
  // from the next sample: going hidden is exactly when the samples stop, so the
  // news has to travel ahead of the silence it explains.
  addEventListener("visibilitychange", () =>
    traceRecord({ t: "vis", at: Date.now(), hidden: document.hidden }), true);
  // The tail of the record survives the tab even with no server listening.
  setInterval(() => {
    try { localStorage.setItem(TRACE_KEY, JSON.stringify(traceRing.slice(-TRACE_KEEP))); }
    catch (e) { /* quota or private mode; the POSTs are the real channel */ }
  }, 2000);
  // A breakdown of what the tab is holding, by type — JavaScript, DOM, and
  // which realm each belongs to. Chrome, and only when cross-origin-isolated,
  // which is what freeze_log.py's headers are for. It is slow, so rarely.
  //
  // Read it next to imageMB rather than instead of it: it measured 27.8 MB on a
  // page whose decoded images were 102 MB, because canvas backing stores and
  // decoded image surfaces are not in its scope. That gap is the useful part —
  // this number moving means the JS side is growing, and this number staying
  // put while the tab dies says the memory is somewhere it cannot see.
  if (performance.measureUserAgentSpecificMemory) {
    setInterval(async () => {
      try {
        const m = await performance.measureUserAgentSpecificMemory();
        traceRecord({ t: "uaMemory", at: Date.now(),
                      totalMB: +(m.bytes / 1048576).toFixed(1),
                      breakdown: m.breakdown.filter(b => b.bytes > 0).map(b => ({
                        mb: +(b.bytes / 1048576).toFixed(1),
                        types: b.types, scopes: b.attribution.map(a => a.scope),
                      })) });
      } catch (e) { /* rejected while the document is hidden, and other refusals */ }
    }, 20000);
  }
  for (const ev of ["pagehide", "visibilitychange", "freeze"]) {
    addEventListener(ev, () => {
      const rec = traceSample("final");
      rec.reason = ev;
      // sendBeacon survives the document going away in a way fetch does not.
      try { navigator.sendBeacon("/_trace", JSON.stringify(rec)); } catch (e) {}
    });
  }
  console.log("[transit] tracing to /_trace and localStorage — transitTrace() "
            + "reads the tail back after a reload, even if the tab was closed.");
}

// The tail of the record, straight from the console, after reopening the tab.
window.transitTrace = () => {
  try { return JSON.parse(localStorage.getItem(TRACE_KEY) || "[]"); }
  catch (e) { return []; }
};

// The next frame is scheduled first, before any of the work — so nothing in the
// body of this function can end the loop, whether or not it is caught.
//
// It used to be scheduled last, outside the try, on the reasoning that keeping
// it out of the try protected it. It didn't: traceFrame() and noteSlowFrame()
// also sit outside the try, between the catch and the scheduling, and both
// allocate — which is precisely what fails when the process is short of memory.
// Either one throwing would have ended the animation while reporting
// `frameErrors: 0`, making it indistinguishable from a freeze below the page.
// Asking for the next frame up front costs nothing and removes the ambiguity.
// Callbacks registered for the same frame are handed the same timestamp, so this
// is what makes the watchdog's re-arm safe: if the original registration was
// live all along and the browser simply wasn't ticking, both fire when it
// resumes, and the second returns here without re-arming. One loop survives,
// which is the point — two would double every frame's work for the rest of the
// session, permanently, as the price of a recovery that already worked.
let lastRafNow = -1;
function frame(now) {
  if (now === lastRafNow) return;      // duplicate registration; one loop is enough
  lastRafNow = now;
  requestAnimationFrame(frame);
  const t0 = performance.now();
  frames++;
  // Liveness, so a snapshot can report real fps — and the gap since the previous
  // callback, which is the one number that separates a loop running slowly from
  // a loop not being called at all.
  const gap = lastRafAt ? t0 - lastRafAt : 0;
  if (gap > rafGapMax) rafGapMax = gap;
  lastRafAt = lastFrameAt = t0;
  frameAtVis = visibleMs();   // the watchdog measures the stall from here
  try {
    drawFrame(now);
  } catch (err) {
    noteFrameError(err);
  }
  const cost = performance.now() - t0;
  // What a frame actually costs this page, as opposed to how often it is called.
  // fps has read ~43 on this display through every freeze, and that is two very
  // different things: a page spending 23 ms a frame is saturating the main
  // thread and the canvas is too big for it, while a page spending 3 ms and
  // still getting 43 callbacks is being given fewer frames than it could use,
  // and its own drawing is not what to look at. slowFrames cannot tell them
  // apart — it only fires past 500 ms, and has read 0 in all seven reports.
  frameCostSum += cost; frameCostN++;
  if (cost > frameCostMax) frameCostMax = cost;
  // The same millisecond, split three ways. Charged after the fact so a frame
  // that threw is still counted whole by the line above: drawFrame sets the two
  // measured phases and the sprites are the remainder, floored at zero in case
  // an exception left last frame's values behind.
  costComposeSum += frameComposeMs; costBlitSum += frameBlitMs;
  costSpriteSum += Math.max(0, cost - frameComposeMs - frameBlitMs);
  if (frameComposeMs > costComposeMax) costComposeMax = frameComposeMs;
  frameComposeMs = frameBlitMs = 0;
  traceFrame(cost, gap);
  if (cost > SLOW_FRAME_MS) noteSlowFrame(cost, t0);
}

// The gap since the previous frame, in seconds, but never more than a beat.
//
// `now - lastFrame` is real elapsed time, and after a tab has been in the
// background it is however long the tab was away — rAF stops there and the first
// frame back carries the whole absence. At 400x that made a single frame advance
// the simulation by hours, and one subtraction of a day doesn't bring a jump
// like that back into range: the clock landed outside [0, 86400), no trip's
// window contained it, and every vehicle on the map faded out for that frame.
// It corrected itself on the next one, so what it left was a blink of an empty
// map on returning to the tab — and, if that frame happened to be the last one
// drawn, an empty map for as long as the tab stayed that way.
//
// Clamping is the right answer rather than wrapping properly, because there is
// no sense in which the missed time should be played: nothing was drawn for it.
// A frame that arrives late gets one frame's worth of clock and the animation
// picks up where the eye left it.
const MAX_STEP_SEC = 0.25;   // 4 fps; below this, real frame pacing is preserved

function drawFrame(now) {
  const dt = Math.min(MAX_STEP_SEC, Math.max(0, (now - lastFrame) / 1000));
  lastFrame = now;
  if (playing) {
    simT += dt * +speedSel.value;
    if (simT >= 86400) simT -= 86400;
    scrub.value = simT | 0;
  }
  // Whether tiles may be fetched this frame — see getTile. Decided once, here,
  // so every lookup in the frame agrees, and so the moment the view lands can be
  // seen: nothing has changed at that instant, so composeBackground would skip,
  // and the frame that would have asked for the tiles never runs. One forced
  // recompose is what turns "stopped moving" into "fetch what is on screen".
  const vkey = `${view.x}|${view.y}|${view.k}`;
  if (vkey !== viewSeen) { viewSeen = vkey; viewMovedAt = performance.now(); }
  const still = performance.now() - viewMovedAt >= TILE_SETTLE_MS;
  if (still && !tilesMayLoad) bgDirty = true;
  tilesMayLoad = still;
  if (!still) tileHoldFrames++;
  const tCompose = performance.now();
  composeBackground();
  const tBlit = performance.now();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.drawImage(bg, 0, 0);
  ctx.setTransform(DPR * view.k, 0, 0, DPR * view.k, -view.x * DPR * view.k, -view.y * DPR * view.k);
  // The two phases that are one draw each. Everything after this is sprites,
  // and frame() charges it what the frame cost less these two.
  frameComposeMs = tBlit - tCompose;
  frameBlitMs = performance.now() - tBlit;

  // clock above the Metro logo (map px: logo x 240-460, y 3640-3900)
  ctx.fillStyle = "#1a1a1a";
  ctx.font = "700 110px -apple-system, 'Helvetica Neue', Arial";
  ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
  const hh = String(Math.floor(simT / 3600)).padStart(2, "0");
  const mm = String(Math.floor(simT % 3600 / 60)).padStart(2, "0");
  ctx.fillText(`${hh}:${mm}`, 240, 3590);

  // path inspector: stroke the whole line the selected vehicle runs (and its
  // downtown mirror), so its variant reads against the artwork
  if (pathTrip >= 0) {
    const tr = trips[pathTrip], pat = tr && data.patterns[tr.p];
    if (pat && sysOn[data.routes[tr.r].sy]) {
      ctx.lineJoin = ctx.lineCap = "round";
      const stroke = shape => {
        ctx.lineWidth = 8 / view.k; ctx.strokeStyle = "rgba(0,0,0,.7)"; strokeShape(shape);
        ctx.lineWidth = 4.5 / view.k; ctx.strokeStyle = PATH_INK; strokeShape(shape);
      };
      stroke(shapes[pat.s]);
      const runs = insetRuns[pat.s];
      if (runs && insetRect) {
        ctx.save();
        ctx.beginPath();
        ctx.rect(insetRect[0], insetRect[1], insetRect[2] - insetRect[0], insetRect[3] - insetRect[1]);
        ctx.clip();
        for (const run of runs) if (run) stroke(run);
        ctx.restore();
      }
    } else pathTrip = -1;   // the trip's system was filtered out
  }

  // vehicles
  let active = 0, drawn = 0;
  const t = simT;
  const s = spriteScale();
  const insetDraws = [];
  const step = dt / FADE_SEC;   // opacity change this frame (real time, not sim time)
  for (let i = 0; i < trips.length; i++) {
    const tr = trips[i];
    const pat = data.patterns[tr.p];
    // a vehicle is "present" while its trip is running and its system is shown;
    // ease opacity toward that so it fades in on appear and out on disappear
    const present = pat && t >= tr.t0 && t <= tr.t1 && sysOn[data.routes[tr.r].sy];
    let a = vAlpha[i] + (present ? step : -step);
    a = a < 0 ? 0 : a > 1 ? 1 : a;
    vAlpha[i] = a;
    if (a <= 0.01 || !pat) continue;
    // position from the trip clock, clamped to the trip window so a vehicle
    // fading out past its last stop lingers at the terminal, not at (0,0)
    const tc = t < tr.t0 ? tr.t0 : t > tr.t1 ? tr.t1 : t;
    const times = tr.times, d = pat.d;
    let lo = 0, hi = times.length - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; (times[m] <= tc ? lo = m : hi = m); }
    const dist = distAt(times, d, lo, hi, tc, data.routes[tr.r].rail);
    const [x, y] = posAlong(shapes[pat.s], dist);
    if (x < -15 || x > map.width + 15 || y < 708 || y > map.height + 15) continue;  // off the drawn map (708 = under the title banner)
    const sp = sprites[tr.r];
    if (a < 1) ctx.globalAlpha = a;
    const [spx, sw] = spriteSize(sp.half, s);
    ctx.drawImage(spriteAt(tr.r, spx), x - sw / 2, y - sw / 2, sw, sw);
    drawn++;
    if (i === pathTrip) {   // ring the vehicle whose path is shown
      ctx.beginPath(); ctx.arc(x, y, (sp.half + 4) * s, 0, 7);
      ctx.lineWidth = 2 * s; ctx.strokeStyle = "#111"; ctx.stroke();
    }
    if (a < 1) ctx.globalAlpha = 1;
    if (present) active++;
    // mirror into the DTLA inset panel. Inset motion is computed in inset
    // space (the schematic main map collapses downtown): each stop knows its
    // run + distance along the run's inset polyline, and the vehicle's
    // progress through the current segment interpolates between them.
    const ir = pat.ir;
    if (ir) {
      const ra = ir[lo], rb = ir[hi];
      let run = -1, i0 = 0, i1 = 0;
      if (ra >= 0 && ra === rb) { run = ra; i0 = pat.id[lo]; i1 = pat.id[hi]; }
      else if (ra < 0 && rb >= 0) { run = rb; i0 = 0; i1 = pat.id[hi]; }             // entering
      else if (ra >= 0 && rb < 0) {                                                  // leaving
        run = ra; i0 = pat.id[lo];
        const g = insetRuns[pat.s][ra]; i1 = g.cum[g.cum.length - 1];
      }
      if (run >= 0) {
        // progress through the segment: from the main-map Hermite when the
        // segment has extent there, else by time (downtown is so compressed
        // on the main map that adjacent stops can share a rounded distance)
        const span = d[hi] - d[lo], tspan = times[hi] - times[lo];
        const fp = span > 0 ? Math.min(1, Math.max(0, (dist - d[lo]) / span))
                 : tspan > 0 ? (tc - times[lo]) / tspan : 0;
        const [ix, iy] = posAlong(insetRuns[pat.s][run], i0 + (i1 - i0) * fp);
        insetDraws.push(tr.r, ix, iy, a);
      }
    }
  }
  if (insetDraws.length && insetRect) {
    ctx.save();
    ctx.beginPath();
    ctx.rect(insetRect[0], insetRect[1], insetRect[2] - insetRect[0], insetRect[3] - insetRect[1]);
    ctx.clip();
    for (let k = 0; k < insetDraws.length; k += 4) {
      const ri = insetDraws[k], x = insetDraws[k+1], y = insetDraws[k+2], a = insetDraws[k+3];
      if (a < 1) ctx.globalAlpha = a;
      const [spx, sw] = spriteSize(sprites[ri].half, s);
      ctx.drawImage(spriteAt(ri, spx), x - sw / 2, y - sw / 2, sw, sw);
      drawn++;
      if (a < 1) ctx.globalAlpha = 1;
    }
    ctx.restore();
  }
  // How many draws the sprite phase above actually made. The cull is against
  // the drawn *map*, not the viewport, so at deep zoom most of these land
  // entirely outside the canvas — worth knowing beside what the phase cost.
  spriteDraws = drawn;
  const hhmm = `${hh}:${mm}`;
  stats.textContent = `${hhmm} · ${active} vehicles` +
    (pathTrip >= 0 && trips[pathTrip] ? ` · path: ${data.routes[trips[pathTrip].r].n}` : "") +
    (DEBUG ? ` · tiles:${tilesDrawn}/${tileCache.size}` : "");
}
