// Exercise index.html's visible-clock + stall watchdog under a DOM shim.
//
// The client is far too entangled with canvas/fetch to run whole, so this
// lifts out the block under test verbatim from app.js — the visible clock,
// the snapshot fields that hang off it, and the watchdog — and drives it with a
// fake clock, a fake visibilitychange, and a fake rAF. If the extraction stops
// matching the file the guard at the bottom fails.
//
//     node scripts/stall_test.mjs        # from the repo root
import fs from "node:fs";
import path from "node:path";

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const SRC = fs.readFileSync(path.join(ROOT, "app.js"), "utf8");

function slice(from, to) {
  const a = SRC.indexOf(from);
  const b = SRC.indexOf(to, a);
  if (a < 0 || b < 0) throw new Error(`extraction failed at ${JSON.stringify(from)}`);
  return SRC.slice(a, b);
}

const clockBlock = slice("let visClock = 0;", "// Baselines for the per-second rates");
const watchdogBlock = slice("const STALL_VISIBLE_MS", "// The record from the session that froze");

// ---- shim -----------------------------------------------------------------
let nowMs = 0;
const timers = [];
const store = new Map();
const listeners = [];
const logs = { error: [], warn: [] };

const env = {
  performance: { now: () => nowMs },
  // `timeline.currentTime` is the browser's frame clock: the shim advances it
  // only when the *browser* renders, which is the whole point of the probe —
  // the page can be getting no frames while it climbs, and vice versa.
  document: { hidden: false, title: "LA Transit — 24h", timeline: { currentTime: 0 } },
  navigator: { userAgent: "shim" },
  innerWidth: 1200, innerHeight: 800,
  localStorage: {
    getItem: k => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, v),
  },
  console: { error: (...a) => logs.error.push(a), warn: (...a) => logs.warn.push(a), log: () => {} },
  setInterval: (fn, ms) => { timers.push({ fn, ms, next: nowMs + ms }); return timers.length; },
  addEventListener: (ev, fn) => listeners.push({ ev, fn }),
  Date: { now: () => 1700000000000 + nowMs },
  // page state the samples copy
  frames: 0, tileCache: new Map(), tileDecodes: 0, tileEvictions: 0,
  bgComposes: 0, frameErrors: 0, slowFrames: 0, view: { k: 1.5 }, DPR: 2,
  tileQueue: [], baseDrawn: false, tileHoldFrames: 0,
  frameCostSum: 0, frameCostN: 0,
  // the frame-cost split and the two non-page explanations the samples copy
  costComposeSum: 0, costBlitSum: 0, costSpriteSum: 0, spriteDraws: 0, ctxLost: 0,
  DEBUG: false, TRACE: false,
  resourceStats: () => ({ fake: true }),
};

function advance(ms, step = 100) {
  for (let done = 0; done < ms; done += step) {
    nowMs += step;
    for (const t of timers) while (nowMs >= t.next) { t.next += t.ms; t.fn(); }
  }
}

function fire(hidden) {
  env.document.hidden = hidden;
  for (const l of listeners) if (l.ev === "visibilitychange") l.fn();
}

// Stand-ins for the page globals the extracted blocks reach for. These live
// inside the evaluated body rather than in `env` because the blocks *assign* to
// them — W and H through resize(), which is the page's own two lines — and a
// value passed in as a parameter could not be written back out.
const prelude = `
  let W = innerWidth, H = innerHeight, resizes = 0;
  const cv = { width: innerWidth * DPR, height: innerHeight * DPR };
  function resize() {
    W = innerWidth; H = innerHeight;
    cv.width = W * DPR; cv.height = H * DPR; resizes++;
  }
  let rafArmed = 0, rafFn = null;
  function requestAnimationFrame(fn) { rafArmed++; rafFn = fn; }
  function frame() { frames++; frameAtVis = visibleMs(); }
`;

