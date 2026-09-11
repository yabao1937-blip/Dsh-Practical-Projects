"""手工补录历史"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import require_write
from ..database import get_db
from ..models import ManualEntry
from ..schemas import ManualEntryIn, ManualEntryOut

router = APIRouter(prefix="/manual-entries", tags=["手工补录"])


@router.get("", response_model=list[ManualEntryOut])
def list_entries(db: Session = Depends(get_db)):
    return db.query(ManualEntry).order_by(ManualEntry.id.desc()).all()


@router.post("", response_model=ManualEntryOut, dependencies=[Depends(require_write)])
def add_entry(body: ManualEntryIn, db: Session = Depends(get_db)):
    e = ManualEntry(**body.model_dump())
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


@router.delete("/{eid}")
def delete_entry(eid: int, db: Session = Depends(get_db)):
    e = db.get(ManualEntry, eid)
    if not e:
        raise HTTPException(404, "entry not found")
    db.delete(e)
    db.commit()
    return {"ok": True}
