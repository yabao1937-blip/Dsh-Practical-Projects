# -*- coding: utf-8 -*-
"""生成 Excel 文档：glm-5.3 改动审查与优化清单。

产出 docs/代码审查-glm5.3改动-优化清单.xlsx，六个 Sheet：
总览与结论 / 真实数据验证记录 / 后端问题与优化 / 前端问题与优化 /
优化优先级与建议 / 保留（做得好的部分）

审查范围：git 84c6924..HEAD 的 10 个提交 + 工作区未提交改动
（frontend/js/float.js、frontend/index.html）。
所有"实测"结论均来自对 backend/data/dense_medium.db 的只读查询与
services.brief / services.density_model 的直接调用，非推测。
"""
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

DOCS = Path(__file__).resolve().parent.parent.parent / "docs"
OUT = DOCS / "代码审查-glm5.3改动-优化清单.xlsx"

HEAD_FILL = PatternFill("solid", fgColor="2F5597")
HEAD_FONT = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
TITLE_FONT = Font(name="微软雅黑", size=13, bold=True, color="2F5597")
BODY_FONT = Font(name="微软雅黑", size=10)
BOLD_FONT = Font(name="微软雅黑", size=10, bold=True)
HIGH_FILL = PatternFill("solid", fgColor="F8CBAD")     # 高优先级
MID_FILL = PatternFill("solid", fgColor="FFE699")      # 中优先级
LOW_FILL = PatternFill("solid", fgColor="E2EFDA")      # 低优先级
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
    "glm-5.3 改动审查：总览与结论（审查基线 84c6924 → HEAD + 工作区）",
    ["项目", "内容"],
    [22, 130],
    [
        ["审查范围", "10 个未推送提交（fe719e8…ef91913）+ 工作区未提交改动（frontend/js/float.js、frontend/index.html）"],
        ["代码规模", "38 个文件，+1778 / -156 行；新增 5 个脚本、2 个测试文件；测试用例 48 → 59"],
        ["总体结论", "质量高、方向正确，可以推送。测试全绿，口径改动前后端同步落地，双轨架构的结构性缺陷（旧浏览器镜像洗库）被真正修掉。"
                   "但发现 4 项「必须修」：其中 1 项是本次未提交改动完全没生效，2 项是把一次性历史修复写成了常驻生产逻辑，1 项是决策日志的响应信号取错源。"],
        ["必须修（高）", "4 项：A1 分布图刻度改动无效 / A2 简报弹窗文案与行规则说明过期 / A3 导入路径常驻删除历史记录 / A4 决策日志响应取被控量（闭环偏差）"],
        ["需确认（中）", "4 项：B1 数据驱动 K 当前恒为 valid=false（功能等于未启用）/ B2 501 直读暂无数据源 / B3 简报行数 1069→263 / B4 kSource 前后端比较方式不一致"],
        ["健壮性（低）", "7 项：时间戳解析容错、防回退守卫覆盖面、删记录与自动回拉冲突、条数多者胜的误判、bool 数值穿透、conftest import 副作用、AGENTS.md 路径过期"],
        ["实测数据要点", "① 真实库三表均有数据且窗重叠 → 简报 263 行（非 0）；② 种子数据三表窗不重叠 → 简报 0 行（旧规则 1069 行全是错位续传凑的）；"
                       "③ 360 条灰分密度记录全部 belt=502，零条 501；④ 数据驱动 K 斜率实测为负（A -0.0221 / B -0.0196）→ 恒回退 0.075"],
        ["建议动作", "先修 A1（一行改动即让本次改动真正生效）+ A3（防止将来误删真实数据），其余按优先级表逐项确认后再动。"
                   "B2/B3 涉及工艺口径与现场期望，建议先和现场对齐再改。"],
    ],
    fill_by_col0={"必须修（高）": HIGH_FILL, "需确认（中）": MID_FILL, "健壮性（低）": LOW_FILL},
)

