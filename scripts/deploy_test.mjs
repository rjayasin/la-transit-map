// Check the deploy stamp end to end, in a real browser.
//
// The deploy rewrites app.js on the way to Pages: a build id the page reports as
// its own, and content identities on the data URLs. index.html stays constant
// and unversioned on purpose — it is the one URL nobody can put a version on —
// and the bootstrapper in it is what turns the current build into a URL. If any of that
// silently stops happening, the symptom is the one this whole mechanism exists
// to remove — a page that cannot say which code it is — and it would not show up
// until the next time somebody asked. So it is checked rather than assumed.
//
// Self-contained: stamps a copy into a temp directory, serves it, drives it.
//
//     node scripts/deploy_test.mjs
import { spawn } from "node:child_process";
import fs from "node:fs";
import crypto from "node:crypto";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const BUILD = "abc1234 2026-01-01 00:00";
const TILES_REV = "deadbee";
const LIVE = "zzz9999 2026-01-02 00:00";   // what the server advertises
const LIVE_SHA = "beef1234567890abcdef";    // and the sha the bootstrapper asks by

function findChrome() {
  if (process.env.CHROME) return process.env.CHROME;
  const cache = path.join(os.homedir(), ".cache/puppeteer/chrome-headless-shell");
  if (fs.existsSync(cache)) {
    for (const v of fs.readdirSync(cache)) {
      const hit = path.join(cache, v, "chrome-headless-shell-mac-arm64/chrome-headless-shell");
      if (fs.existsSync(hit)) return hit;
    }
  }
  const app = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  return fs.existsSync(app) ? app : null;
}
const BIN = findChrome();
if (!BIN) { console.log("no Chrome found — set CHROME=/path/to/chrome. Skipping."); process.exit(0); }

// ---- stage a stamped copy, and an unstamped one -----------------------------
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "la-deploy-"));
for (const name of ["stamped", "dev"]) {
  const dir = path.join(tmp, name);
  fs.mkdirSync(dir);
  for (const f of ["index.html", "app.js"]) {
    fs.copyFileSync(path.join(ROOT, f), path.join(dir, f));
  }
  for (const f of ["map.png", "schedule.json", "tiles"]) {
    fs.symlinkSync(path.join(ROOT, f), path.join(dir, f));
  }
  // Only the deployed copy has a version.json — it is written by the workflow,
  // so a working copy served off `python3 -m http.server` simply 404s it, which
  // is the case the bootstrapper's fallback exists for.
  if (name === "stamped") {
    fs.writeFileSync(path.join(dir, "version.json"),
                     JSON.stringify({ build: LIVE, sha: LIVE_SHA }));
  }
}
const stamp = spawn(process.execPath,
  [path.join(ROOT, "scripts/stamp_build.mjs"), BUILD, TILES_REV],
  { cwd: path.join(tmp, "stamped"), stdio: "inherit" });
await new Promise((res, rej) => stamp.on("exit", c => (c ? rej(new Error(`stamp exited ${c}`)) : res())));

const TYPES = { ".html": "text/html", ".json": "application/json", ".js": "text/javascript",
                ".png": "image/png", ".webp": "image/webp" };
const server = http.createServer((req, res) => {
  const [urlPath] = req.url.split("?");
  const file = path.join(tmp, decodeURIComponent(urlPath));
  if (!file.startsWith(tmp) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404).end(); return;
  }
  res.writeHead(200, { "content-type": TYPES[path.extname(file)] || "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
});
await new Promise(r => server.listen(0, r));
const PORT = server.address().port;

// ---- drive it ---------------------------------------------------------------
const CDP = 9335;
const chrome = spawn(BIN, [`--remote-debugging-port=${CDP}`, "--headless=new",
  "--window-size=1600,1000", `--user-data-dir=${tmp}/profile`, "--no-first-run",
  "about:blank"], { stdio: "ignore" });
const sleep = ms => new Promise(r => setTimeout(r, ms));

let targets;
for (let i = 0; i < 60; i++) {
  try { targets = await (await fetch(`http://127.0.0.1:${CDP}/json`)).json(); break; }
  catch { await sleep(200); }
}
const ws = new WebSocket(targets.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => (ws.onopen = r));
let id = 0; const waiting = new Map(); let net = [];
ws.onmessage = e => {
  const m = JSON.parse(e.data);
  if (m.id && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); }
  if (m.method === "Network.requestWillBeSent") net.push(m.params.request.url);
};
const send = (method, params = {}) => {
  const n = ++id; ws.send(JSON.stringify({ id: n, method, params }));
  return new Promise(r => waiting.set(n, r));
};
const ev = async x => (await send("Runtime.evaluate",
  { expression: x, returnByValue: true, awaitPromise: true })).result?.result?.value;

