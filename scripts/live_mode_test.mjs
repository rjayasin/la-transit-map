// Switch the page into live mode, in a real browser, and check it is telling
// the truth: the clock it shows is Los Angeles', it advances at 1×, and the
// controls that would fight it are dead.
//
// Live is the one mode whose correctness can't be seen by looking at the map —
// a plausible-looking swarm at the wrong hour, or on the viewer's own zone,
// looks exactly like a right one. So the clock is computed here, independently,
// and the two are compared. Checked across a stretch with no frames too, since
// the mode exists to be left open in a background tab.
//
//     python3 -m http.server 8741      # from the repo root, in another shell
//     node scripts/live_mode_test.mjs
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
const PORT = 9335;
const URL = "http://localhost:8741/index.html";

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
  "--window-size=1600,1000", "--user-data-dir=/tmp/cdp-profile-livemode",
  "--no-first-run", "about:blank",
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

// The page's answer has to be checked against a clock the page had no hand in,
// so this is the same reading taken here rather than anything read back out.
const fmt = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles", hourCycle: "h23",
  hour: "2-digit", minute: "2-digit", second: "2-digit",
});
function laNow() {
  const p = {};
  for (const { type, value } of fmt.formatToParts(new Date())) p[type] = value;
  return (+p.hour % 24) * 3600 + +p.minute * 60 + +p.second;
}
// Signed distance between two times of day, by the short way round: every
// comparison below straddles midnight one run in ninety.
const gap = (a, b) => {
  let d = (a - b) % 86400;
  if (d > 43200) d -= 86400;
  if (d < -43200) d += 86400;
  return d;
};
const mode = label => `[...filtersEl.querySelectorAll(".modes button")]`
  + `.find(b => b.textContent === "${label}").click()`;
const pressed = `[...filtersEl.querySelectorAll(".modes button")]`
  + `.map(b => b.getAttribute("aria-pressed"))`;

await send("Page.enable");
await send("Runtime.enable");
await send("Page.navigate", { url: URL });
for (let i = 0; i < 60 && !(await evaluate("!!(window.trips && trips.length)")); i++) await sleep(500);
check("loaded", await evaluate("trips.length > 0"), true);

// ---- the default is still the time-lapse ---------------------------------
check("starts in time-lapse", await evaluate("live"), false);
check("its controls are live",
      await evaluate("[playBtn.disabled, scrub.disabled, speedSel.disabled]"), [false, false, false]);
const lapse0 = await evaluate("simT");
await sleep(1000);
check("and it runs faster than real time", gap(await evaluate("simT"), lapse0) > 30, true);

// ---- the popover carries the switch, and still carries the systems -------
await evaluate("sys.click()");
check("popover opens", await evaluate("filtersEl.classList.contains('open')"), true);
check("it offers both modes",
      await evaluate(`[...filtersEl.querySelectorAll(".modes button")].map(b => b.textContent)`),
      ["Time-lapse", "Live"]);
check("time-lapse reads as the current one", await evaluate(pressed), ["true", "false"]);
check("the system list is intact",
      await evaluate("filtersEl.querySelectorAll('.grid label').length === data.systems.length"), true);

// ---- switch to live ------------------------------------------------------
await evaluate(mode("Live"));
check("live is on", await evaluate("live"), true);
check("and reads as the current one", await evaluate(pressed), ["false", "true"]);
check("the controls it would fight are dead",
      await evaluate("[playBtn.disabled, scrub.disabled, speedSel.disabled]"), [true, true, true]);
check("the speed reads its own rate",
      await evaluate("speedSel.options[speedSel.selectedIndex].textContent"), "1×");
check("it is playing", await evaluate("playing"), true);
check("and the space bar cannot pause it",
      await evaluate("dispatchEvent(new KeyboardEvent('keydown', {code:'Space'})); playing"), true);

await sleep(400);   // a frame or two to take the clock over
const here = laNow(), there = await evaluate("simT");
console.log(`Los Angeles ${here} s, page ${there.toFixed(1)} s, off by ${gap(there, here).toFixed(2)} s`);
check("the clock is Los Angeles'", Math.abs(gap(there, here)) < 2, true);
check("the readout says which mode this is",
      await evaluate("stats.textContent.startsWith('live · ')"), true);
// Within half a step: the scrubber quantizes to 10 s, which is finer than its
// thumb can show over a 24-hour track.
check("the scrubber follows along", await evaluate("Math.abs(+scrub.value - simT) <= 5"), true);

const a0 = await evaluate("simT");
await sleep(3000);
const advanced = gap(await evaluate("simT"), a0);
console.log(`3 s of real time advanced the map ${advanced.toFixed(2)} s`);
check("it runs at 1×", Math.abs(advanced - 3) < 0.3, true);

// A tab that gets no frames for a while is the normal case for this mode, and
// integrating dt would come back behind by exactly the time it was away.
await evaluate("window.__raf = requestAnimationFrame; window.requestAnimationFrame = () => 0;");
await sleep(3000);
await evaluate("window.requestAnimationFrame = window.__raf; armFrame();");
await sleep(500);
check("frames stopping does not strand it",
      Math.abs(gap(await evaluate("simT"), laNow())) < 2, true);

// ---- and back ------------------------------------------------------------
const handover = await evaluate("simT");
await evaluate(mode("Time-lapse"));
check("live is off", await evaluate("live"), false);
check("the controls are back",
      await evaluate("[playBtn.disabled, scrub.disabled, speedSel.disabled]"), [false, false, false]);
check("with the speed that was set before", await evaluate("speedSel.value"), "60");
check("and its own rate withdrawn",
      await evaluate("[...speedSel.options].map(o => o.value)"), ["30", "60", "150", "400"]);
check("the clock carries on from where live left it",
      Math.abs(gap(await evaluate("simT"), handover)) < 90, true);
await sleep(600);
check("running again", gap(await evaluate("simT"), handover) > 10, true);
check("and the readout drops the mark",
      await evaluate("stats.textContent.startsWith('live · ')"), false);

// ---- ?live opens straight into it ----------------------------------------
await send("Page.navigate", { url: URL + "?live" });
for (let i = 0; i < 60 && !(await evaluate("!!(window.trips && trips.length)")); i++) await sleep(500);
await sleep(600);
check("?live: opens live", await evaluate("live"), true);
check("?live: on Los Angeles' clock",
      Math.abs(gap(await evaluate("simT"), laNow())) < 2, true);
check("?live: the mode row shows it", await evaluate(pressed), ["false", "true"]);
check("?live: nothing threw on the way", await evaluate("frameErrors"), 0);

const pad = Math.max(...results.map(r => r[0].length));
let bad = 0;
for (const [name, got, want, ok] of results) {
  if (!ok) bad++;
  console.log(`${ok ? "ok  " : "FAIL"} ${name.padEnd(pad)}  got ${got}${ok ? "" : `  want ${want}`}`);
}
console.log(bad ? `\n${bad} failed` : `\nall ${results.length} passed`);
ws.close();
chrome.kill();
process.exit(bad ? 1 : 0);
