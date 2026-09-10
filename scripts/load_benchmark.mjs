// Measure cold loads of staged clients with Chromium network and CPU throttling.
// Usage: node scripts/load_benchmark.mjs DIR [DIR ...]
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import http from "node:http";
import zlib from "node:zlib";
import { spawn } from "node:child_process";

const roots = process.argv.slice(2).map(p => path.resolve(p));
if (!roots.length) throw new Error("Pass one or more client directories");
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "la-load-bench-"));
const bin = process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const sleep = ms => new Promise(r => setTimeout(r, ms));
const cache = new Map();
const types = { ".html": "text/html", ".js": "text/javascript", ".json": "application/json",
  ".webp": "image/webp", ".png": "image/png" };
const server = http.createServer((req, res) => {
  const parts = new URL(req.url, "http://localhost").pathname.split("/").filter(Boolean);
  const root = roots[Number(parts.shift())];
  if (!root) { res.writeHead(404).end(); return; }
  const file = path.resolve(root, parts.join("/") || "index.html");
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404).end(); return;
  }
  if (!cache.has(file)) {
    const ext = path.extname(file), compress = [".html", ".json", ".js"].includes(ext);
    const raw = fs.readFileSync(file);
    cache.set(file, { body: compress ? zlib.gzipSync(raw, { level: 5 }) : raw,
      headers: { "content-type": types[ext] || "application/octet-stream",
        "cache-control": "max-age=600", ...(compress ? { "content-encoding": "gzip" } : {}) } });
  }
  const { body, headers } = cache.get(file);
  res.writeHead(200, { ...headers, "content-length": body.length }).end(body);
});
await new Promise(r => server.listen(0, "127.0.0.1", r));
const chrome = spawn(bin, ["--headless=new", "--remote-debugging-port=0",
  `--user-data-dir=${profile}`, "--no-first-run", "--no-default-browser-check", "about:blank"],
  { stdio: "ignore" });
