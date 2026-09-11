"""配置读写（settings 键值表）"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import require_write
from ..database import get_db
from ..models import Setting
from ..schemas import SettingOut, SettingPut

router = APIRouter(prefix="/settings", tags=["配置"])


@router.get("", response_model=list[SettingOut])
def list_settings(db: Session = Depends(get_db)):
    return db.query(Setting).order_by(Setting.key).all()


@router.get("/{key}", response_model=SettingOut)
def get_setting(key: str, db: Session = Depends(get_db)):
    s = db.get(Setting, key)
    if not s:
        raise HTTPException(404, f"setting {key} not found")
    return s


@router.put("/{key}", response_model=SettingOut, dependencies=[Depends(require_write)])
def put_setting(key: str, body: SettingPut, db: Session = Depends(get_db)):
    s = db.get(Setting, key)
    if not s:
        s = Setting(key=key)
        db.add(s)
    s.value = body.value
    if body.value_type is not None:
        s.value_type = body.value_type
    db.commit()
    db.refresh(s)
    return s
