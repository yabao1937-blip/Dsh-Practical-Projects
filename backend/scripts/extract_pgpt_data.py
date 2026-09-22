"""Read only the supplied workbooks; never import the DS/GPT data/model pipeline."""
import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[2]


def date_cell(cell, year):
    if isinstance(cell.value, (dt.date, dt.datetime)):
        return dt.date(year, cell.value.month, cell.value.day)
    # Excel's 7.1 with 0.0 means July 1; with 0.00 means July 10.
    fmt = cell.number_format.split(';')[0]
    if '.' in fmt:
        digits = len(fmt.split('.')[1].split('_')[0])
        text = f'{float(cell.value):.{digits}f}'
    else:
        text = str(cell.value)
    month, day = text.split('.')
    return dt.date(year, int(month), int(day))


def number(value):
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def switch(value):
    if value in ('开', 1):
        return 1.0
    if value in ('停', '关', 0):
        return 0.0
    return None


def extract(folder, year):
    records, sources, rejected, duplicate_count = {}, [], [], 0
    for path in sorted(folder.glob('粗精*.xlsx')):
        sources.append({'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        workbook = openpyxl.load_workbook(path, data_only=True)
        for sheet in workbook:
            previous_day, previous_time, offset = None, None, 0
            for cells in list(sheet.rows)[3:]:
                r = [c.value for c in cells]
                if not isinstance(r[2], dt.time):
                    continue  # trailing empty template rows
                day = date_cell(cells[0], year)
                if day != previous_day:
                    offset, previous_time = 0, None
                if previous_time is not None and r[2] < previous_time:
                    offset += 1  # ordered samples cross midnight within a production day
                timestamp = dt.datetime.combine(day + dt.timedelta(days=offset), r[2])
                previous_day, previous_time = day, r[2]
                origin = {'file': path.name, 'sheet': sheet.title, 'row': cells[0].row}
                y = number(r[12])
                x = [number(r[4]), number(r[5]), *[switch(v) for v in r[6:12]], number(r[14])]
                key = f'{day.isoformat()}#{int(r[1])}'
                row = {'id': key, 'day': day.isoformat(), 'time': timestamp.isoformat(),
                       'sequence': int(r[1]), 'x': x, 'y': y, 'face': r[3], 'sources': [origin]}
                if key in records:
                    old = records[key]
                    if any(old[k] != row[k] for k in ('time', 'x', 'y', 'face')):
                        raise ValueError(f'Conflicting duplicate: {key}: {origin}')
                    old['sources'].append(origin)
                    duplicate_count += 1
                else:
                    records[key] = row
                    if y is None or not 0 <= y <= 100:
                        rejected.append({'id': key, 'source': origin, 'reason': '315灰分不是有效百分数', 'value': str(r[12])})
    rows = sorted(records.values(), key=lambda r: (r['day'], r['sequence']))
    return {'schema': 1, 'year': year, 'sources': sources, 'duplicateCount': duplicate_count,
            'rejectedTargets': rejected, 'rows': rows,
            'features': ['原煤灰分(%)', '小时带煤量(t/h)', 'A系统', 'B系统', '401系统', '402系统', '473脱粉', '474脱粉', '精磁尾液位(%)'],
            'datePolicy': '原表日期为生产日；按序号跨午夜递增自然日期；一天一班，班内逐次采样。',
            'targetPolicy': '315灰分为目标；日值为有效采样算术平均，无粗精煤泥产量，不能计算产量加权灰分。'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    parser.add_argument('--year', type=int, default=2026)
    args = parser.parse_args()
    data = extract(args.folder, args.year)
    output = ROOT / 'frontend' / 'pgpt' / 'data.js'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('/* Generated from source Excel by extract_pgpt_data.py. */\n'
                      'globalThis.PGPT_DATA = ' + json.dumps(data, ensure_ascii=False, allow_nan=False) + ';\n', encoding='utf-8')
    print(json.dumps({'uniqueSamples': len(data['rows']), 'duplicates': data['duplicateCount'],
                      'invalidTargets': len(data['rejectedTargets']), 'days': len(set(r['day'] for r in data['rows']))}, ensure_ascii=False))
