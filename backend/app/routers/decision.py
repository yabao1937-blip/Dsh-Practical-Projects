"""Stateless decision calculations, without saving inputs or issuing actions."""
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session
from sqlalchemy import text

from ..database import get_db
from ..services.decision_live import calculate, LABELS
from ..services.state import load_store

router = APIRouter(prefix='/decision', tags=['实时决策计算'])


class InputCell(BaseModel):
    model_config = ConfigDict(extra='forbid')
    value: float | None = Field(None, strict=True, allow_inf_nan=False)
    status: Literal['good','manual','historical','default','held','stopped','missing'] = 'missing'
    unit: str = Field('',max_length=20)
    source: str = Field('',max_length=800)
    reason: str = Field('',max_length=2000)
    observed_at: str | None = Field(None,max_length=64)
    entered_at: str | None = Field(None,max_length=64)
    item_id: str = Field('',max_length=200)
    source_id: str = Field('',max_length=64)
    batch_id: str | None = Field(None,max_length=64)


class SnapshotInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    cells: dict[str,InputCell] = Field(max_length=len(LABELS))
    target_min: float = Field(8.5,ge=0,le=100,allow_inf_nan=False)
    target_max: float = Field(9.0,ge=0,le=100,allow_inf_nan=False)

    @model_validator(mode='after')
    def check(self):
        if self.target_min > self.target_max or set(self.cells)-LABELS.keys():
            raise ValueError('目标范围或输入字段不正确')
        return self


@router.post('/snapshots')
def snapshot(body: SnapshotInput, db: Session = Depends(get_db)):
    if db.bind.dialect.name == 'sqlite':
        db.execute(text('BEGIN'))
    return calculate({k:v.model_dump() for k,v in body.cells.items()},load_store(db),body.target_min,body.target_max)
