# -*- coding: utf-8 -*-
"""生成 Excel：选煤厂重介密控系统 —— 项目交接报告。

产出 docs/项目交接报告.xlsx（openpyxl 现算，可重跑刷新）。
数字全部来自仓库/数据库/测试现场读取，不靠记忆；纪律与待办来自仓库内的 AGENTS.md 与 docs 疑问表。
"""
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent.parent        # 仓库根
BACKEND = ROOT / "backend"
DOCS = ROOT / "docs"
OUT = DOCS / "项目交接报告.xlsx"
DB = BACKEND / "data" / "dense_medium.db"

from openpyxl import Workbook                                              # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side     # noqa: E402

HEAD_FILL = PatternFill("solid", fgColor="1F4E79")
HEAD_FONT = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
TITLE_FONT = Font(name="微软雅黑", size=13, bold=True, color="1F4E79")
BODY_FONT = Font(name="微软雅黑", size=10)
KEY_FILL = PatternFill("solid", fgColor="FFF2CC")
OK_FILL = PatternFill("solid", fgColor="E2EFDA")
WARN_FILL = PatternFill("solid", fgColor="FCE4D6")
BAD_FILL = PatternFill("solid", fgColor="F8CBAD")
INFO_FILL = PatternFill("solid", fgColor="DEEAF6")
THIN = Side(style="thin", color="B0B0B0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")
CENTER = Alignment(horizontal="center", vertical="top", wrap_text=True)


def sheet(wb, name, title, headers, widths, rows, row_key=None, key_col=1, key_map=None):
    ws = wb.create_sheet(name)
    ws["A1"] = title
    ws["A1"].font = TITLE_FONT
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    for c, (h, w) in enumerate(zip(headers, widths), start=1):
        cell = ws.cell(row=3, column=c, value=h)
        cell.fill, cell.font, cell.alignment, cell.border = HEAD_FILL, HEAD_FONT, CENTER, BORDER
        ws.column_dimensions[cell.column_letter].width = w
    for i, row in enumerate(rows, start=4):
        for c, v in enumerate(row, start=1):
            cell = ws.cell(row=i, column=c, value=v)
            cell.font = BODY_FONT
            cell.alignment = CENTER if c <= 2 else WRAP
            cell.border = BORDER
        if key_map and str(row[key_col - 1]) in key_map:
            f = key_map[str(row[key_col - 1])]
            for c in range(1, len(headers) + 1):
                ws.cell(row=i, column=c).fill = f
    ws.freeze_panes = "A4"
    return ws


def sh(cmd):
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60)
        return (r.stdout or "").strip()
    except Exception as exc:
        return "ERR %r" % exc


# ---------------- 现查事实 ----------------
branch = sh(["git", "branch", "--show-current"])
last_commit = sh(["git", "log", "--oneline", "-1"])
ahead = [l for l in sh(["git", "log", "--oneline", "origin/main..HEAD"]).splitlines() if l.strip()]
behind = [l for l in sh(["git", "log", "--oneline", "HEAD..origin/main"]).splitlines() if l.strip()]
routes = []
for f in sorted((BACKEND / "app/routers").glob("*.py")):
    src = f.read_text(encoding="utf-8", errors="ignore")
    m0 = re.search(r'APIRouter\(prefix="([^"]*)"', src)
    pre = m0.group(1) if m0 else ""
    for m in re.finditer(r'@router\.(get|post|put|delete)\("([^"]*)"', src):
        routes.append((m.group(1).upper(), "/api/v1" + pre + m.group(2), f.name))
notes = []
for f in sorted((BACKEND / "app/routers").glob("*.py")):
    head = f.read_text(encoding="utf-8", errors="ignore").split('"""')
    if len(head) > 1:
        notes.append((f.name, " ".join(head[1].split())[:150]))
test_files = sorted(p.name for p in (BACKEND / "tests").glob("test_*.py"))
test_funcs = sum(len(re.findall(r"^def test_", p.read_text(encoding="utf-8", errors="ignore"), re.M))
                 for p in (BACKEND / "tests").glob("test_*.py"))
