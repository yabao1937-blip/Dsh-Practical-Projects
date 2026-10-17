"""请求关联与安全错误响应：不记录请求体、查询参数、认证信息。"""
import json
import logging
import re
import time
import uuid

from fastapi.responses import JSONResponse

from .config import REMOTE_READONLY

log = logging.getLogger("dmcs.http")


def readonly_failure_details(exc):
    """Bounded, value-free diagnostics; production exception logging is unchanged."""
    if not REMOTE_READONLY:
        return {}
    from remote_sqlite import _RemoteError
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    details, pending, seen = {}, [exc], set()
    while pending and len(seen) < 12:
        current = pending.pop(0)
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, _RemoteError):
            details.setdefault('readonly_error_code', current.error_code)
            if current.status is not None:
                details.setdefault('readonly_http_status', current.status)
        if isinstance(current, PoolTimeout):
            details['readonly_pool_timeout'] = True
        pending.extend([getattr(current, 'orig', None), current.__cause__, current.__context__])
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions[:12])
    return details


def install_observability(app):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.setLevel(logging.INFO)
    @app.middleware("http")
    async def request_context(request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", supplied) else uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            # Never stringify exceptions: ORM errors include SQL and parameters.
            details = readonly_failure_details(exc)
            suffix = ' diagnostics=' + json.dumps(details, sort_keys=True) if details else ''
            log.error("request_failed request_id=%s code=%s%s", request_id, type(exc).__name__, suffix)
            response = JSONResponse({"detail": {"error": "服务器内部错误，请使用请求编号排查"},
                "request_id": request_id}, status_code=500)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            log.info(json.dumps({"request_id": request_id, "method": request.method,
                "path": request.url.path, "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1)}, ensure_ascii=False))
        return response
