// Test the renderer's position functions on loops and inset boundaries.
import fs from "node:fs";
import assert from "node:assert/strict";

const src = fs.readFileSync(new URL("../app.js", import.meta.url), "utf8");
function extract(from, to) {
  const a = src.indexOf(from), b = src.indexOf(to, a);
  assert(a >= 0 && b > a, `missing ${from}`);
  return src.slice(a, b);
}
const posAlong = new Function(extract("function posAlong(", "// trace a shape") + "return posAlong;")();
const distAt = new Function(extract("function segSpeed(", "function posAlong(") + "return distAt;")();
function shape(flat) {
  const pts = Float32Array.from(flat), cum = new Float32Array(flat.length / 2);
  for (let i = 1; i < cum.length; i++) cum[i] = cum[i - 1] + Math.hypot(
    pts[2*i] - pts[2*i-2], pts[2*i+1] - pts[2*i-1]);
  return { pts, cum };
}
function placement(runs, ranges) {
  return new Function("insetRuns", "insetRanges", "posAlong",
    extract("function insetPosAt(", "// map-px position of a trip's mirrored") + "return insetPosAt;")(
      runs, ranges, posAlong);
}
const runs = [[shape([0,0, 10,0]), shape([20,0, 30,0])]];
const place = placement(runs, [[[0.1,0.3], [0.7,0.9]]]);
const pat = {s:0, d:[0,100], u:[0.2,0.8], ir:[0,1], id:[5,5]};
assert.deepEqual(place(pat,0,1,0,0,[0,100]), [5,0]);
assert.deepEqual(place(pat,0,1,100,100,[0,100]), [25,0]);
assert.equal(place(pat,0,1,50,50,[0,100]), null);
assert(place(pat,0,1,10,10,[0,100])[0] > 5);
assert(place(pat,0,1,90,90,[0,100])[0] < 25);
const crossing = {...pat, u:[0,1], ir:[-1,-1], id:[0,0]};
assert(Math.abs(place(crossing,0,1,20,20,[0,100])[0] - 5) < 1e-9);
assert.equal(place(crossing,0,1,50,50,[0,100]), null);
const flat = {...pat, d:[0,0]};
assert.equal(place(flat,0,1,0,50,[0,100]), null);
assert.deepEqual(place(flat,0,1,0,0,[0,100]), [5,0]);
const entering = {...pat, u:[0,0.2], ir:[-1,0], id:[0,5]};
assert.equal(place(entering,0,1,25,25,[0,100]), null);
assert.deepEqual(place(entering,0,1,75,75,[0,100]).map(Math.round), [3,0]);
const leaving = {...pat, u:[0.2,0.5], ir:[0,-1], id:[5,0]};
assert.equal(place(leaving,0,1,75,75,[0,100]), null);
assert(place(leaving,0,1,25,25,[0,100])[0] > 5);
const loop = shape([0,0, 10,0, 0,0, 0,10]);
assert.deepEqual(posAlong(loop,15), [5,0]);
assert.deepEqual(posAlong(loop,25), [0,5]);
for (const halt of [false,true]) {
  const times = [0,1,90,91], d = [0,10,11,100];
  for (let lo = 0; lo < 3; lo++) {
    let before = d[lo];
    for (let j = 0; j <= 100; j++) {
      const at = times[lo] + (times[lo+1] - times[lo]) * j / 100;
      const v = distAt(times,d,lo,lo+1,at,halt);
      assert(v >= before - 1e-9 && v <= d[lo+1] + 1e-9);
      before = v;
    }
  }
}
// Every real cross-run interval must retain both assigned stops.
const data = JSON.parse(fs.readFileSync(process.argv[2] || new URL("../schedule.json", import.meta.url)));
assert(data.insetRanges && data.patterns.some(p => p?.u), "schedule lacks inset source positions");
const real = placement(data.insets.map(rs => rs && rs.map(shape)), data.insetRanges || []);
let transitions = 0;
for (const p of data.patterns) {
  if (!p?.u) continue;
  for (let i = 0; i < p.ir.length - 1; i++) {
    if (p.ir[i] < 0 || p.ir[i+1] < 0 || p.ir[i] === p.ir[i+1]) continue;
    const times = p.d.map((_, j) => j * 100);
    assert(real(p,i,i+1,p.d[i],times[i],times), `missing outgoing stop in shape ${p.s}`);
    assert(real(p,i,i+1,p.d[i+1],times[i+1],times), `missing incoming stop in shape ${p.s}`);
    transitions++;
  }
}
assert(transitions > 0, "no cross-run transitions exercised");
console.log(`motion checks passed; ${transitions} real inset transitions checked`);