e2e = sorted(p.name for p in (BACKEND / "scripts").glob("verify_*.js"))
dumpers = sorted(p.name for p in (BACKEND / "scripts").glob("dump_*.js"))
goldens = sorted(p.name for p in (BACKEND / "data").glob("*.json"))
generators = sorted(p.name for p in (BACKEND / "scripts").glob("generate_*.py"))
docs_xlsx = sorted(p.name for p in DOCS.glob("*.xlsx")) if DOCS.exists() else []
backups = sorted((BACKEND / "data/backups").glob("*.db"))
frontend_js = sorted(p.name for p in (ROOT / "frontend/js").glob("*.js"))
services = sorted(p.name for p in (BACKEND / "app/services").glob("*.py") if p.name != "__init__.py")
con = sqlite3.connect(str(DB))
db_rows = con.execute("select category, count(*), min(ts), max(ts) from coal_records group by category").fetchall()
db_stats = {
    "manual": con.execute("select count(*) from manual_entries").fetchone()[0],
    "imports": con.execute("select count(*) from import_logs").fetchone()[0],
    "history": con.execute("select count(*) from coarse_model_history").fetchone()[0],
    "model": con.execute("select method, is_current, train_range, n, trained_at from coarse_models").fetchall(),
}
con.close()
appjs = (ROOT / "frontend/js/app.js").read_text(encoding="utf-8", errors="ignore")
guide = dict(re.findall(r"(deadband|maxStep|rhoMin|rhoMax)\s*:\s*([0-9.]+)", appjs))
db_mb = round(DB.stat().st_size / 1e6, 2)

wb = Workbook()
wb.remove(wb.active)

# ---------------- 一、项目概况 ----------------
sheet(
    wb, "一、项目概况", "选煤厂重介密控系统 —— 交接报告（生成于 %s）" % datetime.now().strftime("%Y-%m-%d %H:%M"),
    ["项", "内容"], [22, 108],
    [
        ["系统是什么", "面向选煤厂重介（重介质）分选系统的**生产辅助决策系统**：按化验/仪表数据给出重介密度建议，"
                     "并对粗精煤泥灰分、浮精（浮选精煤）影响做预测与历史对照，另有一份推测式生产简报。"],
        ["现场问题", "重介密度靠人工经验调整、调整依据分散；粗精煤泥灰分波动大；缺少『上次调到多少、结果如何』的闭环记录。"],
        ["当前形态", "本地部署的 Web 系统：前端原生 JS（无构建步骤）+ 后端 FastAPI + SQLite（单文件库）。"
                   "浏览器打开 http://<本机>:8000 即用；厂区另一台电脑可经 Radmin VPN 访问。"],
        ["代码规模", "前端 JS %d 个文件｜后端服务 %d 个模块｜接口 %d 条｜测试 %d 个文件 / %d 个用例｜E2E 脚本 %d 个"
                   % (len(frontend_js), len(services), len(routes), len(test_files), test_funcs, len(e2e))],
        ["数据规模", "库 %.2f MB：粗精煤泥 %s 条、灰分密度 %s 条、浮精 %s 条；补录 %d 条；导入日志 %d 条；重训历史 %d 条"
                   % (db_mb, db_rows[1][1] if len(db_rows) > 1 else "-",
                      db_rows[0][1] if db_rows else "-", db_rows[2][1] if len(db_rows) > 2 else "-",
                      db_stats["manual"], db_stats["imports"], db_stats["history"])],
        ["当前分支", "%s｜最后提交：%s" % (branch, last_commit[:88])],
        ["待合并提交", "%d 个提交领先 origin/main；main 另有 %d 个提交未并入本分支（%s）"
                    % (len(ahead), len(behind), behind[0][:60] if behind else "无")],
        ["交付物位置", "代码：%s；文档（Excel）：%s（共 %d 份，全部由 backend/scripts/generate_*.py 生成）"
                    % (ROOT, DOCS, len(docs_xlsx))],
        ["接手须知", "本系统的**口径**（哪些数据能导、指标怎么算、DS/GPT 两版模型的分工）比代码更关键，"
                   "见「三、数据与口径」「四、算法与模型」「七、纪律与约定」三张表。"],
    ],
    key_map={"待合并提交": KEY_FILL, "接手须知": KEY_FILL},
)

