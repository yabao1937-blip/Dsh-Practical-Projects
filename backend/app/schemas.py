"""Pydantic 模型（API 请求/响应）"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .services.manual_measurements import METRICS, normalize_time


class HealthResponse(BaseModel):
    status: str
    modules: list[str]




# ---------- 配置 settings ----------

class SettingOut(BaseModel):
    key: str
    value: Any
    value_type: str = "json"


class SettingPut(BaseModel):
    value: Any
    value_type: str | None = None


# ---------- 录入层 inputs（auto_state 键值） ----------



# ---------- 三表数据 coal_records ----------







# ---------- 重介采样 ----------

class HeavySampleIn(BaseModel):
    ts: str
    rho: float = Field(ge=1.3, le=1.65, allow_inf_nan=False)
    ash_content: float = Field(ge=0, le=100, allow_inf_nan=False)
    source: str = "采样"
    client_id: str | None = Field(default=None, min_length=1, max_length=64)


# ---------- 手工补录 ----------





# ---------- Single-value manual process measurements ----------
ManualMetric = Literal["heavy_clean_coal_ash", "raw_coal_slime_content", "coarse_slime_amount", "coarse_slime_ash", "float_clean_coal_ash"]
ManualEntryMetric = Literal["heavy_clean_coal_ash", "raw_coal_slime_content", "coarse_slime_ash", "float_clean_coal_ash"]


class ManualMeasurementIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: ManualEntryMetric
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


class ManualMeasurementUpdate(BaseModel):
    """Partial edit of a stored measurement: only metric and value may change."""
    model_config = ConfigDict(extra="forbid")
    metric: ManualEntryMetric | None = None
    value: float | None = Field(default=None, ge=0, strict=True, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_pair(self):
        if self.metric is None and self.value is None:
            raise ValueError("至少需要修改信息名或值")
        if self.metric is not None and self.value is not None:
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
    time_basis: str


class ManualMeasurementList(BaseModel):
    total: int
    items: list[ManualMeasurementOut]
