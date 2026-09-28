# -*- coding: utf-8 -*-
"""生成 Excel：换笔记本的选型建议（重介密控系统 + KEPServer 时序数据 + 多 Agent 全栈开发）。

产出 docs/笔记本选型建议-密控系统与多Agent.xlsx
口径：能用实测就用实测（本机规格、项目占用、KEPServer 标签构成、数据量测算都在脚本里现算）；
      **机型只给"该按什么规格挑"，不编造在售型号与价格**（生成时本机外网不通，无法核实时价）。
"""
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from openpyxl import Workbook, load_workbook                                 # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side       # noqa: E402

DOCS = ROOT.parent / "docs"
OUT = DOCS / "笔记本选型建议-密控系统与多Agent.xlsx"
DB = ROOT / "data" / "dense_medium.db"
TAGS_XLSX = DOCS / "KEPServer标签分类-瞬时累计周期.xlsx"
PICK_XLSX = DOCS / "KEPServer标签筛选-重介密控需接入.xlsx"

HEAD_FILL = PatternFill("solid", fgColor="1F4E79")
HEAD_FONT = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
TITLE_FONT = Font(name="微软雅黑", size=13, bold=True, color="1F4E79")
BODY_FONT = Font(name="微软雅黑", size=10)
KEY_FILL = PatternFill("solid", fgColor="FFF2CC")     # 关键要求
OK_FILL = PatternFill("solid", fgColor="E2EFDA")
WARN_FILL = PatternFill("solid", fgColor="FCE4D6")
BAD_FILL = PatternFill("solid", fgColor="F8CBAD")
INFO_FILL = PatternFill("solid", fgColor="DEEAF6")
THIN = Side(style="thin", color="B0B0B0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")
CENTER = Alignment(horizontal="center", vertical="top", wrap_text=True)


def sheet(wb, name, title, headers, widths, rows, fill_col=None, fill_map=None, row_key=None):
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
        if row_key and str(row[0]) in row_key:
            for c in range(1, len(headers) + 1):
                ws.cell(row=i, column=c).fill = row_key[str(row[0])]
        if fill_col and fill_map:
            f = fill_map.get(str(row[fill_col - 1]))
            if f:
                for c in range(1, len(headers) + 1):
                    ws.cell(row=i, column=c).fill = f
    ws.freeze_panes = "A4"
    return ws


def ps(cmd):
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True,
                           text=True, encoding="utf-8", timeout=40)
        return r.stdout.strip()
    except Exception as exc:
        return "（读取失败：%s）" % exc


# ---------------- ① 现役机器实测 ----------------
cpu = ps("(Get-CimInstance Win32_Processor).Name")
cores = ps("(Get-CimInstance Win32_Processor).NumberOfCores")
threads = ps("(Get-CimInstance Win32_Processor).NumberOfLogicalProcessors")
model = ps("(Get-CimInstance Win32_ComputerSystem).Model")
ram_gb = ps("[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1)")
free_ram = ps("[math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1MB,1)")
mods = ps("(Get-CimInstance Win32_PhysicalMemory | ForEach-Object { \"$($_.Capacity/1GB)GB@$($_.Speed)\" }) -join ' + '")
slots_used = ps("(Get-CimInstance Win32_PhysicalMemory | Measure-Object).Count")
disks = ps("(Get-PhysicalDisk | ForEach-Object { \"$($_.FriendlyName) $([math]::Round($_.Size/1GB))GB $($_.MediaType)/$($_.BusType)\" }) -join '; '")
vols = ps("(Get-Volume | Where-Object DriveLetter | ForEach-Object { \"$($_.DriveLetter): $([math]::Round($_.Size/1GB))GB 剩$([math]::Round($_.SizeRemaining/1GB))GB\" }) -join ' | '")
gpu = ps("(Get-CimInstance Win32_VideoController | Where-Object { $_.Name -notmatch 'Virtual|Radeon' } | ForEach-Object { $_.Name }) -join ', '")
nics = ps("(Get-NetAdapter | Where-Object Status -eq 'Up' | ForEach-Object { \"$($_.Name)[$($_.LinkSpeed)]\" }) -join ', '")
proc_mem = ps("[math]::Round(((Get-Process | Measure-Object WorkingSet64 -Sum).Sum)/1GB,1)")
top = ps("(Get-Process | Group-Object ProcessName | ForEach-Object { [pscustomobject]@{n=$_.Name; g=[math]::Round((($_.Group | Measure-Object WorkingSet64 -Sum).Sum)/1GB,2)} } | Sort-Object g -Descending | Select-Object -First 5 | ForEach-Object { \"$($_.n) $($_.g)GB\" }) -join '、'")