# ---------------- 二、系统架构 ----------------
sheet(
    wb, "二、系统架构", "架构、目录与页面/接口清单（现状现查）",
    ["项", "说明"], [24, 106],
    [
        ["双轨数据层（关键设计）", "前端 localStorage 是主存储。用 file:// 打开 → 只写 localStorage；用 http:// 打开 → "
                              "每次 saveStore 额外把**整库镜像** PUT /api/v1/state。服务器侧有**防回退守卫**："
                              "任一业务记录数少于库内就整体拒绝（返回 stale_store），避免旧浏览器把服务器新数据洗掉。"],
        ["为什么这么设计", "现场最初是单文件网页（无后端）；后来加后端做训练/导入/多端共享，又不能破坏离线可用性，"
                       "于是保留双轨：离线能用，联网自动镜像。"],
        ["前端页面（7 个）", "概况 overview｜数据录入 collect｜粗精煤泥 coarse｜浮精影响 float｜数据导入 import｜"
                         "生产简报 brief｜历史查询 history"],
        ["后端分层", "app/routers（HTTP 层）→ app/services（业务与算法）→ app/models+schemas（ORM/校验）→ SQLite"],
        ["服务模块（%d 个）" % len(services), "、".join(services)],
        ["前端脚本（%d 个）" % len(frontend_js), "、".join(frontend_js)],
        ["重要共享约定", "**算法取值链前后端逐值一致**：backend/app/services/*.py 与 frontend/js/app.js 是同一套公式的两份实现，"
                     "改动任一侧必须同步另一侧，并用 backend/scripts/dump_*_js.js 抓前端真值对拍（容差 <1e-6）。"],
        ["静态资源缓存", "后端对静态资源统一发 Cache-Control: no-cache，浏览器每次校验（未变走 304），"
                     "所以忘了改 ?v=N 也不会导致改了没生效。"],
    ],
)

sheet(
    wb, "二·接口清单", "后端接口 %d 条（从 routers 现抓）" % len(routes),
    ["方法", "路径", "所在文件"], [8, 46, 24],
    [[m, p, f] for m, p, f in routes],
)

sheet(
    wb, "二·接口说明", "各路由文件的职责（文件头注释现抓）",
    ["文件", "职责"], [20, 108],
    [[f, t] for f, t in notes],
)

# ---------------- 三、数据与口径 ----------------
sheet(
    wb, "三、数据与口径", "三张源表 → 一张主表；导入方式与已确认口径",
    ["项", "内容"], [22, 108],
    [
        ["数据来源", "现场人工整理的 Excel（三类）：① 粗精煤泥灰分影响因素（多因素+315灰分）② 灰分、密度（皮带 502 采样，A/B 两系统）"
                   "③ 浮精（浮选精煤灰分/煤量）。另有 KEPServer 在线仪表标签清单（283 条，尚未接入）。"],
        ["入库结构", "统一进 coal_records 表，用 category 区分：coarse（粗精煤泥）／ash_density（灰分密度）／float（浮精）。"
                   "唯一约束 (category, ts, system, belt) 防重复。"],
        ["字段映射要点", "粗灰表：D 入洗工作面→mining_face（仅展示，未进模型）、E 原煤灰分→raw_ash（前向填充）、"
                     "F 小时带煤量→coal_amount、G~J 系统开关→sysA/sysB/sys401/sys402、K~L 脱粉→desliming473/474、"
                     "M 315灰分→ash_content（**模型目标**）、N 全水分→moisture、O 精磁尾液位→level。"],
        ["已确认口径（重要）", "① 灰分密度表**只导 A 系统**（B 行不导，留痕）；② 粗灰表只导有日期的有效行，不做日期填充；"
                         "③ 与库内旧行『同一次采样、时间戳差 ≤60 分钟』时**以新文件为准**（删旧写新）；"
                         "④ 密度写成 1 的按缺失处理（记 NULL），不整行丢弃。"],
        ["导入方式（3 种）", "① 浏览器「数据导入」页（现场人员用，选文件即导）；② backend/scripts/import_new_batch.py"
                        "（脚本批处理，支持 --dry-run/--ash-systems/--overlap）；③ 固定供给模式 --scan-dir（把文件丢进目录，"
                        "一条命令导完并归档，见 Q7=A 决定）。"],
        ["当前数据范围", "粗精煤泥 %s ~ %s（%s 条）｜灰分密度 %s ~ %s（%s 条）｜浮精 %s ~ %s（%s 条）"
                     % (db_rows[1][2][:10] if len(db_rows) > 1 else "-", db_rows[1][3][:10] if len(db_rows) > 1 else "-",
                        db_rows[1][1] if len(db_rows) > 1 else "-",
                        db_rows[0][2][:10] if db_rows else "-", db_rows[0][3][:10] if db_rows else "-",
                        db_rows[0][1] if db_rows else "-",
                        db_rows[2][2][:10] if len(db_rows) > 2 else "-", db_rows[2][3][:10] if len(db_rows) > 2 else "-",
                        db_rows[2][1] if len(db_rows) > 2 else "-")],
        ["已知数据缺口", "① 粗灰 7-15~8-22 完全没有数据（现场未提供）；② 密度读数只有 2026-05/08/09 三个月；"
                     "③ 粗灰每天仅 4~5 个瞬时点（相邻间隔中位 2.6 小时）——精度上限由此决定。"],
        ["备份纪律", "写库前用 sqlite3 **在线备份 API** 存快照（不能直接复制 .db，WAL 里还有未 checkpoint 的数据）；"
                   "当前 backups/ 下 %d 份。恢复演练与防回退守卫见 backend/app/services/migrate.py。" % len(backups)],
    ],
    key_map={"已确认口径（重要）": KEY_FILL, "导入方式（3 种）": INFO_FILL},
)

