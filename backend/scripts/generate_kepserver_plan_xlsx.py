"""生成《KEPServer接入-需求与计划》Excel（按板块分文件）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

OUT_PATH = Path(__file__).resolve().parents[2] / "docs" / "KEPServer接入-需求与计划.xlsx"

# Sheet1：需求与疑问（序号 | 板块 | 需求/待办 | 我的建议 | 你的回复（待填））
REQS = [
    (1, "A. 接入方式",
     "KEPServer 连接方式：OPC UA 还是 OPC DA？",
     "建议 OPC UA（KEPServerEX 开启 UA 服务即可，跨平台、免 DCOM）；OPC DA 仅限 Windows 老方案", ""),
    (2, "A. 接入方式",
     "KEPServer 中 PLC 标签地址清单（通道/设备/标签名）及其对应的系统字段？",
     "请提供标签清单，我据此生成标签映射配置表（yaml/json），不写死在代码里", ""),
    (3, "A. 接入方式",
     "采集扫描周期？",
     "建议 30秒~1分钟扫描，整点聚合后推送小时值", ""),
    (4, "B. 采集服务",
     "开发独立采集进程 collector.py：OPC UA 客户端 + 标签映射 + 断线重连 + 缓存补数 + 运行日志",
     "独立于 Web 后端运行，故障不影响页面", ""),
    (5, "B. 采集服务",
     "小时聚合规则？",
     "量类字段求和（累计差）、液位/密度/灰分取均值", ""),
    (6, "C. 后端接口",
     "粗精煤泥表 PLC 导入接口",
     "已有 POST /coarse-slime/import/plc，无需开发（✓）", ""),
    (7, "C. 后端接口",
     "浮精信息表 PLC 导入接口",
     "待开发：压滤机循环数、药剂量等字段的 /flotation/import/plc", ""),
    (8, "C. 后端接口",
     "重介精煤信息表 PLC 导入接口",
     "待开发：501/502皮带量、密度、重介系统参数的 /dense/import/plc", ""),
    (9, "D. 落库细节",
     "密度（5分钟）如何入库？",
     "采集端聚合为小时均值，整点推送；表头周期标注 5分钟", ""),
    (10, "D. 落库细节",
     "皮带灰分 501/502（5分钟）如何入库？",
     "同上，小时均值入库；501 灰分数据源待你提供", ""),
    (11, "D. 落库细节",
     "断线补数机制？",
     "采集端本地缓存未推送的小时数据，重连后按 record_time 回填（现有接口支持幂等 upsert）", ""),
    (12, "E. 部署环境",
     "KEPServerEX 所在位置与网络？",
     "请告知：本机还是局域网服务器、端口（UA 默认 49320）、是否需要账号认证", ""),
]

# Sheet2：接入计划（阶段 | 任务 | 具体内容 | 产出/验收 | 前置条件）
PLAN = [
    ("阶段1", "确认接入方式与标签清单",
     "确定 OPC UA/DA、标签地址清单、采集周期、部署位置",
     "标签映射配置表（Excel/配置文档）", "你提供 KEPServer 信息"),
    ("阶段2", "后端补齐 PLC 导入接口",
     "开发 /flotation/import/plc 与 /dense/import/plc（复用粗精煤泥表导入逻辑）",
     "接口可用（Postman/页面验证）", "字段口径确定"),
    ("阶段3", "开发采集服务 collector.py",
     "OPC UA 客户端 + 标签映射配置 + 30s~1min 扫描 + 本地聚合（求和/均值）+ 断线重连 + 缓存补数 + 日志",
     "采集服务独立运行，模拟标签可通", "阶段1、2 完成"),
    ("阶段4", "联调",
     "KEPServer 真实标签 → 采集服务 → 导入接口 → 前端三表展示核对",
     "整点数据与 KEPServer 读数一致", "现场 KEPServer 可访问"),
    ("阶段5", "上线运行",
     "采集服务作为后台服务常驻（Windows 服务或任务计划）；与人工/Excel 数据并存对照；运行监控",
     "稳定运行一周无断档", "阶段4 验收通过"),
]


def build():
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="1F4E79")
    section_fill = PatternFill("solid", fgColor="DDEBF7")
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="center")

    # Sheet1
    ws = wb.active
    ws.title = "需求与疑问"
    cols = ["序号", "板块", "需求/待办", "我的建议", "你的回复（待填）"]
    widths = [6, 14, 44, 46, 24]
    ws.append(cols)
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for c in ws[1]:
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = border
    for row in REQS:
        ws.append(list(row))
    for r in ws.iter_rows(min_row=2, max_row=1 + len(REQS)):
        for c in r:
            c.border = border
            c.alignment = wrap
        r[0].alignment = Alignment(horizontal="center", vertical="center")
        r[1].alignment = Alignment(horizontal="center", vertical="center")
        r[1].fill = section_fill
    ws.freeze_panes = "A2"

    # Sheet2
    ws2 = wb.create_sheet("接入计划")
    cols2 = ["阶段", "任务", "具体内容", "产出/验收", "前置条件"]
    widths2 = [8, 22, 52, 26, 18]
    ws2.append(cols2)
    for i, w in enumerate(widths2, 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    for c in ws2[1]:
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = border
    for row in PLAN:
        ws2.append(list(row))
    for r in ws2.iter_rows(min_row=2, max_row=1 + len(PLAN)):
        for c in r:
            c.border = border
            c.alignment = wrap
        r[0].alignment = Alignment(horizontal="center", vertical="center")
        r[0].fill = section_fill
    ws2.freeze_panes = "A2"

    wb.save(OUT_PATH)
    print(f"[gen] 已生成：{OUT_PATH}")


if __name__ == "__main__":
    build()
