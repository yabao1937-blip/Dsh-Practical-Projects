"""GPT 专用增量重训：先冻结旧数据模型检验新增日期，再用全部记录训练。

只读源 Excel / 状态快照，只写指定输出目录；不连接或改写运行中的数据库。
首个日期有 M.D 歧义时通过 --first-date 显式校正，校正及源文件哈希进入报告。
"""
import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import math
import sys
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
from app.services import coarse_training as training
from app.services.importer import parse_coarse_factors
from app.services.modeling import MLR_FEATURES, predict_coarse_ash
from app.services.training import _metrics


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def digest(rows):
    payload = [{f: r.get(f) for f in ["timestamp", "ash_content", *MLR_FEATURES]} for r in rows]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read_new(path, first_date, as_of):
    records, corrections, sheets = [], [], []
    content = path.read_bytes()
    workbook = load_workbook(io.BytesIO(content), data_only=True)
    for index, sheet in enumerate(workbook.worksheets):
        raw = [list(row) for row in sheet.values]
        if index == 0 and first_date:
            header = next(i for i, row in enumerate(raw[:12]) if any("原煤灰分" in str(c or "") for c in row))
            first = next(i for i in range(header + 2, len(raw)) if raw[i][0] is not None)
            cell = raw[first][0]
            # 仅纠正源表能表达的月份/日期，不允许借参数任意移动样本日期。
            value = float(cell)
            month = int(value)
            fraction = round((value - month) * 100)
            possible = {fraction, fraction // 10} if fraction % 10 == 0 else {fraction}
            if first_date.month != month or first_date.day not in possible:
                raise ValueError("首日校正与源表 M.D 日期不匹配")
            corrections.append({"sheet": sheet.title, "cell": f"A{first + 1}", "original": cell,
                                "resolved": first_date.isoformat(),
                                "reason": f"显式首日消歧为{first_date.isoformat()}；截止日为{as_of.isoformat()}"})
            raw[first][0] = first_date.isoformat()
        parsed = parse_coarse_factors(raw)
        if parsed["errors"]:
            raise ValueError(parsed["errors"])
        sheets.append({"sheet": sheet.title, "records": len(parsed["records"])})
        records.extend(parsed["records"])
    workbook.close()
    rows = training.prepare_rows(records)
    if len(rows) != len(records):
        raise ValueError("新增表存在重复时间或无效记录，请先核对")
    if not rows or any(r["timestamp"][:10] > as_of.isoformat() for r in rows):
        raise ValueError("新增表为空或含截止日之后的日期")
    return rows, {"file": path.name, "sha256": hashlib.sha256(content).hexdigest(),
                  "sheets": sheets, "dateCorrections": corrections}


def evaluate(rows, model, baseline_mean):
    actual = [r["ash_content"] for r in rows]
    predicted = [predict_coarse_ash(r, model) for r in rows]
    return {"n": len(rows), "first": rows[0]["timestamp"], "last": rows[-1]["timestamp"],
            **_metrics(actual, predicted, 0, .8),
            "meanBaseline": _metrics(actual, [baseline_mean] * len(rows), 0, .8),
            "outsideTrainingRangeN": sum(any(
                isinstance(r.get(f), (int, float)) and not b[0] <= r[f] <= b[1]
                for f, b in zip(MLR_FEATURES, model.get("inputBounds", []))) for r in rows)}


def summary(result):
    active = result[result["production"]]
    metrics = active["metrics"]
    return {"n": result["n"], "production": result["production"], "config": active["config"],
            "training": {k: metrics[k] for k in ("r2", "mae", "rmse", "passRate")},
            "timeSelectionQ2": metrics["q2"], "groupCv": metrics["groupCv"],
            "nestedTimeValidation": metrics["pipelineValidation"]}


def model_snapshot(result, trained_at):
    return {"engine": "gpt", "production": result["production"], "range": "all", "tolerance": .8,
            "n": result["n"], "trainedAt": trained_at,
            **{kind: {k: v for k, v in result[kind].items() if k != "yhat"} for kind in ("mlr", "pls")}}


def run(args):
    previous = json.loads((ROOT / "docs/gpt-process-evaluation-20260922.json").read_text(encoding="utf-8"))
    if args.archived_directory:
        # 归档的合并记录含旧训练数据，按归档截止时间恢复，再校验原哈希。
        archived = json.loads((args.archived_directory / "records.json").read_text(encoding="utf-8"))
        cutoff_timestamp = previous["audit"]["last"]
        old = training.prepare_rows([r for r in archived if r["timestamp"] <= cutoff_timestamp])
        additions = [r for r in archived if r["timestamp"] > cutoff_timestamp]
        source = json.loads((args.archived_directory / "report.json").read_text(encoding="utf-8"))["source"]
        replay = True
    else:
        state = json.loads(args.baseline_state.read_text(encoding="utf-8"))
        old = training.prepare_rows(state["coarseCoal"])
        additions, source = read_new(args.xlsx, args.first_date, args.as_of)
        replay = False
    old_keys = {r["timestamp"]: r for r in old}
    for row in additions:
        prior = old_keys.get(row["timestamp"])
        if prior and any(prior.get(f) != row.get(f) for f in ["ash_content", *MLR_FEATURES]):
            raise ValueError(f"旧记录有变动，不能作为纯新增数据处理：{row['timestamp']}")
    merged = training.prepare_rows(old + additions)
    cutoff = old[-1]["timestamp"][:10]
    # 完整日期隔离：旧数据已经含9月3日，该日新增两条只能用于重训，不能算新日期检验。
    test_rows = [r for r in additions if r["timestamp"][:10] > cutoff]
    if not test_rows:
        raise ValueError("没有晚于旧数据末日的新日期")
    if digest(old) != previous["audit"]["sha256"]:
        raise ValueError("旧数据与已归档报告不一致，请重新确认冻结基线")
    if replay:
        archived_audit = json.loads((args.archived_directory / "report.json").read_text(encoding="utf-8"))["audit"]
        if digest(merged) != archived_audit["sha256"]:
            raise ValueError("归档记录哈希不匹配")
    trained_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report = {"trainedAt": trained_at, "revision": training.TRAINING_REVISION, "source": source,
              "replayedFromArchive": replay,
              "protocol": "冻结上一版算法和旧数据，先评估完整新增日期；评估后全部数据用于重训，不再作为未来独立测试集。",
              "audit": {"oldN": len(old), "oldDays": len({r['timestamp'][:10] for r in old}),
                        "oldSha256": digest(old), "newN": len(merged) - len(old),
                        "totalN": len(merged), "totalDays": len({r['timestamp'][:10] for r in merged}),
                        "first": merged[0]["timestamp"], "last": merged[-1]["timestamp"],
                        "sha256": digest(merged), "testN": len(test_rows),
                        "testDays": len({r['timestamp'][:10] for r in test_rows}),
                        "sameOldDayExcludedN": len([r for r in additions if r['timestamp'][:10] == cutoff]),
                        "missingFeatures": {f: sum(r.get(f) is None for r in merged) for f in MLR_FEATURES}},
              "results": {}}
    bundle = {"revision": training.TRAINING_REVISION, "audit": report["audit"], "models": {}, "frozenOldModels": {}}
    details = []
    args.output.mkdir(parents=True, exist_ok=True)
    write_json(args.output / "records.json", merged)
    for mode in ("sample", "daily"):
        aggregate = training.aggregate_daily if mode == "daily" else lambda r: r
        development, test, all_rows = aggregate(old), aggregate(test_rows), aggregate(merged)
        print(f"{mode}: 冻结旧数据模型 {len(development)}，检验新数据 {len(test)}", flush=True)
        frozen = training.train_models(development, "all", .8)
        expected = previous["results"][mode]["GPT"]["training"]
        for metric, value in summary(frozen)["training"].items():
            if not math.isclose(value, expected[metric], abs_tol=1e-8):
                raise ValueError(f"{mode} 旧模型未复现：{metric}")
        old_model = frozen[frozen["production"]]
        holdout = evaluate(test, old_model, sum(r["ash_content"] for r in development) / len(development))
        print(json.dumps({"mode": mode, "newDateHoldout": holdout}, ensure_ascii=False), flush=True)
        print(f"{mode}: 全量重训 {len(all_rows)}", flush=True)
        fresh = training.train_models(all_rows, "all", .8)
        fresh_model = fresh[fresh["production"]]
        report["results"][mode] = {"oldTraining": summary(frozen), "newDateHoldout": holdout,
                                   "retrained": summary(fresh)}
        bundle["models"][mode] = model_snapshot(fresh, trained_at)
        bundle["frozenOldModels"][mode] = model_snapshot(frozen, trained_at)
        for row in test:
            predicted = predict_coarse_ash(row, old_model)
            details.append({"mode": mode, "timestamp": row["timestamp"], "actual": row["ash_content"],
                            "frozenPrediction": predicted, "frozenError": predicted - row["ash_content"],
                            "refittedPredictionInSample": predict_coarse_ash(row, fresh_model)})
        print(json.dumps({"mode": mode, "retrained": summary(fresh)}, ensure_ascii=False), flush=True)
        write_json(args.output / "report.json", report)
        write_json(args.output / "models.json", bundle)
    with (args.output / "new-date-predictions.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(details[0]))
        writer.writeheader()
        writer.writerows(details)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--baseline-state", type=Path)
    inputs.add_argument("--archived-directory", type=Path)
    parser.add_argument("--xlsx", type=Path)
    parser.add_argument("--first-date", type=dt.date.fromisoformat)
    parser.add_argument("--as-of", type=dt.date.fromisoformat)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.baseline_state and (not args.xlsx or not args.as_of):
        parser.error("--baseline-state 还需指定 --xlsx 和 --as-of")
    if args.archived_directory and args.output.resolve() == args.archived_directory.resolve():
        parser.error("复现输出目录不得覆盖归档输入目录")
    run(args)
