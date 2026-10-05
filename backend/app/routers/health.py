"""存活与就绪分开：就绪实际检查 DB 和运行所需的表。"""
import logging
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, text

from ..config import APP_VERSION
from ..database import engine
from ..telemetry.domain import iso_time, utc_now

router = APIRouter(tags=["系统"])
started = time.monotonic()
log = logging.getLogger("dmcs.health")
REQUIRED_TABLES = {"coal_records", "settings", "auto_state", "telemetry_sources", "telemetry_points", "telemetry_readings"}


@router.get("/health/live")
def live():
    return {"status": "ok", "version": APP_VERSION, "uptime_seconds": round(time.monotonic() - started, 1)}


@router.get("/health/ready")
@router.get("/health")
def health():
    status, code = "ok", None
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            if not REQUIRED_TABLES.issubset(inspect(connection).get_table_names()):
                status, code = "unavailable", "schema_not_ready"
    except Exception as exc:
        status, code = "unavailable", "database_unavailable"
        log.warning("database_health_failed code=%s", type(exc).__name__)
    return JSONResponse({"status": status, "modules": ["coal-records", "telemetry"],
        "version": APP_VERSION, "checked_at": iso_time(utc_now()),
        "uptime_seconds": round(time.monotonic() - started, 1),
        "checks": {"database": {"status": status, "error_code": code}}}, status_code=200 if status == "ok" else 503)