let ws;
try {
  const portFile = path.join(profile, "DevToolsActivePort");
  for (let i = 0; !fs.existsSync(portFile) && i < 100; i++) await sleep(100);
  const port = fs.readFileSync(portFile, "utf8").split("\n")[0];
  const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
  ws = new WebSocket(targets.find(t => t.type === "page").webSocketDebuggerUrl);
  await new Promise(r => { ws.onopen = r; });
  let id = 0;
  const waiting = new Map();
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.id) { waiting.get(m.id)?.(m); waiting.delete(m.id); }
  };
  const send = async (method, params = {}) => {
    const n = ++id;
    const result = new Promise(r => waiting.set(n, r));
    ws.send(JSON.stringify({ id: n, method, params }));
    const m = await result;
    if (m.error) throw new Error(JSON.stringify(m.error));
    return m.result;
  };
  const ev = async expression => {
    const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
    return r.result.value;
  };
  await send("Network.enable");
  await send("Network.setCacheDisabled", { cacheDisabled: true });
  await send("Page.enable");
  await send("Page.addScriptToEvaluateOnNewDocument", { source: `
    window.loadMarks = {};
    addEventListener('error', e => { loadMarks.error = e.message; });
    addEventListener('unhandledrejection', e => { loadMarks.error = String(e.reason); });
    function measure() {
      if (typeof frames !== 'undefined' && typeof data !== 'undefined' && data
          && typeof trips !== 'undefined' && trips.length && typeof bgComposes !== 'undefined' && bgComposes) {
        if (!loadMarks.firstFrame) loadMarks.firstFrame = performance.now();
        const full = map instanceof HTMLImageElement ?
          (view.k * DPR <= 1.05 || tilesCover()) : tilesCover();
        if (full && !bgDirty && !loadMarks.ready) loadMarks.ready = performance.now();
      }
      requestAnimationFrame(measure);
    }
    requestAnimationFrame(measure);
  ` });
  const scenarios = [
    { name: "desktop", width: 1440, height: 900, dpr: 2, mbps: 20, latency: 40, cpu: 1, query: "" },
    { name: "mobile", width: 390, height: 844, dpr: 3, mbps: 4, latency: 100, cpu: 4, query: "" },
    { name: "zoomed", width: 1440, height: 900, dpr: 2, mbps: 20, latency: 40, cpu: 1,
      query: "&k=3&x=2000&y=1300" },
  ];
  const rows = [];
  const repeats = Number(process.env.REPEATS || 3);
  for (const s of scenarios) {
    await send("Emulation.setDeviceMetricsOverride", { width: s.width, height: s.height,
      deviceScaleFactor: s.dpr, mobile: s.name === "mobile" });
    await send("Emulation.setCPUThrottlingRate", { rate: s.cpu });
    await send("Network.emulateNetworkConditions", { offline: false, latency: s.latency,
      downloadThroughput: s.mbps * 1e6 / 8, uploadThroughput: s.mbps * 1e6 / 8 });
    for (let run = 0; run < repeats; run++) {
      for (let offset = 0; offset < roots.length; offset++) {
        const index = (offset + run) % roots.length;
        await send("Page.navigate", { url: "about:blank" });
        await sleep(100);
        await send("Network.clearBrowserCache");
        await send("Page.navigate", { url: `http://127.0.0.1:${server.address().port}/${index}/index.html?t=12:00&paused=1${s.query}` });
        let marks;
        const start = Date.now();
        do {
          await sleep(100);
          marks = await ev("window.loadMarks || {}");
          if (marks.error) throw new Error(marks.error);
          if (Date.now() - start > 90000) throw new Error(`Load timed out: ${roots[index]}`);
        } while (!marks.ready);
        // Wait for queued image requests to finish before counting total startup bytes.
        await ev(`new Promise(resolve => {
          function check() {
            if (typeof tileInflight !== 'undefined' && (tileInflight || tileQueue.some(t => t.state === 'queued'))) {
              setTimeout(check, 50); return;
            }
            resolve();
          }
          check();
        })`);
        const resources = await ev(`[...performance.getEntriesByType('navigation'),
          ...performance.getEntriesByType('resource')].map(r => ({
          name: r.name, bytes: r.encodedBodySize, transfer: r.transferSize }))`);
        const row = { scenario: s.name, candidate: path.basename(roots[index]), run: run + 1,
          firstFrameMs: Math.round(marks.firstFrame), readyMs: Math.round(marks.ready),
          bytes: resources.reduce((n, r) => n + r.bytes, 0),
          imageBytes: resources.filter(r => /\.(png|webp)(\?|$)/.test(r.name)).reduce((n, r) => n + r.bytes, 0),
          requests: resources.length };
        rows.push(row);
        console.log(JSON.stringify(row));
        if (process.env.BENCH_OUT) {
          fs.writeFileSync(process.env.BENCH_OUT, JSON.stringify({ scenarios, rows }, null, 2));
          if (run === repeats - 1) {
            const shot = await send("Page.captureScreenshot", { format: "png" });
            fs.writeFileSync(path.join(path.dirname(process.env.BENCH_OUT),
              `${s.name}-${path.basename(roots[index])}.png`), Buffer.from(shot.data, "base64"));
          }
        }
      }
    }
  }
  for (const s of scenarios) {
    for (const root of roots) {
      const candidate = path.basename(root);
      const runs = rows.filter(r => r.scenario === s.name && r.candidate === candidate);
      const median = key => runs.map(r => r[key]).sort((a, b) => a - b)[Math.floor(runs.length / 2)];
      console.log(JSON.stringify({ scenario: s.name, candidate,
        medianReadyMs: median("readyMs"), bytes: median("bytes"), imageBytes: median("imageBytes") }));
    }
  }
} finally {
  ws?.close();
  chrome.kill();
  server.close();
}
