"""Certified closed files only; unknown legacy backups remain protected."""
from pathlib import Path

from .files import (execute_cleanup, installation_identity, plan_cleanup,
                    web_log_archive_root, backup_registry_root)
from .policy import assert_writable


def roots(engine, install_root):
    data = Path(engine.url.database).resolve().parent
    candidates = [data/'telemetry-runtime', web_log_archive_root(install_root)]
    try:
        candidates.insert(0, backup_registry_root(install_root, Path(engine.url.database)))
    except ValueError:
        # An unsupported cross-volume layout cannot silently weaken 2+1 rules.
        pass
    return list(dict.fromkeys(p.resolve() for p in candidates if p.is_dir()))


def file_tick(engine, install_root, now=None, *, execute=True):
    assert_writable(engine)
    identity = installation_identity(install_root)
    results = []
    for root in roots(engine, install_root):
        # One superseded full backup at a time, rather than many large unlinks.
        plan = plan_cleanup(root, installation_id=identity, now=now, max_items=1)
        if execute and plan['actions']:
            results.append({'root':str(root), 'result':execute_cleanup(root, plan,
                installation_id=identity, now=now, max_items=1, max_seconds=3)})
        elif not execute:
            results.append({'root':str(root), 'plan':plan})
    return results
