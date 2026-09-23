# 重介密控系统（Dense Medium Density Control System）

选煤厂重介分选密度控制与精煤灰分监控系统。总精煤 = **重介精煤** + **浮精** + **粗精煤泥**。

已从「纯前端（localStorage）」重构为**前后端分离**：后端 FastAPI + SQLAlchemy，前端原生 JS（无构建），双轨过渡。

## 目录结构

```
dense-medium-density-control-system/
├── backend/                    # Python FastAPI 后端
│   ├── requirements.txt        # fastapi/uvicorn/SQLAlchemy/alembic/pydantic/numpy/scikit-learn/openpyxl
│   ├── run.py                  # 启动入口（uvicorn :8000）
│   ├── alembic.ini             # Alembic 迁移配置
│   ├── migrations/             # 迁移（0001 init + 0002 训练列）
│   ├── data/                   # seed_store.json + dense_medium.db(SQLite)
│   ├── scripts/                # golden dump(4) + 前端回归 verify(5) + Excel 生成器(generate_*.py)
│   ├── tests/                  # pytest（用例数量以收集为准）
│   └── app/
│       ├── main.py             # FastAPI 应用（建表 + 路由 + 前端静态托管）
│       ├── config.py           # DATABASE_URL、FRONTEND_DIR（相对定位）
│       ├── database.py         # SQLAlchemy 引擎/会话
│       ├── models.py           # 11 张表 ORM
│       ├── schemas.py          # Pydantic 请求/响应模型
│       ├── routers/            # health/state/settings/inputs/records/samples/
│       │                       # manual_entries/overview/migration/import_api/training
│       └── services/           # density(密度建议)/brief(简报)/resolvers(取值链)/
│                               # modeling(灰分预测)/training(训练精确移植)/
│                               # training_sklearn(sklearn 训练)/migrate(迁移)/importer/state
├── frontend/                   # 原生 JS 前端（后端托管，无构建）
│   ├── index.html              # 单页入口
│   └── js/
│       ├── app.js              # 全局核心：store/saveStore/取值链/密度建议/MLR+PLS 训练
│       ├── api.js              # 数据层（GET/PUT /state、远程训练）
│       ├── seed_data.js        # 种子数据（113 条粗精煤泥 + 出厂模型）
│       ├── coarse.js           # 粗精煤泥灰分页（重训练/散点图）
│       ├── overview.js         # 总览仪表盘
│       ├── import.js           # Excel 导入
│       ├── float.js / collect.js / history.js
│       └── chart-lite.js / xlsx-lite.js
├── docs/                       # Excel 文档（按板块）
└── README.md
```

## 快速启动

### 后端（Python 3.11，端口 8000）

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py     # 或 python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

后端同时**托管前端**（`FRONTEND_DIR` 指向仓库根的 `frontend/`），访问 **http://127.0.0.1:8000/** 即打开系统；接口文档 http://127.0.0.1:8000/docs 。

