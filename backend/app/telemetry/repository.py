"""采集存储：短事务、单来源租约、幂等批次、当前快照和限频历史。"""
from datetime import timedelta

from sqlalchemy import delete, select, text

from ..database import SessionLocal
from .config import CollectorConfig
from .domain import availability, definition_id, iso_time, parse_time, utc_now
from .models import TelemetryPoint, TelemetryReading, TelemetrySource
from .derived import density_average_points, system_status_points
from .retained import (raw_sample, point_memory, remember_point, remember_density,
                       density_rows, density_memory, density_definition)


class CollectorBusy(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class TelemetryRepository:
    def __init__(self, session_factory=SessionLocal):
        self.sessions = session_factory

    @staticmethod
    def _lock(db):
        if db.bind.dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))

    @staticmethod
    def _owned(db, source_id, owner):
        row = db.get(TelemetrySource, source_id, with_for_update=True)
        if not row or row.owner != owner:
            raise LeaseLost("collector lease lost")
        return row

    def acquire(self, cfg: CollectorConfig, owner: str):
        now = utc_now()
        with self.sessions() as db:
            self._lock(db)
            row = db.get(TelemetrySource, cfg.source_id, with_for_update=True)
            if row and row.owner != owner and row.lease_until and parse_time(row.lease_until) > now:
                raise CollectorBusy("another collector owns this source")
            if not row:
                row = TelemetrySource(source_id=cfg.source_id, name=cfg.name, state="connecting")
                db.add(row)
            row.name, row.owner, row.state = cfg.name, owner, "connecting"
            row.heartbeat_at = iso_time(now)
            row.lease_until = iso_time(now + timedelta(seconds=cfg.lease_seconds))
            row.error_code, row.retry_seconds = None, 0
            for previous in db.scalars(select(TelemetryPoint).where(TelemetryPoint.source_id == cfg.source_id)):
                previous.enabled = False
            for point in cfg.points:
                current = db.get(TelemetryPoint, (cfg.source_id, point.key))
                if current is None:
                    current = TelemetryPoint(source_id=cfg.source_id, key=point.key, config=point.model_dump())
                    db.add(current)
                elif any(current.config.get(k) != getattr(point, k) for k in ("node_id", "unit", "scale", "offset", "value_type", "minimum", "maximum")):
                    current.latest, current.last_history_at = None, None
                current.config, current.enabled = point.model_dump(), True
            db.commit()

    def state(self, cfg, owner, state, error_code=None, retry_seconds=0, release=False):
        now = utc_now()
        with self.sessions() as db:
            self._lock(db)
            row = self._owned(db, cfg.source_id, owner)
            row.state, row.error_code, row.retry_seconds = state, error_code, retry_seconds
            row.heartbeat_at = iso_time(now)
            row.lease_until = iso_time(now + timedelta(seconds=cfg.lease_seconds))
            if state == "stopped" or release:
                row.owner, row.lease_until = None, None
            db.commit()

    def persist(self, cfg, owner, batch: dict):
        if batch["source_id"] != cfg.source_id:
            raise ValueError("spool source mismatch")
        now = utc_now()
        with self.sessions() as db:
            self._lock(db)
            source = self._owned(db, cfg.source_id, owner)
            source.heartbeat_at = iso_time(now)
            source.lease_until = iso_time(now + timedelta(seconds=cfg.lease_seconds))
            for sample in batch["samples"]:
                received = sample["received_at"]
                point = db.get(TelemetryPoint, (cfg.source_id, sample["key"]))
                if point is None:
                    raise ValueError("unknown spool point")
                existing = db.scalar(select(TelemetryReading.id).where(
                    TelemetryReading.source_id == cfg.source_id, TelemetryReading.point_key == sample["key"],
                    TelemetryReading.batch_id == batch["batch_id"]))
                changed_quality = not point.latest or (point.latest.get("quality"), point.latest.get("issue")) != (sample["quality"], sample.get("issue"))
                due = not point.last_history_at or (parse_time(received) - parse_time(point.last_history_at)).total_seconds() >= cfg.history_interval_seconds
                if existing is None and (due or changed_quality):
                    db.add(TelemetryReading(source_id=cfg.source_id, point_key=sample["key"],
                        batch_id=batch["batch_id"], received_at=received, sample=sample))
                    if not point.last_history_at or received > point.last_history_at:
                        point.last_history_at = received
                # 重放旧批次/旧点位定义可以保留历史，但不能回退当前快照。
                matches = sample.get("definition_id") == definition_id(point.config)
                if matches and (not point.latest or received >= point.latest["received_at"]):
                    # Preserve the nearest pre-hour reading before a 5-second latest
                    # update replaces it. The regular history interval is 60 seconds.
                    previous = raw_sample(point.latest)
                    if (previous and previous.get("batch_id")
                            and parse_time(previous["received_at"]).replace(minute=0, second=0, microsecond=0)
                            < parse_time(received).replace(minute=0, second=0, microsecond=0)):
                        saved = db.scalar(select(TelemetryReading.id).where(
                            TelemetryReading.source_id == cfg.source_id,
                            TelemetryReading.point_key == point.key,
                            TelemetryReading.batch_id == previous["batch_id"]))
                        if saved is None:
                            db.add(TelemetryReading(source_id=cfg.source_id, point_key=point.key,
                                batch_id=previous["batch_id"], received_at=previous["received_at"], sample=previous))
                    point.latest = remember_point(db, point, {**sample, "batch_id": batch["batch_id"]})
                if not source.last_sample_at or received > source.last_sample_at:
                    source.last_sample_at = received
            remember_density(db, cfg.source_id)
            db.commit()

    def prune(self, cfg):
        cutoff = iso_time(utc_now() - timedelta(days=cfg.retention_days))
        with self.sessions() as db:
            ids = select(TelemetryReading.id).where(TelemetryReading.source_id == cfg.source_id,
                TelemetryReading.received_at < cutoff).order_by(TelemetryReading.received_at).limit(5000)
            db.execute(delete(TelemetryReading).where(TelemetryReading.id.in_(ids)))
            db.commit()