# ---------------------------------------------------------------- 2 实测
sheet(
    wb, "真实数据验证记录",
    "审查依据：对真实数据库的只读实测结果（backend/data/dense_medium.db）",
    ["验证项", "方法", "实测结果", "结论"],
    [30, 34, 60, 46],
    [
        ["三表数据量与时窗", "sqlite3 只读查询 coal_records 按 category 分组",
         "coarse 152 条 2026-06-16 09:31 ~ 2026-09-03 07:22；float 17 条 2026-05-21 07:23 ~ 2026-09-03 07:19；"
         "ash_density 360 条 2026-05-21 08:23 ~ 2026-09-03 08:22", "三表在 8-23~9-03 段同时新鲜，其余段有缺口"],
        ["简报行数（新规则）", "load_store + build_hourly_brief(真实库)",
         "263 行（覆盖 2026-08-23 08:00 ~ 2026-09-03 08:00）；其中 col12（建议密度）为空 0 行、col14（预测粗灰）为空 0 行",
         "新规则下真实数据仍有 263 行，不是 0；且「保留公式缺项行」这条改动在当前数据上未被触发（无用例覆盖该分支）"],
        ["简报行数（旧规则对比）", "同一结果再套旧过滤条件 col12≠'' and col14≠''",
         "263 行全部通过 → 差值为 0", "行数从 1069→263 的变化 100% 来自「三表齐全(24h)」新规则，与「取消事后过滤」无关"],
        ["种子数据简报", "build_hourly_brief(seed_store.json)（tests/test_brief.py::test_seed_no_overlap_no_rows）",
         "0 行", "演示/种子数据三表时窗不重叠（表1=6-7月，表2/3=5月），简报为空是规则的正确结果——但用户点按钮时会看到空结果"],
        ["501 / 502 数据源", "sqlite3 查 coal_records.belt 分布",
         "ash_density 360 条全部 belt='502'，belt='501' 共 0 条", "「501=总灰分直读」当前没有任何在线数据支撑，ash_501 恒为默认常量 8.8"],
        ["简报 501 列实测值", "build_hourly_brief 首/末行",
         "col6(501皮带灰分仪) 恒为 8.80；col5(重介精煤灰分)=7.80、col7(502皮带灰分仪)=7.80（一致，502 链路正确）",
         "502 链路（重介灰分）改动已生效且正确；501 链路是常量占位"],
        ["数据驱动 K 增益", "services.density_model.fit_density_gain(真实库)",
         "valid=False, source='none', totalPoints=283；系统A k=-0.02206 (n=180, R²=0.049)、系统B k=-0.01962 (n=103, R²=0.064)",
         "两个系统斜率均为负 → 被正性约束全丢弃 → 永远回退 K_PREDICT=0.075。本提交在当前数据上不产生任何行为变化"],
        ["测试套件", "pytest tests -q（DMCS_DATABASE_URL 临时库）",
         "59 passed, 1 warning in 17.58s", "全绿；且已隔离到临时库，不触碰真实数据"],
        ["前端缓存版本号", "index.html 比对",
         "xlsx-lite 5→6、api 1→2、app 64→70、overview 37→38、import 17→20；工作区 float 11→12",
         "所有被改动的 JS 均已递增 ?v=N，符合项目硬性规则"],
    ],
    fill_by_col0={"数据驱动 K 增益": HIGH_FILL, "501 / 502 数据源": HIGH_FILL, "种子数据简报": MID_FILL},
)

