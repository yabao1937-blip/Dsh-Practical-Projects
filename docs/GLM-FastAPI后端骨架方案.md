# 重介密控系统 FastAPI 后端骨架方案

> 来源：GLM-5.2（通过 workflow 以 provider=zai / model=glm-5.2 派活产出）
> 版本：v0.1（方案文档，不含实现代码）
> 技术基线：FastAPI + SQLAlchemy 2.x + Alembic + Pydantic v2；算法 numpy + sklearn（LinearRegression / PLSRegression）；Excel openpyxl；SQLite(开发) → MySQL/PostgreSQL(生产)
> 兼容现状：现有 `backend/run.py`、`app/config.py`、`app/database.py` 保留复用，本方案在其上补全分层

---

## 一、后端目录结构

```text
backend/
├── run.py                      # 现有：uvicorn 启动入口（保留）
├── alembic.ini                 # Alembic 配置（DATABASE_URL 从环境变量读取）
├── requirements.txt            # fastapi / uvicorn / sqlalchemy>=2.0 / alembic / pydantic>=2 / numpy / scikit-learn / openpyxl / python-multipart
├── app/
│   ├── __init__.py
│   ├── main.py                 # 【新建】FastAPI 实例、CORS(file:// 与静态源)、路由挂载、/api/v1 前缀、异常处理器、启动时执行 settings 缺省值播种
│   ├── config.py               # 现有：BASE_DIR / DATABASE_URL / MODEL_DIR，扩展为 pydantic-settings（env: DSH_ENV, DB_URL, CLAMP_DENSITY_MIN/MAX 等）
│   ├── database.py             # 现有：engine / SessionLocal / Base / get_db（保留，生产切换连接池参数）
│   ├── models/                 # 【新建】SQLAlchemy ORM 模型（每表一文件或单文件 models.py，按表名组织）
│   │   ├── coal_record.py      # coal_records 主表（三表合并）
│   │   ├── setting.py          # settings 键值表
│   │   ├── auto_state.py       # auto_state 手动有效期表
│   │   ├── alert.py            # alerts 告警
│   │   ├── import_log.py       # import_logs 导入日志
│   │   ├── coarse_model.py     # coarse_models（当前模型+历史 coarseModelHistory）
│   │   └── heavy_sample.py     # heavy_samples 重介采样（如并入 coal_records 则此处仅放扩展字段表）
│   ├── schemas/                # 【新建】Pydantic v2 请求/响应模型（与 ORM 分离；ConfigDict(from_attributes=True)）
│   │   ├── coal.py             # 三表记录、分页、批量导入结果
│   │   ├── setting.py          # 单值/键值配置读写
│   │   ├── dashboard.py        # 总览页聚合出参（dashboard/calcState/densityGuide）
│   │   ├── modeling.py         # 训练入参(特征列表/时间范围/方法)、模型指标、预测出参
│   │   ├── brief.py            # 推测简报 16 列行结构
│   │   └── common.py           # Page<T>、通用响应包、错误码
│   ├── routers/                # 【新建】仅做参数校验 + 调 service + 组装响应，不写业务
│   │   ├── dashboard.py        # /overview/* 总览页聚合
│   │   ├── records.py          # /records/* 三表 CRUD + 手工补录
│   │   ├── settings.py         # /settings/* 配置读写
│   │   ├── inputs.py           # /inputs/* 录入层 + 手动有效期
│   │   ├── modeling.py         # /models/* 训练/预测/模型历史
│   │   ├── brief.py            # /brief/* 推测简报
│   │   ├── importer.py         # /import/* Excel 导入
│   │   └── migration.py        # /migrate/* localStorage 一次性迁移
│   ├── services/               # 【新建】纯业务逻辑（不 import fastapi，便于单测），由现有 JS 函数逐条迁入并保持口径
│   │   ├── ash.py              # calcTotalAsh / resolveTotalAsh / formulaTotalAsh（取值链：手动>公式>录入>默认加权）
│   │   ├── density.py          # computeDensityGuidance / expertAdjust / densityGainK（专家表 0.05/0.15/0.25、斜率0.1外推、钳制1.35~1.60、死区/钳制模式）
│   │   ├── modeling.py         # trainMlr / trainPls / predictCoarseAsh / _timeCvQ2 / polyRegression；递归软测量(基值=首次采样、反馈重置、无采样=上小时预测+模型增量)
│   │   ├── brief.py            # buildHourlyBrief（三表 1h 对齐、整点、16 列、过滤无效行）
│   │   ├── importer.py         # parseCoarseFactors / normalizeTs / parseRawData（openpyxl）
│   │   ├── alerts.py           # 告警判定与生成（超容差、数据缺失、模型漂移）
│   │   └── migrate.py          # dmcs_store JSON → 各表的完整映射（数组/单值/键值三段）
│   ├── jobs/                   # 【新建】后台任务（apscheduler 或 FastAPI BackgroundTasks 起步）
│   │   ├── hourly_brief.py     # 每小时整点生成简报快照落库
│   │   └── auto_tick.py        # 自动值打点：写 auto_state.auto_value + auto_at，供手动有效期判定
│   ├── migrations/             # Alembic 版本目录（versions/ 内含 0001_init、0002_seed_settings）
│   └── tests/                  # pytest：services 层口径单测（专家表、取值链、软测量递归）+ routers 层 API 测试（TestClient + 临时 SQLite）
└── data/                       # 现有：SQLite 文件、模型持久化目录
```

