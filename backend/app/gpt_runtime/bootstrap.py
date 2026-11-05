"""Small, read-only inputs for opening the core decision workspace.

Historical stores and model reports stay behind their existing lazy endpoints.
The source resolver is shared with production, but its inventory adapter avoids
loading telemetry samples, auto-state model variants or historical input tables.
"""
from collections import defaultdict
from types import SimpleNamespace

from sqlalchemy import select

from ..hourly_brief.inputs import Inputs
from ..models import Setting
from ..services.decision_sources import config_view
from ..services.state import SETTING_TO_STORE
from ..telemetry.domain import iso_time
from ..telemetry.models import TelemetryPoint


class SourceChoices:
    """Supply only the operations config_view consumes, using original methods."""
    point = Inputs.point
    manual = Inputs.manual

    def status_members(self, members, source_id=None):
        # Startup lists the current enabled catalog, not historical cutover aliases.
        return members

    def __init__(self, db, when):
        self.db, self.hour, self.cutoff = db, when, iso_time(when)
        self.points = defaultdict(list)
        rows = db.execute(select(TelemetryPoint.source_id, TelemetryPoint.key,
            TelemetryPoint.config).where(TelemetryPoint.enabled.is_(True)))
        for source, key, config in rows:
            item = config.get('item_id')
            self.points[item].append(SimpleNamespace(source_id=source, key=key, config={'item_id': item}))


def bootstrap_inputs(db, when):
    """Return finite settings and the complete shared source view, never /state."""
    settings = {SETTING_TO_STORE[key]: value for key, value in db.execute(
        select(Setting.key, Setting.value).where(Setting.key.in_(tuple(SETTING_TO_STORE))))}
    sources = config_view(db, when=when, inputs=SourceChoices(db, when))
    return {'settings': settings, 'sources': sources}
