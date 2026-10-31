"""Offline assay experiment. No database, service, training registry or activation imports."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math

import numpy as np
from scipy.optimize import least_squares

from app.gpt_coarse.expert8 import predict as expert_predict

LOCAL = timezone(timedelta(hours=8))
FIT_NAMES = ("mean", "fixed_slope_offset", "free_level", "free_level_coal", "robust_level")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def date_of(row):
    return datetime.fromisoformat(row["observed_at"]).astimezone(LOCAL).date().isoformat()


def midnight(day):
    return datetime.fromisoformat(day).replace(tzinfo=LOCAL)


def running_systems(row):
    return sum(row["features"][k] for k in ("sysA", "sysB", "sys401", "sys402"))


def cohort(rows, count, approximate=False):
    allowed = {"strict", "minute_approximation"} if approximate else {"strict"}
    return sorted((r for r in rows if r["eligibility"] in allowed and running_systems(r) == count),
                  key=lambda r: (r["observed_at"], r["id"]))


def available_before(rows, day):
    cutoff = midnight(day)
    return [r for r in rows if datetime.fromisoformat(r["observed_at"]) < cutoff
            and datetime.fromisoformat(r["created_at"]) < cutoff]


def design(rows, coal=False):
    # Center only for conditioning; coefficients and predictions remain unrestricted.
    columns = [np.ones(len(rows)), np.array([r["features"]["level"] - 50 for r in rows])]
    if coal:
        columns.append(np.array([(r["features"]["coal_amount"] - 500) / 100 for r in rows]))
    return np.column_stack(columns)


def ols(x, y, weights=None):
    w = np.ones(len(y)) if weights is None else np.asarray(weights)
    xx, yy = x * np.sqrt(w)[:, None], np.asarray(y) * np.sqrt(w)
    coef, _, rank, _ = np.linalg.lstsq(xx, yy, rcond=None)
    if rank != x.shape[1]:
        raise ValueError("Unidentifiable regression: rank-deficient training design")
    return coef


def fit(rows, name, equal_dates=False):
    if not rows:
        raise ValueError("Empty training data")
    y = np.array([r["value"] for r in rows], dtype=float)
    counts = Counter(date_of(r) for r in rows)
    w = np.array([1 / counts[date_of(r)] if equal_dates else 1 for r in rows])
    if name == "mean":
        coef = [float(np.average(y, weights=w))]
    elif name == "fixed_slope_offset":
        coef = [float(np.average(y + .3 * np.array([r["features"]["level"] for r in rows]), weights=w))]
    else:
        x = design(rows, coal=name == "free_level_coal")
        coef = ols(x, y, w)
        if name == "robust_level":
            result = least_squares(lambda b: np.sqrt(w) * (x @ b - y), coef,
                                   loss="huber", f_scale=1.5, max_nfev=2000)
            if not result.success:
                raise ValueError("Huber optimizer failed: " + result.message)
            coef = result.x
        elif name not in ("free_level", "free_level_coal"):
            raise ValueError("Unknown fitted candidate " + name)
        coef = coef.tolist()
    basis = {"mean": "constant predicted ash", "fixed_slope_offset": "intercept at L=0; fixed level slope=-0.3"}.get(
        name, "intercept at L=50 and Q=500; coal coefficient per 100 t/h")
    return {"name": name, "coef": coef, "n": len(rows), "dates": len(counts),
            "training_ids": [r["id"] for r in rows], "equal_dates": equal_dates,
            "basis": basis}


def predict(rows, name, model, frozen):
    level = np.array([r["features"]["level"] for r in rows])
    if name == "empirical_raw":
        return 30.5 - .3 * level
    if name == "empirical_clipped":
        return 30.5 - .3 * np.clip(level, 25, 75)
    if name == "frozen_expert8":
        values = [expert_predict(r["features"], frozen) for r in rows]
        if any(v is None for v in values):
            raise ValueError("Frozen EXPERT8 rejected an eligible assay")
        return np.array(values)
    if name == "two_system_lookup":
        return np.where(level >= 30, 14., 15.)
    coef = model["coef"]
    if name == "mean":
        return np.full(len(rows), coef[0])
    if name == "fixed_slope_offset":
        return coef[0] - .3 * level
    return design(rows, coal=name == "free_level_coal") @ np.array(coef)


def metrics(records):
    if not records:
        return {"n": 0, "dates": 0}
    errors = np.array([r["prediction"] - r["actual"] for r in records])
    by_date = defaultdict(list)
    for r, e in zip(records, errors):
        by_date[r["date"]].append(abs(float(e)))
    return {"n": len(records), "dates": len(by_date),
            "mae": float(np.mean(abs(errors))), "rmse": float(np.sqrt(np.mean(errors ** 2))),
            "bias": float(np.mean(errors)), "max_abs_error": float(max(abs(errors))),
            "date_mae": float(np.mean([np.mean(v) for v in by_date.values()]))}


def evaluate(rows, models, frozen, names, group, phase, fold=None):
    records = []
    for name in names:
        values = predict(rows, name, models.get(name), frozen)
        for row, value in zip(rows, values):
            records.append({"group": group, "phase": phase, "fold": fold, "model": name,
                            "id": row["id"], "date": date_of(row), "observed_at": row["observed_at"],
                            "quality": row["eligibility"], "actual": row["value"],
                            "level": row["features"]["level"], "coal": row["features"]["coal_amount"],
                            "prediction": float(value)})
    return records


def slope_diagnostics(rows, protocol):
    base = fit(rows, "free_level")
    days = sorted({date_of(r) for r in rows})
    daily = {d: [r for r in rows if date_of(r) == d] for d in days}
    by_day = []
    for d in days:
        remaining = [r for r in rows if date_of(r) != d]
        if len(remaining) >= 3:
            by_day.append({"excluded_date": d, "slope": fit(remaining, "free_level")["coef"][1]})
    by_row = [{"excluded_id": r["id"], "slope": fit([q for q in rows if q["id"] != r["id"]], "free_level")["coef"][1]}
              for r in rows]
    rng = np.random.default_rng(protocol["bootstrap_seed"])
    boot, skipped = [], 0
    for _ in range(protocol["bootstrap_replicates"]):
        sample = [r for day in rng.choice(days, len(days), replace=True) for r in daily[day]]
        try:
            boot.append(fit(sample, "free_level")["coef"][1])
        except ValueError:
            skipped += 1
    within_x, within_y, within_coal = [], [], []
    means = []
    for day in days:
        rr = daily[day]
        ll = np.array([r["features"]["level"] for r in rr])
        yy = np.array([r["value"] for r in rr])
        qq = np.array([r["features"]["coal_amount"] for r in rr])
        means.append([float(ll.mean()), float(yy.mean())])
        if len(rr) >= 2:
            within_x.extend(ll - ll.mean())
            within_y.extend(yy - yy.mean())
            within_coal.extend((qq - qq.mean()) / 100)
    wx, wy, wq = map(np.array, (within_x, within_y, within_coal))
    within = float(wx @ wy / (wx @ wx)) if len(wx) and wx @ wx > 1e-12 else None
    partial = None
    if len(wx):
        xx = np.column_stack([wx, wq])
        if np.linalg.matrix_rank(xx) == 2:
            partial = ols(xx, wy).tolist()
    means = np.array(means)
    between = float(ols(np.column_stack([np.ones(len(means)), means[:, 0]]), means[:, 1])[1]) if len(means) >= 2 else None
    no_extreme = [r for r in rows if r["id"] != 63]
    return {"ols": base, "equal_date_ols": fit(rows, "free_level", equal_dates=True),
            "without_id63": fit(no_extreme, "free_level") if len(no_extreme) != len(rows) else None,
            "leave_one_date_out": by_day, "leave_one_row_out": by_row,
            "date_bootstrap": {"replicates": len(boot), "skipped": skipped,
                               "slope_percentiles_2_5_50_97_5": np.percentile(boot, [2.5, 50, 97.5]).tolist(),
                               "fraction_negative": float(np.mean(np.array(boot) < 0))},
            "within_date_slope": within, "within_date_level_coal_coefficients": partial,
            "between_date_slope": between, "within_date_rows": len(wx),
            "level_coal_correlation": float(np.corrcoef([r["features"]["level"] for r in rows],
                                                        [r["features"]["coal_amount"] for r in rows])[0, 1])}


def run_experiment(audit, protocol, frozen):
    if digest(audit["rows"]) != protocol["dataset_sha256"] or audit["dataset_sha256"] != protocol["dataset_sha256"]:
        raise ValueError("Audit does not match the locked protocol dataset hash")
    rows = audit["rows"]
    outputs, records = {}, []
    for count in (3, 2):
        for approximate in (False, True):
            key = f"systems{count}_" + ("strict_plus_approx" if approximate else "strict")
            eligible = cohort(rows, count, approximate)
            eligible = [r for r in eligible if protocol["development_start"] <= date_of(r) < protocol["holdout_end_exclusive"]]
            train = available_before(eligible, protocol["holdout_start"])
            tail = [r for r in eligible if date_of(r) >= protocol["holdout_start"]]
            names = protocol["candidates"] + (["two_system_lookup"] if count == 2 else [])
            models = {name: fit(train, name) for name in FIT_NAMES}
            records.extend(evaluate(train, models, frozen, names, key, "training_fit"))
            for quality, phase in (("strict", "tail_strict"), ("minute_approximation", "tail_approx")):
                # Strict-trained model also evaluates approximation rows, kept separate.
                tail_rows = [r for r in cohort(rows, count, True) if r["eligibility"] == quality
                             and protocol["holdout_start"] <= date_of(r) < protocol["holdout_end_exclusive"]]
                records.extend(evaluate(tail_rows, models, frozen, names, key, phase))
            folds = []
            day = midnight(protocol["rolling_first_date"])
            while day < midnight(protocol["holdout_start"]):
                d = day.date().isoformat()
                training = available_before(eligible, d)
                validation = [r for r in eligible if date_of(r) == d]
                fold = {"date": d, "train_ids": [r["id"] for r in training],
                        "test_ids": [r["id"] for r in validation],
                        "unavailable_prior_ids": [r["id"] for r in eligible if date_of(r) < d and r not in training]}
                if not validation:
                    fold["skipped"] = "no validation assay"
                elif len(training) < protocol["minimum_training_rows"]:
                    fold["skipped"] = "insufficient training assays"
                else:
                    try:
                        fitted = {name: fit(training, name) for name in FIT_NAMES}
                    except ValueError as exc:
                        fold["skipped"] = str(exc)
                    else:
                        if approximate:
                            for quality, phase in (("strict", "rolling_strict"), ("minute_approximation", "rolling_approx")):
                                subset = [r for r in validation if r["eligibility"] == quality]
                                records.extend(evaluate(subset, fitted, frozen, names, key, phase, fold=d))
                        else:
                            records.extend(evaluate(validation, fitted, frozen, names, key, "rolling", fold=d))
                        fold["models"] = fitted
                folds.append(fold)
                day += timedelta(days=1)
            outputs[key] = {"training_ids": [r["id"] for r in train], "tail_ids": [r["id"] for r in tail],
                            "models": models, "folds": folds, "names": names,
                            "diagnostics": slope_diagnostics(train, protocol),
                            "range": {k: [min(r["features"][k] for r in train), max(r["features"][k] for r in train)]
                                      for k in ("level", "coal_amount")}}
    scored = defaultdict(list)
    for r in records:
        scored[(r["group"], r["phase"], r["model"])].append(r)
    summaries = [{"group": g, "phase": p, "model": m, **metrics(rr)} for (g, p, m), rr in scored.items()]
    if any(not math.isfinite(r["prediction"]) for r in records):
        raise ValueError("Non-finite prediction")
    return {"schema": "gpt-level-experiment-v1", "status": "offline_only_not_activated",
            "dataset_sha256": protocol["dataset_sha256"], "protocol_sha256": digest(protocol),
            "groups": outputs, "metrics": summaries, "predictions": records}
