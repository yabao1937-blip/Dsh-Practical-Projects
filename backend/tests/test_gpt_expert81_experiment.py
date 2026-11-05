"""Meaningful synthetic causal/version/numerical contracts for EXPERT8.1."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import numpy as np
import pytest
from scipy.optimize import minimize

from app.gpt_coarse.experiments.expert81 import (
    DEFAULT_CONFIG, available_versions, digest, fit_state, huber_ridge_fit,
    inside, last_observed_reference, make_c5_association, make_c5_receipt, parse_time, predict_state,
    validate_c5_receipt, verify_state, weighted_median,
)

GROUP = "0,1,1,1,0,0"
UTC = timezone.utc
SAMPLE = datetime(2026, 11, 2, 4, tzinfo=UTC)  # Beijing 12:00.


def event(i, value, sample_at, available_at=None, **changes):
    row = {"assay_id": i, "version_id": f"v{i}", "sample_at": sample_at.isoformat(),
           "version_available_at": (available_at or sample_at+timedelta(hours=1)).isoformat(),
           "value": value, "group_key": GROUP,
           "features": {"level": 40.0+i, "coal_amount": 600.0+3*i},
           "frozen_prediction": 13.0, "provenance": {"kind": "immutable_test_version"}}
    row.update(changes)
    return row


@pytest.fixture
def history():
    return [event(i, 9.0+i, SAMPLE-timedelta(hours=55-8*i)) for i in range(1, 6)]


def fitted(history, at=SAMPLE, method="C1", **kwargs):
    return fit_state(history, at, method, group_key=GROUP, **kwargs)


def test_1200_sample_1300_entry_cannot_correct_own_prediction_but_updates_later(history):
    entry_at = SAMPLE+timedelta(hours=1)
    new = event(99, 15.0, SAMPLE, entry_at)
    at_sample = fitted(history+[new], SAMPLE)
    assert predict_state(at_sample, {"level": 50, "coal_amount": 650})["prediction"] == 12.0
    assert 99 not in at_sample["training_ids"]
    assert 99 not in fitted(history+[new], entry_at)["training_ids"]
    after = fitted(history+[new], entry_at+timedelta(microseconds=1))
    assert 99 in after["training_ids"]
    assert predict_state(after, {"level": 50, "coal_amount": 650})["prediction"] == 13.0
    assert fitted(history+[new], SAMPLE, exclude_assay_id=99) == fitted(history, SAMPLE, exclude_assay_id=99)


@pytest.mark.parametrize("method", [f"C{i}" for i in range(8)])
def test_future_values_and_current_target_have_no_state_or_prediction_influence(history, method):
    future = event(88, 99.0, SAMPLE-timedelta(hours=1), SAMPLE+timedelta(hours=1))
    current = event(99, 13.0, SAMPLE-timedelta(hours=3), SAMPLE-timedelta(hours=2))
    first = fitted(history+[future, current], method=method, exclude_assay_id=99)
    future["value"] = float("nan")
    current["value"] = -500
    second = fitted(history+[future, current], method=method, exclude_assay_id=99)
    assert first == second == fitted(history, method=method, exclude_assay_id=99)


def test_later_revision_and_tombstone_do_not_replace_earlier_known_version(history):
    revision = deepcopy(history[0]); revision.update(version_id="revision2", value=17.0,
                                                    version_available_at=(SAMPLE+timedelta(hours=1)).isoformat())
    early = available_versions(history+[revision], SAMPLE, group_key=GROUP)
    late = available_versions(history+[revision], SAMPLE+timedelta(hours=2), group_key=GROUP)
    assert next(r for r in early if r["assay_id"] == 1)["value"] == 10.0
    assert next(r for r in late if r["assay_id"] == 1)["value"] == 17.0
    tombstone = deepcopy(revision); tombstone.update(version_id="deleted3", deleted=True,
                                                      version_available_at=(SAMPLE+timedelta(hours=3)).isoformat())
    deleted = available_versions(history+[revision, tombstone], SAMPLE+timedelta(hours=4), group_key=GROUP)
    assert 1 not in [r["assay_id"] for r in deleted]


def test_latest_version_is_selected_before_group_or_sampling_time_eligibility(history):
    moved = deepcopy(history[0])
    moved.update(version_id="moved_to_two_system", group_key="0,1,1,0,0,0",
                 version_available_at=(SAMPLE-timedelta(hours=1)).isoformat())
    assert 1 not in [r["assay_id"] for r in available_versions(history+[moved], SAMPLE, group_key=GROUP)]
    corrected_future_sample = deepcopy(history[0])
    corrected_future_sample.update(version_id="future_time_revision", sample_at=(SAMPLE+timedelta(hours=2)).isoformat(),
                                   version_available_at=(SAMPLE-timedelta(hours=1)).isoformat())
    assert 1 not in [r["assay_id"] for r in available_versions(history+[corrected_future_sample], SAMPLE, group_key=GROUP)]
    unknown_group_tombstone = {"assay_id":1,"version_id":"unknown_group_tombstone","deleted":True,
                              "version_available_at":(SAMPLE-timedelta(hours=1)).isoformat(),
                              "provenance":{"kind":"immutable_deletion"}}
    assert 1 not in [r["assay_id"] for r in available_versions(history+[unknown_group_tombstone], SAMPLE, group_key=GROUP)]
    invalid = deepcopy(history[0]); invalid.update(version_id="invalid_value_revision",value=float("nan"),
                                                  version_available_at=(SAMPLE-timedelta(hours=1)).isoformat())
    with pytest.raises(ValueError, match="finite"):
        available_versions(history+[invalid], SAMPLE, group_key=GROUP)


def test_five_labels_and_two_dates_are_operational_gates_and_two_system_never_borrows(history):
    assert fitted(history[:4])["reason"] == "insufficient_normal_history_or_dates"
    one_day = [event(i, 12.0, SAMPLE-timedelta(hours=7-i)) for i in range(1, 6)]
    assert fitted(one_day)["status"] == "unavailable"
    state = fit_state(history, SAMPLE, "C5", group_key="0,1,1,0,0,0")
    assert state["reason"] == "unsupported_group" and state["training_ids"] == []


def test_shared_training_fence_keeps_outliers_in_audit_and_last_assay_reference(history):
    outlier = event(90, 50.0, SAMPLE-timedelta(hours=2), SAMPLE-timedelta(hours=1))
    states = [fitted(history+[outlier], method=f"C{i}") for i in range(8)]
    assert all(s["fence"] == states[0]["fence"] for s in states)
    assert all(s["training_ids"] == states[0]["training_ids"] for s in states)
    assert all(90 in s["excluded_training_ids"] for s in states)
    assert all(90 in s["training_ids"] for s in states)
    assert last_observed_reference(states[0]) == 50.0
    assert not inside(50.0, states[0]["fence"])


def test_huber_penalty_matches_independent_convex_objective(history):
    cfg = deepcopy(DEFAULT_CONFIG)
    rows = [event(i, 11.0-.04*(i-8)+(.3 if i % 2 else -.3), SAMPLE-timedelta(days=2, hours=i)) for i in range(1, 16)]
    rows[-1]["value"] = 20.0
    model = huber_ridge_fit(rows, ["level", "coal_amount"], cfg)
    x = np.array([[1, (r["features"]["level"]-50)/10, (r["features"]["coal_amount"]-600)/100] for r in rows])
    y = np.array([r["value"] for r in rows])
    def objective(beta):
        residual = y-x@beta
        loss = np.where(np.abs(residual) <= 1, .5*residual**2, np.abs(residual)-.5).sum()
        return loss+.5*np.dot(beta[1:], beta[1:])
    independent = minimize(objective, np.array([12., 0., 0.]), method="BFGS", options={"gtol":1e-7})
    assert objective(np.array(model["coefficients"])) == pytest.approx(independent.fun, abs=1e-8)
    assert model["gradient_max"] < 1e-6


def test_C7_formal_cold_start_has_zero_bias_and_proxy_receipts_cannot_enter(history):
    c7 = fitted(history, method="C7")
    assert c7["model"]["intercept_shift"] == 0
    assert c7["fallback"] == "C5_zero_bias_insufficient_prequential_receipts"
    proxy = fitted(history, SAMPLE-timedelta(hours=1), "C5", config={"receipt_mode":"historical_entry_proxy"})
    receipt = make_c5_receipt(proxy, {"level":50,"coal_amount":650}, sample_at=SAMPLE)
    assert receipt
    later = event(99, 13.0, SAMPLE, SAMPLE+timedelta(hours=1), c5_receipt=receipt)
    with pytest.raises(ValueError, match="modes cannot be mixed"):
        fitted(history+[later], SAMPLE+timedelta(hours=2), "C7")


def test_C7_only_uses_saved_prior_C5_predictions_and_rejects_in_sample_receipt(history):
    completed = deepcopy(history)
    receipt_states = {}
    for i in range(3):
        sample = SAMPLE+timedelta(hours=2*i)
        c5 = fitted(completed, sample, "C5", exclude_assay_id=90+i)
        receipt = make_c5_receipt(c5, {"level":50,"coal_amount":650}, sample_at=sample)
        receipt_states[receipt["state_hash"]] = c5
        completed.append(event(90+i, 13.0, sample, sample+timedelta(hours=1), c5_receipt=receipt,
                               features={"level":50.0,"coal_amount":650.0}))
    c7 = fitted(completed, SAMPLE+timedelta(hours=6), "C7")
    assert len(c7["residual_receipt_ids"]) == 3
    recent = [r for r in completed if r.get("c5_receipt")]
    expected = float(np.median([r["value"]-r["c5_receipt"]["prediction"] for r in recent]))*3/8
    assert c7["model"]["intercept_shift"] == pytest.approx(expected)
    forged = deepcopy(recent[-1]["c5_receipt"])
    forged["training_ids"].append(recent[-1]["assay_id"]); forged["training_versions"].append(recent[-1]["version_id"])
    forged["receipt_hash"] = digest({k:v for k,v in forged.items() if k != "receipt_hash"})
    with pytest.raises(ValueError, match="own target"):
        validate_c5_receipt(forged, recent[-1]["sample_at"], assay_id=recent[-1]["assay_id"])


def test_receipt_prediction_versions_and_features_replay_instead_of_trusting_a_hash(history):
    c5 = fitted(history, method="C5")
    receipt = make_c5_receipt(c5, {"level":50,"coal_amount":650}, sample_at=SAMPLE)
    wrong_prediction = deepcopy(receipt); wrong_prediction["prediction"] += .1
    wrong_prediction["receipt_hash"] = digest({k:v for k,v in wrong_prediction.items() if k != "receipt_hash"})
    with pytest.raises(ValueError, match="does not replay"):
        validate_c5_receipt(wrong_prediction, SAMPLE)
    wrong_versions = deepcopy(receipt); wrong_versions["training_versions"][0] += "altered"
    wrong_versions["receipt_hash"] = digest({k:v for k,v in wrong_versions.items() if k != "receipt_hash"})
    with pytest.raises(ValueError, match="mismatch"):
        validate_c5_receipt(wrong_versions, SAMPLE)
    with pytest.raises(ValueError, match="exact sampling-time features"):
        validate_c5_receipt(receipt, SAMPLE, features={"level":55,"coal_amount":650})


def test_association_preserves_original_receipt_and_checks_future_sample_window(history):
    original = make_c5_receipt(fitted(history, method="C5"), {"level":50,"coal_amount":650},
                               sample_at=SAMPLE,clock_domain="host",clock_epoch="epoch1")
    fields = {"sample_at":(SAMPLE+timedelta(seconds=20)).isoformat(),"input_asof":SAMPLE.isoformat(),
              "postseal_at":(SAMPLE+timedelta(seconds=5)).isoformat(),"max_age_seconds":30,
              "matching_policy":"latest-eligible","prediction_event_hash":"a"*64,"prediction_seal_hash":"b"*64,
              "label_event_hash":"c"*64,"label_version_id":"c"*64,"association_seal_hash":"d"*64,
              "issued_features_hash":original["features_hash"],"training_features_hash":digest({"level":55.0,"coal_amount":700.0}),
              "group_key":GROUP,"clock_domain":"host","clock_epoch":"epoch1",
              "bound_at":(SAMPLE+timedelta(hours=1)).isoformat()}
    validator = lambda envelope: True  # The synthetic immutable registry's verdict.
    bound = make_c5_association(original, association_validator=validator, **fields)
    assert bound["original_receipt"] == original
    assert validate_c5_receipt(bound, fields["sample_at"], group_key=GROUP,
                               features={"level":55,"coal_amount":700}, association_validator=validator) == original
    assert validate_c5_receipt(bound, fields["sample_at"], prediction_at=SAMPLE+timedelta(minutes=30)) is None
    with pytest.raises(ValueError, match="sample window"):
        make_c5_association(original, association_validator=validator, **{**fields,"sample_at":(SAMPLE+timedelta(seconds=31)).isoformat()})
    with pytest.raises(ValueError, match="clock domain"):
        make_c5_association(original, association_validator=validator, **{**fields,"clock_epoch":"other_epoch"})
    with pytest.raises(ValueError, match="trusted runtime registry"):
        validate_c5_receipt(bound, fields["sample_at"])
    with pytest.raises(ValueError, match="different label version"):
        validate_c5_receipt(bound, fields["sample_at"],label_version_id="newer",association_validator=validator)


def test_serialized_hash_replays_and_tampering_is_rejected(history):
    state = fitted(history, method="C5")
    decoded = json.loads(json.dumps(state))
    verify_state(decoded)
    features = {"level": 90.0, "coal_amount": 600.0}
    assert predict_state(state, features) == predict_state(decoded, features)
    assert predict_state(decoded, features)["outside_predictors"] == ["level"]
    decoded["model"]["coefficients"][0] += 1
    with pytest.raises(ValueError, match="hash mismatch"):
        predict_state(decoded, features)


def test_clip_uses_finite_raw_and_C7_adds_bias_to_saved_output_semantics(history):
    c7 = fitted(history, method="C7")
    c7.pop("state_hash")
    c7["model"].update(coefficients=[150.0,0.0],intercept_shift=-10.0)
    c7["state_hash"] = digest(c7)
    result = predict_state(c7, {"level":50,"coal_amount":650})
    assert result["base_raw_prediction"] == 150.0
    assert result["base_final_prediction"] == 100.0
    assert result["raw_prediction"] == result["prediction"] == 90.0
    c5 = fitted(history, method="C5")
    c5.pop("state_hash")
    c5["model"].update(coefficients=[1e308,1e308])
    c5["state_hash"] = digest(c5)
    result = predict_state(c5, {"level":1e308,"coal_amount":650})
    assert result["prediction"] is None
    assert result["status"] == "unavailable" and "finite" in result["reason"]


def test_timezone_provenance_and_weighted_median_contracts(history):
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_time("2026-11-02T12:00:00")
    wrong = deepcopy(history); wrong[0].pop("provenance")
    with pytest.raises(ValueError, match="provenance"):
        fitted(wrong)
    assert weighted_median([1, 2, 3, 4], [1, 1, 1, 1]) == 2.5
    assert weighted_median([1, 2, 3], [1, 6, 1]) == 2.0


def test_development_selection_never_uses_later_results_and_scores_raw_separately():
    from scripts.gpt_expert81_experiment import choose_candidate, score, file_hash, PROTOCOL_PATH, LOCKED_PROTOCOL_SHA
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    assert file_hash(PROTOCOL_PATH) == LOCKED_PROTOCOL_SHA
    records = []
    for method in [f"C{i}" for i in range(8)]:
        for i in range(2):
            error = .5 if method == "C1" else .8
            records.append({"mode":"historical_entry_proxy","phase":"development","assay_id":i,
                            "method":method,"prediction":12+error,"normal":True,"error":error,
                            "abs_error":error,"raw_error":error+1,"raw_abs_error":error+1,"date":f"2026-10-{20+i}"})
    assert choose_candidate(records, protocol)["selected_candidate_id"] == "C1"
    later = [{**r,"phase":"later_already_viewed","abs_error":0,"error":0} for r in records if r["method"] == "C7"]
    assert choose_candidate(records+later, protocol)["selected_candidate_id"] == "C1"
    own = [r for r in records if r["method"] == "C1"]
    assert score(own)["MAE"] == .5 and score(own,raw=True)["MAE"] == 1.5
    clipped = [{**own[0],"abs_error":0.0,"error":0.0,"raw_abs_error":2.0,"raw_error":2.0},
               {**own[1],"abs_error":1.0,"error":1.0,"raw_abs_error":4.0,"raw_error":4.0}]
    assert score(clipped)["date_balanced_MAE"] == .5
    assert score(clipped,raw=True)["date_balanced_MAE"] == 3.0