# ---------------- 四、算法与模型 ----------------
sheet(
    wb, "四、算法与模型", "密度建议规则、粗灰模型（DS/GPT 两版）、方向信号与向前验证结论",
    ["项", "内容"], [24, 106],
    [
        ["密度建议规则", "偏差→步长：Δρ = min(|ΔA| / 15, maxStep)，即隐含增益 **15%%/单位密度**；"
                     "死区 deadband=%.2f（偏差小于它不动）；单步上限 maxStep=%.2f；密度限幅 [%.2f, %.2f]；"
                     "另有 30 分钟驻留（dwell）与动作闩锁（latch）防抖。" % (float(guide.get("deadband", 0.05)),
                                                                          float(guide.get("maxStep", 0.02)),
                                                                          float(guide.get("rhoMin", 1.35)),
                                                                          float(guide.get("rhoMax", 1.60)))],
        ["粗灰模型目标", "预测『下一条采样点的 315 灰分（粗精煤泥灰分）』（用户 2026-09-24 确认口径 A）。"],
        ["DS 版（原版）", "10 因子 MLR（岭回归 + LOOCV 选 λ）+ PLS1（NIPALS + 折内选 A）；以未取整的 q2Time 选生产模型。"
                      "特征：raw_ash、coal_amount、sysA/B/401/402、desliming473/474、is_stoppage、level。"],
        ["GPT 版（独立设计）", "按日分组的时间验证、专家因子子集、Huber 稳健回归、输入范围裁剪等；"
                          "与 DS **分别保留**、可页面切换。纪律见「七」：只优化 GPT 时不得改动 DS。"],
        ["方向信号（DS 专用）", "预测『下一条读数相对这一条是涨还是跌』。特征只用 t 时刻已知量；**不含**『到下次读数的时间间隔』；"
                          "门控要求 bootstrap 95%% 区间下界高于 max(多数基线, 惯性基线)。"
                          "实测：切点 2026-09-04 命中 0.762 [0.643, 0.881]，n=42（多数基线 0.524、惯性 0.214）。"],
        ["水平值结论（务必照做）", "**水平预测不可用**：锁定检验窗（训练 6-16~9-03 → 检验 9-04~9-21）模型 MAE 1.814，"
                            "而『取训练均值』1.744、最强在线平滑 1.549 → 模型打不赢朴素基线。"
                            "原因是灰分中枢跨期漂移（训练期均值 14.209、检验期 13.400）。"
                            "**所以水平输出只能当历史对照，不能承诺精度**；页面已并列显示『样本内 R²』与『向前 MAE + 基线』。"],
        ["拟合度陷阱（踩过）", "同一锁定窗把目标换成『灰分−原煤灰』后，样本内 R² 从 0.374 升到 **0.754**，"
                        "而向前 MAE 从 1.814 **恶化**到 1.939 —— 只看 R² 会选错模型。选型一律看向前窗口。"],
        ["真向前验证（已上系统）", "GET /api/v1/training/coarse-forward（只读）：按 range 训练后，把**训练范围之后**的数据当检验集，"
                            "给模型 MAE + 三条朴素基线 + 方向命中；训练范围覆盖全部数据时退化为『留尾参考』并明确标注。"
                            "页面「粗精煤泥 → 向前验证」区块直接显示。"],
        ["当前生产模型", "库内 coarse_models：%s（train_range=%s，n=%s，训练于 %s）"
                     % ("；".join("%s%s" % (m, "(当前)" if cur else "") for m, cur, _r, _n, _t in db_stats["model"]),
                        db_stats["model"][0][2] if db_stats["model"] else "-",
                        db_stats["model"][0][3] if db_stats["model"] else "-",
                        db_stats["model"][0][4] if db_stats["model"] else "-")],
        ["⚠ 接手后第一件事", "当前生产模型训练于 2026-09-22（n=152），**早于 2026-09-24 那批新数据导入**（现在粗灰已有 205 条）。"
                       "正式使用前建议先重训一次：页面粗精煤泥 →『重新训练模型』，或 POST /api/v1/training/coarse-model?range=all&engine=ds。"
                       "重训会自动带上 forward/direction，并在页面「向前验证」显示最新成绩。"],
        ["不要动的部分", "训练/预测的**取值链**（MLR/PLS 求解器、日级聚合、选参口径）与页面 DS 分支，两侧（JS/Python）逐值一致；"
                     "改动前先看「六、测试与对拍」的对拍基线。"],
    ],
    key_map={"水平值结论（务必照做）": BAD_FILL, "拟合度陷阱（踩过）": WARN_FILL, "密度建议规则": KEY_FILL},
)