// Evaluate the extracted blocks against the shim.
const body = `
  ${prelude}
  ${clockBlock}
  ${watchdogBlock}
  return { visibleMs, frame, renderTick,
           stalled: () => Math.round(visibleMs() - frameAtVis),
           counts: () => ({ stalls, firstStallAt }),
           setWin: (w, h) => { innerWidth = w; innerHeight = h; },
           win: () => ({ W, H, cvW: cv.width, cvH: cv.height, resizes }),
           rafArmed: () => rafArmed,
           events: () => stallEvents, during: () => stallEventsDuring, noteEvent,
           runRaf: () => { const f = rafFn; rafFn = null; if (f) f(); } };
`;
const names = Object.keys(env);
const api = new Function(...names, body)(...names.map(n => env[n]));

// The page's own frame() bumps `frames`, which the shim owns; keep them in step.
// A page frame implies the browser rendered, so it advances that clock too.
const drawFrame = () => { env.frames++; env.document.timeline.currentTime = nowMs; api.frame(); };
// The browser rendering while this page gets nothing — the case the frame clock
// exists to name, and the one no page-side counter can see.
const browserFrame = () => { env.document.timeline.currentTime = nowMs; };

// ---- the cases ------------------------------------------------------------
const results = [];
const check = (name, got, want) => {
  results.push([name, JSON.stringify(got), JSON.stringify(want), JSON.stringify(got) === JSON.stringify(want)]);
};

// 1. a page drawing normally never stalls
for (let i = 0; i < 60; i++) { advance(16, 16); drawFrame(); }
check("drawing: stalledVisibleMs ~0", api.stalled() <= 16, true);
advance(10000);
check("drawing: no stall recorded", api.counts().stalls, 0);

// 2. a backgrounded tab is not a stall, however long it lasts
for (let i = 0; i < 5; i++) { advance(16, 16); drawFrame(); }
fire(true);
advance(120000);                      // two minutes hidden
check("hidden 120 s: stalledVisibleMs", api.stalled(), 0);
check("hidden 120 s: no stall recorded", api.counts().stalls, 0);

// 3. coming back and drawing again is not a stall either — the resume itself
//    must not read as five seconds of not drawing
fire(false);
for (let i = 0; i < 400; i++) { advance(16, 16); drawFrame(); }   // ~6.4 s of frames
check("after resume: no stall recorded", api.counts().stalls, 0);

// 4. visible and not drawing IS a stall, and lands within a tick of the limit
const before = nowMs;
advance(6000);
check("visible 6 s, no frames: stall recorded", api.counts().stalls, 1);
const rec = JSON.parse(store.get("transit.freeze"));
// two lines, and deliberately: the stall, then the verdict on it a tick later
check("stall: console.error fired, then the verdict", logs.error.length, 2);
check("stall: the verdict names who stopped",
      /below the page/.test(logs.error[1][0]), true);
check("stall: record has a run-up", rec.runup.length > 0, true);
check("stall: run-up carries the stalled clock", rec.runup.at(-1).stalled >= 4000, true);
check("stall: detected within a tick of the limit",
      Math.round((rec.at - (1700000000000 + before)) / 1000), 5);

// 5. a stall already recorded doesn't re-record every tick
advance(6000);
check("still stalled: not re-recorded", api.counts().stalls, 1);

// 6. recovering and stalling again bumps the count, keeping the first record
advance(16, 16); drawFrame();
advance(6000);
check("second stall: counted", api.counts().stalls, 2);
const rec2 = JSON.parse(store.get("transit.freeze"));
check("second stall: first record kept", rec2.at, rec.at);
check("second stall: count updated in storage", rec2.stalls, 2);

// 7. hiding mid-stall stops the clock rather than inflating it
const held = api.stalled();
fire(true);
advance(60000);
check("hidden mid-stall: clock frozen", api.stalled(), held);

// 8. the verdict on the stall that just happened. The browser's frame clock did
//    not move while the page was visible and asking to draw, which is the
//    freeze as reported — and is now stated rather than inferred.
const recV = JSON.parse(store.get("transit.freeze"));
check("verdict: recorded a tick after detection", !!recV.verdict, true);
check("verdict: the browser was not rendering either", recV.verdict.browserRendering, false);
check("stall: the tab strip says so", env.document.title.startsWith("⚠ frozen"), true);
check("stall: a frame was re-armed", api.rafArmed() > 0, true);

