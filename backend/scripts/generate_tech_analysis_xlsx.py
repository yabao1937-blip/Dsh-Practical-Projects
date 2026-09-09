"""生成《技术方案与真实数据-分析梳理》Excel（按板块分文件）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

OUT_PATH = Path(__file__).resolve().parents[2] / "docs" / "技术方案与真实数据-分析梳理.xlsx"

# Sheet1：可用数据清单（序号 | 数据/指标 | 来源 | 频率 | 用途建议 | 我们系统现状）
DATA_ROWS = [
    (1, "原煤灰分(%)", "在线灰分仪", "1分钟（方案）/5分钟（真实文件）",
     "粗精煤泥灰分预测因子①；重介密度模型输入", "已有：raw_coal_ash（暂用固定17）"),
    (2, "原煤煤量(t/h)", "皮带秤", "1分钟",
     "预测因子③入洗原煤量", "已有：raw_coal_feed（演示值，待PLC）"),
    (3, "401/402/A/B 系统运行信号", "PLC 合介泵运行信号", "1分钟",
     "判断哪些系统在运行，影响灰分仪读数；真实文件已含此列", "未建字段"),
    (4, "脱粉筛运行信号(473/474)", "PLC", "1分钟",
     "影响粗精煤泥灰分；真实文件已含此列(开/关)", "未建字段"),
    (5, "501皮带总精煤灰分(%)", "在线灰分仪", "1分钟（方案）/5分钟（文件）",
     "总精煤灰分（含浮精+粗精），平衡反算重介精煤灰分", "已有：belt_501_ash（暂无数据源）"),
    (6, "501皮带精煤煤量(t/h)", "皮带秤2台", "1分钟",
     "总精煤量", "已有：belt_501_amount（演示值）"),
    (7, "502皮带总精煤灰分(%)", "在线灰分仪+人工快灰", "1分钟 / 班次4小时",
     "A/B系统重介精煤灰分（纯重介）", "已有：belt_502_ash（Excel已导入）"),
    (8, "502皮带精煤煤量(t/h)", "皮带秤2台", "1分钟",
     "总精煤量", "已有：belt_502_amount（演示值）"),
    (9, "浮精灰分(%)", "人工/快灰", "班次8小时",
     "影响501总灰分；平衡反算输入", "已有：flotation_ash（暂定11）"),
    (10, "浮精煤量(t/h)", "人工/计算", "固定值",
     "浮选精煤产量", "已有：flotation_amount（循环数×12占位）"),
    (11, "粗精灰分(%)", "人工/快灰", "班次8小时",
     "影响501总灰分；预测模型目标值", "已有：coarse_slime_ash（预测/化验）"),
    (12, "粗精煤量(t/h)", "人工/计算", "固定值",
     "粗精煤泥产量", "已有：coarse_slime_amount=315入量-筛下水"),
    (13, "精磁尾池液位(%)", "PLC", "1秒（方案）",
     "直接影响粗精煤泥灰分；预测因子⑤", "已有：mag_tail_level（周期标注1小时，待修正）"),
    (14, "401/402/A/B 实际密度(g/cm³)", "在线密度计", "1分钟（方案）/5分钟（文件）",
     "四个系统各自分选密度；重介精煤表字段", "已有：density（仅A/B两行，需确认4系统）"),
    (15, "磁性物含量(g/l)", "磁性物含量计", "1分钟",
     "重介系统参数；建议加入重介精煤表", "未建字段"),
    (16, "悬浮液煤泥含量(%)", "由密度+磁性物含量计算", "1分钟",
     "悬浮液粘度/煤泥含量，影响分选", "未建字段"),
    (17, "入洗工作面/煤层", "人工", "按批次",
     "不同煤层煤泥灰分规律不同，分类特征", "未建字段"),
    (18, "液位采集滞后时间", "实测规律", "固定1分30秒",
     "精磁尾液位比采样时间提前1分30秒，用于预测窗口滞后对齐", "未使用"),
]

# Sheet2：真实数据文件（文件名 | 内容 | 规模 | 可用性）
FILE_ROWS = [
    ("重介密控系统技术方案(3).docx",
     "业务需求、输入指标清单、皮带-系统对应关系、真实液位-灰分数据(4.12-4.20，滞后1分30秒)、煤泥含量敏感性表",
     "全文约12k字", "决策方案按你要求忽略；指标清单与真实数据可用"),
    ("粗精煤泥灰分影响因素6.16-7.14.xlsx",
     "真实生产数据(5分钟间隔)：日期/时间/入洗工作面/原煤灰分/小时带煤量/开启系统(A,B,401,402)/脱粉筛473,474开停/315灰分/315全水分/精磁尾液位",
     "118行×16列，约1个月", "★★★★★ 可直接训练粗精煤泥灰分预测模型（目标=315灰分）"),
    ("粗精煤泥、精磁尾 (1).xlsx",
     "新版采样数据：系统列为运行组合(如401、A、B)，含采集液位时间(提前1分30秒)与采样时间两列",
     "87+23行，5.1-5.26", "★★★★ 系统组合信息+滞后时间，应切换为新版导入格式"),
    ("浮精 (1).xlsx",
     "新版浮精数据：采样时间/系统/浮精灰分/浮精煤量(t/h)/压滤机运行，系统含401/402等",
     "5行", "★★★ 新版浮精文件，煤量27t/h级别"),
    ("灰分、密度 （导入版）.xlsx",
     "皮带502灰分%与密度值(5分钟)", "110行", "已导入 ✓"),
]

# Sheet3：疑问清单（序号 | 类别 | 疑问 | 我的建议 | 你的回复（待填））
Q_ROWS = [
    (1, "系统维度(最关键)",
     "之前你定 A(401)、B(402)；但技术方案和新Excel显示 401/402/A/B 是四个独立系统，样本的「系统」列是运行组合(如401、A、B)。以哪个为准？",
     "按真实数据：恢复401/402/A/B四个系统；粗精煤泥表系统列存运行组合字符串", ""),
    (2, "皮带-系统对应",
     "方案：501皮带=401/402重介精煤+浮精+粗精；502皮带=A/B重介精煤。重介精煤表的密度/皮带量是否按此调整（密度4个系统各一行，501/502量不再放汇总行）？",
     "按方案执行，重构重介精煤表系统维度", ""),
    (3, "真实数据训练",
     "6.16-7.14真实数据(原煤灰分/带煤量/运行系统/脱粉筛/精磁尾液位→315灰分)是否导入，训练粗精煤泥灰分预测模型，替换当前专家加权占位模型？",
     "导入并训练（回归模型），占位模型作为冷启动兜底", ""),
    (4, "滞后对齐",
     "精磁尾液位比采样时间提前1分30秒（真实文件自带该列），预测时是否按此滞后对齐输入窗口？",
     "是，预测窗口按滞后时间平移", ""),
    (5, "新字段",
     "是否把磁性物含量、悬浮液煤泥含量、系统运行信号、脱粉筛运行信号(473/474)、入洗工作面加入系统？加在哪张表？",
     "重介精煤表加磁性物含量/悬浮液煤泥含量；粗精煤泥表加脱粉筛运行与运行系统组合；入洗工作面作分类字段", ""),
    (6, "采样频率标注",
     "灰分仪/密度：方案写1分钟，真实文件是5分钟间隔；精磁尾液位方案1秒。表头周期标注以哪个为准？",
     "以真实文件为准：灰分仪/密度5分钟；液位周期标注改为「在线(1秒采集)」", ""),
    (7, "新版导入文件",
     "粗精煤泥、精磁尾(1).xlsx 和 浮精(1).xlsx 是新版格式（系统组合+滞后时间列），是否切换为新版导入解析？",
     "切换新版；旧版兼容保留", ""),
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

    def style_header(ws, ncols):
        for c in ws[1]:
            c.font = header_font
            c.fill = header_fill
            c.alignment = Alignment(horizontal="center", vertical="center")
            c.border = border

    def style_body(ws, nrows, section_col=None):
        for r in ws.iter_rows(min_row=2, max_row=1 + nrows):
            for c in r:
                c.border = border
                c.alignment = wrap
            r[0].alignment = Alignment(horizontal="center", vertical="center")
            if section_col:
                r[section_col].alignment = Alignment(horizontal="center", vertical="center")
                r[section_col].fill = section_fill

    # Sheet1
    ws = wb.active
    ws.title = "可用数据清单"
    cols = ["序号", "数据/指标", "来源", "频率", "用途建议", "我们系统现状"]
    widths = [6, 26, 18, 20, 40, 32]
    ws.append(cols)
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    style_header(ws, len(cols))
    for row in DATA_ROWS:
        ws.append(list(row))
    style_body(ws, len(DATA_ROWS))
    ws.freeze_panes = "A2"

    # Sheet2
    ws2 = wb.create_sheet("真实数据文件")
    cols2 = ["文件名", "内容", "规模", "可用性"]
    widths2 = [34, 60, 14, 36]
    ws2.append(cols2)
    for i, w in enumerate(widths2, 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    style_header(ws2, len(cols2))
    for row in FILE_ROWS:
        ws2.append(list(row))
    style_body(ws2, len(FILE_ROWS))
    ws2.freeze_panes = "A2"

    # Sheet3
    ws3 = wb.create_sheet("疑问清单")
    cols3 = ["序号", "类别", "疑问", "我的建议", "你的回复（待填）"]
    widths3 = [6, 16, 52, 36, 24]
    ws3.append(cols3)
    for i, w in enumerate(widths3, 1):
        ws3.column_dimensions[get_column_letter(i)].width = w
    style_header(ws3, len(cols3))
    for row in Q_ROWS:
        ws3.append(list(row))
    style_body(ws3, len(Q_ROWS), section_col=1)
    ws3.freeze_panes = "A2"

    wb.save(OUT_PATH)
    print(f"[gen] 已生成：{OUT_PATH}")


if __name__ == "__main__":
    build()