const results = [];
const check = (n, g, w) => results.push(
  [n, JSON.stringify(g), JSON.stringify(w), JSON.stringify(g) === JSON.stringify(w)]);

await send("Network.enable"); await send("Page.enable"); await send("Runtime.enable");

// zoomed in, so the tile fetches this is checking actually happen
async function load(dir) {
  net = [];
  await send("Page.navigate",
    { url: `http://localhost:${PORT}/${dir}/index.html?debug&k=3&x=2000&y=1300` });
  await sleep(9000);
  return JSON.parse(await ev("JSON.stringify(transitDebug())"));
}

// 1. the stamped copy
const s = await load("stamped");
const h = f => crypto.createHash("sha256").update(fs.readFileSync(path.join(ROOT, f)))
  .digest("hex").slice(0, 10);

console.log("stamped:", { build: s.build, stale: s.staleBuild, frames: s.framesTotal });
check("stamped: the page reports its build", s.build, BUILD);
// the bootstrapper asked version.json who is live, then named that build in the
// URL it loaded the client from — which is the whole mechanism
check("stamped: version.json read before the client",
      net.findIndex(u => u.includes("version.json")) < net.findIndex(u => u.includes("app.js")), true);
check("stamped: the client fetched at the live build's URL",
      net.some(u => u.endsWith(`app.js?v=${LIVE_SHA.slice(0, 7)}`)), true);
// index.html is the URL that cannot be versioned, and must not pretend to be
check("stamped: index.html itself carries no version",
      net.some(u => /index\.html\?[^"]*[?&]v=/.test(u)), false);
check("stamped: it is drawing", s.framesTotal > 0, true);
check("stamped: schedule.json carries its content hash",
      net.some(u => u.includes(`schedule.json?v=${h("schedule.json")}`)), true);
check("stamped: map.png carries its content hash",
      net.some(u => u.includes(`map.png?v=${h("map.png")}`)), true);
check("stamped: tiles carry the tiles rev",
      net.some(u => /tiles\/\d+\/.*\.webp\?v=deadbee/.test(u)), true);
check("stamped: version.json polled past the CDN cache",
      net.some(u => /version\.json\?ts=\d+/.test(u)), true);
check("stamped: a newer live build is noticed", s.staleBuild, LIVE);
check("stamped: the bar offers the update",
      await ev("!document.getElementById('upd').hidden"), true);
// the one thing it must never do: throw away a view somebody chose
check("stamped: it did not reload itself", s.framesTotal > 100, true);

// 2. an unstamped working copy must not poll, and must not nag
const d = await load("dev");
console.log("dev:", { build: d.build, stale: d.staleBuild });
check("dev: build is the placeholder", d.build, "__BUILD__");
// No version.json to be had, so the bootstrapper falls back and the client
// itself, seeing an unstamped build, never polls or nags.
check("dev: the client loads unversioned", net.some(u => u.endsWith("app.js")), true);
check("dev: nothing stale to report", d.staleBuild, null);
check("dev: no update button", await ev("!document.getElementById('upd').hidden"), false);
check("dev: still draws", d.framesTotal > 0, true);

let bad = 0;
for (const [n, g, w, ok] of results) {
  if (!ok) bad++;
  console.log(`${ok ? "ok  " : "FAIL"}  ${n}${ok ? "" : `  got ${g} want ${w}`}`);
}
console.log(bad ? `\n${bad} failed` : `\nall ${results.length} passed`);
ws.close(); chrome.kill(); server.close();
// Chrome is still tearing its profile down, so the directory can refill under
// the removal. The temp dir is disposable either way; failing the run over it
// would report a passing check suite as a failure.
await sleep(300);
try { fs.rmSync(tmp, { recursive: true, force: true }); } catch { /* the OS will */ }
process.exit(bad ? 1 : 0);