# 项目占用
def du(rel):
    p = ROOT.parent / rel if rel.startswith("backend") or rel.startswith("frontend") else ROOT.parent / rel
    if not p.exists():
        return 0.0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e6


repo_mb = du(".git")
app_mb = du("backend/app")
venv_mb = du("backend/.venv")
db_mb = DB.stat().st_size / 1e6 if DB.exists() else 0
con = sqlite3.connect(str(DB))
rows = dict(con.execute("select category, count(*) from coal_records group by category").fetchall())
con.close()
code_lines = 0
for sub in ("backend/app", "backend/scripts", "frontend/js", "backend/tests"):
    for f in (ROOT.parent / sub).rglob("*"):
        if f.suffix in (".py", ".js") and f.is_file():
            code_lines += sum(1 for _ in f.open(encoding="utf-8", errors="ignore"))

# ---------------- ② KEPServer 标签与数据量 ----------------
tag_counts = {}
if TAGS_XLSX.exists():
    wb0 = load_workbook(TAGS_XLSX, data_only=True)
    for sn in wb0.sheetnames:
        if sn == "总览":
            continue
        tag_counts[sn] = max(0, wb0[sn].max_row - 1)
    wb0.close()
inst = tag_counts.get("瞬时", 0)
acc = tag_counts.get("累计", 0)
per = tag_counts.get("周期", 0)
st = tag_counts.get("设定与状态(非测量)", 0)
total_tags = inst + acc + per + st
picks = {}
if PICK_XLSX.exists():
    # 该表的表头与数据列有错位（优先级实际落在 G 列），所以按"整行扫 P0/P1/P2"统计，别按列号取
    wb1 = load_workbook(PICK_XLSX, data_only=True)
    for row in wb1["筛选结果"].iter_rows(values_only=True):
        for v in row:
            if isinstance(v, str) and v.strip() in ("P0", "P1", "P2"):
                picks[v.strip()] = picks.get(v.strip(), 0) + 1
    wb1.close()
picked = sum(picks.values())

continuous = inst + per + acc                       # 连续/窗口量
SCEN = [
    ("生产口径（推荐）", 60, "连续量每 60 秒落一条；状态量只在变化时落", continuous, st),
    ("精细口径（回放/诊断）", 10, "连续量每 10 秒落一条；状态量变化时落", continuous, st),
    ("全量 5 秒原始", 5, "建议接入的 %d 条标签全部按 5 秒落库" % (picked or total_tags), picked or total_tags, 0),
]
scen_rows = []
for name, iv, note, ntags, nstates in SCEN:
    per_day = ntags * 86400 / iv + nstates * 6        # 状态量按每标签每天约 6 次变化估
    per_year = per_day * 365
    gb_tsdb = per_year * 60 / 1e9                     # 时序库压缩后约 60 B/行
    gb_sqlite = per_year * 120 / 1e9                  # SQLite 行+索引约 120 B/行
    scen_rows.append([name, iv, note, "%s 条" % f"{ntags:,}",
                      "%,.0f" % per_day if False else "{:,.0f}".format(per_day),
                      "{:,.0f}".format(per_year / 1e6) + " 百万",
                      "%.1f GB" % gb_tsdb, "%.1f GB" % gb_sqlite,
                      "%.0f GB" % (gb_tsdb * 3), "%.0f GB" % (gb_sqlite * 3)])

