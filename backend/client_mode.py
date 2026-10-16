"""Local opt-in browser client: no database or collector is started on this PC."""
import json
from pathlib import Path
from urllib.parse import urlsplit


def client_url(path=None):
    path = Path(path) if path is not None else Path(__file__).parent/'data/client-mode.json'
    if not path.exists():
        return None
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    if config.get('mode') != 'client':
        raise ValueError('Invalid local client-mode configuration; no service was started')
    url = config.get('server_url', '')
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('','/')):
        raise ValueError('Client server_url must be an HTTP(S) origin')
    return url.rstrip('/')+'/'
