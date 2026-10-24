"""Inspect policy, run one bounded unit, or restore a cold bundle in isolation."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['status','preview-files','tick','restore','stable-backup'])
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--backup-root', type=Path)
    parser.add_argument('--backup-id')
    args = parser.parse_args()
    from app.database import SessionLocal, engine
    from app.storage_retention.policy import assert_writable
    from app.storage_retention.router import status
    if args.command == 'status':
        with SessionLocal() as db:
            result = status(db)
    else:
        assert_writable(engine)
        if args.command == 'preview-files':
            from app.storage_retention.housekeeping import file_tick
            result = file_tick(engine, Path(__file__).resolve().parents[2], execute=False)
        elif args.command == 'tick':
            from app.storage_retention.service import run_tick
            result = run_tick(SessionLocal)
        elif args.command == 'restore':
            if args.archive is None or args.destination is None:
                parser.error('restore requires --archive and a NEW --destination SQLite file')
            from app.storage_retention.bundle import restore_bundle
            result = {'restored':str(restore_bundle(args.archive, args.destination))}
        else:
            if args.backup_root is None or args.backup_id is None:
                parser.error('stable-backup requires --backup-root and --backup-id')
            from app.storage_retention.files import set_stable_backup, installation_identity
            result = set_stable_backup(args.backup_root.resolve(), args.backup_id,
                installation_id=installation_identity(Path(__file__).resolve().parents[2]))
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))


if __name__ == '__main__':
    main()
