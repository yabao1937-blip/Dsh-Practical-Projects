"""Time-bounded reads and explicit artifact activation; never replaces DS tables."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy import select

from .models import GptCoarseRevision
from .predict import valid


def stamp(when=None):
    when = when or datetime.now(timezone.utc)
    return when.astimezone(timezone.utc).isoformat(timespec='milliseconds')


def identity(artifact):
    return hashlib.sha256(json.dumps(artifact, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def active(db, when=None, include_analysis=False):
    row = db.scalar(select(GptCoarseRevision).where(GptCoarseRevision.activated_at <= stamp(when))
                    .order_by(GptCoarseRevision.activated_at.desc(), GptCoarseRevision.id.desc()).limit(1))
    if not row:
        return None
    artifact = row.artifact
    result = dict(engine='gpt', production='expert9', expert9=deepcopy(artifact['model']),
                  trainedAt=artifact['trained_at'], activated_at=row.activated_at, revision=row.id,
                  artifact_id=row.artifact_id, n=artifact['model']['n'], range='expert-workbook')
    if include_analysis:
        result.update(report=deepcopy(artifact['report']), source=deepcopy(artifact['source']),
                      rows=deepcopy(artifact['rows']))
    return result


def activate(db, artifact, when=None):
    if not valid(artifact.get('model')):
        raise ValueError('无效的GPT专家九因子模型')
    key = identity(artifact)
    existing = db.scalar(select(GptCoarseRevision).where(GptCoarseRevision.artifact_id == key))
    if existing:
        return existing
    row = GptCoarseRevision(artifact_id=key, activated_at=stamp(when), artifact=deepcopy(artifact))
    db.add(row)
    db.flush()
    return row
