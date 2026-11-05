"""Scoring consumes immutable predictions and cannot refit after label changes."""
from copy import deepcopy

import pytest

from scripts.score_gpt_expert81_frozen import score


def example():
    metadata = {"3": {"fence": {"lower": 6., "upper": 17.}}}
    lock = {"predicted_at_utc": "2026-11-10T08:00:00+00:00", "rows": [
        {"record_id": "db:manual_measurements:220", "assay_id": 220, "route": "independent_system_count:3", "public_quality": "strict", "prediction": 11., "raw_prediction": 11.},
        {"record_id": "db:manual_measurements:222", "assay_id": 222, "route": "independent_system_count:3", "public_quality": "minute_approximation", "prediction": 12., "raw_prediction": 12.}]}
    labels = [{"assay_id": 220, "actual": 11.4, "available_at": "2026-11-10T09:00:00+00:00"}, {"assay_id": 222, "actual": 31.}]
    return metadata, lock, labels


def test_changed_labels_change_scores_never_locked_predictions_or_fence():
    metadata, lock, labels = example()
    original_metadata, original_lock = deepcopy(metadata), deepcopy(lock)
    before = score(metadata, lock, labels)
    labels[0]["actual"] = 16.
    after = score(metadata, lock, labels)
    assert metadata == original_metadata and lock == original_lock
    assert [r["prediction"] for r in before["rows"]] == [r["prediction"] for r in after["rows"]]
    assert before["summary"]["strict"]["all"]["hit1"] == 1
    assert after["summary"]["strict"]["all"]["hit1"] == 0
    assert before["fits_performed"] == after["fits_performed"] == 0
    assert before["new_predictions_performed"] == after["new_predictions_performed"] == 0
    assert before["summary"]["combined"]["all"]["eligible_n"] == 2
    assert before["summary"]["combined"]["normal"]["eligible_n"] == 1


def test_first_capture_time_does_not_prove_prospective_report_availability():
    metadata, lock, labels = example()
    result = score(metadata, lock, labels)
    assert result["prospective_proven_n"] == 0
    labels[0].update(actual_report_available_at="2026-11-10T09:00:00+00:00", report_clock_comparable=True, immutable_report_version_proven=True)
    assert score(metadata, lock, labels)["prospective_proven_n"] == 1


def test_revealed_labels_must_match_exact_prelocked_IDs():
    metadata, lock, labels = example()
    with pytest.raises(ValueError, match="exactly"):
        score(metadata, lock, labels[:1])
    with pytest.raises(ValueError, match="duplicate"):
        score(metadata, lock, labels+[labels[0]])


def test_failed_prediction_with_known_system_count_keeps_normal_coverage():
    metadata, lock, labels = example()
    lock["rows"][0].update(prediction=None, raw_prediction=None, route=None, system_count=3)
    result = score(metadata, lock, labels)
    assert result["summary"]["combined"]["normal"]["eligible_n"] == 1
    assert result["summary"]["combined"]["normal"]["predicted_n"] == 0
    assert result["summary"]["combined"]["all"]["eligible_n"] == 2