# ---------------------------------------------------------------- 3 后端
sheet(
    wb, "后端问题与优化",
    "后端（backend/）问题与优化建议",
    ["编号", "优先级", "问题", "位置", "影响", "建议修法"],
    [8, 10, 40, 36, 46, 52],
    [
        ["B-1", "高",
         "决策日志的「灰分响应」取自被控量本身：_completeDensityDecisionResponses 用 getHeavyAsh()，"
         "而该函数已改为优先返回 502 在线仪表值",
         "frontend/js/app.js（_completeDensityDecisionResponses）\n后端 store.densityDecisionLog 落库见 services/migrate.py AUTO_STATE_KEYS",
         "决策日志的设计目的是「每次真实调整 = 一个 mini 阶跃点，供 K 标定」，"
         "但响应量取闭环内的在线仪表 → ΔA≈0（回路把灰分拉回目标），K 估计系统性偏小。"
         "另外若窗口内又发生一次密度调整，rhoNow-e.rhoCur 会把后续调整的累积量算到本条上（数据污染）",
         "① 响应只用「决策后首次人工采样」（heavySamples / 315 化验），取不到就保持 response=null；"
         "② 窗口内出现新决策则把本条标 invalidated=true，不再补记；③ 补记时同时存采样时间与滞后时长"],
        ["B-2", "中",
         "「数据驱动 K 增益」在当前真实数据上恒为 valid=false（斜率实测为负被正性约束全部丢弃），"
         "即 fe719e8 这个提交声称的「激活」实际未激活",
         "backend/app/services/density_model.py（_k_ok: K_MIN<K<K_MAX）\n调用处 app/routers/overview.py",
         "功能看起来已上线，实际永远走 K_PREDICT=0.075 回退；且 R²≈0.05~0.06 说明该回归本身没有解释力。"
         "容易被误读为「已用数据标定」",
         "① 保留代码（等阶跃实验数据接入后自然生效）；② UI/接口显式区分三态：data / default(数据不可辨识) / default(样本不足)，"
         "把 systems[].k 与 r2 展示出来，让现场看到「斜率是负的所以不用」；③ 在文档里写明这是闭环数据的预期结果"],
        ["B-3", "中",
         "501 直读口径已落地但无数据源：真实库 360 条灰分密度记录全部 belt=502，零条 501",
         "backend/app/services/resolvers.py INSTRUMENT_DEFAULT / resolve_total_ash\nbackend/app/services/brief.py DEF / total_ash 兜底",
         "ash_501 恒为常量 8.8 → 简报第 6 列恒显 8.80、resolve_total_ash 兜底恒为 8.8。"
         "改动前的 501/502 加权虽口径不对，但至少用了真实的 502 读数；现在这条兜底路径丢掉了一个真实信号",
         "① 短期：501 无在线值时，兜底改用「502 读数推算的总灰分」（与公式链同源），而不是常量；"
         "② 层级文案显式标成「默认常量 8.8」而不是「仪表」，避免被当成实测值；"
         "③ PLC 接入 501 后本项自动消失"],
        ["B-4", "中",
         "kSource 前后端判定方式不一致：后端 is not（对象身份比较），前端 ===（值比较）",
         "backend/app/services/density.py:66\nfrontend/js/app.js computeDensityGuidance",
         "当数据拟合出的 K 恰好等于 0.075 时，前端报 default、后端报 data，破坏「前后端逐值一致」的项目不变量。"
         "当前 K 恒为 default 故未暴露",
         "后端改为显式布尔：k_from_data = isinstance(...) and k_gain>0，再据此设 kSource；或统一用 != 值比较"],
        ["B-5", "低",
         "_epoch 只接受唯一一种格式 %Y-%m-%d %H:%M:%S，解析失败返回 -inf，"
         "而该值参与「24h 新鲜度」判定 → 解析失败被当成「超窗」静默丢行",
         "backend/app/services/brief.py:33-37、144-148",
         "前端 toTs 用 new Date(t)，能解析 HH:MM、HH:MM:SS、T 分隔等多种写法；"
         "一旦出现无秒时间戳（'YYYY-MM-DD HH:MM'），后端会比前端少出甚至不出行，且无任何报错——"
         "这正是「简报生成失败」类症状的典型来源。当前库数据已核实全为 HH:MM:SS，属潜在缺口",
         "① 依次尝试 %Y-%m-%d %H:%M:%S / %Y-%m-%d %H:%M / datetime.fromisoformat；"
         "② 把「时间戳无法解析」与「超出 24h 窗口」分开计数，前者记 warning 日志；"
         "③ 加一条单测覆盖无秒时间戳"],
        ["B-6", "低",
         "PUT /state 防回退守卫只比较 coal_records 总数",
         "backend/app/services/migrate.py replace()",
         "旧浏览器若 coal_records 数相等或更多、但 calc_logs / heavy_samples 更少，仍会洗掉服务器侧的新补录与日志。守卫是「部分有效」",
         "① 守卫扩展为各业务表数量的向量比较（任一表回退即提示）；"
         "② 更长治：引入单调 revision（服务端自增）+ updatedAt，让「新者胜」而不是「多者胜」"],
        ["B-7", "低",
         "brief.py 的局部 _num 与两处内联数值判定未排除 bool；density.py 的 isinstance(k_gain,(int,float)) 同样放行 bool",
         "backend/app/services/brief.py:56,84,86\nbackend/app/services/density.py:58",
         "与 resolvers._is_num「排除 bool」的既有约定不一致（isinstance(True,int) 为 True），JSON 里出现 true 会被当成 1 穿透",
         "统一改用 resolvers._is_num（或提到公共模块），并在 density.py 用同一判定"],
        ["B-8", "低",
         "conftest.py 在 import 期执行建表 + 灌种子（模块级副作用），且临时目录不清理",
         "backend/conftest.py",
         "功能上没问题（测试全绿），但 DB 写入发生在 collection 阶段，行为受导入顺序影响，后续排查困难",
         "改为 @pytest.fixture(scope='session', autouse=True) 内完成建表与灌种子；临时目录保留但在文档里说明位置"],
    ],
    fill_by_col0={"高": HIGH_FILL, "中": MID_FILL, "低": LOW_FILL},
)