### 测试

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q          # 临时库隔离，不触真实数据
node scripts\verify_api_errors.js                # 无网络：同步失败分类回归
node scripts\dump_train_js.js                     # 重生成训练 oracle（需 Edge）
node scripts\import_xlsx_via_ui.js [目录]         # 通过真实 UI 路径批量导入三表 xlsx（需后端+Edge）
node scripts\verify_pull_from_server.js           # 双向同步/防回退端到端验证
```

## 架构：前后端分离（双轨过渡）

| 维度 | file:// 打开 | http:// 打开（后端托管） |
|---|---|---|
| 数据存储 | localStorage | localStorage + 后端 SQLite 镜像（PUT /state） |
| 模型训练 | 本地 JS | 后端同算法（POST /training/coarse-model） |
| 后端不可用 | 无影响 | 保留待同步数据；训练明确提示失败 |

前端保持原生 JS；`js/api.js` 做双轨：file:// 仅 localStorage，http:// 额外把整库镜像到后端。

**双向同步与防回退（2026-09）**：
- 旧浏览器打开页面时若服务器记录数更多 → `autoPullIfStale` 自动反向同步；顶栏「从服务器恢复数据」为手动入口；
- `PUT /state` 带防回退守卫：入库记录数少于现库 → 拒绝（旧 localStorage 镜像不能洗掉服务器侧导入/训练的新数据），`?force=true` 供清空/恢复备份等有意回退；
- 启动期默认值补齐/种子灌入仅写本地，不镜像。

## 数据模型（11 张表）

统一主表 **`coal_records`**（`category` 区分 `coarse|float|ash_density` 三表，`UNIQUE(category, ts, system)` 防重复导入）；时间戳 `ts` 存字符串 `YYYY-MM-DD HH:MM:SS`（字符串序==时间序，规避时区）。

| 表 | 用途 |
|---|---|
| coal_records | 三表合并主表 |
| calc_logs | 补录日志（灰分仪/密度计/皮带秤） |
| manual_entries / heavy_samples / import_logs / alerts | 补录 / 采样 / 导入日志 / 告警 |
| coarse_models | 当前粗灰模型（mlr+pls 两行，train_run_id 关联） |
| coarse_model_history | 重训练历史（detail JSON 全量快照） |
| regression_models | 单因素密度模型（linear/poly2/poly3） |
| settings / auto_state | 键值配置（8 单值）/ 录入层对象键（10 个） |

详见 `docs/前后端分离-差异清单.xlsx`。

## 算法（前后端逐值一致）

- **密度建议**：`calc_total_ash` + `expert_adjust` + `compute_density_guidance`（总灰分/重介灰分两版）。
- **粗精煤泥灰分预测**：10 特征 MLR/PLS（`predict_coarse_ash`）。
- **推测简报**：`build_hourly_brief`（16 列、1h 对齐、递归软测量）。
- **粗灰模型 DS / GPT 切换**：分析页按钮同时切换采样模型、日级模型及粗灰预测取值链。DS 保留原版 MLR/PLS；GPT 为优化版（不是调用外部 AI 接口）。两套模型保存在 `coarseModelVariants`，重训只覆盖所选版本；新安装默认 DS，已有存档按实际模型识别版本。
  首次切换或范围、容差不匹配时训练对应版本；已有匹配模型直接复用。新增数据后应重训需比较的各版本。
  两版日级训练均遵循所选范围，修复原版日级忽略训练范围的问题。DS 日开关取多数状态，Q² 保留留一验证；GPT 日开关取采样开启比例，Q² 为时间调参得分，两者不可直接比较高低。
- **GPT 粗灰模型训练**：`coarse_training.py` / `App._trainCoarseRows` 逐值对拍。
  2026-09-22起，GPT参数选择同时参考向前时间验证和留整日验证，外层时间检验仍独立执行，页面Q²仍保留原时间口径。DS保持不变；结果与已发现的退步见[GPT第一阶段优化](docs/粗精煤泥GPT优化-20260922.md)。
  当前第二轮仅改GPT：增加训练范围外的保守估计，日级比较普通/Huber稳健回归并优先按MAE选参；提供同窗口DS对照，详见[GPT独立优化与预测检验](docs/粗精煤泥GPT独立优化-20260922.md)。
  按完整日期向前验证；内层选择 MLR 正则化强度、PLS 成分数和专家候选，外层评估误差并对照历史均值。
  按采样建模；日级按采样点均值聚合，开关保留开启比例。历史拟合与时间验证分别展示。
  `training.py` / `App._trainCoarseDs` 为 DS 原版；`training_sklearn.py` 保留通用求解器验证。
  数据审计、后期检验和限制见 [粗灰模型优化说明](docs/粗精煤泥灰分模型优化-20260921.md)。

## API 一览（前缀 /api/v1）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /health | 健康检查 |
| GET/PUT | /state | 读/写整库快照（DB↔store） |
| GET/PUT | /settings | 配置读写 |
| GET/PUT | /inputs | 录入层 |
| GET/POST/DELETE | /records | 三表数据 CRUD（upsert 唯一键防重） |
| POST | /samples/heavy-ash | 重介灰分采样 |
| GET/POST | /manual-entries | 手工补录 |
| GET | /overview/dashboard | 总览（取值链+密度建议） |
| POST | /migrate/preview · /migrate/localstorage | localStorage 迁移 |
| POST | /import | Excel 导入 |
| POST | /training/coarse-model?range=jun_jul\|30d\|all&engine=ds\|gpt | 所选版本 MLR/PLS 训练 + 独立保存 + 回填（默认 ds）；支持 X-DMCS-Revision，返回新 revision |

## 文档（docs/）

按板块的 Excel：`前后端分离-差异清单.xlsx`、`MLR-PLS训练后移-难点与建议.xlsx`、`前后端分离-难点与建议.xlsx` 等。

2026-09-21 优化：测量灰分不再叠加密度仿真，总览与助手共用服务端决策入口，动作保护和重介来源开关持久化，补齐删除鉴权与权限错误分类。测试包含 11 个真实前端函数与后端的逐值对拍场景。

本轮改动及后续 12 项任务见 `docs/项目优化与后续计划-20260921.xlsx`，通过 `backend/scripts/generate_optimization_plan_xlsx.py` 复现。整库同步仍处于过渡阶段，版本冲突检测与增量同步列为下一批 P0 工作。
