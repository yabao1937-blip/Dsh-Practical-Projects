"""Excel 导入解析端点（三表 → 预览）"""
from io import BytesIO

from fastapi import APIRouter, File, UploadFile
from openpyxl import load_workbook

from ..services.importer import parse_coarse_factors

router = APIRouter(prefix="/import", tags=["导入"])


@router.post("/parse")
async def parse_import(file: UploadFile = File(...)):
    """上传三表 Excel，解析为记录预览（表1 多因素用 parse_coarse_factors）。"""
    data = await file.read()
    try:
        wb = load_workbook(BytesIO(data), data_only=True)
    except Exception as e:
        return {"ok": False, "error": f"无法读取文件：{e}", "records": []}

    # 合并所有 sheet 的行
    all_rows = []
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        if not all_rows:
            all_rows = rows
        else:
            # 去掉重复表头：只追加数据行
            all_rows += rows[1:]

    # 识别表1 多因素（含"原煤灰分"）
    is_coarse = any("原煤灰分" in str(c or "") for r in all_rows[:12] for c in r)
    if is_coarse:
        result = parse_coarse_factors(all_rows)
        records = result["records"]
    else:
        # 表2/表3 模板（简化：按行原样返回，时间列规范化）
        records = []
        for r in all_rows:
            if r and any(c not in (None, "") for c in r):
                records.append({"row": [str(c) if c is not None else "" for c in r]})

    return {
        "ok": True,
        "category": "coarse_factors" if is_coarse else "template",
        "total": len(records),
        "records": records[:500],
        "errors": result.get("errors", []) if is_coarse else [],
    }