# ---------------------------------------------------------------- 4 前端
sheet(
    wb, "前端问题与优化",
    "前端（frontend/）问题与优化建议",
    ["编号", "优先级", "问题", "位置", "影响", "建议修法"],
    [8, 10, 40, 36, 46, 52],
    [
        ["F-1", "高",
         "本次未提交改动里，分布图（柱状图）的时间刻度防重叠完全没生效："
         "chart-lite 的 _drawBar 根本不读 this.xTickLabels，只有 _drawLine 读",
         "frontend/js/float.js:361（this.distChart.xTickLabels = ...）\nfrontend/js/chart-lite.js:557-630（_drawBar，line 616 无条件绘制 labels[i]）\n同文件 line 407-416 是 line 图才有的 xTickLabels 支持",
         "distChart 是 type:'bar'，所以那行赋值是死代码：12 根柱下仍然各画一个完整时间标签，"
         "横向必然叠在一起（label 形如 '08-23 07:19'，12×约60px 远大于图宽）——正是这次改动想解决的问题",
         "① 推荐：给 _drawBar 补 xTickLabels 支持（照 _drawLine 的 38px 防重叠写法），一次修好所有柱状图；"
         "② 或退一步：float.js 直接把 distChart.data.labels 换成稀疏数组（置空处传 ''）。"
         "另外 distChart 未设 xTimes（按序号等距），而 _timeTickLabels 按时间比例算像素位置，两者坐标口径不一致，"
         "建议给 _timeTickLabels 传「取 x 的函数」或同时设 xTimes，避免将来出现错位"],
        ["F-2", "高",
         "简报弹窗文案过期 + 缺行规则说明：仍写死「重介灰分 8.50%」，且不解释新的「三表齐全(24h)」行规则",
         "frontend/js/import.js:937（恒值说明文案）\n同文件 922-925（0 行提示）",
         "重介灰分已改为「502 在线 / 默认 7.9」，弹窗却仍说 8.50%，口径自相矛盾。"
         "更关键：行数从 1069 掉到 263（种子数据为 0）时，用户只会看到「暂无可对齐的三表数据，请先导入三张表」——"
         "三表其实已导入，真实原因是三表没同时落在 24h 窗口内。这就是「简报生成失败」这类误解的直接来源",
         "① 恒值说明改为动态渲染（重介灰分显示当前值+来源：手动采样/502在线/默认7.9）；"
         "② 弹窗头部加一行行规则说明：「本行需三表在 24h 内均有记录，否则不出行」；"
         "③ 0 行时提示分两种：「三表无数据」vs「三表有时间窗缺口（最近数据：表1 x / 表2 y / 表3 z）」，"
         "后者直接把三表最近时间点列出来，用户一眼能看懂为什么不出行"],
        ["F-3", "高",
         "一次性历史数据修复被写成常驻生产逻辑：每次导入粗精煤泥都会删除匹配固定时间戳的记录",
         "frontend/js/import.js:498-507（badTs = /^2026-07-(20|30) /、strayTs = /^2026-07-01 (00:47|02:23|08:12|10:41):/）",
         "这是为修 7 月两处历史遗留写的一次性清理，却永久留在导入路径上："
         "将来若 7-20 / 7-30（都是正常生产日）或那几个时刻出现真实采样，会在导入时被静默删除，且无日志无提示。"
         "同时导入函数里直接改写 App.store.coarseCoal/magneticTail，职责耦合",
         "照项目已有的 __merged 版本标记写法，改成一次性迁移："
         "if (!(App.store.__fixes && App.store.__fixes.julStray)) { ...执行清理...; App.store.__fixes = {...}; } "
         "并且清理时记录条数到 importLogs，便于追溯"],
        ["F-4", "中",
         "autoPullIfStale 以「三表条数多者胜」自动覆盖本地且无确认、无撤销",
         "frontend/js/app.js autoPullIfStale()",
         "判据只看数量：服务器条数多但内容更旧（例如服务器被别的脚本改过）时会误判并覆盖本地；"
         "自动执行、只弹一句 toast，用户若正在录入会被打断",
         "① 至少改为比较「最新记录时间戳 / revision」而不是条数；② 覆盖前若本地有未镜像的改动则先提示；"
         "③ 提供「撤销」或保留一份覆盖前的快照键（如 __bak_before_pull）"],
        ["F-5", "中",
         "防回退守卫与「用户在 UI 里删记录」相互作用：删除后整库镜像会被服务器拒绝，"
         "而下次开页 autoPullIfStale 又把数据拉回来 → 表现为「删了又回来」",
         "backend/app/services/migrate.py replace()（守卫）\nfrontend/js/api.js putState()（只 console.warn）\nfrontend/js/app.js autoPullIfStale()",
         "用户会认为删除功能失效；控制台警告用户看不到",
         "① 前端在「删除类」操作后走 putState(store, true)（force），或在设置里给一个「强制同步到服务器」按钮；"
         "② 镜像被拒时用 toast（而非 console.warn）提示，并附「从服务器恢复数据」入口"],
        ["F-6", "低",
         "frontend/AGENTS.md 仍指向旧路径与旧规则",
         "frontend/AGENTS.md",
         "写着「本目录 D:\\Users\\liu\\Desktop\\测试\\web (2) 是唯一允许修改代码的地方」，"
         "项目已整合到 D:\\dense-medium-density-control-system（backend/frontend/docs 同仓库），"
         "该文件会持续误导后续 agent（包括把改动写回桌面的旧副本）",
         "更新为当前仓库布局：说明三目录同仓库、后端改动落在 backend/、文档产出到 docs/、"
         "以及「改 JS 必须同步递增 index.html 的 ?v=N」这条仍然有效的硬性规则"],
        ["F-7", "低",
         "决策日志存 auto_state，上限 200 条，整库 saveStore 时一并镜像",
         "frontend/js/app.js DECISION_LOG_MAX / _logDensityDecision",
         "体量可控（约 80KB），但每条决策都会触发一次整库 PUT /state；"
         "且 200 条上限是静默 slice，旧记录被丢弃时无提示",
         "① 上限触发时记一条提示；② 长期考虑把决策日志改为独立接口增量追加（POST /density-decisions），"
         "不再随整库镜像；③ 导出功能（Excel/CSV）便于直接作为 K 标定输入"],
    ],
    fill_by_col0={"高": HIGH_FILL, "中": MID_FILL, "低": LOW_FILL},
)