wb = Workbook()
wb.remove(wb.active)

sheet(
    wb, "一、现役机器实测", "先看现在的机器卡在哪（%s 实测）" % __import__("datetime").date.today(),
    ["项", "实测值", "说明 / 影响"], [20, 46, 60],
    [
        ["机型", "%s / %s" % (ps("(Get-CimInstance Win32_ComputerSystem).Manufacturer"), model), ""],
        ["CPU", "%s（%s 核 / %s 线程）" % (cpu, cores, threads),
         "CPU 其实很够：16 核 32 线程。换机时**不要降级**到 8~10 核。"],
        ["内存", "共 %s GB（%s，%s 个插槽已用满）｜当前可用仅 %s GB" % (ram_gb, mods, slots_used, free_ram),
         "★ 这是真正的瓶颈：两个插槽都被 8GB 占满，扩不了；多开 Agent/浏览器/Docker 就吃紧。"],
        ["内存占用现状", "全部进程合计 %s GB；前五：%s" % (proc_mem, top),
         "注意：Agent 本身是「瘦客户端」（调云端模型），真正的内存大头顶在 Docker/WSL2/时序库/浏览器/编辑器上。"],
        ["磁盘", "%s｜%s" % (disks, vols),
         "★ 第二瓶颈：C 盘只剩十几 GB，1TB 单盘且只有 1 个分区可用；KEPServer 数据一上来会更紧。"],
        ["显卡", gpu or "（未识别到独显）", "本项目（岭回归/PLS + Excel + 网页）**不需要独显**；只有本地跑大模型才需要。"],
        ["网络", nics, "有 2.5GbE 有线口（当前未接）——厂区 PLC 网段建议走有线，别依赖 Wi-Fi。"],
        ["项目占用", "仓库 .git %0.1f MB；后端代码 %0.1f MB；虚拟环境 %0.1f MB；数据库 %0.2f MB" % (repo_mb, app_mb, venv_mb, db_mb),
         "项目本身极轻（代码约 %s 行）。硬件压力**不来自现在的项目**，而来自 KEPServer 时序数据 + 多 Agent 开发。" % f"{code_lines:,}"],
        ["数据库现状", "粗灰 %s 条／灰分密度 %s 条／浮精 %s 条" % (rows.get("coarse"), rows.get("ash_density"), rows.get("float")),
         "SQLite 单文件，四个月才 0.37 MB —— 关系型业务数据可以忽略不计。"],
    ],
    row_key={"内存": KEY_FILL, "磁盘": KEY_FILL},
)

