# -*- coding: utf-8 -*-
"""生成 Excel 文档：后续优化待办计划（含原因）。

产出 docs/待办-后续优化计划.xlsx，四个 Sheet：
总览与建议批次 / 待办清单(含原因) / 待你决策的问题 / 暂缓项与原因

范围：2026-09-10 完成「glm-5.3 改动审查 + 7 项修复」（提交 dff835b）之后
剩余的待办。每项均附「原因」——即为什么必须做/为什么这样做，
原因是判断优先级与取舍的依据，不是复述做法。
清单只包含已由实测或代码证据确认的问题，不含推测项。
"""
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

DOCS = Path(__file__).resolve().parent.parent.parent / "docs"
OUT = DOCS / "待办-后续优化计划.xlsx"

HEAD_FILL = PatternFill("solid", fgColor="2F5597")
HEAD_FONT = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
TITLE_FONT = Font(name="微软雅黑", size=13, bold=True, color="2F5597")
BODY_FONT = Font(name="微软雅黑", size=10)
HIGH_FILL = PatternFill("solid", fgColor="F8CBAD")
MID_FILL = PatternFill("solid", fgColor="FFE699")
LOW_FILL = PatternFill("solid", fgColor="E2EFDA")
ASK_FILL = PatternFill("solid", fgColor="DDEBF7")
HOLD_FILL = PatternFill("solid", fgColor="EDEDED")
THIN = Side(style="thin", color="B0B0B0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")
CENTER = Alignment(horizontal="center", vertical="top", wrap_text=True)


def sheet(wb, name, title, headers, widths, rows, fill_col=None, fill_map=None):
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
            cell.alignment = CENTER if c <= 2 else WRAP
            cell.border = BORDER
        if fill_col and fill_map:
            f = fill_map.get(str(row[fill_col - 1]))
            if f:
                for c in range(1, len(headers) + 1):
                    ws.cell(row=i, column=c).fill = f
    ws.freeze_panes = "A4"
    return ws


wb = Workbook()
wb.remove(wb.active)

# ------------------------------------------------------------------ 1 总览
sheet(
    wb, "总览与建议批次",
    "后续优化待办：总览与建议执行批次",
    ["项目", "内容"],
    [20, 132],
    [
        ["已完成（基线）", "提交 dff835b：glm-5.3 改动审查 + 7 项修复（F-1 分布图刻度、I-2 影响值口径统一、"
                       "F-2 简报文案与 0 行分诊、F-3 7月清理改一次性迁移、I-3 层级文案、B-1 决策日志响应改人工采样、"
                       "I-5/6/10 日期解析两侧同步）。pytest 59→65；真实库简报对拍 4208 格零差异；浏览器 A/B 与冒烟全通过。"],
        ["本表范围", "基线之后剩余的全部待办：8 项可直接执行、8 项需你决策、4 项建议暂缓。"
                   "每项都写明「原因」，原因是判断优先级和取舍的依据。"],
        ["待办分布", "可直接执行 10 项：缺陷修复 2 项 · 健壮性 3 项 · 文档 1 项 · 测试基建 1 项 · 数据治理 2 项 · 交付 1 项；"
                   "需决策 8 项；建议暂缓 4 项。"],
        ["推荐批次 1（约 1.5h，零口径风险）", "T-1 浮精灰分解析失败不再当 0 · T-2 xlsx-lite 补 inlineStr/十六进制引用 · "
                                        "T-3 更新 frontend/AGENTS.md · T-4 补导入跨语言 golden。"
                                        "理由：两项是「静默产生假数据」的缺陷，两项是防后续踩坑的基建，改动都不触及算法与口径。"],
        ["推荐批次 2（约 2h）", "T-5 年份常量化与跨年 · T-6 防回退守卫覆盖面 · T-7 存量影响值重算 · T-8 决策日志导出。"
                          "理由：都依赖批次 1 的测试基建来兜底；T-7/T-8 直接影响现场能不能用这些数据。"],
        ["推荐批次 3（需先决策）", "T-9 自定义日期格式对拍 · T-10 两个「新旧」判据统一。"
                            "理由：T-10 的修法取决于「是否引入 revision」这个架构选择，先定方向再动手。"],
        ["最该优先的一件", "T-1：浮精灰分单元格为空白或写「-」「？」时，解析结果是 0%，并且作为一条真实记录入库，"
                       "进而被 resolve_float_ash 取为最新值直接进入总灰分公式 —— 它会无声地拉低总灰分并影响密度建议，"
                       "而现场从界面上看不出来。"],
        ["流程提醒", "① 11 个提交（含 dff835b）尚未推送到 GitHub；② 有另一个 agent（glm-5.3）在本仓库并发提交，"
                   "我的 9 个文件改动曾被裹进它的提交，建议同一时间只让一个 agent 动这个仓库。"],
    ],
)

