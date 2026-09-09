# 重介密控系统 后端（FastAPI 前后端分离）

选煤厂重介密度控制 + 精煤灰分监控平台的后端。数据与算法下沉到后端，前端由本服务静态托管（同源）。

## 目录结构

```
backend/
├── run.py                    # 启动入口：python run.py
├── alembic.ini               # 迁移配置
├── requirements.txt
├── app/
│   ├── main.py               # FastAPI 装配、CORS、路由、静态托管前端
│   ├── config.py             # DATABASE_URL / FRONTEND_DIR / 模型路径
│   ├── database.py           # SQLAlchemy engine/SessionLocal/Base/get_db
│   ├── models.py             # 11 张表（coal_records 统一主表 + (category,ts,system) 唯一索引）
│   ├── schemas.py            # Pydantic 请求/响应模型
│   ├── routers/              # health/state/migration/settings/inputs/records/samples/manual_entries/overview/import_api
│   └── services/
│       ├── density.py        # 总灰分公式 + 专家表建议密度（逐值对齐前端）
│       ├── modeling.py       # 粗灰多因素模型预测（MLR/PLS）
│       ├── brief.py          # 推测简报（1h 对齐 + 递归软测量，16 列）
│       ├── resolvers.py      # 取值链（手动>公式>录入>默认）
│       ├── migrate.py        # localStorage → DB 迁移（preview/apply/replace）
│       ├── importer.py       # 三表 Excel 解析（M.DD 日期补偿）
│       └── state.py          # DB → 前端 store 桥接
├── migrations/               # Alembic 迁移版本
├── tests/                    # pytest（算法口径 + golden 对拍 + API）
├── scripts/                  # 冒烟测试 / headless 验证 / golden 导出
└── data/                     # SQLite + golden 对拍数据
```

## 启动

```powershell
cd backend
.\.venv\Scripts\python.exe -m alembic upgrade head   # 建表（首次）
.\.venv\Scripts\python.exe run.py                     # 启动 http://127.0.0.1:8000
```

浏览器访问 `http://127.0.0.1:8000/` 即为前端（本服务静态托管 `FRONTEND_DIR`，见 `config.py`）。

## 测试

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q
```

覆盖：专家表建议密度、总灰分公式、递归软测量简报（与前端 1069 行 golden 逐值对拍）、取值链、导入解析（M.DD 补偿）、迁移、端到端 dashboard。

## 数据迁移（localStorage → SQLite）

1. 前端导出 localStorage（备份 CSV）或在 http 访问时 `saveStore` 自动镜像。
2. `POST /api/v1/migrate/localstorage`（body 为前端 `App.store` JSON）或 `PUT /api/v1/state` 整库快照。
3. `GET /api/v1/migrate/preview` 可先 dry-run 查看各表条数与未识别键。

## 关键口径（迁移/维护时不得偏离）

- 总灰分 = (重介灰分×重介量 + 浮灰×浮量 + 粗灰×粗量) / 总煤量
- 专家表：|ΔA|≤0.05 不调；0.15→0.01；0.25→0.02；>0.25 斜率 0.1 外推；密度钳制 1.35~1.60
- 重介灰分 = 静态值（默认 8.50），不实时反推
- 粗灰 = 315灰分（人工采样稀疏），MLR/PLS 预测 + 递归软测量
- 密度计纯手动录入，系统只建议不执行
