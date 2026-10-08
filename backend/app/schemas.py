"""Pydantic 模型（API 请求/响应）"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .services.manual_measurements import METRICS, normalize_time


class HealthResponse(BaseModel):
    status: str
    modules: list[str]


class MigrateReport(BaseModel):
    """localStorage 迁移报告（preview / apply 共用）"""
    counts: dict[str, int] = {}
    unmapped_keys: list[str] = []
    ok: bool = True
    error: str | None = None


# ---------- 配置 settings ----------

class SettingOut(BaseModel):
    key: str
    value: Any
    value_type: str = "json"


class SettingPut(BaseModel):
    value: Any
    value_type: str | None = None


# ---------- 录入层 inputs（auto_state 键值） ----------

class InputPut(BaseModel):
    value: Any


# ---------- 三表数据 coal_records ----------

class CoalRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: str
    ts: str
    system: str
    source: str
    ash_content: float | None = None
    coal_amount: float | None = None
    density: float | None = None
    level: float | None = None
    raw_ash: float | None = None
    moisture: float | None = None
    belt: str | None = None
    sysA: int = 0
    sysB: int = 0
    sys401: int = 0
    sys402: int = 0
    desliming473: int = 0
    desliming474: int = 0
    is_stoppage: int = 0
    mining_face: str | None = None
    filter_press_running: int | None = None
    influence_value: float | None = None
    annotation: str | None = None
    predicted_ash: float | None = None
    extra: dict | None = None


class CoalRecordIn(BaseModel):
    category: str = Field(..., description="coarse|float|ash_density")
    ts: str = Field(..., description="YYYY-MM-DD HH:MM:SS")
    system: str = "default"
    source: str = "import"
    ash_content: float | None = None
    coal_amount: float | None = None
    density: float | None = None
    level: float | None = None
    raw_ash: float | None = None
    moisture: float | None = None
    belt: str | None = None
    sysA: int = 0
    sysB: int = 0
    sys401: int = 0
    sys402: int = 0
    desliming473: int = 0
    desliming474: int = 0
    is_stoppage: int = 0
    mining_face: str | None = None
    filter_press_running: int | None = None
    influence_value: float | None = None
    annotation: str | None = None
    predicted_ash: float | None = None
    extra: dict | None = None


class CoalRecordList(BaseModel):
    total: int
    items: list[CoalRecordOut]


# ---------- 重介采样 ----------

class HeavySampleIn(BaseModel):
    ts: str
    rho: float = Field(ge=1.3, le=1.65, allow_inf_nan=False)
    ash_content: float = Field(ge=0, le=100, allow_inf_nan=False)
    source: str = "采样"
    client_id: str | None = Field(default=None, min_length=1, max_length=64)


# ---------- 手工补录 ----------

class ManualEntryIn(BaseModel):
    ts: str
    category: str
    values: dict | None = None
    remark: str | None = None
    operator: str | None = None
    status: str | None = None


class ManualEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ts: str
    category: str
    values: dict | None = None
    remark: str | None = None
    operator: str | None = None
    status: str | None = None


# ---------- Single-value manual process measurements ----------
ManualMetric = Literal["heavy_clean_coal_ash", "raw_coal_slime_content", "coarse_slime_amount"]


class ManualMeasurementIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: ManualMetric
    value: float = Field(ge=0, strict=True, allow_inf_nan=False)
    observed_at: str | None = Field(default=None, max_length=64)
    client_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("observed_at")
    @classmethod
    def validate_time(cls, value):
        return normalize_time(value)

    @model_validator(mode="after")
    def validate_percentage(self):
        maximum = METRICS[self.metric]["maximum"]
        if maximum is not None and self.value > maximum:
            raise ValueError("灰分和煤泥含量须在 0–100% 之间")
        return self


class ManualMeasurementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    metric: ManualMetric
    name: str
    value: float
    unit: str
    observed_at: str
    created_at: str


class ManualMeasurementList(BaseModel):
    total: int
    items: list[ManualMeasurementOut]
