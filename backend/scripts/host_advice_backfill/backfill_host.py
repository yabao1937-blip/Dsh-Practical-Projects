"""Host-side bootstrap: run the advice backfill against the installed backend.

Usage (from BackfillHourlyAdvice.cmd):
    python backfill_host.py <install>/backend [--days 7] [--dry-run]

Loads advice_backfill.py from this folder (byte-identical to the repository's
backend/app/decision_hourly/advice_backfill.py) after putting the installed
backend on sys.path, so its absolute ``app.*`` imports resolve to the running
production code.
"""
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
backend = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
if backend is None or not (backend / 'app' / 'database.py').is_file():
    print('用法：backfill_host.py <安装目录>/backend [--days N] [--dry-run]')
    raise SystemExit(2)
sys.path.insert(0, str(here))
sys.path.insert(0, str(backend))

import advice_backfill  # noqa: E402

raise SystemExit(advice_backfill.main(sys.argv[2:]))
