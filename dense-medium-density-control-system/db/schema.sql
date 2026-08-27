-- =====================================================================
-- 重介密控系统 - 真实数据表结构（SQLite）
-- 说明：宽表 + 指标字典元数据；列名与 targets.yaml 的 features/targets 键一致
-- 详细设计见 docs/data_schema.md
-- =====================================================================

-- 1) 指标字典：每个指标的定义、来源、周期、范围、滞后补偿（新增指标只需在此登记）
CREATE TABLE IF NOT EXISTS indicator_dict (
    code                TEXT PRIMARY KEY,   -- 指标编码（与实时/化验表列名一致）
    name_cn             TEXT NOT NULL,      -- 中文名
    category            TEXT NOT NULL,      -- feature | target | derived
    data_source         TEXT NOT NULL,      -- plc | online_gauge | lab | lims | derived
    unit                TEXT DEFAULT '',    -- % | t/h | cP | g/cm3 ...
    valid_min           REAL,               -- 合理范围下限
    valid_max           REAL,               -- 合理范围上限
    sample_period_seconds INTEGER,          -- 采集周期（秒）
    lag_seconds         INTEGER DEFAULT 0,  -- 采样滞后补偿（负=实际时刻早于记录时刻）
    belt                TEXT,               -- 501 | 502 | NULL(通用)
    system_tag          TEXT,               -- 401 | 402 | A | B | NULL(通用)
    used_by_targets     TEXT DEFAULT '',    -- 被哪些软测量目标使用（逗号分隔）
    enabled             INTEGER DEFAULT 1,
    remark              TEXT DEFAULT ''
);

-- 2) 高频实时表：PLC/在线仪表采集的可测工艺量（每 sample_period 一行）
CREATE TABLE IF NOT EXISTS realtime_signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,              -- 'YYYY-MM-DD HH:MM:SS'
    raw_ash     REAL,                       -- 原煤灰分(%)
    coal_amount REAL,                       -- 小时带煤量(t/h)
    sysA        INTEGER,                    -- A系统运行(0/1)
    sysB        INTEGER,                    -- B系统运行(0/1)
    sys401      INTEGER,                    -- 401系统运行(0/1)
    sys402      INTEGER,                    -- 402系统运行(0/1)
    desliming473 INTEGER,                   -- 473脱粉筛运行(0/1)
    desliming474 INTEGER,                   -- 474脱粉筛运行(0/1)
    is_stoppage INTEGER,                    -- 停机/低负荷(0/1)
    level       REAL,                       -- 精磁尾液位(%)
    quality     INTEGER DEFAULT 0,          -- 0正常 1可疑 2坏点
    source      TEXT DEFAULT 'plc',
    UNIQUE(timestamp)
);
CREATE INDEX IF NOT EXISTS idx_rt_ts ON realtime_signals(timestamp);

-- 3) 低频化验表：人工化验/LIMS 的目标真值（每 lab_period 一行）
CREATE TABLE IF NOT EXISTS lab_assays (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,              -- 采样时刻（非出结果时刻）
    clean_ash   REAL,                       -- 精煤真实灰分(%)
    medium_viscosity REAL,                  -- 介质粘度(cP)
    clean_moisture REAL,                    -- 精煤水分(%)
    suspension_density REAL,                -- 悬液密度(g/cm3)
    recovery    REAL,                       -- 回收率/产率(%)
    lab_time    TEXT,                       -- 出结果时刻（用于滞后统计）
    assay_method TEXT DEFAULT '',           -- 国标/快速法
    operator    TEXT DEFAULT '',
    quality     INTEGER DEFAULT 0,          -- 0正常 1可疑 2坏点
    source      TEXT DEFAULT 'lab',
    UNIQUE(timestamp)
);
CREATE INDEX IF NOT EXISTS idx_lab_ts ON lab_assays(timestamp);

-- 4) 生产班次/皮带-系统组合（可选：用于分组统计与工况标注）
CREATE TABLE IF NOT EXISTS production_batch (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no    TEXT,                       -- 班次号
    belt        TEXT,                       -- 501 | 502
    system_tag  TEXT,                       -- 401/402/A/B 组合
    start_time  TEXT,
    end_time    TEXT,
    remark      TEXT DEFAULT ''
);

-- 5) 模型版本登记（可选：上线管理，供后续控制模型/大模型选版）
CREATE TABLE IF NOT EXISTS model_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target      TEXT NOT NULL,              -- 目标指标键
    model_type  TEXT NOT NULL,              -- xgboost | lightgbm | ensemble
    artifact_path TEXT,                     -- artifacts/<target>/...
    metrics_json TEXT,                      -- 测试集指标
    trained_on  TEXT,                       -- 训练数据截止时刻
    created_at  TEXT
);
