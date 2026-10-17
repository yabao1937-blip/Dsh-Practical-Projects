"""Coalesce the laptop's three initial model/source/store reads."""
import json
import logging

from fastapi import HTTPException
from sqlalchemy import text

from ..observability import readonly_failure_details
from .runtime import ReadOnlyRuntimeUnavailable, SingleFlightBundle, read_window


log = logging.getLogger('dmcs.readonly_startup')


def _build_startup():
    from ..database import SessionLocal
    from ..gpt_coarse.repository import active
    from ..services.decision_sources import config_view
    from ..services.state import load_store

    try:
        # Acquire the display budget before checking out a remote connection.
        with read_window('display'), SessionLocal() as db:
            db.execute(text('BEGIN'))
            # Keep the legacy default time semantics: neither model query is
            # passed a synthetic "now" or a different model_at cutoff.
            result = {'state': load_store(db), 'model': dict(model=active(db, include_analysis=True)),
                      'sources': config_view(db)}
            return json.loads(json.dumps(result, allow_nan=False))
    except Exception as error:
        log.warning('readonly_startup_failed code=%s diagnostics=%s', type(error).__name__,
                    json.dumps(readonly_failure_details(error), sort_keys=True))
        raise


_startup = SingleFlightBundle(_build_startup, ttl_seconds=1, wait_seconds=8, failure_seconds=1)


def startup_view(part):
    if part not in ('state', 'model', 'sources'):
        raise ValueError('Unknown read-only startup view')
    try:
        return _startup.get()[part]
    except ReadOnlyRuntimeUnavailable:
        raise HTTPException(503, detail={
            'error_code': 'readonly_startup_busy',
            'message': '测试服正在读取主机初始数据或主机暂不可用，请稍后重试',
        }) from None