# ------------------------------------------------------------------ 2 待办清单
sheet(
    wb, "待办清单(含原因)",
    "可直接执行的待办清单（每项附原因）",
    ["编号", "优先级", "类型", "模块", "要做的事", "原因（为什么必须做）", "证据 / 位置", "具体做法", "预估", "前置依赖"],
    [8, 8, 12, 14, 34, 56, 40, 46, 10, 14],
    [
        ["T-1", "高", "缺陷修复", "导入解析",
         "浮精灰分/煤量单元格解析失败时返回 null，不再用 || 0 兜底",
         "空白或写「-」「？」的灰分单元格会变成 0%，并且作为一条真实记录 push 进 floatCoal；"
         "resolve_float_ash 取的是最新一条记录的灰分，于是 0% 会直接进入总精煤灰分公式，"
         "无声拉低总灰分并影响密度建议——现场从界面上完全看不出来。"
         "同一函数里紧邻的密度列做了 1.3~1.6 范围校验，灰分列却没有，属明显遗漏。",
         "frontend/js/import.js:716-717（float_ash 分支）\n同文件 :684-687（液位/水分/煤量同模式）\n后端对照 backend/app/services/resolvers.py resolve_float_ash",
         "解析失败返回 null（不参与该条记录的灰分字段），由 resolvers 的 _is_num 在取值链里丢弃；"
         "同时给该行记一条 skipped 以便导出错误记录时可见",
         "20 分钟", "无"],
        ["T-2", "高", "缺陷修复", "xlsx 读取",
         "xlsx-lite 补 inlineStr（<is><t>）分支，并把字符引用正则扩展到十六进制",
         "① t=\"inlineStr\" 的单元格（部分导出器与 WPS 会这样写）在浏览器读到 null，openpyxl 却能正常读到——"
         "同一文件在工作面列上「浏览器整列为空、后端有值」，属双轨分叉；"
         "② 本次只修了十进制 &#10;，十六进制 &#x0A; 仍留在文本里，导致「修了一半」。"
         "这两个缺口都在本次刚修过的同一段代码旁边，顺手补掉成本最低。",
         "frontend/js/xlsx-lite.js:215（只读 <v>，无 <is> 分支）\n同文件 :275（_decodeXml 只匹配 &#(\\d+);）\n"
         "对照 openpyxl worksheet/_reader.py 能读 inlineStr 且解码十六进制引用",
         "在单元格解析里补 <is><t>…</t></is> 分支并复用 _decodeXml；"
         "把正则改为 /&#(x?[0-9A-Fa-f]+);/g；"
         "同时删掉 import.js:439 与 importer.py 里多余的 &#10; replace，让「读取器负责解码」这一契约单一化",
         "40 分钟", "无"],
        ["T-3", "高", "文档", "工程规范",
         "更新 frontend/AGENTS.md：路径与工作目录规则",
         "该文件仍写着「本目录（D:\\Users\\liu\\Desktop\\测试\\web (2)）是唯一允许修改代码的地方」，"
         "而项目已整合到 D:\\dense-medium-density-control-system（backend/frontend/docs 同仓库）。"
         "任何读这份文件的 agent 都可能把改动写回桌面旧副本，或在错误的目录里找文件——"
         "这正是本次审查期间真实发生过的困惑来源。",
         "frontend/AGENTS.md 全文",
         "改为描述当前布局：三目录同仓库；后端改动落 backend/、前端落 frontend/、文档与 Excel 产出到 docs/；"
         "保留仍然有效的硬性规则「改 JS 必须同步递增 index.html 里的 ?v=N」",
         "15 分钟", "无"],
        ["T-4", "中", "测试基建", "导入解析",
         "新增 backend/scripts/dump_import_js.js，建立 parseCoarseFactors 的跨语言 golden",
         "brief / training / resolvers / seed 都有 dump_*_js.js 跨语言 golden，唯独导入解析没有——"
         "偏偏本次所有对拍风险都集中在这一块。本次修复只能靠我临时写的「从源码切片求值」脚本验证，"
         "这种脚本用完即弃、无法进 CI。补上之后，日期与列的任意一侧改动都会被立刻发现。",
         "对照 backend/scripts/dump_brief_js.js 的既有写法\n本次临时脚本已证明切片对拍可行（29 例日期 + 11 例序列）",
         "照 dump_brief_js.js 写 dump_import_js.js：向页面注入一组固定行，导出 parseCoarseFactors 结果；"
         "在 tests/test_importer.py 增加 golden 对拍用例",
         "50 分钟", "无"],
        ["T-5", "中", "健壮性", "导入解析",
         "年份常量化 + 跨年处理（JS 提同名 DEFAULT_YEAR，解析结果早于上一行时年份 +1）",
         "Python 用 DEFAULT_YEAR=2026，JS 在 3 处写字面量 2026——只改 Python 常量会静默破坏双轨对拍；"
         "日期辅助函数只返回 {m,d}，Excel 单元格里的真实年份被丢弃（2027-01-05 会变成 2026-01-05）；"
         "12.30 之后接 1.1 时候选月份不成立，会退回约一年前的时间戳。"
         "当前数据都在同一年内，所以是潜在缺陷而非现行故障——但一旦跨年就会一次性污染全部时间戳。",
         "backend/app/services/importer.py DEFAULT_YEAR 与 parse_ts\n"
         "frontend/js/import.js:396、:609、:613（字面量 2026）",
         "JS 侧提为与 Python 同名的 DEFAULT_YEAR 常量（三处共用）；"
         "日期辅助函数改为返回 {y,m,d}；解析结果早于上一行完整日期时把年份 +1；补 12月→1月 用例",
         "1 小时", "T-4"],
        ["T-6", "中", "健壮性", "同步守卫",
         "防回退守卫从「只比 coal_records」扩展为多业务表向量比较",
         "守卫目前只比 coal_records 总数：旧浏览器若该表数量相等或更多、但 calc_logs / heavy_samples 更少，"
         "仍会整体洗掉服务器侧的新补录与日志——守卫是「部分有效」，而它要防的正是这类结构性事故。"
         "先做多表向量比较成本低、不动架构；revision 方案留作后续彻底解。",
         "backend/app/services/migrate.py replace()（incoming/current 均只数 CoalRecord）",
         "统计各业务表数量做向量比较，任一表回退即拒绝并返回具体是哪张表；"
         "保留 force=true 语义不变；补一条「calc_logs 更少也应被拒」的用例",
         "40 分钟", "无"],
        ["T-7", "中", "数据治理", "口径一致性",
         "存量 influence_value 一次性重算迁移（__fixes.influence 标记）",
         "库里已有的浮精/粗精记录，其 influence_value 是用写死的重介灰分 8.50 算出来的，"
         "与现在的口径（502 在线 / 默认 7.9）不一致。显示层已经改为实时重算、不再读它，"
         "但这个字段仍会随整库镜像写回服务器并对外提供，留着旧口径值会持续误导下游使用它的人。",
         "frontend/js/import.js 与 collect.js 的历史写入值\n本次已将写入侧改为 App.getHeavyAsh()",
         "照 __fixes.julStray 的写法加一个一次性迁移：重算所有 floatCoal/coarseCoal 的 influence_value 并写回，"
         "记录条数到 importLogs",
         "40 分钟", "T-1（避免把 0% 灰分的记录一起重算成有意义的值）"],
        ["T-8", "中", "可用性", "K 标定",
         "决策日志导出（Excel/CSV）",
         "决策日志的设计目的就是给 K(工况) 标定提供「决策+响应」数据对，但现在只能通过 GET /api/v1/state "
         "看 JSON——现场人员拿不到、用不了，等于数据采了却无法进入标定流程。"
         "导出成表格是让这套观测闭环真正可用的最后一步。",
         "frontend/js/app.js densityDecisionLog（上限 200 条）\n"
         "查看方式见 app.js 顶部注释「GET /api/v1/state → densityDecisionLog」",
         "在合适的页面加入口，导出为表格：每行一条决策（时间/触发/方案/目标/ρ旧/ρ新/Δρ/ΔA/K 及来源/工况上下文/"
         "响应来源/滞后分钟/是否 invalidated），并用现有 XLSX.writeFile 落盘",
         "40 分钟", "无"],
        ["T-9", "低", "健壮性", "xlsx 读取",
         "xlsx-lite 支持自定义日期数字格式（与 openpyxl 同规则判定）",
         "浏览器只认内置 numFmtId（14-22/27-36/45-47/50-58），openpyxl 按格式字符串是否含 [dmhys] 判定。"
         "同一单元格 6.16 若套了自定义格式，JS 得到 6月16日、Python 得到 1月6日。"
         "当前两轨不会同时处理同一文件（浏览器自解析），所以不是现行故障；"
         "但它是「解析搬后端」这个既定计划的前置对拍缺口，不补就不能迁移。",
         "frontend/js/xlsx-lite.js:111-119\n对照 openpyxl styles/stylesheet.py is_date_format",
         "在 _readDateStyles 里解析 <numFmts> 段，用与 openpyxl 相同的 [dmhys] 规则判定；"
         "补一条自定义格式日期列的对拍用例",
         "1 小时", "T-4"],
        ["T-10", "低", "健壮性", "双轨同步",
         "统一「新旧」判据：让镜像守卫与自动拉取用同一个口径",
         "守卫按 coal_records 计数，autoPullIfStale 按 store 三个数组（含独立的 calc_logs 表）计数——"
         "两个判据不同，就可能出现「自动拉取判定服务器更新并覆盖本地」而「镜像被守卫拒绝」的组合，"
         "用户看到「导入完成」但数据没落库。修法取决于是否引入 revision，故排在决策之后。",
         "backend/app/services/migrate.py replace()\nfrontend/js/app.js autoPullIfStale()",
         "若采纳 revision：服务端自增计数 + 前端携带，守卫与拉取都按 revision 比较（「新者胜」）；"
         "若不采纳：把两侧计数口径统一为同一组表，并补一条端到端用例",
         "1 小时", "Q-8 决策"],
        ["T-11", "低", "交付流程", "版本管理",
         "推送 11 个未推送提交到 GitHub（origin/main）",
         "本地领先 origin/main 共 11 个提交（含本次审查与修复），长期不推会失去远端备份，"
         "也不便于在多台机器之间接力。推送前建议先确认拆分与提交信息（见 Q-1、Q-2）。",
         "git log origin/main..HEAD → 11 个提交",
         "确认后执行 git push origin main",
         "5 分钟", "Q-1 / Q-2 决策"],
    ],
    fill_col=2, fill_map={"高": HIGH_FILL, "中": MID_FILL, "低": LOW_FILL},
)