// 9. coming back is recorded on the same record, and the title goes back
fire(false);
advance(16, 16); drawFrame();
advance(1000);
const recR = JSON.parse(store.get("transit.freeze"));
check("recovery: title restored", env.document.title, "LA Transit — 24h");
check("recovery: written to the record that froze", !!recR.recovered, true);
check("recovery: counts the frames that came back", recR.recovered.framesSince > 0, true);
check("recovery: the first record is still the first", recR.at, rec.at);

// 10. the other verdict: the browser rendering normally while this page's loop
//     gets nothing. Same stalledVisibleMs, opposite cause, opposite fix.
for (let i = 0; i < 8; i++) { advance(1000); browserFrame(); }
const recB = JSON.parse(store.get("transit.freeze"));
check("verdict: browser rendering, page not called", recB.verdict.browserRendering, true);

// 11. a resize event that never arrived is reconciled from the tick, once
const r0 = api.win().resizes;
api.setWin(1000, 600);
advance(1000);
check("dropped resize: canvas recut to the window", [api.win().cvW, api.win().cvH], [2000, 1200]);
check("dropped resize: recut once, not once a second", api.win().resizes, r0 + 1);
advance(3000);
check("no drift: no further recuts", api.win().resizes, r0 + 1);

// 12. a record left by an *earlier run* must not swallow this one. localStorage
//     outlives the session, so "is there a record already" was true on every
//     machine that had ever frozen — which kept the first freeze ever seen and
//     silently dropped every later run's snapshot, run-up and verdict. Caught in
//     the wild: a freeze two minutes after the verdict shipped left a record
//     from 26 minutes earlier with nothing but `lastAt` changed.
store.set("transit.freeze", JSON.stringify({
  at: 1, session: "an-earlier-run", stalls: 5,
  snapshot: { stale: true }, runup: [{ stale: true }],
}));
store.delete("transit.freeze.prev");
advance(16, 16); drawFrame();          // clear the stall from case 10
for (let i = 0; i < 8; i++) { advance(1000); browserFrame(); }
const recN = JSON.parse(store.get("transit.freeze"));
check("earlier run: superseded, not bumped", recN.session === "an-earlier-run", false);
check("earlier run: this run's snapshot recorded", recN.snapshot.stale, undefined);
check("earlier run: this run's run-up recorded", recN.runup.length > 1, true);
// What the user was doing on the way in. The shim's own visibility changes are
// seconds apart, so each stands alone; the coalescing is exercised below.
check("earlier run: the input run-up recorded", Array.isArray(recN.events), true);
check("earlier run: and it is this run's events",
      recN.events.every(e => e.e && e.n >= 1 && e.t1 >= e.t0), true);
check("earlier run: the verdict is written after all", !!recN.verdict, true);
check("earlier run: and it is this run's verdict", recN.verdict.browserRendering, true);
const prevN = JSON.parse(store.get("transit.freeze.prev"));
check("earlier run: the superseded record stepped back one slot", prevN.at, 1);

// 13. two stalls in the *same* session still keep the first, as before
const firstAt = recN.at;
advance(16, 16); drawFrame();
advance(6000);
const recS = JSON.parse(store.get("transit.freeze"));
check("same session: first record kept", recS.at, firstAt);
check("same session: count bumped", recS.stalls > recN.stalls, true);
check("same session: nothing pushed to the previous slot",
      JSON.parse(store.get("transit.freeze.prev")).at, 1);

