# -*- coding: utf-8 -*-
"""从 KEPServer 导出的标签清单里，筛出「重介密控系统」需要接入的标签，生成 Excel。

输入：C:/Users/25925/Desktop/web(2)导入数据/kepserver/在线仪表关联标签_*.xlsx（标签清单 + 概况）
输出：docs/KEPServer标签筛选-重介密控需接入.xlsx
  Sheet1 筛选结果（按优先级）  每条选中标签：优先级 / 用途 / 建议接入点
  Sheet2 按仪表汇总            每个仪表选中数 vs 总数
  Sheet3 筛选规则（可复现）     关键词 → 优先级 → 理由
  Sheet4 未选中清单            便于人工复核，避免漏项

优先级口径（重介密控的实际需要）：
  P0 直接进模型/控制：密度计、501/502 测灰、501/502 皮带秤、精磁尾液位、
                     A/B/401/402 与 473/474 脱粉运行信号
  P1 辅助与可信度   ：仪表故障信号、上下限/设定值、累计量、磁性物/介质、旋流器压力、分流量
  P2 参考           ：其余与分选/介质/产品有关的量
  不选              ：与本项目无关的设备量（人工复核 Sheet4）
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

SRC_DIR = Path(r"C:\Users\25925\Desktop\web(2)导入数据\kepserver")
OUT = Path(__file__).resolve().parent.parent.parent / "docs" / "KEPServer标签筛选-重介密控需接入.xlsx"

HEAD_FILL = PatternFill("solid", fgColor="2F5597")
HEAD_FONT = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
TITLE_FONT = Font(name="微软雅黑", size=13, bold=True, color="2F5597")
BODY_FONT = Font(name="微软雅黑", size=10)
P0_FILL = PatternFill("solid", fgColor="F8CBAD")
P1_FILL = PatternFill("solid", fgColor="FFE699")
P2_FILL = PatternFill("solid", fgColor="E2EFDA")
THIN = Side(style="thin", color="B0B0B0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")
CENTER = Alignment(horizontal="center", vertical="top", wrap_text=True)

# (关键词列表, 优先级, 用途, 建议接入点)
RULES = [
    (["密度计", "密度值", "DENS"], "P0", "悬浮液/合格介质密度（控制量核心）", "在线仪表：density"),
    (["AH_1", "AH_10", "AH_30", "测灰", "ASH"], "P0", "在线测灰（瞬时/10分/30分均值）", "在线仪表：ash_501 / ash_502"),
    (["皮带秤", "秤", "WEIGHT", "煤量", "流量"], "P0", "带煤量 t/h（原煤/精煤/各系统）", "在线仪表：scale_501 / scale_502"),
    (["精磁尾", "液位"], "P0", "精磁尾液位（模型 10 因子之一）", "在线仪表：level_tail"),
    (["运行信号", "运行", "启停", "RUN"], "P0", "设备运行状态（A/B/401/402/473/474 → 0/1 特征）", "模型特征 sysA/sysB/sys401/sys402/desliming473/474"),
    (["故障", "FAULT", "报警"], "P1", "仪表/设备故障 → 读数不可信时应冻结", "数据可信度：接到守卫（故障时不给建议）"),
    (["上限", "下限", "设定", "SET", "SP"], "P1", "量程/设定值 → 合理性校验", "输入校验：越界拒收"),
    (["累计", "TOTAL", "累计量"], "P1", "班/日累计产量", "统计与产量核算"),
    (["磁性物", "介质", "合介", "磁选", "MAG"], "P1", "介质系统（磁性物含量/黏度相关）→ 前馈候选", "前馈：介质侧信息"),
    (["旋流器", "旋流", "CYCLONE", "压力", "PRESS"], "P1", "旋流器入口压力 → 实际分选条件", "前馈：分选条件"),
    (["分流", "阀门", "VALVE", "开度"], "P1", "分流量/阀位 → 实际分选密度", "前馈：分选条件"),
    (["灰分", "硫", "水分", "发热量"], "P2", "产品质量参考量", "参考展示"),
]


def classify(text):
    for keys, pri, use, dest in RULES:
        if any(k.upper() in text.upper() for k in keys):
            return pri, use, dest
    return None, None, None


def sheet(wb, name, title, headers, widths, rows, fill_col=None, fill_map=None):
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
            cell.alignment = CENTER if c <= 3 else WRAP
            cell.border = BORDER
        if fill_col and fill_map:
            f = fill_map.get(str(row[fill_col - 1]))
            if f:
                for c in range(1, len(headers) + 1):
                    ws.cell(row=i, column=c).fill = f
    ws.freeze_panes = "A4"
    return ws


src = sorted(SRC_DIR.glob("在线仪表关联标签_*.xlsx"))[-1]
wb_in = load_workbook(src, data_only=True)
ws_in = wb_in["标签清单"]
hdr = [c.value for c in ws_in[1]]
idx = {name: i for i, name in enumerate(hdr) if name}
rows = []
for r in ws_in.iter_rows(min_row=2, values_only=True):
    if r[0] is None:
        continue
    rows.append(r)

sel, unsel = [], []
by_dev = {}
for r in rows:
    dev = str(r[idx["设备"]] or "")
    path = str(r[idx["分组路径"]] or "")
    tag = str(r[idx["标签名"]] or "")
    desc = str(r[idx["说明"]] or "")
    text = " ".join([dev, path, tag, desc])
    pri, use, dest = classify(text)
    by_dev.setdefault(dev, [0, 0])
    by_dev[dev][1] += 1
    rec = [r[idx.get("序号", 0)], dev, path, tag, desc, r[idx.get("数据类型", 8)], pri or "", use or "", dest or ""]
    if pri:
        by_dev[dev][0] += 1
        sel.append(rec)
    else:
        unsel.append(rec)

order = {"P0": 0, "P1": 1, "P2": 2}
sel.sort(key=lambda x: (order.get(x[6], 9), str(x[1])))

wb = Workbook()
wb.remove(wb.active)
sheet(wb, "筛选结果", "KEPServer 标签筛选：重介密控需接入（共 %d 条 / 清单 %d 条）" % (len(sel), len(rows)),
      ["优先级", "序号", "设备", "分组路径", "标签名", "说明", "数据类型", "用途", "建议接入点"],
      [8, 6, 14, 30, 22, 30, 10, 34, 34], sel,
      fill_col=1, fill_map={"P0": P0_FILL, "P1": P1_FILL, "P2": P2_FILL})
sheet(wb, "按仪表汇总", "每个设备的标签数与建议接入数（缺口＝该仪表资料里没有我们需要的量）",
      ["设备", "标签总数", "建议接入", "未接入"], [16, 12, 12, 12],
      [[d, v[1], v[0], v[1] - v[0]] for d, v in sorted(by_dev.items(), key=lambda x: -x[1][1])])
sheet(wb, "筛选规则", "筛选规则（可复现）：关键词 → 优先级 → 用途 → 建议接入点",
      ["关键词", "优先级", "用途", "建议接入点"],
      [40, 8, 40, 40], [[", ".join(k), p, u, d] for k, p, u, d in RULES])
sheet(wb, "未选中清单", "未选中的标签（%d 条）——人工复核用，确认没有漏掉关键量" % len(unsel),
      ["序号", "设备", "分组路径", "标签名", "说明", "数据类型"], [6, 14, 34, 24, 34, 10], unsel)

OUT.parent.mkdir(parents=True, exist_ok=True)
wb.save(OUT)
print("源文件:", src.name)
print("清单条数:", len(rows), "| 建议接入:", len(sel), "| 未选:", len(unsel))
from collections import Counter
print("优先级分布:", dict(Counter(x[6] for x in sel)))
print("按设备(选中/总数):", {d: "%d/%d" % (v[0], v[1]) for d, v in sorted(by_dev.items())})
print("P0 样例:", [(x[2], x[4][:18]) for x in sel if x[6] == "P0"][:8])
print("已生成:", OUT)
