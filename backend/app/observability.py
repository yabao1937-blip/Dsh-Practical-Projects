"""请求关联与安全错误响应：不记录请求体、查询参数、认证信息。"""
import json
import logging
import re
import time
import uuid

from fastapi.responses import JSONResponse

log = logging.getLogger("dmcs.http")


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
            # 仅异常类型可进日志；SQL 参数/路径/密钥不能随异常文本回显。
            log.error("request_failed request_id=%s code=%s", request_id, type(exc).__name__)
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
