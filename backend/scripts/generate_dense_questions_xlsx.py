"""生成《重介精煤信息表-构建疑问清单》Excel（按板块分文件）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

OUT_PATH = Path(__file__).resolve().parents[2] / "docs" / "重介精煤信息表-构建疑问清单.xlsx"

# 序号 | 板块 | 疑问 | 我的建议 | 你的回复（待填）
ROWS = [
    (1, "一、重介系统参数和入洗密度",
     "「入洗密度」是指重介悬浮液密度吗？Excel 里的「密度值」（5分钟在线，系统A/B）就是它、本次直接导入使用？",
     "是，导入 Excel 使用", ""),
    (2, "一、重介系统参数和入洗密度",
     "「重介系统参数」具体指哪些？请列清单（如：磁性物含量、合格介质桶液位、分流阀开度、稀介泵压力…）",
     "先建「悬浮液密度」一个，其余参数你列清单后我再加，未列字段先占位不填数", ""),
    (3, "一、重介系统参数和入洗密度",
     "采样周期？",
     "密度按 5分钟（在线，Excel 推断），其余 PLC 参数按小时", ""),
    (4, "二、重介精煤量",
     "501、502 皮带分别是什么？（如 501=块精煤、502=末精煤）皮带秤量来自 PLC、按小时累计？",
     "均 PLC 小时累计，两皮带量分列存储并合计", ""),
    (5, "二、重介精煤量",
     "浮精量、粗精煤泥量取哪个口径？",
     "浮精=压滤机循环数换算值（1天）；粗精煤泥=315入量−筛下水量（计算值）", ""),
    (6, "二、重介精煤量",
     "粒度对齐：浮精量是 1天一次，皮带量/粗精煤泥是小时级——重介精煤量如何出值？",
     "按天出值：日皮带总量 − 日浮精 − 日粗精煤泥；小时粒度只存原料数据", ""),
    (7, "三、重介精煤灰分",
     "Excel 已有在线灰分仪值（皮带502，高可信 7.36~8.72），灰分标注「人工」如何处理两者关系？",
     "Excel 在线灰分仪值直接入库（高可信 ±0.5%），人工化验值另作来源并存对照（低可信 ±2.0%）", ""),
    (8, "三、重介精煤灰分",
     "人工灰分暂定多少？（浮精暂定11；精煤灰分通常8~10）",
     "暂定 9", ""),
    (9, "四、表结构",
     "是否沿用整套模式（小时存储+天视图+隐藏卡片+可信度误差）？系统 A/B（401→A、402→B）？",
     "沿用", ""),
    (10, "四、表结构",
     "本次是否实现「灰分、密度（导入版）」Excel 的导入功能（解析已就绪，之前只是拦截提示）？",
     "是", ""),
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

    ws = wb.active
    ws.title = "疑问清单"
    cols = ["序号", "板块", "疑问", "我的建议", "你的回复（待填）"]
    widths = [6, 22, 52, 44, 26]
    ws.append(cols)
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for c in ws[1]:
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = border
    for row in ROWS:
        ws.append(list(row))
    for r in ws.iter_rows(min_row=2, max_row=1 + len(ROWS)):
        for c in r:
            c.border = border
            c.alignment = wrap
        r[0].alignment = Alignment(horizontal="center", vertical="center")
        r[1].alignment = Alignment(horizontal="center", vertical="center")
        r[1].fill = section_fill
    ws.freeze_panes = "A2"

    wb.save(OUT_PATH)
    print(f"[gen] 已生成：{OUT_PATH}")


if __name__ == "__main__":
    build()