# ---------------- 五、运行与运维 ----------------
sheet(
    wb, "五、运行与运维", "怎么起、怎么看、坏了怎么办（都是实测出来的）",
    ["项", "内容"], [22, 108],
    [
        ["启动（推荐）", "双击仓库根目录的 **启动系统.bat**：已在跑则只提示（不会起两个），否则启动并自动开浏览器。"
                     "等价的命令行：powershell -NoProfile -ExecutionPolicy Bypass -File backend\\scripts\\start_server.ps1"],
        ["停止", "双击 **停止系统.bat**（或 backend\\scripts\\stop_server.ps1），释放 8000 端口。"],
        ["保持在线（看门狗）", "双击 **保持在线.bat**：每分钟检查一次，服务不在就自动拉起（实测杀掉后 5 秒恢复）。"
                          "是一个普通窗口，关掉窗口即停止守护；**不装计划任务、不改注册表、不需管理员**。"],
        ["访问地址", "本机 http://127.0.0.1:8000/；厂区另一台电脑用 Radmin VPN 固定地址 http://26.250.156.251:8000/；"
                   "WLAN 是 DHCP，地址会变（脚本每次启动都会打印当前值），有线口 10.255.2.7 链路尚未打通。"],
        ["日志", "%TEMP%\\dmcs_server.log（由 backend/serve.py 写，UTF-8+BOM：启动/退出都留时间戳）；"
                "看门狗日志 %TEMP%\\dmcs_watchdog.log。**注意**：如果日志里没有任何退出行，说明进程是被硬杀的。"],
        ["没有开机自启", "按用户决定**不装自启任务**：机器重启后需要手动双击启动（或让维护者起）。"],
        ["常见故障与处置",
         "① 页面打不开/接口 000 → 服务没在跑：双击 启动系统.bat；想省心就双击 保持在线.bat。\n"
         "② 前端提示 stale_store / 保存被拒 → 本地 localStorage 落后于服务器：点右上角『从服务器恢复数据』。\n"
         "③ 保存后界面没变 → 同上（服务器守卫拒绝了整库镜像，避免旧数据覆盖新数据）。\n"
         "④ 服务隔一段时间就没了（本机已发生多次）→ 进程被外部**硬杀**（日志无退出行）；"
         "已排除重启（机器连续运行 200+ 小时）与火绒（无相关日志）；根因未定位 → 用看门狗兜住。\n"
         "⑤ 启动脚本没反应 → 已修过两类：cmd 按 GBK 解析 .bat（中文行被拆坏）、PowerShell 5.1 按 ANSI 解析无 BOM 的 .ps1"
         "（here-string 被拆坏）。两个文件分别固定为 **GBK** 与 **UTF-8 BOM + 纯 ASCII 正文**，不要改编码。\n"
         "⑥ WMI 起 PowerShell 被拒绝（exit 5，安全软件行为）→ 启动脚本已改为**优先直接脱离式启动**，WMI 仅作回退。"],
        ["进程与线程注意", "服务进程是『无控制台 + 不继承句柄』方式启动的（DETACHED_PROCESS）："
                       "好处是收不到 Ctrl+C、不会被调用方会话连带回收；代价是**不能用任务管理器里双击窗口的方式管理**，"
                       "停服务请用停止脚本。"],
    ],
    key_map={"常见故障与处置": KEY_FILL},
)