# ---------------------------------------------------------------- 5 优先级
sheet(
    wb, "优化优先级与建议",
    "建议执行顺序（按「收益 / 风险 / 成本」排序）",
    ["顺序", "项", "动作", "为什么先做这个", "预估成本"],
    [6, 10, 52, 56, 20],
    [
        [1, "F-1", "给 chart-lite._drawBar 补 xTickLabels 支持（或改 float.js 传稀疏 labels）",
         "本次未提交改动的核心目的目前完全落空，用户能直接看到标签叠在一起；改动量极小", "10 分钟"],
        [2, "F-2", "简报弹窗：动态显示重介灰分来源与值 + 补行规则说明 + 0 行提示分两种",
         "直接消除「简报生成失败」这类误解；用户上次报的失败现象很可能就源于此。纯文案+提示逻辑，零算法风险", "20 分钟"],
        [3, "F-3", "把 7 月历史清理改为一次性迁移（__fixes 标记）",
         "唯一带「静默删真实数据」风险的项，越早摘掉越好；改法项目里已有现成范式", "15 分钟"],
        [4, "B-1", "决策日志响应改用人工采样，窗口内新决策标 invalidated",
         "决定 K 标定数据是否可用——现在采到的响应数据基本是闭环偏差，白采", "30 分钟"],
        [5, "B-5", "_epoch 支持多格式时间戳 + 区分「解析失败/超窗」+ 加单测",
         "低成本堵住「前端出行、后端不出行」的静默不一致（项目核心不变量）", "20 分钟"],
        [6, "B-4", "kSource 判定统一为值比较/显式布尔",
         "恢复前后端逐值一致；当前未暴露，但改起来只有一行", "5 分钟"],
        [7, "B-6", "防回退守卫扩展到各业务表（或引入 revision）",
         "守卫当前只管一张表，仍存在洗掉 calc_logs 的通道；revision 是更彻底的做法", "1~2 小时（revision 版）"],
        [8, "B-2", "K 增益三态展示（data / 不可辨识 / 样本不足）+ 暴露 systems[].k 与 r2",
         "让现场看见「斜率是负的所以回退」，而不是误以为已标定", "30 分钟"],
        [9, "B-3", "501 无在线值时兜底改用 502 推算；层级文案改为「默认常量」",
         "需要先和现场确认口径期望，再动兜底公式", "待确认后 30 分钟"],
        [10, "F-4 / F-5", "自动回拉判据改为时间戳/revision；删除类操作走 force + toast 提示",
         "提升双轨同步的可预期性，消除「删了又回来」", "1 小时"],
        [11, "B-7 / B-8 / F-6 / F-7", "bool 判定统一、conftest 改 fixture、AGENTS.md 更新、决策日志导出",
         "整洁性与可维护性，随时可做", "1 小时"],
    ],
)

