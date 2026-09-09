"""生成《密控两板块-影响因素清单》Excel（分板块两表）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

OUT_PATH = Path(__file__).resolve().parents[2] / "docs" / "密控两板块-影响因素清单.xlsx"

# Sheet1：板块一 介质预测与调控（内环，介质状态维持）
# 序号 | 直接影响因素 | 影响程度 | 影响方向/说明 | 辅助判断该因素的间接因素 | 数据来源 | 我们系统现状
BOARD1 = [
    (1, "补水/循环水量", "高",
     "加水稀释→密度↓、粘度↓；补水是调节密度的主要手段之一",
     "稀介桶液位、合格介质桶液位、喷水量设定、循环水泵频率/开停信号",
     "PLC", "未接入（KEPServer 计划内）"),
    (2, "分流操作（分流箱开度）", "高",
     "分流↑→进入磁选的稀介↑→介质净化，间接影响密度与煤泥含量",
     "分流阀开度、稀介流量、磁选机入料量",
     "PLC", "未接入"),
    (3, "加介量（介质补充）", "高",
     "加介→磁性物含量↑→密度↑；介质消耗需及时补充",
     "加介泵开停、介质仓料位、磁性物含量下降速率",
     "PLC", "未接入"),
    (4, "悬浮液煤泥含量", "高",
     "煤泥↑→粘度↑→分选变差、密度计表观读数偏差；是介质状态核心量",
     "原煤煤泥含量、脱粉筛473/474运行、精磁尾液位、磁选机尾矿阀开度、脱泥筛运行",
     "由密度+磁性物含量计算", "已有：suspension_slime_content（演示值）"),
    (5, "磁性物含量", "高",
     "介质真实状态的另一半；磁性物↓→介质稀→需加介",
     "磁选机运行状态/电流、磁性物含量计读数、尾矿流失、介质制备系统",
     "磁性物含量计/PLC", "已有：mag_content（演示值）"),
    (6, "精磁尾液位", "中高",
     "反映煤泥水系统负荷与介质系统关联工况",
     "煤泥水系统负荷、粗精煤泥量、315筛运行、压滤/浓缩系统",
     "PLC（1秒采集，滞后90秒）", "已有：mag_tail_level（已标滞后90秒）"),
    (7, "入洗原煤量（带煤量）", "中",
     "负荷扰动：带煤量波动会打破介质系统物料平衡",
     "原煤皮带秤、401/402/A/B系统运行信号(合介泵)、启停车状态",
     "皮带秤/PLC", "已有：raw_coal_feed（演示值）"),
    (8, "密度计读数（被控量）", "高",
     "本板块的被控目标量，其余因素都围绕它调节",
     "密度计标定/漂移、取样管堵塞、表观密度vs真实密度（需磁性物含量修正）",
     "在线密度计", "已有：density（四系统）"),
]

# Sheet2：板块二 介质密度选择（外环，灰分→密度设定值）
BOARD2 = [
    (1, "重介精煤灰分（501/502皮带灰分）", "高",
     "核心反馈量：灰分超标→密度↓；偏低→密度↑",
     "在线灰分仪读数、人工化验快灰、501皮带的浮精灰分/粗精灰分影响值、灰分仪安装位置滞后",
     "在线灰分仪/人工化验", "已有：belt_501_ash、belt_502_ash（501暂无数据源）"),
    (2, "原煤灰分", "高",
     "前馈量：原煤性质变了密度设定应提前调整，不等产品灰分反馈",
     "在线灰分仪、入洗工作面/煤层、可选性曲线、煤种切换",
     "在线灰分仪/人工", "已有：raw_coal_ash（暂用17，真实数据已导入）"),
    (3, "目标灰分区间（质量标准）", "高",
     "控制目标约束：决定合格区间与调整方向",
     "产品标准、灰分区间调整表、合同质量要求",
     "人工配置", "未建（可配置项）"),
    (4, "悬浮液煤泥含量", "中高",
     "煤泥含量↑粘度↑分选效率↓，密度设定需补偿（煤泥每增1%密度+0.01）",
     "密度+磁性物含量计算、脱粉筛运行、精磁尾液位",
     "板块一传递", "已有：suspension_slime_content"),
    (5, "原煤煤量/处理量", "中",
     "负荷影响分选效率与灰分波动",
     "皮带秤、系统运行组合、启停车",
     "皮带秤/PLC", "已有：raw_coal_feed"),
    (6, "浮精/粗精影响值（仅501皮带）", "中",
     "501总灰分含浮精+粗精，需扣除其影响才得到401/402重介精煤灰分",
     "浮精灰分/量、粗精灰分/量、历史数据回归系数",
     "化验+计算", "已有平衡灰分公式（dense_clean_ash_balance）"),
    (7, "精煤产率/回收率权衡", "中",
     "灰分-产率权衡：密度过高灰分超、过低产率损失",
     "可选性数据、历史统计数据、吨煤效益核算",
     "数据分析", "未建"),
    (8, "磁性物含量", "中",
     "介质状态约束：磁性物过低时不宜大幅调密度",
     "磁选机状态、加介记录（板块一传递）",
     "磁性物含量计", "已有：mag_content"),
]


def build():
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="1F4E79")
    degree_fills = {
        "高": PatternFill("solid", fgColor="FCE4D6"),
        "中高": PatternFill("solid", fgColor="FFF2CC"),
        "中": PatternFill("solid", fgColor="E2EFDA"),
    }
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="center")

    for title, rows in (("板块一-介质预测与调控", BOARD1), ("板块二-介质密度选择", BOARD2)):
        ws = wb.create_sheet(title)
        cols = ["序号", "直接影响因素", "影响程度", "影响方向/说明",
                "辅助判断该因素的间接因素", "数据来源", "我们系统现状"]
        widths = [6, 20, 9, 40, 44, 18, 26]
        ws.append(cols)
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        for c in ws[1]:
            c.font = header_font
            c.fill = header_fill
            c.alignment = Alignment(horizontal="center", vertical="center")
            c.border = border
        for row in rows:
            ws.append(list(row))
        for r in ws.iter_rows(min_row=2, max_row=1 + len(rows)):
            for c in r:
                c.border = border
                c.alignment = wrap
            r[0].alignment = Alignment(horizontal="center", vertical="center")
            r[2].alignment = Alignment(horizontal="center", vertical="center")
            if r[2].value in degree_fills:
                r[2].fill = degree_fills[r[2].value]
        ws.freeze_panes = "A2"

    wb.save(OUT_PATH)
    print(f"[gen] 已生成：{OUT_PATH}")


if __name__ == "__main__":
    build()
