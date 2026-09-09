"""重介精煤灰分采样"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import HeavySample
from ..schemas import HeavySampleIn

router = APIRouter(prefix="/samples", tags=["采样"])


@router.post("/heavy-ash")
def add_heavy_ash_sample(body: HeavySampleIn, db: Session = Depends(get_db)):
    s = HeavySample(ts=body.ts, rho=body.rho, ash_content=body.ash_content, source=body.source)
    db.add(s)
    db.commit()
    db.refresh(s)
    return {"id": s.id, "ok": True}