def latest_view(db, source_id=None, now=None):
    now = now or utc_now()
    sources_query = select(TelemetrySource).order_by(TelemetrySource.source_id)
    points_query = select(TelemetryPoint).where(TelemetryPoint.enabled.is_(True)).order_by(TelemetryPoint.source_id, TelemetryPoint.key)
    if source_id:
        sources_query = sources_query.where(TelemetrySource.source_id == source_id)
        points_query = points_query.where(TelemetryPoint.source_id == source_id)
    sources = []
    online = {}
    for row in db.scalars(sources_query):
        alive = bool(row.lease_until and parse_time(row.lease_until) > now)
        online[row.source_id] = row.state == "connected" and alive
        state = "offline" if row.state in {"connecting", "connected", "retrying"} and not alive else row.state
        sources.append({"source_id": row.source_id, "name": row.name, "state": state,
            "heartbeat_at": row.heartbeat_at, "last_sample_at": row.last_sample_at,
            "error_code": row.error_code, "retry_seconds": row.retry_seconds})
    items = []
    for row in db.scalars(points_query):
        usable, reason = availability(row.latest, row.config, online.get(row.source_id, False), now)
        memory = point_memory(db, row)
        items.append({"source_id": row.source_id, "key": row.key, "name": row.config["name"],
            "device": row.config.get("device", ""), "tag_name": row.config.get("tag_name", ""),
            "description": row.config.get("description", ""), "item_id": row.config.get("item_id", ""),
            "unit": row.config["unit"], "sample": raw_sample(row.latest), "available": usable, "status": reason,
            "last_valid_sample": memory["last_valid"], "last_nonzero_sample": memory["last_nonzero"]})
    averages = density_average_points(items)
    for item in averages:
        rows = density_rows(db, item["source_id"])
        if all(rows):
            item["sample"]["definition_id"] = density_definition(rows)
            item["last_valid_sample"] = density_memory(db, rows)
    return {"checked_at": iso_time(now), "sources": sources, "items": items,
            "derived_items": system_status_points(items) + averages,
            "summary": {"points": len(items), "available": sum(p["available"] for p in items)}}
