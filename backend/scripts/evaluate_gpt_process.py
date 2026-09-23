"""只读比较独立 GPT 改进与 DS：同一外层日期，保留各自训练/聚合方法。

python backend/scripts/evaluate_gpt_process.py
历史数据已用于开发；此报告不是新数据验收。仅写报告，不写数据库或保存模型。
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path

from evaluate_gpt_optimization import ROOT, current, read_records, summarize
from app.services.modeling import MLR_FEATURES, predict_coarse_ash
from app.services.training import _impute, _metrics, train_coarse_model, train_mlr, train_pls


def ds_model(rows, daily):
    if not daily:
        fitted = train_coarse_model([dict(r) for r in rows], "all")
        return fitted[fitted["production"]]
    X, _ = _impute([[r.get(f) for f in MLR_FEATURES] for r in rows])
    y = [r["ash_content"] for r in rows]
    mlr, pls = train_mlr(X, y), train_pls(X, y)
    return pls if pls["metrics"]["q2"] >= mlr["metrics"]["q2"] else mlr


def ds_days(rows):
    days = current.aggregate_daily(rows)
    for day in days:
        for feature in MLR_FEATURES[2:9]:
            day[feature] = int(sum(r.get(feature) == 1 for r in day["records"]) > len(day["records"]) / 2)
    return days


def scored(rows, model):
    return {**_metrics([r["ash_content"] for r in rows],
                      [predict_coarse_ash(r, model) for r in rows], 0), "n": len(rows)}


def feedback_study(rows):
    """额外输入试验：仅前一天及更早的化验可用；只报告，不写入生产模型。"""
    actual, predictions = [], {gain: [] for gain in (0, .25, .5)}
    daily = all(r.get("day") for r in rows)
    metric = "mae" if daily else "rmse"
    for start, end in current.folds(rows, outer=True):
        selections = {kind: current.select(rows[:start], kind) for kind in ("mlr", "pls")}
        kind = min(selections, key=lambda k: selections[k]["score"][metric])
        model = current.fit(rows[:start], kind, selections[kind]["config"])
        for row in rows[start:end]:
            target_day = datetime.date.fromisoformat(row["timestamp"][:10])
            residuals = {}
            for past in rows:
                age = (target_day - datetime.date.fromisoformat(past["timestamp"][:10])).days
                if 1 <= age <= 3:
                    residuals.setdefault(age, []).append(past["ash_content"] - predict_coarse_ash(past, model))
            weight = sum(1 / age for age in residuals)
            correction = sum(sum(values) / len(values) / age for age, values in residuals.items()) / weight if weight else 0
            base = predict_coarse_ash(row, model)
            actual.append(row["ash_content"])
            for gain in predictions:
                predictions[gain].append(base + gain * correction)
    return {"protocol": "化验当天出结果；保守假设次日起可用。校正可用到较早测试日期的已出结果，故与固定模型检验分列。",
            "maxAgeDays": 3, "productionEnabled": False,
            "candidates": [{"gain": gain, "n": len(actual), **_metrics(actual, values, 0)}
                           for gain, values in predictions.items()]}


def compare(records):
    rows = current.prepare_rows(records)
    if not rows:
        raise ValueError("没有有效粗灰记录")
    payload = [{f: r.get(f) for f in ["timestamp", "ash_content", *MLR_FEATURES]} for r in rows]
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    previous_path = ROOT / "docs/gpt-optimization-evaluation-20260922.json"
    previous = json.loads(previous_path.read_text(encoding="utf-8"))
    same_input = digest == previous["audit"]["sha256"]
    report = {"revision": current.TRAINING_REVISION,
              "protocol": "同一外层日期与目标；DS保留自身聚合和选参。开发期历史复核，不是新数据独立验收。",
              "audit": {"rows": len(rows), "days": len({r["timestamp"][:10] for r in rows}),
                        "first": rows[0]["timestamp"], "last": rows[-1]["timestamp"], "sha256": digest},
              "previousGptSnapshot": str(previous_path.relative_to(ROOT)),
              "previousInputMatches": same_input, "results": {}}
    for daily in (False, True):
        mode = "daily" if daily else "sample"
        data = current.aggregate_daily(rows) if daily else rows
        ds_data = ds_days(rows) if daily else rows
        fitted = current.train_models(data)
        if not fitted:
            report["results"][mode] = {"unavailable": True}
            continue
        active = fitted[fitted["production"]]
        gpt = summarize(fitted, data)
        gpt["selectionPolicy"] = active["metrics"]["selectionPolicy"]
        gpt["inputPolicy"] = active["inputPolicy"]
        gpt["feedbackStudy"] = feedback_study(data)
        full_ds = ds_model(ds_data, daily)
        ds = {"production": full_ds["type"], "training": full_ds["metrics"]}
        actual, pred, windows = [], [], []
        for s, e in current.folds(ds_data, outer=True):
            model = ds_model(ds_data[:s], daily)
            actual.extend(r["ash_content"] for r in ds_data[s:e])
            pred.extend(predict_coarse_ash(r, model) for r in ds_data[s:e])
            windows.append({"trainEnd": ds_data[s-1]["timestamp"], "testStart": ds_data[s]["timestamp"],
                            "testEnd": ds_data[e-1]["timestamp"], "n": e-s, "production": model["type"]})
        ds["nestedTimeValidation"] = {**_metrics(actual, pred, 0), "n": len(actual), "folds": windows}
        # 两者必须比较相同日期、相同目标，不允许因筛选/聚合漏行而悄悄改变难度。
        assert [r["timestamp"] for r in data] == [r["timestamp"] for r in ds_data]
        assert [r["ash_content"] for r in data] == [r["ash_content"] for r in ds_data]
        assert [(w["trainEnd"], w["testStart"], w["testEnd"], w["n"]) for w in windows] == [
            (w["trainEnd"], w["testStart"], w["testEnd"], w["n"]) for w in gpt["nestedTimeValidation"]["folds"]]
        for name, values in (("GPT", data), ("DS", ds_data)):
            early = [r for r in values if r["timestamp"] < "2026-08-01"]
            later = [r for r in values if r["timestamp"] >= "2026-08-01"]
            if not later or len(early) < 12:
                continue
            if name == "GPT":
                early_fitted = current.train_models(early)
                if not early_fitted:
                    continue
                model = early_fitted[early_fitted["production"]]
            else:
                model = ds_model(early, daily)
            target = gpt if name == "GPT" else ds
            target["fixedHistoricalComparison"] = {"developmentN": len(early), **scored(later, model)}
            if name == "GPT":
                target["fixedHistoricalComparison"]["outsideTrainingRangeN"] = sum(
                    any(isinstance(r.get(f), (int, float)) and not (b[0] <= r[f] <= b[1])
                        for f, b in zip(MLR_FEATURES, model["inputBounds"])) for r in later)
        report["results"][mode] = {"DS": ds, "GPT": gpt}
        if same_input:
            report["results"][mode]["previousGPT"] = previous["results"][mode]["after"]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--database", type=Path, default=ROOT / "backend/data/dense_medium.db")
    source.add_argument("--records", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/gpt-process-evaluation-20260922.json")
    args = parser.parse_args()
    report = compare(read_records(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"audit": report["audit"], "previousInputMatches": report["previousInputMatches"],
                      "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
