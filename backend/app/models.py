"""SQLAlchemy ORM 模型：前后端分离的 11 张表。

口径说明：
- 业务时间戳 ts 存为规范的 "YYYY-MM-DD HH:MM:SS" 字符串（与前端 JS 完全一致），
  字符串序 == 时间序，避免 datetime 解析/时区漂移，保证 golden 对拍逐值一致。
- category/source 等枚举用 String + 注释 + Pydantic 校验，跨 SQLite/MySQL/PG 可移植。
"""
from datetime import datetime

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def _now() -> datetime:
    return datetime.now()


class CoalRecord(Base):
    """三表合并主表：coarse=表1粗精煤泥多因素 / float=表2浮精 / ash_density=表3灰分密度"""
    __tablename__ = "coal_records"
    __table_args__ = (
        UniqueConstraint("category", "ts", "system", name="uq_coal_cat_ts_sys"),
        Index("ix_coal_ts", "ts"),
        Index("ix_coal_cat_ts", "category", "ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(16), nullable=False)  # coarse|float|ash_density
    ts: Mapped[str] = mapped_column(String(19), nullable=False)         # YYYY-MM-DD HH:MM:SS
    system: Mapped[str] = mapped_column(String(32), nullable=False, default="default")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="import")  # import|manual|instrument

    # 三类共用（按 category 取义，其余为 NULL）
    ash_content: Mapped[float | None] = mapped_column(Float, nullable=True)   # 表1=315灰分 / 表2=浮精灰分 / 表3=灰分
    coal_amount: Mapped[float | None] = mapped_column(Float, nullable=True)   # 表1=带煤量 / 表2=浮精量
    density: Mapped[float | None] = mapped_column(Float, nullable=True)       # 表3 密度
    level: Mapped[float | None] = mapped_column(Float, nullable=True)         # 表1 精磁尾液位
    raw_ash: Mapped[float | None] = mapped_column(Float, nullable=True)       # 表1 原煤灰分
    moisture: Mapped[float | None] = mapped_column(Float, nullable=True)      # 表1 全水分
    belt: Mapped[str | None] = mapped_column(String(16), nullable=True)       # 表3 皮带 501/502

    # 表1 因子
    sysA: Mapped[int] = mapped_column(Integer, default=0)
    sysB: Mapped[int] = mapped_column(Integer, default=0)
    sys401: Mapped[int] = mapped_column(Integer, default=0)
    sys402: Mapped[int] = mapped_column(Integer, default=0)
    desliming473: Mapped[int] = mapped_column(Integer, default=0)
    desliming474: Mapped[int] = mapped_column(Integer, default=0)
    is_stoppage: Mapped[int] = mapped_column(Integer, default=0)
    mining_face: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 表2
    filter_press_running: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 派生/兜底
    influence_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    annotation: Mapped[str | None] = mapped_column(String(64), nullable=True)
    predicted_ash: Mapped[float | None] = mapped_column(Float, nullable=True)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    import_log_id: Mapped[int | None] = mapped_column(ForeignKey("import_logs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class CalcLog(Base):
    """补录日志：ash_meter(灰分仪) / density_meter(密度计) / belt_scale(皮带秤)"""
    __tablename__ = "calc_logs"
    __table_args__ = (Index("ix_calc_type_ts", "calc_type", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(19), nullable=False)
    calc_type: Mapped[str] = mapped_column(String(24), nullable=False)  # ash_meter|density_meter|belt_scale
    system: Mapped[str] = mapped_column(String(32), default="default")
    input_json: Mapped[str] = mapped_column(String(2000), default="{}")
    output_json: Mapped[str] = mapped_column(String(2000), default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ManualEntry(Base):
    """手工补录历史（审计）"""
    __tablename__ = "manual_entries"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(19), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    values: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    operator: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class HeavySample(Base):
    """重介精煤灰分采样训练对 (ts, rho, ash)"""
    __tablename__ = "heavy_samples"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(19), nullable=False)
    rho: Mapped[float] = mapped_column(Float, nullable=False)
    ash_content: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="采样")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ImportLog(Base):
    """导入日志"""
    __tablename__ = "import_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(19), default="")
    category: Mapped[str] = mapped_column(String(24), default="")
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    errors: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Alert(Base):
    """告警"""
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    system: Mapped[str] = mapped_column(String(32), default="合并")
    level: Mapped[str] = mapped_column(String(16), default="提示")
    message: Mapped[str] = mapped_column(String(512), default="")
    ts: Mapped[str] = mapped_column(String(19), default="")
    ack: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class CoarseModel(Base):
    """当前粗灰模型（MLR/PLS，is_current 标记）；每 method 一行，train_run_id 关联同批两行"""
    __tablename__ = "coarse_models"
    __table_args__ = (UniqueConstraint("method", name="uq_coarse_model_method"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    method: Mapped[str] = mapped_column(String(8), nullable=False)  # mlr|pls
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    train_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 同批 mlr+pls 关联
    feature_names: Mapped[list | None] = mapped_column(JSON, nullable=True)
    intercept: Mapped[float] = mapped_column(Float, default=0.0)
    coefs: Mapped[list | None] = mapped_column(JSON, nullable=True)
    means: Mapped[list | None] = mapped_column(JSON, nullable=True)
    stds: Mapped[list | None] = mapped_column(JSON, nullable=True)
    std_coef: Mapped[list | None] = mapped_column(JSON, nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)      # 存 camelCase：r2/adjR2/rmse/mae/passRate/passRate1/passRate15/q2/q2Time/lambda/drop
    impute_means: Mapped[list | None] = mapped_column(JSON, nullable=True)
    pls_A: Mapped[int | None] = mapped_column(Integer, nullable=True)       # PLS 分量数
    n: Mapped[int] = mapped_column(Integer, default=0)
    tolerance: Mapped[float] = mapped_column(Float, default=0.8)
    train_range: Mapped[str] = mapped_column(String(16), default="jun_jul")
    trained_at: Mapped[str] = mapped_column(String(19), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class CoarseModelHistory(Base):
    """粗灰模型重训练历史（detail 存完整快照：q2Time/rmse/mae/A/lambda/drop 等）"""
    __tablename__ = "coarse_model_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    model_id: Mapped[int | None] = mapped_column(ForeignKey("coarse_models.id"), nullable=True)
    train_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    trained_at: Mapped[str] = mapped_column(String(19), default="")
    n: Mapped[int] = mapped_column(Integer, default=0)
    tolerance: Mapped[float] = mapped_column(Float, default=0.8)
    train_range: Mapped[str] = mapped_column(String(16), default="jun_jul")
    production: Mapped[str] = mapped_column(String(8), default="pls")
    mlr_r2: Mapped[float | None] = mapped_column(Float, nullable=True)
    pls_r2: Mapped[float | None] = mapped_column(Float, nullable=True)
    mlr_q2: Mapped[float | None] = mapped_column(Float, nullable=True)
    pls_q2: Mapped[float | None] = mapped_column(Float, nullable=True)
    pass_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)        # 完整 history dict（含 q2Time/rmse/mae/A/lambda/drop）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class RegressionModel(Base):
    """单因素密度模型 (linear/poly2/poly3)"""
    __tablename__ = "regression_models"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    model_type: Mapped[str] = mapped_column(String(8), nullable=False)  # linear|poly2|poly3
    params: Mapped[list | None] = mapped_column(JSON, nullable=True)
    r_squared: Mapped[float] = mapped_column(Float, default=0.0)
    rmse: Mapped[float] = mapped_column(Float, default=0.0)
    data_points: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(19), default="")


class Setting(Base):
    """键值配置（对应 App.store 单值）"""
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | float | int | bool | None] = mapped_column(JSON, nullable=True)
    value_type: Mapped[str] = mapped_column(String(16), default="json")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class AutoState(Base):
    """录入层 + 手动有效期（通用键值，value=完整 JSON，无损存储前端 store 的对象键）"""
    __tablename__ = "auto_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | float | int | bool | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
