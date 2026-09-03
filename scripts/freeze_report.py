"""Read back what freeze_log.py caught, and say where the page stopped.

The interesting record is always the last one, and the interesting question is
always what was climbing before it. So this prints the run's shape, then the
stalls, then the samples either side of the worst one, which is the state the
tab was in when it went.

    .venv/bin/python scripts/freeze_report.py                 # summarize
    .venv/bin/python scripts/freeze_report.py --tail 40       # last N samples
    .venv/bin/python scripts/freeze_report.py --csv out.csv   # for plotting

What to read, in order:

  worker still posting after the samples stopped
      The main thread is blocked or gone while the process lives. `last` on the
      stall record is the final state the page reported.
  nothing at all after some point
      The content process died with the tab. Whatever the last sample says was
      climbing is the candidate.
  samples continuing with rafGapMax climbing
      Frames are being asked for and not delivered: the compositor, not us.
  tickLateMs / workerLateMs climbing together
      The whole process is being starved, which is memory pressure or the OS.
  driftMs jumping
      The tab was suspended, not wedged, which is a different problem.
"""
import argparse
import datetime
import json
import os
import sys

LOG = "scratch/freeze-trace.jsonl"
DEAD_AFTER_S = 20        # quiet longer than this and the session is over, not idle

# Columns worth watching over time, in the order they answer the question.
COLS = ["n", "fps", "frames", "frameP50", "frameP95", "frameMax", "rafGapMax",
        "tickLateMs", "driftMs", "longTaskMs", "imageMB", "tileMB", "peakTileMB",
        "heapMB", "canvasesMade", "tilesCached", "tileInflight", "decodesPerSec",
        "evictsPerSec", "composesPerSec", "zoom", "frameErrors", "slowFrames"]


def load(path):
    if not os.path.exists(path):
        sys.exit(f"no {path}: run scripts/freeze_log.py and reproduce the freeze")
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def age(stamp):
    """Seconds since a record was received, or None if it can't be read."""
    try:
        return (datetime.datetime.now()
                - datetime.datetime.fromisoformat(stamp)).total_seconds()
    except Exception:
        return None


def fmt(v):
    if isinstance(v, float):
        return f"{v:g}"
    return "" if v is None else str(v)


