"""Report cached GTFS coverage and optionally compare published feeds."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date, datetime
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile
from zoneinfo import ZoneInfo

import build_data as B


def active(calendar, exceptions, day):
    stamp = day.strftime('%Y%m%d')
    weekday = day.strftime('%A').lower()
    ids = {r['service_id'] for r in calendar
           if r.get(weekday) == '1' and r['start_date'] <= stamp <= r['end_date']}
    for r in exceptions:
        if r['date'] == stamp:
            (ids.add if r['exception_type'] == '1' else ids.discard)(r['service_id'])
    return ids


def coverage(read, day):
    calendar, exceptions = read('calendar.txt'), read('calendar_dates.txt')
    counts = Counter(r['service_id'] for r in read('trips.txt'))
    starts = [r['start_date'] for r in calendar if counts[r['service_id']]]
    ends = [r['end_date'] for r in calendar if counts[r['service_id']]]
    additions = [r['date'] for r in exceptions
                 if r['exception_type'] == '1' and counts[r['service_id']]]
    start, end = min(starts + additions, default=None), max(ends + additions, default=None)
    stamp = day.strftime('%Y%m%d')
    return {'start': start, 'end': end, 'expired': end is not None and end < stamp,
            'future': start is not None and start > stamp,
            'tripsOnDate': sum(counts[s] for s in active(calendar, exceptions, day)),
            'frequencyRows': len(read('frequencies.txt')),
            'feedInfo': read('feed_info.txt'), 'missingCoverage': end is None}


def remote_audit(url, feed, day):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'la-transit-map/feed-audit'})
        with urllib.request.urlopen(req, timeout=45) as response:
            body = response.read()
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = {Path(n).name: n for n in archive.namelist() if n.endswith('.txt')}
            def read(name):
                if name not in names:
                    return []
                return list(csv.DictReader(io.StringIO(archive.read(names[name]).decode('utf-8-sig'))))
            result = coverage(read, day)
            changed = []
            for name in ('agency.txt', 'calendar.txt', 'calendar_dates.txt', 'routes.txt',
                         'trips.txt', 'stops.txt', 'stop_times.txt', 'shapes.txt', 'frequencies.txt'):
                local = Path(B.GTFS, feed, name)
                a = local.read_bytes() if local.exists() else b''
                b = archive.read(names[name]) if name in names else b''
                if hashlib.sha256(a).digest() != hashlib.sha256(b).digest():
                    changed.append(name)
            return {**result, 'changedFiles': changed, 'sha256': hashlib.sha256(body).hexdigest()}
    except (OSError, ValueError, KeyError, csv.Error, zipfile.BadZipFile) as error:
        return {'error': str(error)}


def audit(feed, day, sources, remote=False):
    read = lambda name: B.read_csv(feed, name)
    counts = Counter(r['service_id'] for r in read('trips.txt'))
    selected = B.pick_dates(feed, counts)
    result = {'feed': feed, 'agency': B.FEED_NAMES[feed], 'source': sources[feed],
              **coverage(read, day),
              'selectedDates': [d.isoformat() if d else None for d in selected]}
    if remote:
        result['published'] = remote_audit(sources[feed], feed, day)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', type=date.fromisoformat, default=datetime.now(ZoneInfo("America/Los_Angeles")).date())
    parser.add_argument('--remote', action='store_true', help='download sources for comparison; do not replace inputs')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--strict', action='store_true', help='fail if a cached feed has expired or a source check fails')
    args = parser.parse_args()
    sources = json.loads(Path('data/feed_sources.json').read_text())
    with ThreadPoolExecutor(max_workers=4) as pool:
        feeds = list(pool.map(lambda feed: audit(feed, args.date, sources, args.remote), B.FEEDS))
    report = {'checkedDate': args.date.isoformat(), 'targetDate': B.TARGET.isoformat(), 'feeds': feeds}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + '\n')
    for row in feeds:
        status = ('missing' if row['missingCoverage'] else 'EXPIRED' if row['expired']
                  else 'future' if row['future'] else 'in range')
        suffix = ''
        if args.remote:
            remote = row['published']
            suffix = ('; source error: ' + remote['error'] if 'error' in remote else
                      f"; published end {remote['end']}, {len(remote['changedFiles'])} changed files")
        print(f"{row['feed']:12} {status:7} end {row['end']} trips {row['tripsOnDate']:5}{suffix}")
    if args.strict and any(r['expired'] or r['future'] or r['missingCoverage']
                           or r.get('published', {}).get('error')
                           or r.get('published', {}).get('missingCoverage') for r in feeds):
        parser.exit(1, 'Feed audit failed. Review the report before refreshing and rebuilding.\n')


if __name__ == '__main__':
    main()
