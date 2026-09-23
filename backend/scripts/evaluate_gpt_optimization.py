"""只读比较 GPT 优化前后；DS 仅作固定参照，不写数据库或源记录。

python backend/scripts/evaluate_gpt_optimization.py
可指定 --records JSON 替代数据库；报告保留数据哈希和旧算法 Git 版本。
"""
import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.services import coarse_training as current
from app.services.modeling import MLR_FEATURES, predict_coarse_ash
from app.services.training import _impute, _metrics, train_coarse_model, train_mlr, train_pls

BASELINE_REF = "401003800afeeccbb74195966f9d78031cf90591"


def baseline_module():
    source = subprocess.check_output(
        ["git", "show", f"{BASELINE_REF}:backend/app/services/coarse_training.py"],
        cwd=ROOT, encoding="utf-8")
    module = types.ModuleType("app.services._gpt_before_20260922")
    module.__package__ = "app.services"
    exec(compile(source, "<frozen GPT baseline>", "exec"), module.__dict__)
    return module


def read_records(args):
    if args.records:
        data = json.loads(args.records.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else data["coarseCoal"]
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        return [{**dict(r), "timestamp": r["ts"]} for r in con.execute(
            "SELECT * FROM coal_records WHERE category='coarse' ORDER BY ts,id")]


def summarize(result, rows):
    model = result[result["production"]]
    predictions = [predict_coarse_ash(r, model) for r in rows]
    return {"production": result["production"], "n": result["n"], "config": model["config"],
            "training": {k: model["metrics"][k] for k in ("r2", "mae", "rmse", "passRate")},
            "timeSelectionQ2": model["metrics"]["q2"],
            "groupCv": model["metrics"].get("groupCv"),
            "predictionRange": [min(predictions), max(predictions)],
            "nestedTimeValidation": model["metrics"]["pipelineValidation"]}


def ds_reference(rows, daily):
    if not daily:
        result = train_coarse_model([dict(r) for r in rows], "all")
        model = result[result["production"]]
    else:
        days = current.aggregate_daily(rows)
        for day in days:
            for feature in MLR_FEATURES[2:9]:
                day[feature] = int(sum(r.get(feature) == 1 for r in day["records"]) > len(day["records"]) / 2)
        X, _ = _impute([[r.get(f) for f in MLR_FEATURES] for r in days])
        y = [r["ash_content"] for r in days]
        mlr, pls = train_mlr(X, y), train_pls(X, y)
        model = pls if pls["metrics"]["q2"] >= mlr["metrics"]["q2"] else mlr
    return {"method": model["type"], "metrics": model["metrics"],
            "note": "DS 原版只读参照；Q²为留一验证，不与GPT时间Q²直接排序"}


def evaluate(records):
    old = baseline_module()
    rows = current.prepare_rows(records)
    payload = [{f: r.get(f) for f in ["timestamp", "ash_content", *MLR_FEATURES]} for r in rows]
    audit = {"rows": len(rows), "days": len({r["timestamp"][:10] for r in rows}),
             "first": rows[0]["timestamp"], "last": rows[-1]["timestamp"],
             "sha256": hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
    results = {}
    for mode, data in (("sample", rows), ("daily", current.aggregate_daily(rows))):
        results[mode] = {"DS": ds_reference(rows, mode == "daily")}
        for name, algorithm in (("before", old), ("after", current)):
            fitted = algorithm.train_models(data)
            if fitted is None:
                results[mode][name] = {"unavailable": True}
                continue
            summary = summarize(fitted, data)
            early = [r for r in data if r["timestamp"] < "2026-08-01"]
            later = [r for r in data if r["timestamp"] >= "2026-08-01"]
            development = algorithm.train_models(early)
            if development and later:
                summary["fixedHistoricalComparison"] = {
                    "developmentN": len(early), "laterN": len(later),
                    **_metrics([r["ash_content"] for r in later],
                               [predict_coarse_ash(r, development[development["production"]]) for r in later], 0)}
            results[mode][name] = summary
    return {"baselineRef": BASELINE_REF, "revision": current.TRAINING_REVISION,
            "protocol": "同数据、同外层时间窗口；6–7月训练/8–9月比较仅为已知历史复核，不是新的独立检验",
            "audit": audit, "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--database", type=Path, default=ROOT / "backend/data/dense_medium.db")
    source.add_argument("--records", type=Path)
    # 第一轮报告是历史快照，后续算法比较不得默认覆盖它。
    parser.add_argument("--output", type=Path, default=ROOT / f"docs/gpt-baseline-comparison-{current.TRAINING_REVISION}.json")
    args = parser.parse_args()
    report = evaluate(read_records(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"audit": report["audit"], "results": report["results"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
