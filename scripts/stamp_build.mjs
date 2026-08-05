// Rewrite app.js's build placeholders at deploy time.
//
// Pages serves everything with a fixed 10-minute max-age, so a stale page is
// unavoidable; what the build id fixes is the page not *knowing*. index.html is
// deliberately not stamped — it is the one URL that cannot be versioned, so it
// is kept constant instead.
//
// The data files carry the build's content identity in a query string, so a
// cached index.html can never pair with a newer schedule.json. Tiles are keyed
// on the last commit that touched tiles/, so a code-only deploy doesn't make
// every client re-download 5488 images.
//
//     node scripts/stamp_build.mjs "<build id>" "<tiles rev>"
import fs from "node:fs";
import crypto from "node:crypto";

const [build, tilesRev] = process.argv.slice(2);
if (!build || !tilesRev) {
  console.error('usage: stamp_build.mjs "<build id>" "<tiles rev>"');
  process.exit(1);
}

const hash = f => crypto.createHash("sha256")
  .update(fs.readFileSync(f)).digest("hex").slice(0, 10);

const subs = [
  ["__BUILD__", build],
  ["__V_SCHEDULE__", hash("schedule.json")],
  ["__V_MAP__", hash("map.png")],
  ["__V_TILES__", tilesRev],
];

let src = fs.readFileSync("app.js", "utf8");
for (const [from, to] of subs) {
  if (!src.includes(from)) {
    console.error(`app.js has no ${from} placeholder — stamping would be silent`);
    process.exit(1);
  }
  src = src.split(from).join(to);
}
fs.writeFileSync("app.js", src);

for (const [from, to] of subs) console.log(`  ${from} -> ${to}`);
