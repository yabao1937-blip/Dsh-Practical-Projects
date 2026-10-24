"""Small read-only status, safe on the laptop's authenticated SQL bridge too."""
from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..database import get_db
from .models import RetentionArchive, RetentionState
from .policy import RetentionPolicy
from .service import STATE

router = APIRouter(prefix='/storage-retention', tags=['存储维护'])


@router.get('/status')
def status(db: Session=Depends(get_db)):
    exists = db.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='retention_state'")).first()
    if not exists:
        return {'installed':False, 'policy':RetentionPolicy().as_dict()}
    row = db.get(RetentionState, STATE)
    counts = dict(db.execute(select(RetentionArchive.status,func.count()).group_by(RetentionArchive.status)).all())
    return {'installed':True,'policy':RetentionPolicy().as_dict(),
        'state':row.value if row else {'phase':'awaiting-first-maintenance'},'archives':counts,
        'permanent':['hourly_statistics','hourly_chemistry_basis','manual_measurements',
                     'density_actions','model_and_configuration_versions'],
        'space_reclamation':'free-pages-reused-no-automatic-vacuum'}
