"""只读 Excel 审计与粗灰模型时间外推比较；不连接数据库、不改源表。

python backend/scripts/evaluate_coarse_models.py --source-dir <新目录>
固定：6–7 月开发/调参；8–9 月后期检验。禁止用后期检验重新选参数。
"""
import argparse
import collections
import datetime
import hashlib
import json
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.coarse_training import aggregate_daily, prepare_rows, select, train_models
from app.services.importer import parse_coarse_factors
from app.services.modeling import MLR_FEATURES, predict_coarse_ash
from app.services.training import _metrics, train_coarse_model, train_mlr, train_pls


def load_source(root):
    all_rows, files, issues = [], [], []
    carried_raw = 0
    for path in sorted(root.glob("粗*.xlsx")):
        files.append({"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        book = openpyxl.load_workbook(path, data_only=True, read_only=True)
        for sheet in book:
            original = list(sheet.values)
            raw = [[v.strftime("%H:%M:%S") if isinstance(v, datetime.time) else v for v in row] for row in original]
            parsed = parse_coarse_factors(raw)
            if parsed["errors"]:
                raise ValueError(f"{path.name}!{sheet.title}: {parsed['errors']}")
            locations = []
            for i, row in enumerate(original[3:], 4):
                ash = row[12] if len(row) > 12 else None
                if isinstance(ash, (int, float)) and not isinstance(ash, bool) and 1 <= ash <= 40:
                    locations.append(i)
                elif ash is not None:
                    issues.append({"file": path.name, "cell": f"'{sheet.title}'!M{i}",
                                   "value": str(ash), "reason": "315灰分不是1–40范围的数值，排除"})
            if len(locations) != len(parsed["records"]):
                raise ValueError("审计行号与导入器输出不一致")
            for r, i in zip(parsed["records"], locations):
                r = dict(r, origin=f"{path.name}!'{sheet.title}'!A{i}:O{i}")
                r["rawAshCarried"] = not isinstance(original[i - 1][4], (int, float))
                all_rows.append(r)
        book.close()
    by_time, duplicates = {}, 0
    for row in all_rows:
        ts = row["timestamp"]
        if ts in by_time:
            duplicates += 1
            keys = MLR_FEATURES + ["ash_content", "moisture"]
            if any(by_time[ts].get(k) != row.get(k) for k in keys):
                raise ValueError(f"同一采样时间存在冲突，请先核对: {by_time[ts]['origin']} / {row['origin']}")
        else:
            by_time[ts] = row
    records = prepare_rows(list(by_time.values()))
    if len(records) != len(by_time):
        raise ValueError("存在非法采样日期，请先核对")
    carried_raw = sum(r["rawAshCarried"] for r in records)
    audit = {"files": files, "parsedRows": len(all_rows), "duplicateRows": duplicates,
             "uniqueRows": len(records), "days": len({r['timestamp'][:10] for r in records}),
             "rawAshCarriedRows": carried_raw, "excludedCells": issues,
             "deslimingStates": dict(collections.Counter(
                 f"473={r['desliming473']:g},474={r['desliming474']:g}" for r in records))}
    return records, audit


def score(rows, model):
    return {"n": len(rows), **_metrics([r["ash_content"] for r in rows],
                                     [predict_coarse_ash(r, model) for r in rows], 0)}


def legacy_daily(rows):
    for r in rows:
        r = dict(r)
        for f in MLR_FEATURES[2:9]:
            r[f] = 1 if r[f] is not None and r[f] > .5 else 0
        yield r


def evaluate(records):
    report = {}
    for mode, rows in (("sample", records), ("daily", aggregate_daily(records))):
        development = [r for r in rows if r["timestamp"] < "2026-08-01"]
        holdout = [r for r in rows if r["timestamp"] >= "2026-08-01"]
        if not development or not holdout:
            raise ValueError("需要6–7月开发数据和8–9月检验数据")
        optimized = train_models(development)
        if mode == "sample":
            old = train_coarse_model([dict(r) for r in development], "all")
            old_model = old[old["production"]]
            old_test = holdout
        else:
            old_train, old_test = list(legacy_daily(development)), list(legacy_daily(holdout))
            X = [[r[f] for f in MLR_FEATURES] for r in old_train]
            y = [r["ash_content"] for r in old_train]
            mlr, pls = train_mlr(X, y), train_pls(X, y)
            old_model = pls if pls["metrics"]["q2"] >= mlr["metrics"]["q2"] else mlr
        mean_model = {"intercept": sum(r["ash_content"] for r in development) / len(development), "coefs": []}
        selected = optimized[optimized["production"]]
        # 消融只报告开发集时间验证；不能用检验集决定专家权重。
        choices = {kind: select(development, kind) for kind in ("mlr", "pls")}
        ablation = {}
        for subset in ("all", "expert"):
            subset_choices = {k: select(development, k, subset=subset) for k in ("mlr", "pls")}
            winner = min(subset_choices, key=lambda k: subset_choices[k]["score"]["rmse"])
            ablation[subset] = {"kind": winner, **subset_choices[winner]}
        report[mode] = {"developmentN": len(development), "holdoutN": len(holdout),
                        "selected": optimized["production"], "config": selected["config"],
                        "developmentSelection": choices,
                        "developmentAblation": ablation,
                        "legacy": score(old_test, old_model), "optimized": score(holdout, selected),
                        "meanBaseline": score(holdout, mean_model)}
        final = train_models(rows)
        report[mode]["allDataModels"] = {"production": final["production"], "n": final["n"],
                                          **{k: {f: v for f, v in final[k].items() if f != "yhat"}
                                             for k in ("mlr", "pls")}}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[2] / "docs/coarse-model-evaluation.json")
    args = parser.parse_args()
    records, audit = load_source(args.source_dir)
    result = {"protocol": "6–7月开发集按日分组向前调参，8–9月固定后期检验；最终全量模型不是该检验模型",
              "audit": audit, "evaluation": evaluate(records)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"audit": audit, "evaluation": {k: {f: v for f, v in m.items() if f != 'allDataModels'}
                                                    for k, m in result['evaluation'].items()}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
