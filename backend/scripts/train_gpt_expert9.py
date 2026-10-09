"""Reproducible workbook training. Activation is explicit, additive and backed up."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.gpt_coarse.predict import FEATURES, predict
from app.gpt_coarse.training import train
from app.gpt_coarse.workbook import read_workbook

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path)
    parser.add_argument('--year', type=int, default=2026)
    parser.add_argument('--output', type=Path, default=ROOT/'backend/models/gpt_expert9/20261009.json')
    parser.add_argument('--activate', action='store_true', help='Activate an existing artifact, without retraining')
    args = parser.parse_args()
    if args.activate:
        from app.database import SessionLocal, engine
        from app.gpt_coarse.models import GptCoarseRevision
        from app.gpt_coarse.repository import activate
        artifact = json.loads(args.output.read_text(encoding='utf-8'))
        database = Path(engine.url.database)
        backup = database.parent/'backups'/f'before-gpt-expert9-{datetime.now():%Y%m%d-%H%M%S}.db'
        backup.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
        GptCoarseRevision.__table__.create(engine, checkfirst=True)
        with SessionLocal() as db:
            row = activate(db, artifact)
            db.commit()
            print(json.dumps(dict(activated=row.id, artifact_id=row.artifact_id, activated_at=row.activated_at,
                                  backup=str(backup)), ensure_ascii=False))
        return
    if not args.input:
        parser.error('--input is required for training')
    rows, audit = read_workbook(args.input, args.year)
    model, report = train(rows)
    reference = dict(zip(FEATURES[:8], model['reference']))
    report['expert_checks'] = [dict(level=level, prediction=predict(dict(reference, level=level),model)) for level in (25,50,75)]
    report['reference_conditions'] = reference
    report['data_expert_disagreement'] = dict(
        over_5pp=sum(abs(r['ash_content']-(30.5-.3*min(75,max(25,r['level'])))) > 5 for r in rows),
        note='用户已说明历史数值未必准确；误差衡量与旧记录的差异，不证明或否定现场真实精度。')
    artifact = dict(trained_at=datetime.now(timezone.utc).isoformat(timespec='seconds'), source=audit,
                    model=model, report=report, rows=rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    print(json.dumps(dict(output=str(args.output), n=model['n'], days=model['days'], alpha=model['alpha'],
                         inactive=model['inactive_factors'], historical=report['historical_fit'],
                         forward=report['forward_time_check'], anchors=report['expert_checks']), ensure_ascii=False))


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
