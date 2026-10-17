"""Coalesce the laptop's three initial model/source/store reads."""
import json
import logging

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from ..observability import readonly_failure_details
from .runtime import ReadOnlyRuntimeUnavailable, SingleFlightBundle, host_now, read_window


log = logging.getLogger('dmcs.readonly_startup')
router = APIRouter(tags=['readonly-runtime'])


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


def _build_bootstrap():
    from ..config import APP_VERSION
    from ..database import SessionLocal
    from ..gpt_runtime.bootstrap import bootstrap_inputs
    from ..telemetry.domain import iso_time

    try:
        with read_window('display'), SessionLocal() as db:
            db.execute(text('BEGIN'))
            observed = host_now(db)
            result = dict(schema_version=1, mode='readonly-test', app_version=APP_VERSION,
                generated_at=iso_time(observed), **bootstrap_inputs(db, observed))
            return json.loads(json.dumps(result, allow_nan=False))
    except Exception as error:
        log.warning('readonly_bootstrap_failed code=%s diagnostics=%s', type(error).__name__,
                    json.dumps(readonly_failure_details(error), sort_keys=True))
        raise


_bootstrap = SingleFlightBundle(_build_bootstrap, ttl_seconds=1, wait_seconds=8, failure_seconds=1)


def bootstrap_view():
    try:
        return _bootstrap.get()
    except ReadOnlyRuntimeUnavailable:
        raise HTTPException(503, detail={
            'error_code': 'readonly_bootstrap_busy',
            'message': '测试服正在读取主机启动设置或主机暂不可用，请稍后重试',
        }) from None


@router.get('/readonly-runtime/bootstrap')
def bootstrap():
    return bootstrap_view()
