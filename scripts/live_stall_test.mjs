// Drive the real page in a real browser over CDP: load it, kill rAF with the
// document visible, and check the watchdog catches it, names who stopped, warns
// in the tab strip, and recovers when rAF comes back.
//
// scripts/stall_test.mjs proves the arithmetic under a shim. This proves the
// browser actually behaves the way the shim assumes — that a page which stops
// getting frames while the browser keeps rendering is caught, told apart from
// the freeze that was captured in the wild, and brought back by the re-arm
// without a reload.
//
//     python3 -m http.server 8741      # from the repo root, in another shell
//     node scripts/live_stall_test.mjs
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

// Any headless-capable Chrome will do. CHROME= overrides.
function findChrome() {
  if (process.env.CHROME) return process.env.CHROME;
  const cache = path.join(process.env.HOME, ".cache/puppeteer/chrome-headless-shell");
  if (fs.existsSync(cache)) {
    for (const v of fs.readdirSync(cache)) {
      const hit = path.join(cache, v, "chrome-headless-shell-mac-arm64/chrome-headless-shell");
      if (fs.existsSync(hit)) return hit;
    }
  }
  const app = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (fs.existsSync(app)) return app;
  return null;
}

const BIN = findChrome();
const PORT = 9333;
const URL = "http://localhost:8741/index.html?debug";

if (!BIN) {
  console.log("no Chrome found — set CHROME=/path/to/chrome. Skipping.");
  process.exit(0);
}
try {
  await fetch(URL, { method: "HEAD" });
} catch {
  console.error(`nothing serving ${URL} — run \`python3 -m http.server 8741\` from the repo root.`);
  process.exit(1);
}

const chrome = spawn(BIN, [
  `--remote-debugging-port=${PORT}`, "--headless=new",
  "--window-size=2560,1331", "--force-device-scale-factor=2",
  "--user-data-dir=/tmp/cdp-profile-lastall", "--no-first-run",
  "--enable-gpu-rasterization", "about:blank",
], { stdio: "ignore" });

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function targets() {
  for (let i = 0; i < 60; i++) {
    try { return await (await fetch(`http://127.0.0.1:${PORT}/json`)).json(); }
    catch { await sleep(200); }
  }
  throw new Error("chrome never came up");
}

const page = (await targets()).find(t => t.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise(r => (ws.onopen = r));

let id = 0;
const waiting = new Map();
ws.onmessage = e => {
  const m = JSON.parse(e.data);
  if (m.id && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); }
};
function send(method, params = {}) {
  const n = ++id;
  ws.send(JSON.stringify({ id: n, method, params }));
  return new Promise(r => waiting.set(n, r));
}
async function evaluate(expr) {
  const r = await send("Runtime.evaluate", {
    expression: expr, returnByValue: true, awaitPromise: true,
  });
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails));
  return r.result?.result?.value;
}

const results = [];
const check = (name, got, want) => results.push(
  [name, JSON.stringify(got), JSON.stringify(want), JSON.stringify(got) === JSON.stringify(want)]);

await send("Page.enable");
await send("Runtime.enable");
await send("Page.navigate", { url: URL });
await sleep(9000);   // map.png + schedule.json are large
// The profile is reused between runs, so the record this run is about to write
// would be waiting for the next one — and "healthy: nothing recorded" below
// asserts a precondition it has to establish rather than inherit. Cleared after
// load, since the page reads the record at startup to announce it.
await evaluate("localStorage.clear(); true");

// ---- healthy -------------------------------------------------------------
const a = await evaluate("JSON.stringify(transitDebug())").then(JSON.parse);
console.log("healthy:", {
  fps: a.fps, framesTotal: a.framesTotal, stalledVisibleMs: a.stalledVisibleMs,
  renderTick: a.renderTick, win: [a.winW, a.winH], cv: [a.cvW, a.cvH],
  imageMB: a.imageMB, zoom: a.zoom, hidden: a.hidden,
});
check("healthy: frames are being drawn", a.framesTotal > 0, true);
check("healthy: no stall", a.stalledVisibleMs < 500, true);
check("healthy: the browser's frame clock is live", a.renderTick > 0, true);
// The backing store is capped at MAX_CANVAS_PX, so dpr is no longer a round 2 on
// a wide window and the buffer is the rounded product rather than the exact one.
check("healthy: canvas matches the window",
      [a.cvW, a.cvH], [Math.round(a.winW * a.dpr), Math.round(a.winH * a.dpr)]);
check("healthy: the backing store is within the accelerated size",
      Math.max(a.cvW, a.cvH) <= 4096, true);
check("healthy: nothing recorded", await evaluate("transitFreeze() === null"), true);
check("healthy: the snapshot dates the code it is running",
      typeof a.docModified === "string" && a.docModified.length > 0, true);
const title0 = await evaluate("document.title");