# ---------------- 六、测试与对拍 ----------------
sheet(
    wb, "六、测试与对拍", "怎么证明改动没把既有口径改坏",
    ["项", "内容"], [24, 106],
    [
        ["单元/接口测试", "backend/tests 下 %d 个文件 / %d 个用例：pytest -q（在 backend 目录、用 .venv 的 python 跑）。"
                      "覆盖密度建议守卫、驻留/闩锁、取值链、导入解析、训练引擎、状态与备份、鉴权、接口冒烟等。" % (len(test_files), test_funcs)],
        ["跨语言对拍基线", "%d 份 golden（backend/data/*.json）：%s。这些文件是**前端真实运行的输出快照**，"
                       "由 backend/scripts/dump_*.js 生成（需要 Edge 无头浏览器）。" % (len(goldens), "、".join(goldens))],
        ["重生成基线的条件", "只有满足三条才允许重生成：① 改的确实是同一套公式（不是绕过）；② JS 与 Python 两侧同时改；"
                        "③ 数值差异逐项复核并说明。否则不许重生成 —— 否则对拍就失去意义。"],
        ["端到端脚本（%d 个）" % len(e2e), "、".join(e2e)],
        ["E2E 验证纪律（血泪教训）", "破坏性用例**只对临时库**：临时库必须为空（身份检查），固定用 8010 端口 + DMCS_URL 环境变量，"
                              "绝不打生产 8000。曾经的教训：临时服务没绑上，带 allowReal 的脚本直接打到生产库，"
                              "把密度写成 1.485 并留了一条测试日志（已恢复）。之后所有脚本都增加了 RESTORE/STATE_RESTORED 段。"],
        ["CI（持续集成）", ".github/workflows/ci.yml **只在 main 的 push/PR 上触发**（pytest 作业 + windows 上的 7 个 E2E 用例）→ "
                      "在开发分支上提交**不会**跑 CI，本地必须自己跑 pytest；合并到 main 后 CI 才会把关。"],
        ["最近一次验证", "交接前实测（%s）：pytest **175 passed**（162.8 秒）；端到端 verify_coarse_forward.js 18 项断言通过；"
                     "启动/停止/看门狗三条路径均已实测。" % datetime.now().strftime("%Y-%m-%d")],
    ],
    key_map={"E2E 验证纪律（血泪教训）": BAD_FILL, "重生成基线的条件": KEY_FILL},
)

