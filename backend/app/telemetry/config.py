"""采集配置边界：显式点表、固定安全策略、凭据只从环境变量取。"""
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PointConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=512)
    unit: str = Field(default="", max_length=32)
    value_type: Literal["number", "boolean"] = "number"
    scale: float = 1
    offset: float = 0
    minimum: float | None = None
    maximum: float | None = None
    max_age_seconds: float = Field(default=30, ge=1, le=86400)
    # SourceTimestamp 可能是最后变更时间；没有现场依据时不以它判定仪表离线。
    max_source_age_seconds: float | None = Field(default=None, ge=1, le=86400)

    @model_validator(mode="after")
    def check_range(self):
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must not exceed maximum")
        return self


class CollectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    enabled: bool = False
    source_id: str = Field(default="kepserver-main", pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    name: str = Field(default="KEPServer", min_length=1, max_length=128)
    endpoint: str | None = None
    poll_seconds: float = Field(default=5, ge=0.1, le=60)
    timeout_seconds: float = Field(default=10, ge=0.1, le=30)
    retry_min_seconds: float = Field(default=1, ge=0.1, le=30)
    retry_max_seconds: float = Field(default=30, ge=0.1, le=120)
    history_interval_seconds: float = Field(default=60, ge=1, le=3600)
    retention_days: int = Field(default=7, ge=1, le=90)
    security: Literal["Basic256Sha256", "None"] = "Basic256Sha256"
    allow_insecure: bool = False
    application_uri: str = "urn:dmcs:collector"
    client_certificate: str | None = None
    client_key: str | None = None
    server_certificate: str | None = None
    username_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    password_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    points: list[PointConfig] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def validate_connection(self):
        if len({p.key for p in self.points}) != len(self.points):
            raise ValueError("duplicate point key")
        if len({p.node_id for p in self.points}) != len(self.points):
            raise ValueError("duplicate node_id")
        if self.retry_max_seconds < self.retry_min_seconds:
            raise ValueError("retry_max_seconds must be at least retry_min_seconds")
        if bool(self.username_env) != bool(self.password_env):
            raise ValueError("username_env and password_env must be configured together")
        if self.endpoint:
            url = urlsplit(self.endpoint)
            if url.scheme != "opc.tcp" or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("endpoint must be an opc.tcp URL without credentials, query or fragment")
            if url.port is None:
                raise ValueError("endpoint must specify a port")
        if self.enabled:
            if not self.endpoint or not self.points:
                raise ValueError("enabled collector requires endpoint and points")
            if self.history_interval_seconds < self.poll_seconds:
                raise ValueError("history interval must be at least poll interval")
            if any(p.max_age_seconds < self.poll_seconds * 2 for p in self.points):
                raise ValueError("point max_age_seconds must be at least twice poll_seconds")
            if self.security == "None":
                if not self.allow_insecure or self.username_env:
                    raise ValueError("None requires explicit allow_insecure and anonymous access")
            elif not all((self.client_certificate, self.client_key, self.server_certificate)):
                raise ValueError("encrypted connection requires client certificate, key and pinned server certificate")
        return self

    @property
    def lease_seconds(self) -> float:
        return max(30, self.poll_seconds * 3 + self.timeout_seconds * 3, self.retry_max_seconds * 2)


def load_config(path: Path) -> CollectorConfig:
    cfg = CollectorConfig.model_validate_json(path.read_text(encoding="utf-8-sig"))
    for field in ("client_certificate", "client_key", "server_certificate"):
        value = getattr(cfg, field)
        if value:
            candidate = Path(value)
            setattr(cfg, field, str((path.parent / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()))
            if cfg.enabled and cfg.security != "None" and not Path(getattr(cfg, field)).is_file():
                raise ValueError(f"missing {field} file")
    return cfg
