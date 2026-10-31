"""Numerical and leakage checks use synthetic assays, never the held-out lab results."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import numpy as np
import pytest

from app.gpt_coarse.experiments.level_relationship import (
    available_before, cohort, digest, fit, metrics, predict, run_experiment,
)


def row(i, level=50, coal=500, systems=3, day=16, quality="strict"):
    observed = datetime(2026, 10, day, 8, tzinfo=timezone(timedelta(hours=8)))
    return {"id": i, "value": 5 + .4 * level, "observed_at": observed.isoformat(),
            "created_at": (observed + timedelta(hours=2)).isoformat(), "eligibility": quality,
            "features": {"level": level, "coal_amount": coal, "sysA": 0, "sysB": 1,
                         "sys401": 1, "sys402": systems - 2, "desliming473": 0, "desliming474": 0}}


def test_free_line_has_no_sign_output_or_input_constraint():
    rows = [row(i, level=l) for i, l in enumerate([10, 30, 50, 70, 90])]
    model = fit(rows, "free_level")
    assert model["coef"][1] == pytest.approx(.4)
    assert predict([row(90, level=100)], "free_level", model, {})[0] == pytest.approx(45)


def test_level_and_load_coefficients_recover_independent_effects():
    rows = [row(i, level=l, coal=q) for i, (l, q) in enumerate([(30, 500), (40, 600), (50, 500), (60, 650)])]
    for r in rows:
        r["value"] = 12 - .2 * (r["features"]["level"] - 50) + .8 * (r["features"]["coal_amount"] - 500) / 100
    assert fit(rows, "free_level_coal")["coef"] == pytest.approx([12, -.2, .8])


def test_degenerate_design_is_rejected():
    with pytest.raises(ValueError, match="rank-deficient"):
        fit([row(i) for i in range(5)], "free_level")


def test_future_entry_and_exact_cutoff_are_not_training_data():
    early, late, boundary = row(1), row(2), row(3)
    late["created_at"] = "2026-10-20T10:00:00+08:00"
    boundary["created_at"] = "2026-10-20T00:00:00+08:00"
    assert [r["id"] for r in available_before([early, late, boundary], "2026-10-20")] == [1]


def test_cohort_does_not_mix_review_approximate_or_two_system_rows():
    rows = [row(1), row(2, systems=2), row(3, quality="label_review"), row(4, quality="minute_approximation")]
    assert [r["id"] for r in cohort(rows, 3)] == [1]
    assert [r["id"] for r in cohort(rows, 3, True)] == [1, 4]


def test_date_equal_mae_is_not_sample_mae():
    result = metrics([{"date": "a", "actual": 0, "prediction": 0}] * 9 +
                     [{"date": "b", "actual": 0, "prediction": 10}])
    assert result["mae"] == pytest.approx(1)
    assert result["date_mae"] == pytest.approx(5)


def test_fixed_slope_fit_changes_intercept_only():
    rows = [row(i, level=l) for i, l in enumerate([30, 40, 50])]
    for r in rows:
        r["value"] = 28 - .3 * r["features"]["level"]
    model = fit(rows, "fixed_slope_offset")
    assert model["coef"] == pytest.approx([28])
    assert "L=0" in model["basis"]
    assert predict(rows, "fixed_slope_offset", model, {}) == pytest.approx([19, 16, 13])


def test_tail_labels_cannot_change_fits_or_rolling_scores():
    root = Path(__file__).resolve().parents[2]
    protocol = json.loads((root / "docs/verification/gpt-level-relationship-20261031.protocol.json").read_text())
    frozen = json.loads((root / "backend/models/gpt_expert8/20261012/model.json").read_text())["model"]
    protocol.update(holdout_start="2026-10-24", holdout_end_exclusive="2026-10-26", bootstrap_replicates=10)
    rows = [row(count * 100 + i, level=30 + i * 3, coal=500 + (i % 3) * 30, systems=count, day=16 + i)
            for count in (2, 3) for i in range(10)]
    rows.extend(row(count * 100 + 99, level=47, coal=520, systems=count, day=22,
                    quality="minute_approximation") for count in (2, 3))
    audit = {"rows": rows, "dataset_sha256": digest(rows)}
    protocol["dataset_sha256"] = digest(rows)
    a = run_experiment(audit, protocol, frozen)
    changed = copy.deepcopy(rows)
    for r in changed:
        if int(r["observed_at"][8:10]) >= 24:
            r["value"] += 1000
    protocol["dataset_sha256"] = digest(changed)
    b = run_experiment({"rows": changed, "dataset_sha256": digest(changed)}, protocol, frozen)
    for key in a["groups"]:
        assert a["groups"][key]["models"] == b["groups"][key]["models"]
    mixed_training_validation = [r for r in a["predictions"] if r["group"].endswith("strict_plus_approx") and r["phase"].startswith("rolling")]
    assert {r["phase"] for r in mixed_training_validation} == {"rolling_strict", "rolling_approx"}
    assert all(r["quality"] == ("strict" if r["phase"] == "rolling_strict" else "minute_approximation") for r in mixed_training_validation)
    assert [r for r in a["metrics"] if r["phase"] == "rolling"] == [r for r in b["metrics"] if r["phase"] == "rolling"]
    with pytest.raises(ValueError, match="hash"):
        run_experiment(audit, protocol, frozen)


def test_robust_fit_resists_one_extreme_but_does_not_delete_it():
    rows = [row(i, level=l) for i, l in enumerate(range(30, 71, 5))]
    rows[-1]["value"] += 50
    ordinary, robust = fit(rows, "free_level"), fit(rows, "robust_level")
    assert abs(robust["coef"][1] - .4) < abs(ordinary["coef"][1] - .4)
    assert robust["n"] == len(rows)
