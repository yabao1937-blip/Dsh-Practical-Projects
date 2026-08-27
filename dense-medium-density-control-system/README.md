# 重介密控系统 — 预测模型层（第一步：XGBoost + LightGBM 软测量框架）

选煤厂重介密控系统的**预测模型层**第一子模块：用实时可测的工艺量，软测量推断
当前无法在线测量的指标（软传感器），为后续异常检测、控制（PID/MPC）、大模型
决策提供输入。

## 整体规划（本项目演进路径）

| 模块 | 技术方案 | 状态 |
|---|---|---|
| 预测模型 | XGB + LGBM + TCN | **第一步 XGB+LGBM 软测量 ✅ + 第二步 TCN 时序预测 ✅（待 torch 验证）** |
| 异常模型 | 工艺规则 + Isolation Forest + TCN 预测残差 | 待做 |
| 控制模型 | PID 基础 + MPC 高级 | 待做 |
| 大模型 | 本地模型 + RAG + 工具调用 + 结构化输出 | 待做 |
| 执行 | PLC | 待做 |

## 第一步定位

- **任务**：软测量（推断当前真实值），非未来预报（前瞻预测由后续 TCN 负责）
- **目标指标**（`config/targets.yaml`）：精煤真实灰分、介质粘度、精煤水分、
  悬液密度、回收率 —— 指标字典驱动，每指标独立建模
- **模型**：XGBoost + LightGBM 单模型 + 集成（best | weighted）
- **评估**：按时间切分（严禁随机切分）+ walk-forward 滚动验证
- **交付**：CLI（训练/评估/预测）+ 可 import 的预测库（`dmdcs.serving.SoftSensorPredictor`）

## 目录结构

```
├── config/
│   ├── settings.yaml        # 数据源路径、采样/化验/窗口占位值、切分、模型超参
│   └── targets.yaml         # 指标字典：特征清单 + 软测量目标指标
├── src/dmdcs/
│   ├── config.py            # 配置加载（指标字典）
│   ├── data/                # demo 生成 / 加载 / 窗口特征 / 化验对齐
│   ├── models/              # base / xgb / lgbm / ensemble
│   ├── training/            # 指标 / 时序切分 / 训练管线 / 评估
│   ├── serving/             # SoftSensorPredictor（单条/批量）
│   └── utils/               # 模型持久化 / JSON / 日志
├── scripts/
│   ├── gen_demo.py          # 生成合成 demo 数据
│   ├── train.py             # 构建对齐集 + 训练全部/指定目标
│   ├── evaluate.py          # 加载模型评估测试集指标
│   └── predict.py           # 批量/单条预测
├── data/raw/                # realtime.csv（高频实时）+ lab.csv（低频化验）
├── data/processed/          # aligned.csv（窗口聚合特征宽表）
├── artifacts/<target>/      # 模型产物 + meta.json + report.json
└── requirements.txt
```

## 快速开始

```bash
# 1. 安装依赖（Python 3.11，CPU 版 xgboost/lightgbm）
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 生成合成 demo 数据（真实数据表就绪前跑通管线）
.venv\Scripts\python.exe scripts\gen_demo.py

# 3. 构建对齐数据集并训练全部 5 个指标（--rebuild 强制重建）
.venv\Scripts\python.exe scripts\train.py --rebuild
#    或只训练指定指标：--target clean_ash

# 4. 评估已训练模型
.venv\Scripts\python.exe scripts\evaluate.py

# 5. 批量预测
.venv\Scripts\python.exe scripts\predict.py --target clean_ash \
    --input data/processed/aligned.csv --output data/processed/pred_result.csv
```

## 数据管线说明（真实表就绪后如何接入）

1. **实时表** `data/raw/realtime.csv` 或 **SQLite** `data/db/dmdcs.db`（表
   `realtime_signals`）：`timestamp` + 特征列（见 targets.yaml 的 features 清单）
   —— 来自 PLC/数据库/在线仪表导出。
2. **化验表** `data/raw/lab.csv` 或 **SQLite** `data/db/dmdcs.db`（表
   `lab_assays`）：`timestamp` + 目标列 —— 来自人工化验/LIMS。
3. **建库**：`python scripts/init_db.py`（生成数据库 + 指标字典，真实表结构见
   `db/schema.sql`，设计说明见 `docs/data_schema.md`）。
4. **对齐**：以化验时刻 t 为锚点，取 `(t - feature_window_minutes, t]`（默认
   60 分钟）窗口内的实时数据，聚合为 `末值(__last)/均值(__mean)/斜率(__slope)`，
   与化验真值组成训练样本（`data/processed/aligned.csv`）。
5. 训练脚本**自动检测数据库**：`data/db/dmdcs.db` 存在则从数据库读取
   （`--use-db` 强制，`--csv` 回退），否则用 CSV。
6. 采样/化验/窗口均为 **占位值**，真实工艺确定后只改 `settings.yaml`。

## 预测结果消费方式

```python
from dmdcs.config import Config
from dmdcs.serving.predictor import SoftSensorPredictor

cfg = Config()
p = SoftSensorPredictor("clean_ash", cfg)          # 加载 artifacts
res = p.predict_single({"raw_ash__last": 21.5, ...})  # 单条
df = p.predict(features_df)                        # 批量
```

输出含 `pred_xgb` / `pred_lgbm` / `prediction`（集成），供异常模型、控制模型、
大模型工具调用消费。

## TCN 时序预测（第二步）

级联+混合架构：软测量当前估计 + 最近化验真值作为静态锚点，TCN 预测**下一次化验值**（horizon=2h）。设计见 `docs/tcn-design.md`。

```bash
.venv\Scripts\python.exe -m pip install torch==2.5.1 -i https://pypi.tuna.tsinghua.edu.cn/simple   # 需先装 torch

.venv\Scripts\python.exe scripts\train_tcn.py                                                        # 训练 tcn.enabled_targets
.venv\Scripts\python.exe scripts\predict_tcn.py --target clean_ash --realtime data\raw\realtime.csv --last-lab 10.85
```

产物：`artifacts/tcn/<target>/`（tcn_state.pt + scalers + config + report.json）。

## 下一步

- 真实数据表/PLC 点位确定后：更新 `config/targets.yaml`（特征、目标、范围）与
  `config/settings.yaml`（周期、窗口），替换 `data/raw` 下的 CSV
- 异常模型：对 TCN 预测残差做 Isolation Forest + 工艺规则校验