| 目录/文件 | 职责 | 关键约束 |
|---|---|---|
| main.py | 应用装配、CORS、异常→统一错误码 | 业务零逻辑 |
| routers/ | HTTP 编排 | 只调 services，禁止直接写 SQL |
| services/ | 全部业务口径（8 条核心口径的唯一实现处） | 不依赖 FastAPI，可独立单测 |
| jobs/ | 周期任务（简报打点、auto_state 打点） | 幂等，重复执行不产生重复行 |
| migrations/ | Alembic 建表/播种缺省配置 | 生产 MySQL/PG 走同一 versions |

---

## 二、首批数据表（ORM）

### 1. `coal_records` —— 三表合并主表（磁尾/粗精煤泥/浮精/补录日志统一入口）

对应 localStorage 的 `magneticTail`、`coarseCoal`、`floatCoal`、`calcLogs`（灰分密度+仪表补录部分），用 `category` ENUM 区分。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | BigInteger PK autoincrement | |
| category | Enum('magnetic_tail','coarse_coal','float_coal','calc_log') | 磁尾=精磁尾、coarse_coal=表1、float_coal=表2、calc_log=表3 补录 |
| ts | DateTime(带时区转UTC存) NOT NULL | 记录归属时间（整点对齐基准），源数据 `timestamp/时间` 归一化 |
| system | String(32) NOT NULL DEFAULT 'default' | 系统/线别（多系统扩展位；旧数据填 'default'） |
| source | Enum('import','manual','instrument') | 来源：Excel 导入 / 手工补录 / 仪表直采 |
| density | Numeric(6,4) NULL | 密度（表3） |
| ash | Numeric(6,4) NULL | 灰分（表3/浮精/磁尾灰分） |
| amount | Numeric(10,3) NULL | 煤量（皮带秤，t/h） |
| f315_ash | Numeric(6,4) NULL | 315 灰分（粗精煤泥人工采样，稀疏） |
| f501_ash / f502_ash | Numeric(6,4) NULL | 501/502 灰分仪读数 |
| f501_amount / f502_amount | Numeric(10,3) NULL | 501/502 对应皮带秤量（默认加权用） |
| features | JSON NULL | 表1 十特征原始键值（磁尾压力/给煤量/分流等，键名与前端一致），避免为特征逐一建列 |
| extra | JSON NULL | 其余未映射字段原样保留（迁移兜底，防丢数据） |
| import_log_id | BigInteger FK→import_logs.id NULL | 来自哪次导入 |
| created_at / updated_at | DateTime | 审计 |

**索引（硬性要求）**

| 索引 | 列 | 类型 | 目的 |
|---|---|---|---|
| uq_coal_cat_ts_sys | (category, ts, system) | UNIQUE | 从根上防重复导入（同表同时刻同系统只允许一行） |
| ix_coal_ts | (ts) | 普通 | 时间范围查询/简报 1h 对齐 |
| ix_coal_cat_ts | (category, ts) | 复合 | 按表取数 |
| ix_coal_import_log | (import_log_id) | 普通 | 按导入批次回滚/追溯 |

