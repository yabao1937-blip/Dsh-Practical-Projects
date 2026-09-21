"""重介精煤灰分采样"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import require_write
from ..database import get_db
from ..models import HeavySample
from ..schemas import HeavySampleIn

router = APIRouter(prefix="/samples", tags=["采样"])


def sample_dict(s):
    return {"id": s.id, "client_id": s.client_id, "timestamp": s.ts,
            "rho": s.rho, "ash_content": s.ash_content, "source": s.source, "synced": True}


@router.get('/heavy-ash')
def get_heavy_ash_samples(db: Session = Depends(get_db)):
    return [sample_dict(s) for s in db.query(HeavySample).order_by(HeavySample.ts, HeavySample.id)]


@router.post("/heavy-ash", dependencies=[Depends(require_write)])
def add_heavy_ash_sample(body: HeavySampleIn, db: Session = Depends(get_db)):
    try:
        ts = datetime.strptime(body.ts, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M:%S')
    except ValueError:
        raise HTTPException(422, '采样时间须为 YYYY-MM-DD HH:MM:SS')
    if body.client_id:
        existing = db.query(HeavySample).filter_by(client_id=body.client_id).first()
        if existing:
            if (existing.ts, existing.rho, existing.ash_content, existing.source) != (ts, body.rho, body.ash_content, body.source):
                raise HTTPException(409, '同一采样标识携带不同数据')
            return {"ok": True, "sample": sample_dict(existing), "id": existing.id}
    s = HeavySample(ts=ts, rho=body.rho, ash_content=body.ash_content, source=body.source, client_id=body.client_id)
    db.add(s)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if body.client_id:
            return add_heavy_ash_sample(body, db)
        raise
    db.refresh(s)
    return {"id": s.id, "ok": True, "sample": sample_dict(s)}