# ---------------------------------------------------------------- 6 保留
sheet(
    wb, "保留（做得好的部分）",
    "审查中确认做得好、应当保留的做法",
    ["项", "内容", "为什么值得保留"],
    [26, 72, 66],
    [
        ["pytest 数据库隔离", "config.py 读 DMCS_DATABASE_URL；conftest 在 import app 前锁到临时 SQLite；会话级播种一次种子",
         "此前用例隐含依赖真实库恰好存着上一轮种子数据，是「测试改真实数据」的隐患。这是本次最有价值的基建改动"],
        ["PUT /state 防回退守卫", "入库记录少于现库则拒绝，force=true 显式跳过；清表与写入同事务，失败整体回滚",
         "双轨架构里最要命的结构性缺陷——旧浏览器任何一次 saveStore 都能把服务器侧导入/训练成果洗掉，这次真正堵住了"],
        ["启动期补齐只写本地", "启动期默认值补齐、种子灌入统一改 saveStore(true)，不镜像到服务器",
         "与守卫配套，避免「打开页面」这个无害动作产生破坏性写入，思路完全正确"],
        ["口径改动前后端同步落地", "501/502 分工同时改了 ASH_METER_DEFAULT / INSTRUMENT_DEFAULT / DEF / resolve_total_ash /"
                              "totalAshEntry / getHeavyAsh / heavyAshLayer / brief，两侧逐值对齐",
         "这是项目最核心的不变量（golden 对拍 <1e-6），单侧改会立刻破坏；本次是全链一致地改的"],
        ["简报行存在规则", "「三表齐全(24h)」同时落在 brief.py 与 app.js，并把递归状态不推进的理由写进注释",
         "旧规则确实会用陈旧续传凑行（种子数据凑出 1069 行全是无意义行）。规则本身是对的方向，同一处逻辑两侧一致"],
        ["密度决策日志 + 阶跃实验文档", "DECISION_LOG_MAX=200 + 45min 响应补记 + 工况上下文；配套 6 Sheet 实验记录表与自动 K 公式",
         "把「闭环常规数据不可辨识 K」这个结论落成了可执行的数据采集方案——从「知道不行」到「知道怎么才行」"],
        ["注释与文档密度", "新增 5 个脚本、2 个测试文件均有模块 docstring；每个重要改动都带「为什么」而非「做了什么」",
         "与项目既有风格一致，交接成本低"],
        ["?v=N 缓存号纪律", "所有被改动的 JS 均同步递增 index.html 中的版本号",
         "项目硬性规则，本次 5 处改动全部遵守（含未提交的 float.js 11→12）"],
    ],
    fill_by_col0={k: OK_FILL for k in
                  ["pytest 数据库隔离", "PUT /state 防回退守卫", "启动期只写本地", "口径改动前后端同步落地",
                   "简报行存在规则", "密度决策日志 + 阶跃实验文档", "注释与文档密度", "?v=N 缓存号纪律"]},
)

