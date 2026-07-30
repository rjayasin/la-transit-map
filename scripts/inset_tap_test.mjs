// Tap a vehicle in the Downtown call-out panel, in a real browser, and check
// the path inspector picks it — the same as tapping one on the main map.
//
// The panel mirrors a downtown run into its own geometry, so a vehicle inside
// it is drawn somewhere the main-map position never goes. The picker used to
// test only the main-map position, so every tap in the panel landed on nothing.
// Now both go through one placement (`insetPosAt`), and this checks that they
// agree where it matters: at the pixel the tap lands on.
//
//     python3 -m http.server 8741      # from the repo root, in another shell
//     node scripts/inset_tap_test.mjs
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
const PORT = 9334;
// Paused, so the vehicle is still under the pixel between working out where it
// is and tapping there; 08:30 has downtown busy in every system.
const URL = "http://localhost:8741/index.html?t=8:30&paused=1";

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
  "--window-size=1600,1000", "--user-data-dir=/tmp/cdp-profile-insettap",
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

await send("Page.enable");
await send("Runtime.enable");
await send("Page.navigate", { url: URL });
await sleep(9000);   // map.png + schedule.json are large

// A vehicle currently mirrored into the panel, whose mirror is on screen and
// whose main-map sprite is somewhere else entirely — so a tap on the mirror can
// only be answered by the mirrored position.
const pick = await evaluate(`(() => {
  const out = [];
  for (let i = 0; i < trips.length; i++) {
    const tr = trips[i];
    if (!sysOn[data.routes[tr.r].sy]) continue;
    const ip = insetVehiclePos(tr, simT), mp = vehiclePos(tr, simT);
    if (!ip || !mp) continue;
    const sx = (ip[0] - view.x) * view.k, sy = (ip[1] - view.y) * view.k;
    if (sx < 4 || sy < 4 || sx > innerWidth - 4 || sy > innerHeight - 4) continue;
    if (Math.hypot(mp[0] - ip[0], mp[1] - ip[1]) < 200) continue;
    out.push({ i, label: data.routes[tr.r].n, sx, sy, mx: mp[0], my: mp[1] });
  }
  return { n: out.length, first: out[0] || null, panel: insetRect, path: pathTrip };
})()`);

check("the panel has vehicles in it", pick.n > 0, true);
check("nothing is selected to start with", pick.path, -1);

// The main map still answers a tap the way it always did — `vehiclePos` was
// refactored to share its clock arithmetic with the mirror, so it is worth
// saying so out loud.
const main = await evaluate(`(() => {
  for (let i = 0; i < trips.length; i++) {
    const tr = trips[i];
    if (!sysOn[data.routes[tr.r].sy]) continue;
    const p = vehiclePos(tr, simT);
    if (!p) continue;
    const sx = (p[0] - view.x) * view.k, sy = (p[1] - view.y) * view.k;
    if (sx < 20 || sy < 20 || sx > innerWidth - 20 || sy > innerHeight - 20) continue;
    if (insetRect && p[0] >= insetRect[0] && p[0] <= insetRect[2]
        && p[1] >= insetRect[1] && p[1] <= insetRect[3]) continue;
    const o = { clientX: sx, clientY: sy, bubbles: true };
    cv.dispatchEvent(new MouseEvent("mousedown", o));
    dispatchEvent(new MouseEvent("mouseup", o));
    return { picked: pathTrip >= 0 };
  }
  return { picked: null };
})()`);
check("tapping a vehicle on the main map still selects one", main.picked, true);
await evaluate("pathTrip = -1");   // back to nothing selected
if (!pick.first) {
  console.log("no mirrored vehicle on screen — nothing to tap");
} else {
  const { i, label, sx, sy } = pick.first;
  console.log(`tapping the ${label} mirrored at (${sx.toFixed(0)}, ${sy.toFixed(0)})`);
  await evaluate(`(() => {
    const o = { clientX: ${sx}, clientY: ${sy}, bubbles: true };
    cv.dispatchEvent(new MouseEvent("mousedown", o));
    dispatchEvent(new MouseEvent("mouseup", o));
    return true;
  })()`);
  await sleep(300);   // the readout is written by the next frame, not by the tap
  // Downtown is crowded and the panel stacks several mirrors within a sprite of
  // each other, so what has to be true is not that this exact trip won but that
  // the tap was answered by a vehicle drawn *there*, in the panel.
  const after = await evaluate(`(() => {
    const tr = trips[pathTrip], ip = tr && insetVehiclePos(tr, simT);
    return {
      path: pathTrip,
      offBy: ip ? Math.hypot((ip[0] - view.x) * view.k - ${sx},
                             (ip[1] - view.y) * view.k - ${sy}) : null,
      named: tr ? stats.textContent.includes("path: " + data.routes[tr.r].n) : false,
    };
  })()`);
  check("tapping the mirror selects a vehicle", after.path >= 0, true);
  check("and one that is drawn where the tap was", after.offBy !== null && after.offBy < 20, true);
  check("the readout names its route", after.named, true);
  if (after.path !== i) console.log(`  (${label} was the candidate; a nearer mirror won)`);

  // and tapping blank panel clears it again, rather than picking whatever the
  // main map happens to have hidden under the call-out
  const blank = await evaluate(`(() => {
    const r = insetRect, s = 12;
    for (let mx = r[0] + s; mx < r[2]; mx += s) for (let my = r[1] + s; my < r[3]; my += s) {
      const sx = (mx - view.x) * view.k, sy = (my - view.y) * view.k;
      if (sx < 4 || sy < 4 || sx > innerWidth - 4 || sy > innerHeight - 4) continue;
      if (pickVehicle(sx, sy) < 0) return { sx, sy };
    }
    return null;
  })()`);
  if (blank) {
    await evaluate(`(() => {
      const o = { clientX: ${blank.sx}, clientY: ${blank.sy}, bubbles: true };
      cv.dispatchEvent(new MouseEvent("mousedown", o));
      dispatchEvent(new MouseEvent("mouseup", o));
      return true;
    })()`);
    check("tapping blank panel clears the selection",
          await evaluate("pathTrip"), -1);
  }
}

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
