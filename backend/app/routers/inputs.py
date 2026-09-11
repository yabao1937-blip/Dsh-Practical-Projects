"""录入层（auto_state 键值表：amountInputs/ashInputs/instrumentInputs/... 的手动覆盖）"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..auth import require_write
from ..database import get_db
from ..models import AutoState
from ..schemas import InputPut

router = APIRouter(prefix="/inputs", tags=["录入层"])


@router.get("")
def list_inputs(db: Session = Depends(get_db)):
    return {s.key: s.value for s in db.query(AutoState).all()}


@router.put("/{key}", dependencies=[Depends(require_write)])
def put_input(key: str, body: InputPut, db: Session = Depends(get_db)):
    s = db.get(AutoState, key)
    if not s:
        s = AutoState(key=key)
        db.add(s)
    s.value = body.value
    db.commit()
    return {"key": key, "value": body.value}
