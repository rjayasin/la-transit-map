// Compare lazy decoding and frame candidates with a full timetable scan.
import fs from 'node:fs';
import assert from 'node:assert/strict';
import vm from 'node:vm';
const src = fs.readFileSync(new URL('../app.js', import.meta.url), 'utf8');
const data = JSON.parse(fs.readFileSync(new URL('../schedule.json', import.meta.url)));
const start = src.indexOf('function detie('), end = src.indexOf('// Play weekday', start);
const context = vm.createContext({data});
vm.runInContext(src.slice(start, end), context);
const evaluate = code => vm.runInContext(code, context);
evaluate('var all = buildTrips();');
assert(evaluate('all.every(t => t.times === null)'));
for (let day = 0; day < 7; day++) {
  evaluate(`var rows = all.filter(t => t.days >> ${day} & 1); rows.forEach(decodeTrip); tripBuckets = indexTrips(rows);`);
  assert.equal(evaluate('rows.length'), data.tripDays.filter(m => m >> day & 1).length);
  assert(evaluate(`rows.every(tr => {
    const expected = Float64Array.from(tr.raw.slice(2));
    for (let i = 1; i < expected.length; i++) expected[i] += expected[i-1];
    detie(expected, effDist(data.patterns[tr.p]));
    return expected.every((t, i) => t === tr.times[i]);
  })`));
  for (const t of [0, 1, 299, 300, 301, 21600, 43200, 86399, 12000, 0]) {
    evaluate(`var t = ${t}; fadingTrips = new Set([0, rows.length - 1]); var candidates = frameTripIndices(t);`);
    assert(evaluate(`rows.every((tr, i) => !(t >= tr.t0 && t <= tr.t1) || candidates.includes(i))`));
    assert(evaluate('candidates.includes(0) && candidates.includes(rows.length - 1)'));
    assert(evaluate('candidates.every((v, i) => i === 0 || v > candidates[i-1])'));
  }
}
// Exact boundaries, midnight tails, and trips outside the displayed day.
evaluate(`rows = [{t0:-100,t1:0},{t0:300,t1:600},{t0:86000,t1:90000},{t0:90000,t1:91000}];
  tripBuckets = indexTrips(rows); fadingTrips.clear();`);
assert.deepEqual(Array.from(evaluate('frameTripIndices(0)')), [0]);
assert.deepEqual(Array.from(evaluate('frameTripIndices(300)')), [1]);
assert.deepEqual(Array.from(evaluate('frameTripIndices(600)')), [1]);
assert.deepEqual(Array.from(evaluate('frameTripIndices(86399)')), [2]);
evaluate('rows = all.filter(t => t.days & 16); tripBuckets = indexTrips(rows);');
console.log(JSON.stringify(evaluate(`({trips: rows.length,
  noonCandidates: frameTripIndices(43200).length,
  meanCandidates: tripBuckets.reduce((n, b) => n + b.length, 0) / tripBuckets.length})`)));
console.log('ok: lazy times equal full decoding; all active and fading trips stay in draw order');