> 导入策略：`INSERT ... ON CONFLICT (category,ts,system) DO UPDATE`（PG/SQLite）或 MySQL `ON DUPLICATE KEY UPDATE`，并逐条记入 import_logs 的 inserted/updated 计数。

### 2. `settings` —— 单值配置（键值）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | Integer PK | |
| key | String(64) UNIQUE NOT NULL | 见下表键名 |
| value | JSON NOT NULL | 统一 JSON，布尔/数值/对象均可存 |
| value_type | Enum('number','boolean','string','json') | 前端类型还原 |
| updated_at | DateTime | |

预置键（与 App.store 单值一一对应）：

| key | 默认 value | 对应旧字段 |
|---|---|---|
| coarse_tolerance | 0.10 | coarseTolerance |
| coarse_train_range | {"days":30} | coarseTrainRange |
| ash_target | 10.50 | ashTarget |
| ash_target_tol | 0.10 | ashTargetTol |
| guide_scheme | "total_ash"（可切 "heavy_ash"） | guideScheme 总灰分版/重介版 |
| total_ash_manual_on | false | totalAshManualOn |
| density_guide | {"mode":"clamp","deadzone":0.02}（clamp/deadzone） | densityGuide |
| density_auto_on | false | densityAutoOn（仅控制建议计算，绝不写密度） |
| heavy_ash_static | 8.50 | 重介精煤灰分静态初始值（口径1：不实时反推） |
| heavy_ash_backcalc | false | heavyAshBackcalc |
| density_clamp | {"min":1.35,"max":1.60} | 密度钳制范围 |
| coarse_ema_alpha | 0.3 | coarseAshEma 平滑系数 |

### 3. `auto_state` —— 录入层 + 手动有效期（口径8）

替代 `amountInputs / ashInputs / instrumentInputs / heavyAshInput / coarseAshInput / floatAshInput / coarseCalc / coarseAshEma / autoState` 全部键值结构。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | Integer PK | |
| key | String(64) NOT NULL | 录入键名（与前端键一一对应，如 `amountInputs.heavy`、`ashInputs.float`、`heavyAshInput`、`coarseCalc`） |
| system | String(32) NOT NULL DEFAULT 'default' | 与 coal_records.system 对齐 |
| manual_value | JSON NULL | 手动值（手动时刻写入） |
| manual_at | DateTime NULL | 手动时刻 |
| auto_value | JSON NULL | 系统自动值（jobs/auto_tick 或计算时打点） |
| auto_at | DateTime NULL | 自动打点时间 |
| updated_at | DateTime | |

| 索引 | 列 | 类型 |
|---|---|---|
| uq_auto_key_sys | (key, system) | UNIQUE |

**生效规则（service 层统一实现）**：`manual_at >= auto_at` → 用 manual_value；否则 auto_value 接管。重介灰分 `heavyAshInput` 默认 manual_value=8.50。

### 4. `coarse_models` —— 粗灰模型（当前 + 历史）

合并 `coarseModel`（单值）与 `coarseModelHistory`（数组）。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | Integer PK | |
| method | Enum('mlr','pls') | 算法 |
| is_current | Boolean NOT NULL DEFAULT false | 同一 system 仅一行 true（部分唯一索引/应用层保证） |
| feature_names | JSON | 10 特征名有序列表 |
| coef / intercept | JSON | MLR 系数；PLS 存投影矩阵参数 |
| metrics | JSON | R²、_timeCvQ2、RMSE、样本数、训练窗口起止 |
| train_range | JSON | coarseTrainRange 快照 |
| trained_at | DateTime | |
| created_at | DateTime | |

索引：`(is_current)`、`(trained_at)`。

### 5. `import_logs` —— 导入日志

| 字段 | 类型 | 说明 |
|---|---|---|
| id | BigInteger PK | |
| filename | String(255) | |
| sheet_name | String(64) NULL | |
| category | Enum 同 coal_records | 导入到哪类 |
| total_rows / inserted / updated / skipped | Integer | 解析/新增/更新/校验失败计数 |
| errors | JSON | 逐行错误（行号+原因） |
| created_at | DateTime | |

### 6. `alerts` —— 告警

| 字段 | 类型 | 说明 |
|---|---|---|
| id | BigInteger PK | |
| level | Enum('info','warn','error') | |
| type | String(32) | 如 ash_dev_exceed / model_stale / data_missing |
| message | String(512) | |
| detail | JSON | 触发时上下文（ΔA、密度建议等） |
| ack | Boolean DEFAULT false | 确认位 |
| ts / created_at | DateTime | |