sheet(
    wb, "二、KEPServer 数据量测算", "未来的「大量数据」到底有多大：按标签构成现算（%d 条标签，建议接入 %d 条）"
    % (total_tags, picked or total_tags),
    ["口径", "采样间隔(秒)", "做法", "连续量标签", "行/天", "行/年", "时序库 1 年", "SQLite 1 年", "时序库 3 年", "SQLite 3 年"],
    [20, 12, 40, 12, 12, 12, 12, 12, 12, 12],
    scen_rows,
    row_key={SCEN[0][0]: OK_FILL},
)
sheet(
    wb, "二·标签构成", "KEPServer 标签构成（来源：在线仪表关联标签_20260923_170617.xlsx，%d 条）" % total_tags,
    ["类别", "条数", "含义 / 存储建议"], [18, 10, 92],
    [
        ["瞬时测量", inst, "实时物理量（灰分、密度、煤量、液位…）→ 按秒/分钟落库，是数据量的主体"],
        ["周期统计", per, "10/30 分钟窗口均值（厂家已算好）→ 直接存，几乎不占空间，优先用它而不是自己再算"],
        ["累计量", acc, "班/日/总累计（不清零）→ 按 1~5 分钟存，注意**复位分段差分**，不要直接相加"],
        ["设定与状态", st, ("开关量/设定值（Boolean 等）→ **只在变化时落一条**，一天几千条，体积可忽略")],
        ["建议接入合计", picked or 243, "见「KEPServer标签筛选-重介密控需接入.xlsx」（P0 %s / P1 %s / P2 %s）"
         % (picks.get("P0"), picks.get("P1"), picks.get("P2"))],
    ],
)
sheet(
    wb, "二·测算说明", "测算口径与结论",
    ["项", "说明"], [22, 104],
    [
        ["行大小假设", "时序库按压缩后 ~60 字节/行（TimescaleDB/InfluxDB 对缓变模拟量的典型压缩）；"
                     "SQLite 按 ~120 字节/行（行 + 主键索引 + WAL 余量）。"],
        ["三年列的含义", "已含 **备份另存一份**（×2）与 WAL/临时空间余量（×1）→ 约等于单份的 3 倍。"],
        ["结论①", "即使用「生产口径」（连续量 60 秒一条），三年也就几十 GB 级 —— **2TB 硬盘完全够**，"
                 "而且这种量 SQLite/TimescaleDB 都能扛。"],
        ["结论②", "真正需要认真对待的是「全量 5 秒原始」：一年上亿到十亿行。这时**不要用 SQLite 单表**，"
                 "要上 TimescaleDB / InfluxDB / ClickHouse，并且**连续量优先复用厂家的 10/30 分钟均值标签**。"],
        ["结论③", "别在笔记本上做 5 秒全量长期留存：笔记本会被带走、会休眠、磁盘也小。"
                 "建议按「现场常驻机存全量 + 笔记本只留最近 1~3 个月」分工（见「五、架构建议」）。"],
        ["软件依赖", "KEPServerEX 只有 Windows 版 → 必须 Windows 11 **专业版**（Pro 才能用 Hyper-V/远程桌面/组策略）；"
                   "时序库与 Grafana 建议跑在 WSL2/Docker 里（需要内存与虚拟化支持）。"],
    ],
)

sheet(
    wb, "三、规格要求（按优先级）", "挑机器时对着这张表核（★= 硬性，其余为建议）",
    ["优先级", "项", "要求", "为什么（与本项目/多 Agent 的关系）"], [10, 18, 34, 78],
    [
        ["1 ★", "内存", "≥32GB 起步，**目标 64GB**；优先「可换内存的 2 个 SO-DIMM 槽」或「128GB 焊死 LPDDR5X」",
         "实测现状：16GB 只剩 3.5GB。要同时装：Windows + 浏览器（10~20 进程）+ VSCode + 多个 Agent 会话 + "
         "WSL2/Docker（时序库 + Grafana）+ KEPServerEX + Python 测试；64GB 才谈得上「边跑边开发」。"
         "注意别买**内存焊死且只有 16/32GB** 的轻薄本。"],
        ["2 ★", "存储", "**2TB NVMe（Gen4 起）**，最好**两个 M.2 插槽**；条件允许 4TB 或外置硬盘柜/NAS",
         "三年 KEPServer 数据（几十 GB 级）＋ 备份 ＋ Docker 镜像 ＋ 代码仓库/虚拟环境（现约 0.3GB，会长）＋ "
         "若本地跑模型再加 100~300GB 权重。现状 C 盘只剩十几 GB 就是反面教材——**系统盘至少留 200GB 空闲**。"],
        ["3 ★", "CPU", "≥12 核 / 16 线程（16 核 32 线程更佳）",
         "现役 Ryzen 9 7945HX（16C/32T）已很强：后端测试全跑一遍要 ~2.7 分钟（PLS 折内选参单线程为主），"
         "多 Agent 并发、Excel/数据导入聚合都吃 CPU。换机不要降到 8 核。"],
        ["4", "显卡", "不跑本地大模型 → 集显足够（独显 8GB 已富余）；要本地跑 → NVIDIA 16~24GB 显存 或 AMD 统一内存 128GB 平台",
         "本项目模型是岭回归/PLS（毫秒级），**完全不需要 GPU**。只有你打算本地部署 14B~70B 模型（给 Agent 做离线兜底）才值得为显存花钱。"],
        ["5 ★", "有线网口", "RJ45 ≥1GbE（2.5GbE 更佳）；或随机配带网口的扩展坞",
         "厂区 PLC/仪表网段走有线更稳（现在 10.255.2.7 那个有线口就是干这个的，只是链路没通）。Wi-Fi 在厂区不可靠。"],
        ["6 ★", "系统与虚拟化", "Windows 11 **专业版**，BIOS 支持虚拟化（VT-x/AMD-V）",
         "KEPServerEX 只有 Windows 版；WSL2/Docker 需要虚拟化；Pro 才有 Hyper-V/远程桌面（现场远程排障靠它）。"],
        ["7", "接口与形态", "≥2×USB-A、HDMI/DP、可选雷电/USB4；16 寸 2.5K；键盘有数字小键盘更顺手",
         "现场要接 U 盘/串口转换器/显示器；16 寸适合长时间看表格与代码。"],
        ["8", "散热与 7×24", "性能释放 ≥60W 且风扇可长期运转；支持**电池限充**（长插电保护）；进风口不易积灰",
         "服务会常驻。笔记本长期 100% 插电 + 高温是电池鼓包主因；限充（60~80%）能显著延长寿命。"],
        ["9", "保修", "≥3 年上门/意外保（现场机器建议上门）",
         "厂区机器坏了要等寄修很耽误生产；上门保是花小钱省大事。"],
    ],
    row_key={"1 ★": KEY_FILL, "2 ★": KEY_FILL, "3 ★": KEY_FILL, "5 ★": KEY_FILL, "6 ★": KEY_FILL},
)

