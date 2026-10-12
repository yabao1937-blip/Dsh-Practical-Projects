"""Prepare or explicitly activate the reviewed eight-factor artifact, with backup."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'backend'))

from app.gpt_coarse.promotion import prepare_expert8

CANDIDATE = ROOT/'backend/models/gpt_expert8/20261012/model.json'
COMPARISON = ROOT/'docs/verification/gpt-expert8-20261012/comparison.json'
PRODUCTION = CANDIDATE.with_name('production.json')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--activate', action='store_true', help='Append a revision after backing up SQLite')
    args = parser.parse_args()
    candidate = json.loads(CANDIDATE.read_text(encoding='utf-8'))
    comparison = json.loads(COMPARISON.read_text(encoding='utf-8'))
    artifact = prepare_expert8(candidate, comparison, hashlib.sha256(CANDIDATE.read_bytes()).hexdigest())
    if not args.activate:
        PRODUCTION.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
        print(json.dumps(dict(prepared=str(PRODUCTION), features=artifact['model']['feature_names'], activated=False), ensure_ascii=False))
        return
    if json.loads(PRODUCTION.read_text(encoding='utf-8')) != artifact:
        raise ValueError('待启用产物与已审阅候选不符，请先重新准备并验证')
    from app.database import SessionLocal, engine
    from app.gpt_coarse.repository import activate
    database = Path(engine.url.database)
    backup = database.parent/'backups'/f'before-gpt-expert8-{datetime.now():%Y%m%d-%H%M%S}.db'
    backup.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    with SessionLocal.begin() as db:
        row = activate(db, artifact)
        evidence = dict(revision=row.id, artifact_id=row.artifact_id, activated_at=row.activated_at,
                        artifact=str(PRODUCTION), backup=str(backup))
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