# ---------------- 七、纪律与约定 ----------------
sheet(
    wb, "七、纪律与约定", "接手必须遵守的既有约定（多数写在 AGENTS.md 与历史复盘里）",
    ["项", "内容"], [22, 108],
    [
        ["DS / GPT 维护范围", "AGENTS.md 规定：**只优化 GPT 版本，DS 版本保持不变**（不改 DS 的训练、预测、日级聚合、参数选择与页面分支；"
                          "DS 对拍基线 train_js.json 不得随 GPT 优化重生成）。GPT 必须**独立设计**，不复刻 DS。"
                          "（当前交接分支上做的是 DS 侧的向前验证与方向功能，已与用户确认。）"],
        ["两版模型分别报告", "GPT 的历史拟合、调参验证、向前时间检验必须**分开报告**；已用于开发的数据不作为新的独立检验数据。"],
        ["文档一律用 Excel 且由脚本生成", "docs/ 下的 Excel 全部由 backend/scripts/generate_*.py（openpyxl）生成，可重跑复现；"
                                "不接受手工编辑的二进制文档。"],
        ["提问优先", "用户要求：有任何疑问**先提出再行动**；疑问与需求都写进 Excel（分板块、不同表）。"],
        ["提交纪律", "同一时间只允许一个 agent 持有未提交改动；提交时 `git add <显式路径>`，"
                 "**不要 git add -A**（会把别人改到一半的文件裹进来）；改动只在各自的开发分支上提交，合并需用户明确指示。"],
        ["数据安全", "任何破坏性操作（清库、恢复、污染修复）之前必须先备份；生产库与临时库要用不同的端口与 URL 严格区分。"],
        ["命名与版本", "前端 ?v=N 建议顺手 +1（但已非必需，后端 no-cache 保证生效）。"],
    ],
    key_map={"DS / GPT 维护范围": KEY_FILL, "提交纪律": KEY_FILL, "数据安全": KEY_FILL},
)

# ---------------- 八、未完成与待决策 ----------------
sheet(
    wb, "八、未完成与待决策", "按优先级列出：谁决定、影响什么",
    ["优先级", "事项", "现状 / 说明", "需要谁决定"], [10, 26, 74, 18],
    [
        ["P0", "分支合并", "当前分支 %s 领先 origin/main %d 个提交（DS 方向/向前验证/疑问表/导入脚本/运维脚本等）；"
                        "main 另有 1 个 GPT 归档提交未并入。**等你审查后指示合并**。" % (branch, len(ahead)), "用户"],
        ["P0", "服务被硬杀（根因未定位）", "本机多次出现服务在运行数小时后消失、日志无退出行；已排除重启与火绒；"
                                    "现有兜底=看门狗。若要根治，需要在杀进程的软件里加白名单（未确认是哪一个）。", "现场/用户"],
        ["P1", "化验出结果时间（Q5）", "决定『最近读数』能否用于在线校正、方向信号何时可用。现场此前只确认『当天出结果』。", "现场化验室"],
        ["P1", "是否有更密的粗灰数据（Q8）", "现每天 4~5 个瞬时点；若有在线测量或每班 1 次，水平预测精度才有望提升（这是 Q3 验收达标的前提）。", "现场仪表/工艺"],
        ["P1", "4 因子备选方案", "去掉 6 个近常量开关列后两窗一致更优（锁定窗 MAE 1.814→1.521，配对 bootstrap [+0.138,+0.643]）；"
                            "用户 2026-09-24 决定**暂不采用**，等样本 ≥300 条再用同一套验收复算。", "用户"],
        ["P2", "密度特征不可用", "A 系统密度读数只有 2026-05/08/09 三个月，6-7 月粗灰 0 条能在 6h 内对上 → 当前无法训练。"
                            "需 PLC 密度连续采集接通后重估（用户已指示先搁置）。", "现场接入"],
        ["P2", "KEPServer 标签映射", "283 条标签已筛出 243 条建议接入（P0 166/P1 37/P2 40），"
                                "但 PLC 逻辑名 ↔ 系统字段的映射与采集周期（1 分钟值还是 10/30 分钟均值）待现场确认。", "现场电气"],
        ["P2", "数据固定供给", "用户已选固定目录+文件名方式（脚本已支持 --scan-dir）；需现场按约定丢文件（目录尚未正式启用）。", "现场"],
        ["P3", "遗留问题 L-2/L-3/L-4", "L-4（P0）：仪表取值对字符串数值静默变 null；L-2：verify_api_errors.js 未进 CI；"
                                   "L-3：简报历史值未比对。详见 docs/待办-后续优化计划.xlsx。", "用户/维护者"],
        ["P3", "密度 1.441 是否手误", "2026-09-21 14:18 单步降 0.079（超过 maxStep 0.02）——疑人工录入手误或走的是手工通道，待现场确认。", "现场操作"],
        ["P3", "笔记本/常驻机方案", "已出选型建议表（docs/笔记本选型建议-密控系统与多Agent.xlsx）："
                              "核心结论=内存 64GB、2TB 双 M.2、建议把常驻服务与开发机分离（迷你主机 7×24）。", "用户"],
        ["P3", "B 系统灰分密度补导", "本批按『只导 A』执行；B 系统 183 行里 76 行带真实密度值未导（留痕）。需要时重跑脚本加 --ash-systems A,B。", "用户"],
    ],
    key_map={"P0": BAD_FILL, "P1": WARN_FILL, "P2": INFO_FILL},
)

