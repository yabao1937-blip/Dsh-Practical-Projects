"""Pydantic 模型（API 请求/响应）"""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
    rho: float
    ash_content: float
    source: str = "采样"


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
