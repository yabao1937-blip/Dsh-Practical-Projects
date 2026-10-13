"""Database-backed manual process readings, independent of model inputs."""
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import require_write
from ..database import get_db
from ..models import ManualMeasurement
from ..schemas import ManualMeasurementIn, ManualMeasurementList, ManualMeasurementOut, ManualMetric
from ..services.manual_measurements import METRICS, ENTRY_METRICS, latest_points, request_hash, timestamp_now

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
    if db.bind.dialect.name == "sqlite":
        db.execute(text("BEGIN"))
    return latest_points(db)


@router.get("", response_model=ManualMeasurementList)
def list_measurements(metric: ManualMetric | None = None,
                      limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0, le=1000000),
                      db: Session = Depends(get_db)):
    if db.bind.dialect.name == "sqlite":
        db.execute(text("BEGIN"))
    query = db.query(ManualMeasurement)
    if metric is not None:
        query = query.filter(ManualMeasurement.metric == metric)
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
                               client_id=body.client_id, request_hash=digest)
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
