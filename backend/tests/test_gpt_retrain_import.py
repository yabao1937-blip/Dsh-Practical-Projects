"""GPT 批次导入的歧义日期保护；不改变通用 / DS 导入器。"""
import datetime as dt
import copy
import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.gpt import install_workspace_snapshot as installer
from scripts.gpt.install_workspace_snapshot import validate_bundle
from app.services.coarse_training import aggregate_daily
from app.services.modeling import predict_coarse_ash
from app.services.training import _metrics

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/gpt/retrain_from_xlsx.py"
spec = importlib.util.spec_from_file_location("gpt_retrain", SCRIPT)
retrain = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retrain)


@pytest.fixture
def source(tmp_path, monkeypatch):
    header = ["日期", "序号", "时间", "入洗工作面", "原煤灰分", "小时带煤量", "系统", None,
              None, None, "脱粉", None, "315灰分", "315水分", "液位"]
    sub = [None] * 6 + ["A", "B", "401", "402", "473", "474", None, None, None]
    rows = [["粗精煤泥"], header, sub]
    for date in (9.3, 9.4, 9.9, 9.1, 9.11, 9.19, 9.2, 9.21):
        rows.append([date, 1, dt.time(10, 32), "3309", 40, 800, 1, 1, 1, 0, "开", "停", 13, 27, 55])
    sheet = SimpleNamespace(title="Sheet1", values=rows)
    monkeypatch.setattr(retrain, "load_workbook", lambda *a, **kw: SimpleNamespace(worksheets=[sheet], close=lambda: None))
    path = tmp_path / "source.xlsx"
    path.write_bytes(b"test source")
    return path


def test_explicit_first_date_preserves_september_sequence(source):
    rows, audit = retrain.read_new(source, dt.date(2026, 9, 3), dt.date(2026, 9, 23))
    assert [r["timestamp"] for r in rows] == [f"2026-09-{d:02d} 10:32:00" for d in (3, 4, 9, 10, 11, 19, 20, 21)]
    assert audit["dateCorrections"][0]["cell"] == "A4"
    assert audit["dateCorrections"][0]["original"] == 9.3


def test_unresolved_future_dates_are_rejected(source):
    with pytest.raises(ValueError, match="截止日之后"):
        retrain.read_new(source, None, dt.date(2026, 9, 23))


def test_first_date_cannot_relabel_to_unrelated_day(source):
    with pytest.raises(ValueError, match="不匹配"):
        retrain.read_new(source, dt.date(2026, 9, 4), dt.date(2026, 9, 23))


@pytest.fixture
def archive():
    folder = SCRIPT.parents[2] / "models/gpt/retrain-20260923"
    return (json.loads((folder / "records.json").read_text(encoding="utf-8")),
            json.loads((folder / "models.json").read_text(encoding="utf-8")),
            json.loads((folder / "report.json").read_text(encoding="utf-8")))


def test_archived_models_match_data_and_frozen_holdout(archive):
    rows, bundle, report = archive
    validate_bundle(rows, bundle)
    assert len(rows) == 213
    old = [r for r in rows if r["timestamp"] <= "2026-09-03 07:22:00"]
    test = [r for r in rows if r["timestamp"][:10] > "2026-09-03"]
    assert retrain.digest(old) == bundle["audit"]["oldSha256"]
    assert len(test) == 59
    for mode in ("sample", "daily"):
        data = aggregate_daily(test) if mode == "daily" else test
        frozen = bundle["frozenOldModels"][mode]
        model = frozen[frozen["production"]]
        actual = _metrics([r["ash_content"] for r in data], [predict_coarse_ash(r, model) for r in data], 0, .8)
        for key in ("r2", "mae", "rmse", "passRate"):
            assert actual[key] == pytest.approx(report["results"][mode]["newDateHoldout"][key], abs=1e-8)


def test_mismatched_record_bundle_is_rejected(archive):
    records, bundle, _ = archive
    records[0]["ash_content"] += .1
    with pytest.raises(ValueError, match="哈希"):
        validate_bundle(records, bundle)


def test_mismatched_model_parameters_are_rejected(archive):
    records, bundle, _ = archive
    broken = copy.deepcopy(bundle)
    broken["models"]["sample"]["mlr"]["intercept"] += 1
    with pytest.raises(ValueError, match="参数预测"):
        validate_bundle(records, broken)


def test_install_uses_own_database_preserves_ds_and_refuses_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    (tmp_path / "backend/data").mkdir(parents=True)
    baseline = tmp_path / "baseline.json"
    ds = {"engine": "ds", "n": 152, "sentinel": "leave-alone"}
    baseline.write_text(json.dumps({"coarseModelVariants": {"ds": ds}}), encoding="utf-8")
    installer.install(baseline, SCRIPT.parents[2] / "models/gpt/retrain-20260923")
    target = tmp_path / "backend/data/dense_medium.db"
    with sqlite3.connect(target) as con:
        assert con.execute("select count(*) from coal_records where category='coarse'").fetchone()[0] == 213
        variants = json.loads(con.execute("select value from auto_state where key='coarseModelVariants'").fetchone()[0])
        assert variants["ds"] == ds
        run_ids = {row[0] for row in con.execute("select train_run_id from coarse_models")}
        assert run_ids == {con.execute("select train_run_id from coarse_model_history").fetchone()[0]}
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        installer.install(baseline, SCRIPT.parents[2] / "models/gpt/retrain-20260923")
    assert target.read_bytes() == before
