"""Synthetic-data checks for outlier fairness, tolerance objective and availability."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import numpy as np
import pytest

from app.gpt_coarse.experiments.normal_ash import (
    available, choose_candidate, densest_center, fences, fit_snapshot, inside, recent_rows, run, score, value_of,
)
from app.gpt_coarse.experiments.level_relationship import digest

ROOT = Path(__file__).resolve().parents[2]
P = json.loads((ROOT / "docs/verification/gpt-normal-ash-20261031.protocol.json").read_text(encoding="utf-8"))


def row(i, day, value=12, systems=3, hour=8):
    at = datetime(2026, 10, day, hour, tzinfo=timezone(timedelta(hours=8)))
    return {"id": i, "value": value, "observed_at": at.isoformat(),
            "created_at": (at + timedelta(hours=2)).isoformat(), "eligibility": "strict",
            "features": {"level": 30 + day, "coal_amount": 500 + day % 3 * 20,
                         "sysA": 0, "sysB": 1, "sys401": 1, "sys402": systems - 2,
                         "desliming473": 0, "desliming474": 0}}


def test_tukey_is_training_only_one_pass_and_keeps_original_labels():
    rr = [row(i, 16 + i, value=v) for i, v in enumerate([10, 11, 12, 13, 14, 31])]
    original = copy.deepcopy(rr)
    b = fences(rr, P)
    assert b["lower"] == pytest.approx(7.5)
    assert b["upper"] == pytest.approx(17.5)
    assert not inside(31, b)
    snapshot = fit_snapshot(rr, datetime.fromisoformat("2026-10-23T08:00:00+08:00"), P)
    assert snapshot["bounds"] == b
    assert snapshot["excluded_training_ids"] == [5]
    assert rr == original


def test_zero_iqr_does_not_treat_tiny_changes_as_outliers():
    b = fences([row(i, 16, value=12) for i in range(5)], P)
    assert b["raw_iqr"] == 0
    assert b["effective_iqr"] == 1
    assert inside(12.8, b)


def test_density_center_optimizes_hits_and_has_deterministic_ties():
    y = [10, 10.4, 11, 12, 30]
    center = densest_center(y, 1)
    assert center == pytest.approx(11)
    assert sum(abs(v - center) <= 1 for v in y) == 4
    assert densest_center(list(reversed(y)), 1) == center
    with pytest.raises(ValueError):
        densest_center([], 1)


def test_one_point_tolerance_is_inclusive_and_two_is_separate():
    rr = [{"date": "a", "actual": 10, "prediction": p} for p in (9, 11, 11.00001, 12)]
    m = score(rr, P)
    assert m["hit1"] == 2
    assert m["hit2"] == 4


def test_recent_order_uses_sampling_time_and_expires_at_24_hours():
    rr = [row(i, 20, hour=8 + i) for i in range(6)]
    # Old sample entered late does not become the newest sampling time.
    rr[0]["created_at"] = "2026-10-21T07:00:00+08:00"
    at = datetime.fromisoformat("2026-10-21T08:00:00+08:00")
    usable = available(rr + [row(99, 19)], at)
    assert [r["id"] for r in recent_rows(usable, at, P)] == [1, 2, 3, 4, 5]


def test_entry_and_sample_cutoffs_both_strict():
    rr = [row(i, 20) for i in range(3)]
    rr[1]["created_at"] = "2026-10-21T01:00:00+08:00"
    rr[2]["created_at"] = "2026-10-21T00:00:00+08:00"
    assert [r["id"] for r in available(rr, datetime.fromisoformat("2026-10-21T00:00:00+08:00"))] == [0]


def test_stale_recent_correction_falls_back_to_base():
    rr = [row(i, 16 + i, value=11 + .1 * i) for i in range(5)]
    fitted = fit_snapshot(rr, datetime.fromisoformat("2026-10-25T08:00:00+08:00"), P)
    assert fitted["recent_ids"] == []
    assert fitted["models"]["clean_recent_bias5"]["coef"] == fitted["models"]["clean_huber_level"]["coef"]
    assert fitted["models"]["clean_recent_median5"]["coef"] == fitted["models"]["clean_median"]["coef"]


def test_selection_prefers_hit_rate_then_mae_not_best_fit_name():
    scores = [{"model": "a", "n": 5, "hit1_rate": .6, "mae": .9},
              {"model": "b", "n": 5, "hit1_rate": .8, "mae": 1.1}]
    assert choose_candidate(scores, ["a", "b"]) == "b"


def test_local_model_responds_to_inputs_without_reading_target_label():
    protocol = json.loads((ROOT / "docs/verification/gpt-normal-ash-local-20261031.protocol.json").read_text(encoding="utf-8"))
    rr = [row(i, 16 + i % 5, value=10 if i < 5 else 14) for i in range(10)]
    for i, r in enumerate(rr):
        r["features"]["level"] = 30 if i < 5 else 70
    snapshot = fit_snapshot(rr, datetime.fromisoformat("2026-10-25T08:00:00+08:00"), protocol)
    targets = [row(90, 25, value=-999), row(91, 25, value=999)]
    targets[0]["features"]["level"] = 35
    targets[1]["features"]["level"] = 65
    for name in ("clean_local_median_level", "clean_local_median_level_coal", "clean_local_majority_level_coal"):
        assert value_of(snapshot["models"][name], targets).tolist() == pytest.approx([10, 14])


def test_replay_does_not_use_target_or_future_labels_and_quality_scores_stay_separate():
    protocol = copy.deepcopy(P)
    protocol["selection_end_exclusive"] = "2026-10-24"
    rr = [row(systems * 100 + day, day, value=11 + .15 * (day % 4), systems=systems)
          for systems in (2, 3) for day in range(16, 29)]
    approx = row(999, 23, value=11.1)
    approx["eligibility"] = "minute_approximation"
    rr.append(approx)
    frozen = json.loads((ROOT / "backend/models/gpt_expert8/20261012/model.json").read_text())["model"]
    protocol["dataset_sha256"] = digest(rr)
    a = run({"rows": rr, "dataset_sha256": digest(rr)}, protocol, frozen)
    changed = copy.deepcopy(rr)
    for r in changed:
        if r["observed_at"] >= "2026-10-26":
            r["value"] = 40
    protocol["dataset_sha256"] = digest(changed)
    b = run({"rows": changed, "dataset_sha256": digest(changed)}, protocol, frozen)
    assert a["selected_algorithm"] == b["selected_algorithm"]
    # Includes the first changed target day: its own label cannot affect its forecast.
    key = lambda r: (r["group"], r["id"], r["model"])
    before = {key(r): r["prediction"] for r in a["predictions"] if r["date"] <= "2026-10-26"}
    after = {key(r): r["prediction"] for r in b["predictions"] if r["date"] <= "2026-10-26"}
    assert before == after
    index = {r["id"]: r for r in rr}
    for record in a["predictions"]:
        snapshot = a["snapshots"][record["snapshot"]]
        assert record["id"] not in snapshot["training_ids"]
        cutoff = datetime.fromisoformat(record["cutoff"])
        assert all(datetime.fromisoformat(index[i][k]) < cutoff for i in snapshot["training_ids"]
                   for k in ("observed_at", "created_at"))
    assert all(m["quality"] in ("strict", "minute_approximation") for m in a["metrics"])
    assert any(c["skipped"] > 0 for c in a["coverage"])
    assert all(c["eligible"] == c["predicted"] + c["skipped"] for c in a["coverage"])
    with pytest.raises(ValueError, match="hash"):
        run({"rows": rr, "dataset_sha256": digest(rr)}, protocol, frozen)
