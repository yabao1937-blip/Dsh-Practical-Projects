# -*- coding: utf-8 -*-
"""生成 Excel 文档：全项目分析——优化建议清单。

产出 docs/项目优化建议-全项目分析.xlsx，四个 Sheet：
总览与结论 / 现状评估（做得好的） / 优化建议明细（P0-P2） / 建议落地顺序

分析范围：全仓库（backend + frontend + docs + scripts），
基于 2026-09-11 HEAD(41d2ea3) 的静态审查 + pytest 全量运行（76 passed）。
非推测结论均注明文件与行号出处。
"""
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

DOCS = Path(__file__).resolve().parent.parent.parent / "docs"
OUT = DOCS / "项目优化建议-全项目分析.xlsx"

HEAD_FILL = PatternFill("solid", fgColor="2F5597")
HEAD_FONT = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
TITLE_FONT = Font(name="微软雅黑", size=13, bold=True, color="2F5597")
BODY_FONT = Font(name="微软雅黑", size=10)
BOLD_FONT = Font(name="微软雅黑", size=10, bold=True)
HIGH_FILL = PatternFill("solid", fgColor="F8CBAD")     # P0 必须做
MID_FILL = PatternFill("solid", fgColor="FFE699")      # P1 应该做
LOW_FILL = PatternFill("solid", fgColor="E2EFDA")      # P2 可以做
OK_FILL = PatternFill("solid", fgColor="DDEBF7")       # 保留项
THIN = Side(style="thin", color="B0B0B0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")
CENTER = Alignment(horizontal="center", vertical="top", wrap_text=True)


def sheet(wb, name, title, headers, widths, rows, fill_by_col0=None):
    ws = wb.create_sheet(name)
    ws["A1"] = title
    ws["A1"].font = TITLE_FONT
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    for c, (h, w) in enumerate(zip(headers, widths), start=1):
        cell = ws.cell(row=3, column=c, value=h)
        cell.fill = HEAD_FILL
        cell.font = HEAD_FONT
        cell.alignment = CENTER
        cell.border = BORDER
        ws.column_dimensions[cell.column_letter].width = w
    for i, row in enumerate(rows, start=4):
        for c, v in enumerate(row, start=1):
            cell = ws.cell(row=i, column=c, value=v)
            cell.font = BODY_FONT
            cell.alignment = WRAP if c != 1 else CENTER
            cell.border = BORDER
        if fill_by_col0:
            f = fill_by_col0.get(str(row[0]))
            if f:
                for c in range(1, len(headers) + 1):
                    ws.cell(row=i, column=c).fill = f
    ws.freeze_panes = "A4"
    return ws


wb = Workbook()
wb.remove(wb.active)

# ---------------------------------------------------------------- 1 总览
sheet(
    wb, "总览与结论",
    "全项目分析：总览与结论（分析基线 HEAD=41d2ea3，2026-09-11）",
    ["项目", "内容"],
    [22, 130],
    [
        ["分析范围", "backend（29 个 py 文件约 2567 行）+ frontend（原生 JS 无构建）+ docs（41 xlsx）+ scripts（58 个文件）；"
                  "含 pytest 全量运行与 git 近 10 次提交巡视"],
        ["总体结论", "架构清晰（routers/services 分层、无循环依赖）、测试意识强（76 pytest 全过 + golden 对拍 + headless 回归）、文档齐全，"
                   "整体质量高于同类内部工具。核心风险不在功能而在三点：① 整库镜像同步机制随数据增长线性恶化；"
                   "② 同一算法最多三处实现导致改动成本翻倍；③ 回归防线（76 测试 + 8 回归脚本）完全不在流水线里，全靠手跑。"],
        ["P0（必须做）", "4 项：PUT /state 整库镜像改增量写入 / 训练算法三重实现收敛退役 / CORS allow_origins=[*] 直接删除 / "
                     "create_all() 与 Alembic 建表双轨收口"],
        ["P1（应该做）", "4 项：加 CI 流水线 / 手动 ?v=N 缓存版本号改自动 / app.js 2589 行上帝对象拆模块 / seed_data.js 7343 行数据外置"],
        ["P2（可以做）", "4 项：SQLite 开 WAL 与备份策略 / 依赖锁版本 / scripts 目录归档清理 / 文档口径刷新（README 测试数等）"],
        ["实测验证", "pytest -q → 76 passed, 1 warning in 35.51s（2026-09-11，临时库隔离）；"
                  "index.html 中 app.js 已至 ?v=75、overview.js ?v=40；前端 grep saveStore 调用 60+ 处均触发整库 PUT"],
        ["建议落地顺序", "P1-CI（半天，先让防线自动化）→ P0-CORS/P0-建表双轨（各 10 分钟）→ P0-增量写入（收益最大）→ P0-退役双实现 → 其余按优先级"],
    ],
    fill_by_col0={"P0（必须做）": HIGH_FILL, "P1（应该做）": MID_FILL, "P2（可以做）": LOW_FILL},
)

