"""P-GPT source interpretation tests, independent of the existing model pipeline."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

path = Path(__file__).resolve().parents[1] / 'scripts/extract_pgpt_data.py'
spec = importlib.util.spec_from_file_location('pgpt_extract', path)
extractor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extractor)


def test_excel_display_date_precision():
    for value, fmt, expected in [(7.1, '0.0_ ', '2026-07-01'), (7.1, '0.00_ ', '2026-07-10'),
                                  (6.3, '0.00_ ', '2026-06-30'), (7.9, 'General', '2026-07-09'),
                                  (8.3, '0.00_ ', '2026-08-30')]:
        cell = SimpleNamespace(value=value, number_format=fmt)
        assert extractor.date_cell(cell, 2026).isoformat() == expected


def test_time_is_not_a_numeric_ash_measurement():
    import datetime
    assert extractor.number(datetime.time(7, 20)) is None
    assert extractor.number('-') is None
    assert extractor.number(13.7) == 13.7
    assert extractor.switch('关') == 0
    assert extractor.switch('开') == 1


def test_exported_ridge_coefficients_against_numpy():
    import json
    import numpy as np
    root = path.parents[2]
    text = (root / 'frontend/pgpt/data.js').read_text(encoding='utf-8')
    rows = json.loads(text.split('globalThis.PGPT_DATA = ', 1)[1].rstrip(';\n'))['rows']
    report = json.loads((root / 'docs/pgpt-evaluation.json').read_text(encoding='utf-8'))
    rows = [r for r in rows if r['day'] < '2026-08-01' and r['y'] is not None]
    for mode in ('sample', 'day'):
        if mode == 'day':
            groups = {}
            for row in rows:
                groups.setdefault(row['day'], []).append(row)
            data = [{'x': [np.mean([r['x'][j] for r in group if r['x'][j] is not None])
                           if any(r['x'][j] is not None for r in group) else np.nan for j in range(9)],
                     'y': np.mean([r['y'] for r in group])} for group in groups.values()]
        else:
            data = rows
        x = np.array([r['x'] for r in data], dtype=float)
        y = np.array([r['y'] for r in data])
        x = np.where(np.isnan(x), np.nanmedian(x, axis=0), x)
        scales = x.std(axis=0)
        z = (x - x.mean(axis=0)) / np.where(scales > 1e-9, scales, 1)
        model = report[mode]['model']
        expected = np.linalg.solve(z.T @ z / len(y) + model['lambda'] * np.eye(x.shape[1]), z.T @ (y - y.mean()) / len(y))
        np.testing.assert_allclose(model['coefficients'], expected, atol=1e-10)
