"""Opt-in local code server backed only by a remote read-only database."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def settings(path=None):
    explicit = os.environ.get('DMCS_REMOTE_READONLY_CONFIG')
    # Only the pytest harness may opt into its own disposable database. An
    # unrelated DATABASE_URL must never silently disable a configured test server.
    if (path is None and explicit is None
            and os.environ.get('DMCS_ISOLATED_TEST_DB') == '1'
            and os.environ.get('DMCS_DATABASE_URL')):
        return None
    required = path is not None or explicit is not None
    if explicit == '' and path is None:
        raise ValueError('Read-only test configuration path is empty')
    path = Path(path if path is not None else explicit or Path(__file__).parent/'data/remote-readonly.json')
    if not path.exists():
        if required:
            raise ValueError('Read-only test configuration is missing; no local database fallback is permitted')
        return None
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    parsed = urlsplit(data.get('server_url',''))
    if (data.get('mode') != 'readonly-test' or parsed.scheme not in ('http','https')
            or not parsed.hostname or parsed.username or parsed.password or parsed.query
            or parsed.fragment or parsed.path not in ('','/')):
        raise ValueError('Invalid read-only test configuration; no local database fallback is permitted')
    token = data.get('token')
    if not isinstance(token,str) or len(token) < 32 or token.strip() != token or '\r' in token or '\n' in token:
        raise ValueError('Read-only test access token is missing or invalid')
    return dict(server_url=data['server_url'].rstrip('/'),token=token)
