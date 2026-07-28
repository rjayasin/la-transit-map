// Rewrite app.js's build placeholders at deploy time.
//
// Pages serves everything with Cache-Control: max-age=600 and gives no way to
// change it, so a stale index.html is always possible for up to ten minutes and
// a tab left open is stale indefinitely. Neither is fixable in the headers. What
// is fixable is the page not *knowing*: it now carries the id of the build it
// came from, so a snapshot names its code exactly, and it can ask version.json
// whether it is the current one. index.html is deliberately not stamped — it is
// the one URL that cannot be versioned, so it is kept constant instead.
//
// The data files get the build's content identity in a query string, matching
// the pattern the site repo uses: a cached index.html can then never pair with a
// newer schedule.json, because the URL it asks for is the one it was built with.
// Tiles are keyed on the last commit that touched tiles/, so a code-only deploy
// doesn't make every client re-download 5488 images.
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