def table(rows, cols):
    cols = [c for c in cols if any(r.get(c) is not None for r in rows)]
    w = {c: max(len(c), *(len(fmt(r.get(c))) for r in rows)) for c in cols}
    print("  " + "  ".join(c.rjust(w[c]) for c in cols))
    for r in rows:
        print("  " + "  ".join(fmt(r.get(c)).rjust(w[c]) for c in cols))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-f", "--file", default=LOG)
    ap.add_argument("--tail", type=int, default=12, help="samples to print (default 12)")
    ap.add_argument("--csv", help="write every sample to a CSV for plotting")
    ap.add_argument("--all", action="store_true",
                    help="every session in the file, not just the last")
    a = ap.parse_args()

    every = load(a.file)
    if not every:
        sys.exit(f"{a.file} is empty")
    # One page load per session id. A reload beacons its own last record into
    # the file just before the next run starts writing to it, and those two
    # runs' clocks both start at zero, so read one session unless asked
    # otherwise, or the seam between them looks like a fault.
    sids = [s for s in dict.fromkeys(r.get("sid") for r in every) if s]
    recs = every if a.all or not sids else [r for r in every if r.get("sid") == sids[-1]]
    if len(sids) > 1 and not a.all:
        print(f"{len(sids)} sessions in the file; reading the last ({sids[-1]}). "
              f"--all for every one.\n")
    samples = [r for r in recs if r.get("t") in ("sample", "final")]
    stalls = [r for r in recs if r.get("t") == "stall"]
    throttled = [r for r in recs if r.get("t") == "throttled"]
    lates = [r for r in recs if r.get("t") == "workerLate"]
    opens = [r for r in recs if r.get("t") == "open"]
    mem = [r for r in recs if r.get("t") == "uaMemory"]
    notes = [r for r in recs if r.get("t") == "note"]

    print(f"{a.file}: {len(recs)} records, {len(samples)} samples, "
          f"{len(stalls)} stall reports, {len(throttled)} throttle reports, "
          f"{len(lates)} worker-late reports")
    for n in notes:
        print(f"  note: {n.get('note')}")

    # A backgrounded tab is throttled to one timer wake a second, and after a few
    # minutes to one a minute, so its samples are sparse and its frame counts are
    # meaningless. Say so before anyone reads the numbers as the page's own.
    hid = sum(1 for r in samples if r.get("hidden"))
    if hid and samples:
        share = 100 * hid / len(samples)
        print(f"\n!! the tab was in the background for {share:.0f}% of this session. "
              "Every browser clamps a hidden tab's timers, so sparse samples and "
              "long quiet periods there are the browser, not the page, and rAF "
              "is throttled, so nothing was really being presented. Read the "
              "foreground samples; the rest say nothing about a freeze.")
    for o in opens:
        print(f"\nsession {o.get('rx','')}  dpr={o.get('dpr')} {o.get('w')}x{o.get('h')} "
              f"isolated={o.get('isolated')} cores={o.get('cores')} "
              f"mem={o.get('deviceMemoryGB')}GB")
        print(f"  {o.get('ua','')}")

    quiet_for = None
    if samples:
        first, last = samples[0], samples[-1]
        span = (last.get("up", 0) - first.get("up", 0)) / 1000
        quiet_for = age(recs[-1].get("rx"))
        tail = "" if quiet_for is None else f", last record {quiet_for:.0f}s ago"
        # Sessions each restart the clock, so a span across them is meaningless.
        span_s = f"ran {span:.0f}s of samples, " if len(sids) < 2 or not a.all else ""
        print(f"\n{span_s}last at {last.get('rx','?')} "
              f"(sample #{last.get('n')}){tail}")

    # The whole point: did the worker outlive the main thread, and by how long?
    if stalls:
        worst = max(stalls, key=lambda r: r.get("silentMs", 0))
        print(f"\n{len(stalls)} stall reports; worst {worst.get('silentMs')} ms of "
              f"main-thread silence at {worst.get('rx','?')}")
        print("  the worker kept running while the main thread did not, so this is a "
              "blocked or dead main thread, not a dead process")
        lastgood = worst.get("last") or {}
        if lastgood:
            print("\n  last state the page reported before it went quiet:")
            table([lastgood], COLS)
    elif throttled and not stalls:
        worst = max(throttled, key=lambda r: r.get("silentMs", 0))
        print(f"\nno stalls. {len(throttled)} quiet periods, worst "
              f"{worst.get('silentMs')} ms, all while the tab was hidden. That is "
              "the browser's background throttling, not the page.")
    elif samples and samples[-1].get("t") == "final":
        print(f"\nno stalls; the session ended cleanly "
              f"({samples[-1].get('reason', 'final')}).")
    elif quiet_for is not None and quiet_for < DEAD_AFTER_S:
        print(f"\nno stalls, and the trace is still arriving; this session is "
              f"healthy so far. Reproduce the freeze, then read it again.")
    else:
        print("\nno stall reports and nothing since: the samples simply stop. The "
              "worker went quiet *with* the main thread, which is the content "
              "process dying rather than a script hanging; read the last samples "
              "below for what was climbing on the way out.")

    if lates:
        worst = max(lates, key=lambda r: r.get("lateMs", 0))
        print(f"\n{len(lates)} worker-late reports; worst {worst.get('lateMs')} ms. "
              "The worker's own thread was starved too, so this is process-wide.")

    if mem:
        m = mem[-1]
        print(f"\nlast measureUserAgentSpecificMemory: {m.get('totalMB')} MB total")
        for b in sorted(m.get("breakdown", []), key=lambda b: -b.get("mb", 0))[:8]:
            print(f"    {b.get('mb'):8} MB  {','.join(b.get('types') or []) or '?'}")

    if samples:
        print(f"\nlast {min(a.tail, len(samples))} samples:")
        table(samples[-a.tail:], COLS)

    if a.csv:
        import csv as csvmod
        cols = [c for c in COLS if any(r.get(c) is not None for r in samples)]
        with open(a.csv, "w", newline="") as f:
            w = csvmod.DictWriter(f, fieldnames=["rx"] + cols, extrasaction="ignore")
            w.writeheader()
            for r in samples:
                w.writerow(r)
        print(f"\nwrote {len(samples)} samples to {a.csv}")


if __name__ == "__main__":
    main()
