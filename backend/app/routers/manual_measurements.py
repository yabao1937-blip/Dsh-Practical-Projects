"""Database-backed manual process readings, independent of model inputs."""
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import require_write
from ..config import REMOTE_READONLY
from ..database import get_db
from ..models import ManualMeasurement
from ..schemas import (ManualMeasurementIn, ManualMeasurementList, ManualMeasurementOut, ManualMeasurementUpdate,
                       ManualMetric)
from ..services.manual_measurements import (METRICS, ENTRY_METRICS, latest_points, normalize_time, request_hash,
                                            timestamp_now)

class MeasurementRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validate(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # A nonstandard JSON NaN/Infinity must be rejected, not echoed into
                # an invalid JSON error response or accepted as a measurement.
                detail = [{key: error[key] for key in ("loc", "type", "msg")} for error in exc.errors()]
                return JSONResponse(status_code=422, content={"detail": detail})
        return validate


router = APIRouter(prefix="/manual-measurements", tags=["业务指标补录"], route_class=MeasurementRoute)


@router.get("/metrics")
def metrics():
    return {"items": [{"key": key, "name": value["name"], "unit": value["unit"],
                       "minimum": 0, "maximum": value["maximum"],
                       "archive_only": value.get("archive_only", False)} for key, value in ENTRY_METRICS.items()]}


@router.get("/latest")
def latest_measurements(db: Session = Depends(get_db)):
    if REMOTE_READONLY:
        from ..remote_readonly.operations import operations_view
        return operations_view('manual_measurements')
    if db.bind.dialect.name == "sqlite":
        db.execute(text("BEGIN"))
    return latest_points(db)


@router.get("", response_model=ManualMeasurementList)
def list_measurements(metric: ManualMetric | None = None,
                      start: str | None = None, end: str | None = None,
                      limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0, le=1000000),
                      db: Session = Depends(get_db)):
    if db.bind.dialect.name == "sqlite":
        db.execute(text("BEGIN"))
    query = db.query(ManualMeasurement)
    if metric is not None:
        query = query.filter(ManualMeasurement.metric == metric)
    # Optional observed-time window (inclusive bounds, normalized to the same
    # UTC ISO format as stored rows so plain string comparison stays ordered).
    # Without the parameters the historical behaviour is unchanged.
    for name, bound in (("start", start), ("end", end)):
        if bound is None or not bound.strip():
            continue
        try:
            normalized = normalize_time(bound)
        except ValueError as exc:
            raise HTTPException(422, f"{name} 时间无效：{exc}") from exc
        if normalized is None:
            raise HTTPException(422, f"{name} 时间无效")
        column = ManualMeasurement.observed_at >= normalized if name == "start" else ManualMeasurement.observed_at <= normalized
        query = query.filter(column)
    # Entry order lets an older backfilled measurement remain visible after saving.
    return {"total": query.count(), "items": query.order_by(ManualMeasurement.id.desc()).offset(offset).limit(limit).all()}


@router.post("", response_model=ManualMeasurementOut, status_code=201, dependencies=[Depends(require_write)])
def create_measurement(body: ManualMeasurementIn, response: Response, db: Session = Depends(get_db)):
    digest = request_hash(body.metric, body.value, body.observed_at)

    def existing_record():
        return db.query(ManualMeasurement).filter_by(client_id=body.client_id).first() if body.client_id else None

    def reuse(record):
        if record.request_hash != digest:
            raise HTTPException(409, "提交标识已用于其他补录内容，请重新提交")
        response.status_code = 200
        return record

    existing = existing_record()
    if existing is not None:
        return reuse(existing)
    definition = METRICS[body.metric]
    now = timestamp_now()
    record = ManualMeasurement(metric=body.metric, name=definition["name"], unit=definition["unit"],
                               value=body.value, observed_at=body.observed_at or now, created_at=now,
                               client_id=body.client_id, request_hash=digest, time_basis='production-time-v1')
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = existing_record()
        if existing is not None:
            return reuse(existing)
        raise
    db.refresh(record)
    return record


@router.patch("/{record_id}", response_model=ManualMeasurementOut, dependencies=[Depends(require_write)])
def update_measurement(record_id: int, body: ManualMeasurementUpdate, db: Session = Depends(get_db)):
    """Edit only the metric and value; observed_at/created_at semantics stay fixed."""
    record = db.get(ManualMeasurement, record_id)
    if record is None:
        raise HTTPException(404, "补录记录不存在或已删除")
    metric = body.metric if body.metric is not None else record.metric
    definition = METRICS[metric]
    value = record.value if body.value is None else body.value
    maximum = definition["maximum"]
    if value < 0 or (maximum is not None and value > maximum):
        raise HTTPException(422, "灰分和煤泥含量须在 0–100% 之间，煤量不得为负")
    record.metric = metric
    record.name = definition["name"]
    record.unit = definition["unit"]
    record.value = value
    # Keep the original POST idempotency hash: it belongs to the creating
    # submission (whose "server time" placeholder cannot be reconstructed
    # here). A stale client retry of that POST still reuses this row and sees
    # the edited content; a retry with different content keeps its 409.
    db.commit()
    db.refresh(record)
    return record


@router.delete("/{record_id}", dependencies=[Depends(require_write)])
def delete_measurement(record_id: int, db: Session = Depends(get_db)):
    """Hard-delete the stored row (explicit user requirement, no tombstone)."""
    record = db.get(ManualMeasurement, record_id)
    if record is None:
        raise HTTPException(404, "补录记录不存在或已删除")
    db.delete(record)
    db.commit()
    return {"ok": True, "id": record_id}