// 14. the frame clock cannot jump the simulation out of range. One subtraction
//    of a day is all drawFrame does, so a single frame's advance has to stay
//    under a day — which is a property of MAX_STEP_SEC against the fastest
//    speed the page offers, and breaks silently if either is changed.
// A pinch is hundreds of wheel events a second and the ring holds 24 entries,
// so without coalescing the run-up would be one gesture and nothing before it.
advance(16, 16); drawFrame();               // drawing, so events are run-up
const evBefore = api.events().length, duringBefore = api.during().length;
for (let i = 0; i < 300; i++) api.noteEvent("wheel");
for (let i = 0; i < 125; i++) { advance(16, 16); drawFrame(); }   // 2 s of quiet
api.noteEvent("wheel");
const evs = api.events().slice(evBefore);
check("events: a burst of one kind is one entry", evs.length, 2);
check("events: and it counts them", evs[0].n, 300);
check("events: quiet ends the run", evs[1].n, 1);
check("events: the ring is bounded", api.events().length <= 24, true);

// And the poking that arrives once the picture has stopped goes to its own
// ring: a user clicking at a frozen tab was evicting the whole run-up.
advance(6000);                              // stalled again
const evAtStall = JSON.stringify(api.events());
for (let i = 0; i < 12; i++) { api.noteEvent("pointerdown"); api.noteEvent("pointerup"); }
check("events: poking a frozen tab leaves the run-up alone", JSON.stringify(api.events()), evAtStall);
check("events: and is recorded on its own", api.during().length > duringBefore, true);
check("events: the stall's own record carries both",
      (() => { const r = JSON.parse(store.get("transit.freeze"));
               return Array.isArray(r.events) && Array.isArray(r.eventsDuring); })(), true);

const maxStep = +SRC.match(/const MAX_STEP_SEC = ([\d.]+)/)[1];
const maxSpeed = Math.max(...[...SRC.matchAll(/<option value="(\d+)"/g)].map(m => +m[1]));
check("clock: one frame stays inside a day", maxStep * maxSpeed < 86400, true);
check("clock: dt is clamped and floored", /Math\.min\(MAX_STEP_SEC, Math\.max\(0,/.test(SRC), true);

// ---- guard: the extraction still matches the file -------------------------
check("app.js still defines the visible clock", SRC.includes("function visibleMs()"), true);
check("app.js still reports stalledVisibleMs", SRC.includes("stalledVisibleMs:"), true);
check("frame() marks the visible clock", SRC.includes("frameAtVis = visibleMs();   // the watchdog"), true);
check("app.js reports the browser's frame clock", SRC.includes("renderTick: renderTick()"), true);
check("app.js measures what a frame costs",
      SRC.includes("frameCostAvg: frameCostN"), true);
check("app.js records what the compositor was handed",
      SRC.includes("queued: tileQueue.length, base: baseDrawn, hold: tileHoldFrames"), true);
check("app.js splits the frame cost by phase",
      SRC.includes("costCompose: frameCostN") && SRC.includes("costSprites: frameCostN"), true);
check("the run-up carries the split too", SRC.includes("cSprites:"), true);
check("app.js notices the browser taking the canvas away",
      /addEventListener\("contextlost"/.test(SRC) && SRC.includes("ctxLost, ctxRestored"), true);
check("app.js separates suspension from a wedged pass",
      SRC.includes("CLOCK_SKEW0 = Date.now() - performance.now()"), true);
check("the freeze record keeps what the user was doing",
      SRC.includes("events: stallEvents.slice(), eventsDuring: stallEventsDuring.slice()"), true);
check("app.js keeps the poking out of the run-up",
      SRC.includes("? stallEventsDuring : stallEvents"), true);
check("app.js holds tile loading while the view moves",
      SRC.includes("if (!tilesMayLoad) return COLD;"), true);
check("app.js dates the code the tab is running",
      SRC.includes("docModified: document.lastModified"), true);
check("the watchdog reconciles a dropped resize",
      SRC.includes("if (innerWidth !== W || innerHeight !== H) resize();"), true);
check("frame() drops a duplicate rAF callback",
      /if \(now === lastRafNow\) return;/.test(SRC), true);

let bad = 0;
for (const [name, got, want, ok] of results) {
  if (!ok) bad++;
  console.log(`${ok ? "ok  " : "FAIL"}  ${name}${ok ? "" : `  got ${got} want ${want}`}`);
}
console.log(bad ? `\n${bad} failed` : `\nall ${results.length} passed`);
process.exit(bad ? 1 : 0);