索引：`(ts)`、`(ack, level)`。

### 7. `briefs` —— 推测简报快照（jobs 产出）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | BigInteger PK | |
| hour_ts | DateTime NOT NULL | 归属整点 |
| system | String(32) NOT NULL | |
| rows | JSON | 16 列行数组（已过滤无「建议密度+预测粗灰」的行） |
| created_at | DateTime | |

索引：UNIQUE `(hour_ts, system)`（幂等重算覆盖）、`(hour_ts)`。

> `heavySamples`（重介采样）与 `manualEntries`（手工补录）如结构简单，直接落 `coal_records`（source='manual'，heavy 采样放 category='calc_log' 或新增枚举值 'heavy_sample'），extra JSON 兜底；`regressionModels`（单因素模型）并入 `coarse_models`（method 增 'single_factor'）或独立小表，二选一在迁移脚本定稿。

---

## 三、首批 REST API 清单（统一前缀 `/api/v1`）

按「页面级聚合」组织：一个页一次请求拿全量首屏数据，写操作细粒度。

### 3.1 总览页 /overview

| 方法 | 路径 | 用途 | 入参 | 出参 |
|---|---|---|---|---|
| GET | /overview/dashboard | 总览页一次取全 | query: system, hour(可选，默认最近) | settings 快照(ashTarget/ashTargetTol/guideScheme/densityGuide…)；三路煤量(amountInputs 各键生效值)；重介灰分(静态8.50/手动)、浮灰、粗灰(软测量当前值+EMA)；实际总灰分及取值链标注(source: manual/formula/input/default_weighted)；ΔA、达标状态；建议密度(含专家档位、钳制后值、是否变化)；重介版时附目标重介灰分=(A目标×总量−浮−粗)/重介量；未确认告警列表 |
| GET | /overview/density-guide | 单独刷新密度建议 | query: system, total_ash(可选，缺省用最新计算值) | delta_a、档位(deadzone/0.01/0.02/0.1外推)、raw_suggest、clamped_suggest、clamp_range、mode(clamp/deadzone)、updated_at |
| POST | /overview/alerts/{id}/ack | 确认告警 | path: id | {ok:true} |

### 3.2 数据采集与录入 /records、/inputs

| 方法 | 路径 | 用途 | 入参 | 出参 |
|---|---|---|---|---|
| GET | /records | 三表分页查询 | query: category(多选), system, ts_from, ts_to, source, page, page_size | Page<CoalRecordDTO>（features/extra 原样回传） |
| POST | /records | 手工补录/新增单条 | body: {category, ts, system?, density?, ash?, amount?, f315_ash?, f501_ash?, f502_ash?, features?} | 记录 DTO；撞唯一索引返回 409 或 update=true（body 加 upsert 选项） |
| PUT | /records/{id} | 修正单条 | body 同上部分字段 | 记录 DTO |
| DELETE | /records/{id} | 删除单条 | path: id | {ok:true} |
| GET | /inputs | 录入层全量（含生效状态） | query: system | 每键 {key, effective_value, source: manual/auto, manual_at, auto_at, manual_value, auto_value}；覆盖 amountInputs/ashInputs/instrumentInputs/heavyAshInput/coarseAshInput/floatAshInput/coarseCalc/coarseAshEma |
| PUT | /inputs/{key} | 写手动值 | body: {value(可 null=清除手动), manual_at?} | 更新后的键状态（自动按 manual_at≥auto_at 规则即时接管） |
| POST | /inputs/tick | 触发一次自动打点（调试/补偿） | body: {system} | 打点时间戳 |

### 3.3 配置 /settings

| 方法 | 路径 | 用途 | 入参 | 出参 |
|---|---|---|---|---|
| GET | /settings | 全量配置 | — | {key: value} 扁平对象 + updated_at |
| PUT | /settings/{key} | 改单键 | body: {value}（按 value_type 校验：ash_target 数值域、guide_scheme 枚举、density_clamp 1.30≤min<max≤1.70） | 更新后键值 |
| POST | /settings/reset/{key} | 恢复默认 | path: key | 默认值 |

### 3.4 模型（粗灰 MLR/PLS） /models