# ---------------------------------------------------------------- 7 导入解析器
sheet(
    wb, "导入解析器与遗留问题",
    "导入/解析路径专项审查（含子代理深度审查的复核结果与定性修正）",
    ["编号", "优先级", "归属", "问题", "位置", "证据与影响", "建议修法"],
    [8, 10, 20, 38, 40, 62, 46],
    [
        ["I-1", "高", "本次引入",
         "7 月历史清理会删掉刚导入的真实行，且后端无对应过滤 → 双轨立即分叉",
         "frontend/js/import.js:499-508（既有 F-3 的补充证据）",
         "7.20 / 7.30 写成「7.20」「7.30」是两位小数、无歧义，fix_day 得 {7,20}/{7,30} 为正确日期；"
         "该行被插入后在同一调用内被静默 filter 删除，而 imported 与 importLogs 仍计为成功。"
         "后端没有这段过滤 → 同一文件在两轨得到不同的库内容",
         "同 F-3：用 __fixes.julStray 版本标记门控，或移入独立迁移脚本"],
        ["I-2", "高", "本次引入（口径遗留）",
         "口径改动未覆盖导入路径：influence_value 仍按写死的 heavyAsh=8.50 计算，"
         "与新的 getHeavyAsh()（502 在线 / 默认 7.9）不同源",
         "frontend/js/import.js:461、:699、:718（heavyAsh/hAsh 硬编码 8.50）\n"
         "对照 frontend/js/float.js:339-349（图表用实时重算值）、float.js:377/383/402（表格用存量 influence_value）",
         "同一浮精记录存在两个影响值：图表用实时重算（App.getAshByTime(...) ?? 8.50），"
         "表格与 tooltip 用导入时按 8.50 存量算出的 influence_value。重介灰分默认值从 8.50 改为 502在线/7.9 后，"
         "两者差距进一步放大，同一页面上下自相矛盾",
         "① 导入时不再硬编码，改用 App.getHeavyAsh()（或统一在渲染时重算并存回）；"
         "② 表与图统一取同一来源；③ 存量数据的 influence_value 提供一次重算迁移"],
        ["I-3", "高", "本次引入（口径遗留）",
         "重介灰分层级文案判断未同步：三元只认 '手动(采样)'，其余一律打印「（默认8.50）」",
         "frontend/js/overview.js:410",
         "heavyAshLayer() 已改为返回 '手动(采样)' / '502在线' / '默认7.9'，"
         "但该处 else 分支写死「（默认8.50）」→ 实际渲染成「7.80%（默认8.50）」，标签与值自相矛盾",
         "改为直接使用 heavyAshLayer() 的返回值，例如 `（${App.heavyAshLayer()}）`；"
         "同时把 overview.js:346 附近同名文案一并校正"],
        ["I-4", "中", "本次引入",
         "两个「新旧」判据不一致：防回退守卫按 coal_records 行数，autoPullIfStale 按 store 三个数组长度（含 calc_logs 表）",
         "backend/app/services/migrate.py replace()（incoming/current 只数 CoalRecord）\n"
         "frontend/js/app.js autoPullIfStale()（coarseCoal+floatCoal+calcLogs）",
         "calcLogs 同时来自 coal_records(ash_density) 和独立的 calc_logs 表，两个计数口径不同 → "
         "可能出现「自动拉取判定为服务器更新 → 覆盖本地」但「镜像被守卫拒绝」的组合，"
         "用户看到导入完成却未落库（api.js 只 console.warn，toast 仍报成功）",
         "① 统一为同一个判据（建议引入单调 revision）；② 用户主动导入后用 putState(store, true)；"
         "③ 镜像被拒时用 toast 明确提示"],
        ["I-5", "中", "本次可顺手补",
         "新的日期消歧器覆盖不全：月份不推进导致时间倒退；补零手写日无法纠正",
         "backend/app/services/importer.py extract_date_seq（本次新增）\nfrontend/js/import.js:381-392（本次新增）",
         "实测：行序列 6.29 → 6.3 → 6.4 解析为 6月29日 → 6月30日 → **6月4日**"
         "（时间比上一行倒退 26 天，回退分支直接返回 legacy 的 fixDay(6,40)={6,4}）；"
         "实测：文本 '6.02'→6月20日、'6.03'→6月30日、'6.10'→**6月1日**，"
         "而同样内容写成数字 6.02→6月2日 —— 同一文件文本/数字单元格给出不同日期。"
         "_ambiguous_frac 只匹配一位小数，故这两种情况新消歧器永远修不到",
         "① 回退分支中若 (m, legacy_d) < (prev.m, prev.d)，先试 {m+1, f} / {m+1, f*10}；"
         "② 文本形式先规范化尾零（frac 去掉首尾 0）再判定歧义；"
         "③ 两侧同步改并补用例（6.29/6.3/6.4、'6.02'、'6.10'）"],
        ["I-6", "中", "存量（建议一并修）",
         "extract_date 未加保护的 int()：一个畸形日期单元格即抛 ValueError；JS 侧同位置生成 NaN 时间戳「毒行」",
         "backend/app/services/importer.py:51-53\nfrontend/js/import.js:360-361",
         "实测 Python：extract_date('6-16(早班)') / ('6-7月') / ('7-') / ('6/abc') 全部抛 "
         "ValueError: invalid literal for int()。（注：本项在 84c6924 已存在，非本次引入。）"
         "**定性修正**：/import/parse 在 frontend/ 中无任何调用者（全仓库仅文档生成脚本提及），"
         "所以 500 只影响尚未启用的后端解析端点，不构成本次改动的上线阻断；"
         "但 JS 同位置走的是**在用**路径：+p[1] 得 NaN → timestamp = '2026-06-NaN 09:00:00' 毒行入库，"
         "后续会破坏 brief 的时间排序",
         "Python：if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit() and 1 <= int(parts[0]) <= 12；"
         "JS：补 && +p[1] >= 1 && +p[1] <= 31；两侧解析失败一律返回 null/'' 并计数报错"],
        ["I-7", "中", "存量（影响后续迁移）",
         "自定义日期数字格式双轨分叉：JS 只认内置 numFmtId，Python 按格式字符串判定",
         "frontend/js/xlsx-lite.js:111-119（只认 14-22/27-36/45-47/50-58）\n"
         "对照 openpyxl styles/stylesheet.py is_date_format（含 [dmhys] 即判为日期）",
         "同一单元格 6.16 加自定义格式（如 m.d）：JS 保持数字 → 2026-06-16；"
         "openpyxl 先转成 1900-01-06 → 2026-01-06（月与日都错）。内置格式两侧一致。"
         "**定性修正**：浏览器自行解析文件、后端解析端点当前无调用者，两轨不会同时处理同一文件，"
         "所以这是「后端解析迁移」的前置对拍缺口，而非当前线上缺陷",
         "在 _readDateStyles 中解析 <numFmts> 并用与 openpyxl 相同的 [dmhys] 规则判定，"
         "或让 import_api 读取未转换的序列号；迁移前必须补一条自定义格式的对拍用例"],
        ["I-8", "中", "存量",
         "float_ash 导入用 parseFloat(...) || 0：空白/'-'/'？' 的灰分变成 0% 并作为真实记录入库",
         "frontend/js/import.js:716-717（浮精灰分/煤量）\n同文件 :735-738（密度列有 1.3~1.6 校验）",
         "同一函数内密度列做了范围校验，浮精灰分列没有；resolve_float_ash 取最新一条的 ash，"
         "于是 0% 浮精灰分会静默进入总灰分公式。另有 :684-687 同模式（液位/水分/煤量）",
         "无法解析时返回 null，由 resolvers 的 _is_num 丢弃；不要用 || 0 兜底"],
        ["I-9", "低", "存量",
         "xlsx-lite 读取器缺口（本次只修了一半）",
         "frontend/js/xlsx-lite.js:215（只读 <v>，无 inlineStr 分支）\n同文件 :275（_decodeXml 只解十进制 &#10;）",
         "t=\"inlineStr\" 单元格（<is><t>…</t></is>，部分导出器/WPS 会写）在浏览器读到 null，"
         "openpyxl 能正常读到 → 工作面列在 file:// 下整列为空；"
         "十六进制引用 &#x0A; 未被解码（Python 侧已解），本次新增的十进制 &#10; 修复因此不完整",
         "补 <is><t> 分支并复用 _decodeXml；正则扩为 /&#(x?[0-9A-Fa-f]+);/g。"
         "另：xlsx-lite.js:273 特意保留字面量 &#10;，但 import.js:439 又无条件 replace 回换行，两处契约不一致，建议只留读取器解码"],
        ["I-10", "低", "存量/对拍",
         "Python round（银行家舍入）vs JS Math.round（四舍五入）",
         "backend/app/services/importer.py:42、:66（本次新增的 _ambiguous_frac 沿用同一写法）\nfrontend/js/import.js:348、:371",
         "实测数值 6.125：Python round(12.5)=12 → 6月12日；JS Math.round(12.5)=13 → 6月13日。"
         "同类：半秒时间分数、+ash.toFixed(4) vs round(ash,4)。真实数据无此值，故本次 4208 格对拍未能发现",
         "Python 侧统一用 math.floor(x + 0.5) 表达「JS Math.round」"],
        ["I-11", "低", "存量",
         "同文件重复时间戳 last-wins 但计数不扣；空白时间列全部落到 00:00:00",
         "frontend/js/import.js:466-467、:490\nbackend/app/models.py UniqueConstraint(category, ts, system)",
         "按时间戳逐条 filter 再 push，同时间戳的第二行挤掉第一行，而 imported++ 两条都计；"
         "空白时间单元格两轨都映射为 00:00:00，同日多行空时间只剩最后一行；DB 唯一约束也无法表示（system 恒为「合并」）",
         "定策略并两侧一致：要么给重复时间戳排秒（+i 秒），要么显式拒绝并计 skipped"],
        ["I-12", "低", "存量",
         "年份硬编码与跨年错误",
         "backend/app/services/importer.py DEFAULT_YEAR=2026（:10 等）\nfrontend/js/import.js:396、:609、:613（字面量 2026）",
         "日期辅助函数只返回 {m,d}，Excel 里的真实年份被丢弃（2027-01-05 → 2026-01-05）；"
         "12.30 之后接 1.1：候选月份不成立 → 退回 legacy → 2026-01-01，比上一行早了约一年。"
         "只改 Python 常量会静默破坏对拍",
         "JS 侧提为同名 DEFAULT_YEAR 常量（三处共用）；解析结果早于上一行完整日期时把年份 +1；"
         "日期辅助函数改为返回 {y,m,d}"],
        ["I-13", "低", "存量",
         "_excel_serial_to_date 无溢出保护",
         "backend/app/services/importer.py:28 附近",
         "实测 _excel_serial_to_date(9999999) 抛 OverflowError: date value out of range；"
         "日期列出现 7 位游离数字即 500（JS 返回 null 并回退原始字符串）",
         "逐行解析包 try/except，越界返回 None"],
        ["I-14", "低", "测试覆盖缺口",
         "导入路径没有跨语言 golden，且新增代码无用例",
         "backend/tests/test_importer.py（现有覆盖：fix_day/extract_date/extract_time/normalize_ts/extract_date_seq/整表顺序/列提取）",
         "缺：extract_date_seq 回退分支、12月→1月跨年、文本 '6.02'/'6.10'、重复时间戳、bool/NaN 开关、bad _parse_des；"
         "本次新增的 &#10; replace 未被任何用例执行（fixture 用的是真换行）；"
         "brief/training/resolvers/seed 都有 dump_*_js.js 跨语言 golden，唯独 parseCoarseFactors 没有；"
         "POST /import/parse 无任何测试触达",
         "加 backend/scripts/dump_import_js.js（照 dump_brief_js.js 的写法）+ 一条 golden 对拍用例，"
         "并补齐上述边界用例；这正好能兜住 I-5/I-6/I-10/I-11/I-12"],
    ],
    fill_by_col0={"高": HIGH_FILL, "中": MID_FILL, "低": LOW_FILL},
)

DOCS.mkdir(parents=True, exist_ok=True)
wb.save(OUT)
print("已生成:", OUT)
for ws in wb.worksheets:
    print("  - %-16s %d 行" % (ws.title, ws.max_row - 3))