// the frame clock keeps moving on its own
const t1 = await evaluate("transitDebug().renderTick");
await sleep(600);
const t2 = await evaluate("transitDebug().renderTick");
check("healthy: renderTick advances between reads", t2 > t1, true);

// ---- kill rAF, page visible ---------------------------------------------
await evaluate("window.__raf = requestAnimationFrame; window.requestAnimationFrame = () => 0;");
await sleep(7000);

const b = await evaluate("JSON.stringify(transitDebug())").then(JSON.parse);
const rec = await evaluate("JSON.stringify(transitFreeze())").then(JSON.parse);
console.log("stalled:", {
  fps: b.fps, stalledVisibleMs: b.stalledVisibleMs, hidden: b.hidden,
  frameErrors: b.frameErrors, slowFrames: b.slowFrames, renderTick: b.renderTick,
});
console.log("verdict:", rec?.verdict);

check("stall: caught", rec !== null, true);
check("stall: the page was visible", b.hidden, false);
check("stall: seconds of visible non-drawing", b.stalledVisibleMs > 4000, true);
check("stall: the signature every report carried", [b.frameErrors, b.slowFrames], [0, 0]);
check("stall: verdict recorded", !!rec.verdict, true);
// rAF was removed from *this page* while the browser kept rendering — the
// opposite of the captured freeze, and the probe has to say so.
check("stall: names the page, not the browser", rec.verdict.browserRendering, true);
check("stall: the browser's clock moved during it",
      rec.verdict.tickAfter > rec.verdict.tickAtStall, true);
check("stall: the tab strip warns",
      (await evaluate("document.title")).startsWith("⚠ frozen"), true);
check("stall: a run-up was kept", rec.runup.length > 0, true);
check("stall: the run-up carries the browser's clock",
      typeof rec.runup.at(-1).tick, "number");

// ---- give rAF back -------------------------------------------------------
await evaluate("window.requestAnimationFrame = window.__raf;");
await sleep(3000);
const c = await evaluate("JSON.stringify(transitDebug())").then(JSON.parse);
const rec2 = await evaluate("JSON.stringify(transitFreeze())").then(JSON.parse);
console.log("recovered:", { fps: c.fps, stalledVisibleMs: c.stalledVisibleMs,
                            framesTotal: c.framesTotal, recovered: rec2?.recovered });
check("recovery: drawing again", c.framesTotal > b.framesTotal, true);
check("recovery: the re-arm brought it back without a reload", c.fps > 0, true);
check("recovery: title restored", await evaluate("document.title"), title0);
check("recovery: written to the record", !!rec2.recovered, true);
check("recovery: the record still holds the stall", rec2.at, rec.at);

// one loop, not two: a doubled loop would show up as a frame count climbing at
// twice the display rate
await sleep(2000);
const d = await evaluate("JSON.stringify(transitDebug())").then(JSON.parse);
check("recovery: one loop, not two", d.fps <= 75, true);
console.log("post-recovery fps:", d.fps);

// ---- a record left by an earlier run must not swallow the next freeze ------
// This is the one that got through: localStorage outlives the session, so the
// old "is there a record already" test was true on any machine that had ever
// frozen, and every later run's snapshot, run-up and verdict went nowhere.
await evaluate(`localStorage.setItem("transit.freeze", JSON.stringify({
    at: 1, session: "an-earlier-run", stalls: 5,
    snapshot: { stale: true }, runup: [{ stale: true }] }));
  localStorage.removeItem("transit.freeze.prev"); true`);
await evaluate("window.requestAnimationFrame = () => 0;");
await sleep(7000);
const stale = await evaluate("JSON.stringify(transitFreeze())").then(JSON.parse);
const prev = await evaluate("JSON.stringify(transitFreeze(1))").then(JSON.parse);
console.log("superseding:", { session: stale.session, verdict: stale.verdict?.browserRendering,
                              prevAt: prev?.at });
check("earlier run: superseded, not bumped", stale.session === "an-earlier-run", false);
check("earlier run: this run's snapshot recorded", stale.snapshot.stale, undefined);
check("earlier run: the verdict is written after all", !!stale.verdict, true);
check("earlier run: the superseded record stepped back one slot", prev.at, 1);
await evaluate("window.requestAnimationFrame = window.__raf; true");
await sleep(2500);

// and it has to survive the one thing that has ever cured a freeze
await send("Page.navigate", { url: URL });
await sleep(9000);
const after = await evaluate("JSON.stringify(transitFreeze())").then(JSON.parse);
check("after a reload: the record survives", after.at, stale.at);
check("after a reload: so does its verdict", !!after.verdict, true);

let bad = 0;
for (const [name, got, want, ok] of results) {
  if (!ok) bad++;
  console.log(`${ok ? "ok  " : "FAIL"}  ${name}${ok ? "" : `  got ${got} want ${want}`}`);
}
console.log(bad ? `\n${bad} failed` : `\nall ${results.length} passed`);
ws.close();
chrome.kill();
process.exit(bad ? 1 : 0);