sheet(
    wb, "四、三档方案", "三档配置思路（**具体型号与价格请按购买时市场核对** —— 生成本表时本机外网不通，未核实时价）",
    ["档位", "内存/存储", "CPU/GPU", "适合谁", "要点与坑"], [16, 22, 26, 26, 62],
    [
        ["A 大内存移动工作站（最稳）", "64GB（或 96GB）＋ 2TB NVMe（双 M.2 槽）",
         "12~16 核 H/HX 级 ＋ 入门专业卡或集显",
         "把笔记本当主力开发机 + 偶尔兼现场调试",
         "商用/工作站系列（如 ThinkPad P 系、Precision 5000 系、ZBook Power/Studio 系）——"
         "优点是 64GB 可配、驱动与固件稳、上门保修好、长期插电更放心；缺点是同价位性能不如游戏本。"
         "**核对该型号是否支持 2×32GB 与双硬盘**。"],
        ["B 大内存 + 大显存（要本地跑大模型）", "64~128GB 统一内存 或 64GB + 16~24GB 显存；2~4TB",
         "HX 级 ＋ RTX 5080/5090 级 或 AMD 统一内存平台（128GB）",
         "想让 Agent 有本地模型兜底、离线也能跑",
         "AMD 统一内存平台能跑 70B 级量化模型，但内存带宽/生态与 CUDA 有差距；"
         "NVIDIA 路线显存 16~24GB，跑 14~32B 量化模型更省心。**如果只是调云端模型，这档的钱花得不值。**"],
        ["C 游戏本/性价比本 + 迷你主机分离（我最推荐）", "笔记本 32~64GB ＋ 2TB；迷你主机 32GB ＋ 2TB ＋ 双 2.5GbE",
         "笔记本 12 核以上；迷你主机 8~12 核、低功耗",
         "现场要 7×24 跑系统，你又要带着本子到处跑",
         "把「常驻」与「开发」拆开：迷你主机放现场（低功耗、有线、不断电、不怕被带走），笔记本只做开发与调试。"
         "这样笔记本不必为 7×24 买单（可以更轻薄），服务也不会因为本子合盖/休眠/带走而中断 —— "
         "**我们这次「服务起完又没了」就是单机兼任的代价**。迷你主机 3000~6000 元档即可胜任。"],
    ],
    row_key={"C 游戏本/性价比本 + 迷你主机分离（我最推荐）": OK_FILL},
)