# ------------------------------------------------------------------ 3 待决策
sheet(
    wb, "待你决策的问题",
    "需要你（或现场）确认后才能动手的问题",
    ["编号", "优先级", "问题", "背景与原因", "选项", "我的建议"],
    [8, 8, 34, 60, 52, 46],
    [
        ["Q-1", "高", "11 个未推送提交是否现在推送到 GitHub？",
         "本地领先 origin/main 共 11 个提交（fe719e8 … dff835b）。推送后即对外可见，"
         "若还想调整提交结构（见 Q-2），应先在本地做完再推。",
         "① 现在就推 / ② 先调整提交结构再推 / ③ 暂不推送",
         "若你认可拆分（Q-2），先拆再推；否则直接推。"],
        ["Q-2", "中", "是否把 dff835b 拆成两个提交（docs 一个、代码修复一个）？",
         "该提交原本是另一个 agent 的「docs: 阶跃实验文档写回原文件名」，"
         "但它把我这次 9 个文件的代码改动一并提交了，我暂时只修正了提交信息。"
         "内容是对的、未推送，所以拆分成本低；不拆则历史里代码与文档混在一个提交。",
         "① 拆成两个提交（docs / review+fix） / ② 保持现在的一个提交",
         "建议拆分：本项目提交历史一直按「一件事一个提交」维护，混在一起会削弱可追溯性。"],
        ["Q-3", "中", "同一文件内出现重复时间戳时怎么处理？",
         "现在按时间戳逐条 filter 再 push，第二条会挤掉第一条（后者胜），但 imported 计数两条都算；"
         "最典型的触发是时间列空白——两轨都会落到 00:00:00，同一天多行最后只剩一行。"
         "数据库侧 UniqueConstraint(category,ts,system) 也无法表示重复，且 system 恒为「合并」。",
         "① 给重复时间戳排秒（+i 秒）全部保留 / ② 显式拒绝并计 skipped、导出错误记录 / ③ 维持后者胜但把计数改对",
         "建议 ②：三表是化验采样数据，同一时刻重复多半是源表问题，"
         "静默丢数据比报错更危险；现场能通过「导入错误记录」看到并修源表。"],
        ["Q-4", "中", "501 皮带灰分仪无在线值时，总灰分兜底取什么？",
         "「501 = 总混配皮带，其灰分仪读数即在线总灰分」这个口径是对的，但实测真实库 360 条灰分密度记录"
         "全部 belt=502、零条 501 —— 也就是说 ash_501 目前恒为默认常量 8.8，简报第 6 列恒显 8.80。"
         "改动前的 501/502 加权虽然口径不对，但至少用了真实的 502 读数。",
         "① 501 无值时改用 502 推算的总灰分 / ② 保持常量 8.8 但在界面上明确标注「默认常量」而非「仪表」 / ③ 其他口径",
         "建议 ①+②：兜底用 502 推算（保留真实信号），同时把层级文案标成「默认常量 8.8」，"
         "等 PLC 接入 501 后自动切换到直读。"],
        ["Q-5", "中", "是否在界面上展示 K 增益的三态（data / 数据不可辨识 / 样本不足）？",
         "实测数据驱动 K 的两个系统斜率均为负（A −0.0221、B −0.0196，R²≈0.05），被正性约束全部丢弃，"
         "因此永远回退 0.075、valid 恒为 false。这本身印证了「闭环数据不可辨识 K」的结论，"
         "但界面上看不出来，容易被误认为「已经用数据标定过了」。",
         "① 展示三态并给出 systems[].k 与 R²（让现场看到斜率是负的） / ② 只显示「不可辨识」 / ③ 不展示",
         "建议 ①：这不是技术细节而是判断依据——现场看到负斜率才会理解为什么要做阶跃实验。"],
        ["Q-6", "中", "简报在缺表时段的期望行为？",
         "现行规则是「三表在该小时内 24h 内均有记录才成行」，实测真实库因此得到 263 行（旧规则 1069 行是错位续传凑的）。"
         "但浮精每天只化验 1 次，所以只有每次采样后的 24h 内成行；浮精表 5-26~8-22 无数据，该段简报为空。",
         "① 维持严格 24h 三表齐全 / ② 缺表也出行，缺失列标注「缺」并用最近值 / ③ 时间窗口可配置（如 48h）",
         "建议先确认现场用途：若简报是给操作员看当班趋势，②更实用；"
         "若用于事后分析，①更干净。这条属需求变更，改之前要明确口径。"],
        ["Q-7", "低", "在界面上删除记录后，如何与服务器同步？",
         "删除后整库镜像会被防回退守卫拒绝（记录数变少），而下次开页 autoPullIfStale 又会把服务器数据拉回来，"
         "表现为「删了又回来」；用户从界面上看不到任何提示。",
         "① 删除类操作走 force=true 强制同步 / ② 被拒时给明确提示并提供「强制同步」按钮 / ③ 前端不允许删除，只允许作废标记",
         "建议 ②：force 全量覆盖风险偏高；明确提示 + 显式按钮既保留守卫又让用户能完成操作。"],
        ["Q-8", "低", "是否启动「把 Excel 解析搬到后端」的迁移？",
         "/import/parse 端点已存在但前端无任何调用者（全仓库只有文档生成脚本把它列为计划）。"
         "迁移的收益是解析逻辑单一化、可用 Python 测试与 golden 覆盖；"
         "风险是必须先补齐对拍，否则历史数据会整体错位。",
         "① 现在启动（先做 T-9 + T-4 再迁） / ② 暂缓，等 PLC 接入告一段落 / ③ 不迁移",
         "建议 ②：当前浏览器解析工作正常，而 PLC 接入才是影响生产的主线；"
         "等 T-9/T-4 落地后再迁移，风险与成本都最低。"],
    ],
    fill_col=2, fill_map={"高": HIGH_FILL, "中": MID_FILL, "低": LOW_FILL},
)