# ---------------------------------------------------------------- 2 做得好的
sheet(
    wb, "现状评估（做得好的）",
    "审查中确认值得保留的设计与做法（不动的部分）",
    ["方面", "证据", "评价"],
    [24, 62, 56],
    [
        ["后端分层干净", "routers(11) → services(10) 依赖为有向无环：brief→density/modeling/resolvers，training→modeling；无循环依赖",
         "职责边界清晰，改动定位容易，应保持"],
        ["测试隔离设计", "conftest.py 用 DMCS_DATABASE_URL 环境变量把 pytest 隔离到临时 SQLite，绝不触碰真实库",
         "测试可放心全量运行，是全仓库最有价值的工程决策之一"],
        ["前后端一致性兜底", "golden dump(4) + verify(5) headless 回归 + training oracle 逐值对拍（差 ~1e-13）",
         "在「双实现」这一既有约束下做到了最好的防回归手段"],
        ["防回退守卫", "PUT /state 带记录数守卫：入库少于现库则拒绝，?force=true 供有意回退",
         "防住了旧浏览器镜像洗掉服务器数据的真实事故场景"],
        ["no-cache 中间件", "main.py:43-59 给静态资源加 Cache-Control: no-cache，配 ETag 304 校验，修掉「改了前端页面不更新」",
         "最近一次提交落地，解释详尽（注释里写清了为什么），是正确的修复方向"],
        ["统一主表设计", "coal_records 以 category 区分三表 + UNIQUE(category,ts,system) 防重复导入；ts 字符串序==时间序规避时区",
         "在 SQLite 约束下的务实取舍，导入幂等性有保障"],
        ["文档工程化", "docs 41 个 xlsx 全部由 scripts/generate_*_xlsx.py 可复现生成，不手改二进制",
         "文档与脚本同源，可 diff 可再生，优于手工维护 Excel"],
        ["双轨过渡策略", "file:// 仅 localStorage / http:// 镜像后端，后端不可用自动回退本地",
         "迁移期平滑，但应设定收敛终点（见 P0-1/P0-2），双轨不能成为常态"],
    ],
    fill_by_col0={r[0]: OK_FILL for r in [
        ["后端分层干净"], ["测试隔离设计"], ["前后端一致性兜底"], ["防回退守卫"],
        ["no-cache 中间件"], ["统一主表设计"], ["文档工程化"], ["双轨过渡策略"],
    ]},
)

