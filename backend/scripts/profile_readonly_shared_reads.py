"""Bounded three-browser polling probe against the laptop only.

Reports an initial burst separately from reads after that burst. Never sends a
write, queries the production origin directly, or prints business row values.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener


BASE = 'http://127.0.0.1:8000/api/v1'
PATHS = ('/readonly-runtime/operations', '/decision/current',
         '/decision/hourly', '/decision/total-ash-hours')


def request(path):
    started = time.perf_counter()
    try:
        with build_opener(ProxyHandler({})).open(BASE + path, timeout=30) as response:
            raw = response.read()
            value, status = json.loads(raw), response.status
        error = None
        if not isinstance(value, dict):
            value, error = {}, 'response_contract_error'
        elif path == '/readonly-runtime/operations' and (value.get('errors') or
                value.get('health', {}).get('status') != 'ok'):
            error = 'partial_view_unavailable'
    except HTTPError as exc:
        status, value, raw, error = exc.code, {}, b'', 'http_error'
        exc.close()
    except (URLError, OSError, ValueError):
        status, value, raw, error = 0, {}, b'', 'transport_or_response_error'
    stamp = value.get('generated_at') or value.get('checked_at')
    return dict(path=path, status=status, ms=round((time.perf_counter()-started)*1000, 2),
                bytes=len(raw), stamp=stamp, error=error), value


def describe(rows):
    result = {}
    for path in PATHS:
        samples = [row for row in rows if row['path'] == path]
        times = sorted(row['ms'] for row in samples)
        stable = sorted(row['ms'] for row in samples if row.get('same_generation'))
        result[path] = dict(samples=len(times), failures=sum(row['status'] != 200 or row['error'] is not None for row in samples),
            p50_ms=times[math.ceil(len(times)*.5)-1], p95_ms=times[math.ceil(len(times)*.95)-1],
            max_ms=times[-1], snapshot_generations=len({row['stamp'] for row in samples if row['stamp']}),
            same_generation_samples=len(stable),
            same_generation_p95_ms=stable[math.ceil(len(stable)*.95)-1] if stable else None)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rounds', type=int, default=6, choices=range(1, 9))
    args = parser.parse_args()
    mode_row, mode = request('/runtime-mode')
    if mode_row['status'] != 200 or mode.get('mode') != 'readonly-test':
        parser.error('This probe requires the laptop readonly-test runtime on 127.0.0.1:8000')

    with ThreadPoolExecutor(max_workers=12) as clients:
        def burst():
            futures = [clients.submit(request, path) for _browser in range(3) for path in PATHS]
            return [future.result()[0] for future in futures]
        initial = burst()
        previous = {path: {row['stamp'] for row in initial if row['path'] == path and row['stamp']} for path in PATHS}
        after = []
        for _round in range(args.rounds):
            rows = burst()
            for row in rows:
                # Same as the PREVIOUS completed burst proves reuse, without
                # misclassifying concurrent waiters for a new build as hot hits.
                row['same_generation'] = row['stamp'] in previous[row['path']]
            previous = {path: {row['stamp'] for row in rows if row['path'] == path and row['stamp']} for path in PATHS}
            after.extend(rows)
    failed = [row for row in initial+after if row['status'] != 200 or row['error']]
    print(json.dumps(dict(scope='three-browser equivalent; local GET only; bounded short sample, not long-run P95',
        initial_burst=describe(initial), after_initial_burst=describe(after),
        hot_definition='same snapshot timestamp as previous completed burst; changed generations remain in overall p95',
        failures=[dict(path=row['path'], status=row['status'], error=row['error']) for row in failed]),
        ensure_ascii=False))
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