# ------------------------------------------------------------------ 4 暂缓
sheet(
    wb, "暂缓项与原因",
    "建议暂缓或明确不做的事项（附原因）",
    ["编号", "事项", "为什么暂缓", "什么条件下再启动"],
    [8, 30, 76, 52],
    [
        ["D-1", "把 Excel 解析整体搬到后端 /import/parse",
         "端点当前无调用者，浏览器自解析工作正常；而迁移前必须先补齐对拍（T-9 自定义格式、T-4 导入 golden），"
         "否则同一批历史文件在两轨会得到不同日期（已实测：自定义格式下 6.16 会变成 1月6日）。"
         "现在迁移等于在没护栏的情况下换掉正在用的轮子。",
         "T-9 与 T-4 完成、且 PLC 接入主线告一段落后（对应 Q-8）。"],
        ["D-2", "决策日志改为独立接口增量追加",
         "目前 200 条上限、约 80KB，随整库镜像一起 PUT 的代价可以接受；"
         "真正需要优化是在日志开始大量积累之后，现在做属于过早优化。",
         "决策日志接近 200 条上限、或整库镜像明显变慢时。"],
        ["D-3", "后端 _to_num 接受 \"inf\"/\"1e400\" 会生成 inf 并可能 500",
         "受影响的只有 /import/parse，而该端点无调用者（同 D-1）；"
         "修它属于迁移准备工作的一部分，单独修没有收益。",
         "随 D-1 的后端解析迁移一起处理。"],
        ["D-4", "引入 revision 让「新者胜」（彻底替换「多者胜」）",
         "这是比 T-6 更彻底的解法，但需要同时改后端表结构、PUT /state 契约与前端判据，"
         "属架构级改动；而 T-6 的向量比较已能覆盖当前已知的洗库通道。先用低成本方案堵住已知缺口，"
         "等实测再出现「数量多但内容旧」的真实案例再上 revision。",
         "T-6 上线后仍出现误判，或需要多端并发写入时。"],
    ],
    fill_col=1,
)

DOCS.mkdir(parents=True, exist_ok=True)
wb.save(OUT)
print("已生成:", OUT)
for ws in wb.worksheets:
    print("  - %-20s %d 行" % (ws.title, ws.max_row - 3))