# ---------------------------------------------------------------- 3 优化明细
sheet(
    wb, "优化建议明细（P0-P2）",
    "优化建议明细：13 项，含位置、问题、建议与预估工作量",
    ["编号", "优先级", "类别", "位置", "问题", "建议", "预估工作量"],
    [7, 10, 12, 30, 52, 56, 12],
    [
        ["P0-1", "P0", "架构", "frontend/js/app.js saveStore（60+ 处调用）；backend/app/routers/state.py",
         "前端任何小改动（含 EMA 状态更新）都把整库序列化 PUT /api/v1/state：网络与序列化开销 O(N) 随数据量线性恶化；"
         "多浏览器并发下整库覆盖语义靠防回退守卫勉强兜底，本质是 last-full-write-wins，条数守卫可被「多条旧数据」绕过",
         "前端改用已有的细粒度 API 增量写（POST/DELETE /records upsert、PUT /settings、PUT /inputs）；"
         "/state 仅保留备份/恢复与初次迁移用途。可按页面逐步切换，collect/import 优先（写入最频繁）",
         "3-5 天（分页面渐进）"],
        ["P0-2", "P0", "架构", "frontend/js/app.js trainMlr/trainPls；backend/app/services/training.py(486 行)；training_sklearn.py",
         "同一训练算法三重实现：JS 版 + 纯 Python 逐位移植版 + sklearn 版。每次算法改动三处同步，golden 对拍只能证明「没改错」"
         "不能降低同步成本；training.py 486 行存在的唯一理由是验证 sklearn 等价",
         "前端已走 POST /training/coarse-model（coarse.js），可退役 JS 侧本地训练（保留网络失败回退可选）；"
         "training.py 在 golden 等价性已固化进测试后退役，仅留 training_sklearn.py 作唯一实现",
         "2-3 天（含回归验证）"],
        ["P0-3", "P0", "安全", "backend/app/main.py:20-25",
         "CORS allow_origins=[*] 且 allow_methods/headers 全开；而前端由后端同源托管（main.py:40 mount /），CORS 实际无用",
         "直接删除 CORS 中间件；将来前端独立部署时再按域名白名单开启。另：系统无任何鉴权，部署到厂区网前至少加内网访问限制或简单 token",
         "10 分钟（删中间件）"],
        ["P0-4", "P0", "数据", "backend/app/main.py:12 Base.metadata.create_all()",
         "import 时 create_all 与 Alembic 迁移双轨建表并存：新环境用 create_all 建出的 schema 不带迁移版本头，"
         "后续 alembic upgrade 可能误判/漂移，生产环境风险",
         "删 create_all；启动时（或部署脚本）校验 alembic version 与 heads 一致，不一致拒绝启动并提示 upgrade",
         "10 分钟 + 验证"],
        ["P1-5", "P1", "工程化", "全仓库（无 .github/、Dockerfile、Makefile）",
         "76 个 pytest + 8 个 headless 回归脚本（需 Edge+后端）全靠手跑，回归防线不在流水线里，依赖个人自觉",
         "加 GitHub Actions：push 时跑 pytest + ruff（lint 顺带补上）；Edge 依赖的浏览器回归用 self-hosted runner 或夜间任务。"
         "若不用 GitHub，至少提供 run_checks.ps1 一键脚本挂 pre-push",
         "半天"],
        ["P1-6", "P1", "前端", "frontend/index.html:791-800（?v=N）",
         "缓存失效靠人工把 ?v=N 加 1（AGENTS.md 硬性规则），app.js 已 v=75、overview.js v=40，漏改即「改了没生效」事故",
         "no-cache+ETag 中间件已落地，可直接去掉 ?v=N 依赖 304 校验；或由后端启动时按文件内容 hash 注入版本参数，彻底免手动",
         "半天（含全量回归）"],
        ["P1-7", "P1", "前端", "frontend/js/app.js（2589 行，单 App 对象约 90+ 方法）",
         "上帝对象：store 持久化、取值链、密度建议、回归数学（solveLinearSystem/polyRegression）、UI 工具（showToast/openModal）、"
         "决策日志全部混在一个全局对象里，是最大维护风险点",
         "原生 ES Modules（script type=module）无需打包器即可拆分：store/算法/UI 三层先拆开；"
         "拆完后算法层可与后端 services 逐一对照，缩小双实现同步面",
         "2-4 天（分批）"],
        ["P1-8", "P1", "前端", "frontend/js/seed_data.js（7343 行）",
         "113 条粗精煤泥种子数据 + 出厂模型以 JS 代码形式硬编码，更新需动「代码」并手动 bump 版本号",
         "改为 JSON 文件 fetch 加载，或后端提供 /api/v1/seed 下发（与 data/seed_store.json 同源，消除两份种子）",
         "半天"],
        ["P2-9", "P2", "数据", "backend/app/database.py:7",
         "SQLite 未开 WAL：uvicorn 多线程下读写互相阻塞（check_same_thread=False 只允许跨线程，不解决锁竞争），"
         "写入时 GET /state/overview 可能 database is locked；且无备份策略",
         "连接参数加 timeout；启动时 PRAGMA journal_mode=WAL；用脚本定期备份（sqlite3 .backup）到带日期文件；"
         "数据量或并发上来后评估迁 PostgreSQL",
         "1 小时 + 备份脚本"],
        ["P2-10", "P2", "工程化", "backend/requirements.txt",
         "依赖全部 >= 不锁版本，新环境安装可能引入不兼容新版（如 pydantic/fastapi 大版本），构建不可复现",
         "用 pip-tools（pip-compile 生成 requirements.lock）或 uv lock；至少给 fastapi/pydantic/SQLAlchemy 加上限",
         "1 小时"],
        ["P2-11", "P2", "卫生", "backend/scripts/（58 个文件，约 40 个一次性文档生成器）；backend/inspect_out1.txt、inspect_out2.txt",
         "文档生成器与运行时脚本混放，单个生成器最大 535 行；调试输出文件残留入库",
         "生成器挪 docs_tools/ 或 archive/ 子目录；删 inspect_out*.txt 并加 .gitignore 规则",
         "1 小时"],
        ["P2-12", "P2", "文档", "README.md:62",
         "README 写「57 用例」，实际 76 个测试函数；测试运行还有 Starlette/httpx 弃用警告未处理",
         "刷新 README 数字（或改为「以 pytest 收集为准」不写死数字）；升级 starlette/httpx 消除弃用警告",
         "30 分钟"],
        ["P2-13", "P2", "代码", "backend/app/services/state.py:71-77（三遍全表 query）",
         "load_store 对 coal_records 按 category 查三次全表 + calc_logs 全表，store 装配每次 O(全库)；"
         "当前量级（~500 条）无感，随历史积累会拖慢 /state 与 /overview/dashboard",
         "合并为一次 query 后内存分组；更大收益在 P0-1 落地后 /state 调用频率大幅下降，此项可顺带做",
         "1 小时"],
    ],
    fill_by_col0={"P0": HIGH_FILL, "P1": MID_FILL, "P2": LOW_FILL},
)

