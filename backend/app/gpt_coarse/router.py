"""Read-only authoritative GPT model for expert view and historical replay charts."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from .repository import active

router = APIRouter(prefix='/gpt-coarse', tags=['GPT专家模型'])


@router.get('/current')
def current(db: Session = Depends(get_db)):
    return dict(model=active(db, include_analysis=True))