sheet(
    wb, "五、架构建议", "比买哪台本子更重要的两件事",
    ["项", "建议", "理由"], [20, 52, 54],
    [
        ["常驻与开发分离", "现场放一台迷你主机（或旧笔记本）7×24 跑 FastAPI + KEPServerEX + 时序库；"
                          "你的笔记本只做开发/查看", "服务不因合盖、休眠、带走、系统更新而中断；"
                          "现场机器还能接线到 PLC 网段，VPN 只是远程查看的补充"],
        ["数据落库策略", "连续量优先**复用厂家的 10/30 分钟均值标签**；需要秒级时只对关键 P0 标签开 5 秒；"
                        "状态量只在变化时落；累计量按复位分段差分", "同样信息量下，行数差 10~100 倍；"
                        "这决定了三年后是几十 GB 还是几个 TB"],
        ["数据库选型", "业务数据（粗灰/浮精/灰分密度）继续 SQLite；时序数据用 TimescaleDB（PostgreSQL 扩展）或 InfluxDB，"
                      "放 WSL2/Docker；要长期秒级全量再考虑 ClickHouse",
                      "SQLite 单表上亿行会明显变慢；时序库自带压缩与按时间分区/保留策略"],
        ["备份", "每天自动备份到「另一块物理盘 + 移动硬盘/NAS」，保留最近 N 份；"
                "本次导入前我就是用 sqlite3 在线备份 API 存的快照（不能直接复制 .db，WAL 里还有数据）",
                "现场机器故障是常态；有备份才敢重训、敢改口径"],
        ["远程访问", "Radmin VPN（地址固定）+ 有线网段打通；Windows 11 专业版的远程桌面",
                    "厂区 Wi-Fi 地址是 DHCP，会变（本项目这几天就从 192.168.43.104 变到 10.255.249.56）"],
    ],
)

sheet(
    wb, "六、采购核对清单", "下单前逐条核对（可直接给销售看）",
    ["#", "核对项", "为什么"], [6, 44, 78],
    [
        [1, "内存：容量 / 是否可换 / 插槽数 / 最大支持", "焊死 16GB 的机器买了就废；优先 2 槽可换到 64GB"],
        [2, "硬盘：容量 / 是否双 M.2 插槽 / 是否 PCIe4.0", "双槽能后加一条做数据盘或备份盘"],
        [3, "网口：是否有 RJ45，速率多少", "厂区 PLC 网段走有线；没有网口就要配带网口的扩展坞"],
        [4, "系统版本：Windows 11 专业版", "KEPServerEX 只有 Windows 版；Pro 才有 Hyper-V/远程桌面"],
        [5, "CPU：核心数 ≥12", "多 Agent 并发 + 测试 + 数据聚合"],
        [6, "散热：性能释放多少 W、是否有电池限充", "7×24 插电运行，电池与散热是寿命关键"],
        [7, "保修：是否 3 年上门/意外保", "现场机器寄修耽误生产"],
        [8, "屏幕：16 寸 2.5K 起、是否低蓝光", "长时间看表格/代码"],
        [9, "接口：USB-A ≥2、HDMI/DP、是否雷电/USB4", "接 U 盘、串口转换器、外接显示器"],
        [10, "重量与续航：≤2.2kg / 断电续航", "要带着走就重要；纯现场固定用可放宽"],
    ],
)

DOCS.mkdir(parents=True, exist_ok=True)
wb.save(OUT)
print("已生成:", OUT)
for ws in wb.worksheets:
    print("  - %-22s %d 行" % (ws.title, ws.max_row - 3))
print("\n实测摘要：CPU=%s(%sC/%sT)｜内存=%sGB(%s)｜可用=%sGB｜磁盘=%s｜%s"
      % (cpu, cores, threads, ram_gb, mods, free_ram, disks, vols))
print("标签：瞬时%d/累计%d/周期%d/状态%d=共%d；建议接入%d（P0 %s/P1 %s/P2 %s）"
      % (inst, acc, per, st, total_tags, picked, picks.get("P0"), picks.get("P1"), picks.get("P2")))