| 方法 | 路径 | 用途 | 入参 | 出参 |
|---|---|---|---|---|
| GET | /models/coarse/current | 当前模型 | — | {method, feature_names, coef, intercept, metrics{r2,time_cv_q2,rmse,n}, train_range, trained_at, is_current} |
| POST | /models/coarse/train | 训练（MLR/PLS） | body: {method: 'mlr'\|'pls', feature_names?(默认10特征), ts_from, ts_to, min_samples?} | 训练结果（同上）；样本不足/特征全零返回 422+原因；成功后置 is_current 并写历史 |
| GET | /models/coarse/history | 重训练历史 | query: page, page_size | Page<模型摘要>（method/metrics/trained_at） |
| POST | /models/coarse/{id}/activate | 启用历史版本 | path: id | 当前模型 DTO |
| GET | /models/coarse/predict | 软测量当前预测 | query: system, at? | {pred_ash, base(基值来源: first_sample/last_hour), increment, reset_by_sample: bool, ema, ts}；无当前模型返回可预期错误码 model_not_trained |
| POST | /models/coarse/sample | 提交 315 人工采样（触发基值重置） | body: {ts, f315_ash, remark?} | 写入 coal_records(category=coarse_coal, f315_ash) 并重置软测量基值，返回新预测 |

### 3.5 推测简报 /brief

| 方法 | 路径 | 用途 | 入参 | 出参 |
|---|---|---|---|---|
| GET | /brief/hourly | 三表 1h 对齐简报（16 列） | query: date(YYYY-MM-DD)或 ts_from/ts_to, system | 行数组：时间、重介量/灰分、浮量/灰分、粗量/灰分、315 灰分、预测粗灰、总灰分、ΔA、建议密度、档位、钳制标记、达标、数据完整度、备注等 16 列；**已过滤**给不出「建议密度+预测粗灰」的行；每行附 missing 字段列表 |
| POST | /brief/hourly/refresh | 强制重算指定时段 | body: {ts_from, ts_to, system} | 重算并落 briefs 快照，返回行数 |

### 3.6 导入 /import

| 方法 | 路径 | 用途 | 入参 | 出参 |
|---|---|---|---|---|
| POST | /import/xlsx | 上传 Excel 解析入库 | multipart: file, category, system?, upsert=true | {import_log_id, total, inserted, updated, skipped, errors[]}；命中 (category,ts,system) 唯一键按 upsert 处理 |
| GET | /import/logs | 导入日志 | query: page, page_size | Page<ImportLogDTO> |
| POST | /import/logs/{id}/rollback | 按批次回滚 | path: id | 删除/标记该批 import_log_id 下记录数 |

### 3.7 迁移 /migrate

| 方法 | 路径 | 用途 | 入参 | 出参 |
|---|---|---|---|---|
| POST | /migrate/localstorage | 一次性导入 dmcs_store JSON | body: 整个 localStorage store 原文（前端 App.store 序列化） | {mapped: {coal_records: n, settings: n, auto_state: n, coarse_models: n, …}, unmapped_keys: []}；数组/单值/键值三段按第二节映射表落库，未识别键列入 unmangled_keys 不丢弃 |
| GET | /migrate/preview | 预检（dry-run 不落库） | body 同上 | 映射统计 + 冲突明细（撞唯一键行数） |

### 3.8 系统

| 方法 | 路径 | 用途 | 出参 |
|---|---|---|---|
| GET | /health | 存活/DB 连通 | {status, db: ok, version} |

---

### 附：口径对齐落点速查

| 核心口径 | 实现落点 |
|---|---|
| 1 重介灰分静态 8.50 | settings.heavy_ash_static + auto_state(heavyAshInput 默认 manual) |
| 2 总灰分取值链 | services/ash.py resolveTotalAsh，dashboard 出参带 source 标注 |
| 3 专家表+钳制 | services/density.py，settings.density_clamp |
| 4 双版本切换 | settings.guide_scheme，重介版目标灰分公式在 density.py |
| 5 密度纯手动 | 仅 GET 建议；无任何写密度接口 |
| 6 递归软测量 | services/modeling.py + /models/coarse/predict |
| 7 简报 16 列过滤 | services/brief.py + jobs/hourly_brief |
| 8 手动有效期 | auto_state 表 + manual_at≥auto_at 规则 + jobs/auto_tick |
