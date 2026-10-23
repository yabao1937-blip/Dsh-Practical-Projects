"""One bounded host snapshot for the laptop's frequent operational reads."""
import json
import logging
import time

from fastapi import APIRouter, HTTPException
from sqlalchemy import inspect, text

from ..config import APP_VERSION, REMOTE_READONLY
from ..observability import readonly_failure_details
from ..telemetry.domain import iso_time
from .runtime import ReadOnlyRuntimeUnavailable, SingleFlightBundle, host_now, read_window


router = APIRouter(prefix='/readonly-runtime', tags=['只读测试服'])
log = logging.getLogger('dmcs.readonly_operations')


def _json(value):
    # Cache only detached, valid JSON. A bad section cannot poison other sections.
    return json.loads(json.dumps(value, allow_nan=False))


def _begin(db):
    db.execute(text('BEGIN'))
    return host_now(db)


def _log_failure(section, error):
    details = readonly_failure_details(error)
    log.warning('readonly_operations_failed section=%s code=%s diagnostics=%s',
                section, type(error).__name__, json.dumps(details, sort_keys=True))


def _build_operations():
    from ..database import SessionLocal
    from ..routers import health as health_router
    from ..services.manual_measurements import latest_points
    from ..telemetry.repository import latest_view

    with read_window('display'), SessionLocal() as db:
        try:
            observed = _begin(db)
        except Exception as error:
            _log_failure('snapshot', error)
            raise
        stamp = iso_time(observed)
        from .live_cache import attach_runtime
        attach_runtime(db)
        result = {'schema_version': 1, 'generated_at': stamp,
                  'health': None, 'telemetry': None, 'manual_measurements': None, 'errors': {}}
        health = {'ok': True, 'status': 'ok', 'modules': ['coal-records', 'telemetry'],
                  'version': APP_VERSION, 'checked_at': stamp,
                  'uptime_seconds': round(time.monotonic() - health_router.started, 1),
                  'checks': {'database': {'status': 'ok', 'error_code': None}}}
        restart = False
        try:
            db.execute(text('SELECT 1'))
            if not health_router.REQUIRED_TABLES.issubset(inspect(db.connection()).get_table_names()):
                health.update(ok=False, status='unavailable')
                health['checks']['database'] = {'status': 'unavailable', 'error_code': 'schema_not_ready'}
        except Exception as error:
            _log_failure('health', error)
            health.update(ok=False, status='unavailable')
            health['checks']['database'] = {'status': 'unavailable', 'error_code': 'database_unavailable'}
            result['errors']['health'] = {'error_code': 'readonly_health_unavailable'}
            restart = True
        result['health'] = health

        for section, reader in (('telemetry', latest_view), ('manual_measurements', latest_points)):
            try:
                if restart:
                    # A failed remote query may invalidate its snapshot. Do not
                    # retry it: start a fresh snapshot only for the next section.
                    db.rollback()
                    observed = _begin(db)
                result[section] = _json(reader(db, now=observed))
                restart = False
            except Exception as error:
                _log_failure(section, error)
                result['errors'][section] = {'error_code': {
                    'telemetry': 'readonly_telemetry_unavailable',
                    'manual_measurements': 'readonly_manual_unavailable',
                }[section]}
                restart = True
        return _json(result)


_operations = SingleFlightBundle(_build_operations, ttl_seconds=1, wait_seconds=8, failure_seconds=1)


def operations_view(part=None):
    if part not in (None, 'health', 'telemetry', 'manual_measurements'):
        raise ValueError('Unknown read-only operations view')
    try:
        result = _operations.get()
    except ReadOnlyRuntimeUnavailable:
        raise HTTPException(503, detail={
            'error_code': 'readonly_operations_busy',
            'message': '测试服正在读取主机数据或主机暂不可用，请稍后重试',
        }) from None
    if part is None:
        return result
    if result[part] is None:
        raise HTTPException(503, detail=result['errors'][part])
    return result[part]


@router.get('/operations')
def operations():
    if not REMOTE_READONLY:
        raise HTTPException(404, 'Not Found')
    return operations_view()
