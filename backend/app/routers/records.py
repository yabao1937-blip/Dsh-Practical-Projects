"""三表数据 coal_records CRUD"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import CoalRecord
from ..schemas import CoalRecordIn, CoalRecordList, CoalRecordOut

router = APIRouter(prefix="/records", tags=["三表数据"])


@router.get("", response_model=CoalRecordList)
def list_records(
    category: str | None = None,
    ts_from: str | None = None,
    ts_to: str | None = None,
    limit: int = Query(500, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    q = db.query(CoalRecord)
    if category:
        q = q.filter(CoalRecord.category == category)
    if ts_from:
        q = q.filter(CoalRecord.ts >= ts_from)
    if ts_to:
        q = q.filter(CoalRecord.ts <= ts_to)
    total = q.count()   # 过滤后真实总数（非 limit 截断条数）
    items = q.order_by(CoalRecord.ts).limit(limit).all()
    return {"total": total, "items": items}


@router.post("", response_model=CoalRecordOut)
def upsert_record(body: CoalRecordIn, db: Session = Depends(get_db)):
    # 按 (category, ts, system) 唯一键 upsert，从根上防重复导入
    rec = (db.query(CoalRecord)
           .filter(CoalRecord.category == body.category,
                   CoalRecord.ts == body.ts,
                   CoalRecord.system == body.system)
           .first())
    data = body.model_dump()
    if rec:
        for k, v in data.items():
            setattr(rec, k, v)
    else:
        rec = CoalRecord(**data)
        db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


@router.delete("/{rid}")
def delete_record(rid: int, db: Session = Depends(get_db)):
    rec = db.get(CoalRecord, rid)
    if not rec:
        raise HTTPException(404, "record not found")
    db.delete(rec)
    db.commit()
    return {"ok": True}