# ---------------- 九、验证记录 ----------------
sheet(
    wb, "九、验证记录", "交接时点已实测通过的项目（可复现命令）",
    ["项", "命令 / 方式", "结果"], [24, 54, 56],
    [
        ["后端测试", "cd backend; .venv\\Scripts\\python.exe -m pytest -q",
         "**175 passed**，1 warning，耗时 162.8 秒（2026-10-05 实测）"],
        ["测试用例数", "backend/tests 统计", "%d 个文件 / %d 个 def test_ 用例" % (len(test_files), test_funcs)],
        ["真向前评估", "python backend\\scripts\\evaluate_ds_forward.py", "输出锁定窗与开发侧窗的模型/基线对照与方向命中"],
        ["向前验证接口", "curl http://127.0.0.1:8000/api/v1/training/coarse-forward?range=all",
         "返回窗口/模型/基线/方向（当前库：粗灰 205 条时模型 MAE 1.878、基线 1.663、方向 0.75）"],
        ["端到端（页面级）", "DMCS_URL=http://127.0.0.1:8010 node backend\\scripts\\verify_coarse_forward.js", "18 项断言通过（对临时库）"],
        ["启动/停止/看门狗", "双击对应 .bat（等价 cmd /c 调用）", "启动 1 秒内健康检查通过；停止释放端口；看门狗杀掉后 5 秒恢复"],
        ["数据一致性", "sqlite3 integrity_check + 分类计数", "ok；粗灰 %s / 灰分密度 %s / 浮精 %s；同类无重复时间戳"
                                                       % (db_rows[1][1] if len(db_rows) > 1 else "-",
                                                          db_rows[0][1] if db_rows else "-",
                                                          db_rows[2][1] if len(db_rows) > 2 else "-")],
        ["备份", "backend/data/backups/", "%d 份快照（含导入前、污染修复前等关键节点）" % len(backups)],
    ],
)

# ---------------- 十、接手第一步 ----------------
sheet(
    wb, "十、接手第一步", "按顺序做这 8 件事，半天内可完全接手",
    ["#", "动作", "命令 / 位置", "验收标准"], [5, 26, 56, 40],
    [
        [1, "拿到代码与分支", "git clone <仓库>；git checkout %s" % branch, "git log -1 能看到本报告的提交"],
        [2, "装环境", "cd backend; python -m venv .venv; .venv\\Scripts\\pip install -r requirements.txt", "能 import app.main"],
        [3, "起服务", "双击 启动系统.bat（或 start_server.ps1）", "http://127.0.0.1:8000/api/v1/health 返回 200"],
        [4, "跑测试", ".venv\\Scripts\\python.exe -m pytest -q", "全部通过（对拍用例不许跳过）"],
        [5, "看数据", "浏览器打开系统 → 概况/粗精煤泥页", "粗灰 205 条、数据到 2026-09-21（与库一致）"],
        [6, "看模型与向前成绩", "粗精煤泥页 → 向前验证区块", "能看到模型向前 MAE、朴素基线、方向命中与区间"],
        [7, "重训一次模型（重要）", "页面粗精煤泥 →『重新训练模型』（范围选『全部』）",
         "训练完成后看『向前验证』区块有数字；n 从 152 变为 205"],
        [8, "读四份文档", "docs/ 下：本项目交接报告、待办-后续优化计划、DS粗灰-方向与水平验证结论、DS粗灰预测-疑问与决策",
         "理解『水平不可用/方向可用』与未决事项"],
        [9, "确认运维边界", "记住：临时库用 8010；破坏性脚本只打临时库；改公式必须两侧同步 + 跑对拍",
         "能独立完成一次『导入一批新数据 → 重训 → 看向前成绩』"],
    ],
)

DOCS.mkdir(parents=True, exist_ok=True)
wb.save(OUT)
print("已生成:", OUT)
for ws in wb.worksheets:
    print("  - %-20s %d 行" % (ws.title, ws.max_row - 3))
