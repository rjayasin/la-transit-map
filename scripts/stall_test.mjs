// Exercise index.html's visible-clock + stall watchdog under a DOM shim.
//
// The page script is far too entangled with canvas/fetch to run whole, so this
// lifts out the block under test verbatim from index.html — the visible clock,
// the snapshot fields that hang off it, and the watchdog — and drives it with a
// fake clock, a fake visibilitychange, and a fake rAF. If the extraction stops
// matching the file the guard at the bottom fails.
//
//     node scripts/stall_test.mjs        # from the repo root
import fs from "node:fs";
import path from "node:path";

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const SRC = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");

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
  document: { hidden: false },
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

// Evaluate the extracted blocks against the shim.
const body = `
  ${clockBlock}
  ${watchdogBlock}
  return { visibleMs, frame: () => { frames++; frameAtVis = visibleMs(); },
           stalled: () => Math.round(visibleMs() - frameAtVis),
           counts: () => ({ stalls, firstStallAt }) };
`;
const names = Object.keys(env);
const api = new Function(...names, body)(...names.map(n => env[n]));

// The page's own frame() bumps `frames`, which the shim owns; keep them in step.
const drawFrame = () => { env.frames++; api.frame(); };

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
check("stall: console.error fired", logs.error.length, 1);
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

// 8. the frame clock cannot jump the simulation out of range. One subtraction
//    of a day is all drawFrame does, so a single frame's advance has to stay
//    under a day — which is a property of MAX_STEP_SEC against the fastest
//    speed the page offers, and breaks silently if either is changed.
const maxStep = +SRC.match(/const MAX_STEP_SEC = ([\d.]+)/)[1];
const maxSpeed = Math.max(...[...SRC.matchAll(/<option value="(\d+)"/g)].map(m => +m[1]));
check("clock: one frame stays inside a day", maxStep * maxSpeed < 86400, true);
check("clock: dt is clamped and floored", /Math\.min\(MAX_STEP_SEC, Math\.max\(0,/.test(SRC), true);

// ---- guard: the extraction still matches the file -------------------------
check("index.html still defines the visible clock", SRC.includes("function visibleMs()"), true);
check("index.html still reports stalledVisibleMs", SRC.includes("stalledVisibleMs:"), true);
check("frame() marks the visible clock", SRC.includes("frameAtVis = visibleMs();   // the watchdog"), true);

let bad = 0;
for (const [name, got, want, ok] of results) {
  if (!ok) bad++;
  console.log(`${ok ? "ok  " : "FAIL"}  ${name}${ok ? "" : `  got ${got} want ${want}`}`);
}
console.log(bad ? `\n${bad} failed` : `\nall ${results.length} passed`);
process.exit(bad ? 1 : 0);