# ---------------------------------------------------------------- 4 落地顺序
sheet(
    wb, "建议落地顺序",
    "建议落地顺序与依赖关系（先让防线自动化，再做收益最大的架构收敛）",
    ["顺序", "事项", "理由", "依赖/前置", "验收标准"],
    [7, 26, 44, 22, 44],
    [
        ["1", "P1-5 CI 流水线", "后续所有改动（尤其 P0-1/P0-2 的重构）需要自动回归防线兜底，先建流水线让每一步可验证", "无",
         "push 后自动跑 pytest+ruff 全绿"],
        ["2", "P0-3 删 CORS + P0-4 建表收口", "各 10 分钟的低风险快赢，顺手清掉部署风险", "无",
         "前后端功能回归通过；alembic 版本校验生效"],
        ["3", "P0-1 整库镜像改增量", "收益最大：性能随数据增长线性恶化 + 并发写语义缺陷都源于此；细粒度 API 已存在，只改前端调用", "CI 就绪",
         "collect/import 页面写操作走 /records 等 API；PUT /state 仅剩备份/恢复触发"],
        ["4", "P0-2 退役训练双实现", "在增量写入稳定后做，避免两场重构叠加；golden 对拍数据保留作回归证据", "P0-1 稳定一周",
         "training.py 删除后 76+ 测试全绿；前端训练统一走后端（或明确保留离线回退）"],
        ["5", "P1-6 缓存版本自动化 + P1-8 种子外置", "都是「防呆」类改动，风险低见效快", "无",
         "改前端文件不再需要手动 bump 版本；seed_data.js 消失或缩减为加载器"],
        ["6", "P1-7 app.js 拆模块", "纯重构、无功能变化，放在防线齐全后做；可与 P0-2 联动缩小算法同步面", "CI + P0-2",
         "app.js 拆为 ES Modules；script 加载顺序依赖消除"],
        ["7", "P2-9~13 打包处理", "WAL/备份、依赖锁、scripts 归档、README 刷新、load_store 合并查询", "无",
         "按清单逐项勾销"],
    ],
)

wb.save(OUT)
print(f"written: {OUT}")
