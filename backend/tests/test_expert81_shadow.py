"""Shadow timing/immutability contracts; all data is synthetic and all files temporary."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from app.gpt_coarse import expert81_shadow as shadow
from app.gpt_coarse.experiments import expert81 as core


BASE = datetime(2026, 11, 3, 4, 0, tzinfo=timezone.utc)
ORIGIN = "http://example.invalid:8000"
DOMAIN = "production-host-test"


class Clock:
    def __init__(self):
        self.at = BASE + timedelta(seconds=38)

    def __call__(self):
        self.at += timedelta(microseconds=1)
        return self.at

    def host(self, seconds):
        self.at = max(self.at, BASE + timedelta(seconds=38 + seconds))


def anchor(clock, seconds, *, domain=DOMAIN, epoch="test-epoch"):
    return shadow._HostAnchor({"requested_at": shadow.stamp(clock()), "received_at": shadow.stamp(clock()),
        "host_checked_at": shadow.stamp(BASE + timedelta(seconds=seconds)), "response_sha256": "a" * 64,
        "host_origin": ORIGIN, "clock_domain": domain, "clock_epoch": epoch, "transport": "readonly-live-cache"})


def frame(seconds=0, *, group=shadow.PRIMARY_GROUP, formal=True, level=50.0):
    from app.telemetry.domain import definition_id

    values = {"coal_amount": 600.0, "level": level}
    values.update(dict(zip(shadow.STATE_KEYS, map(int, group.split(",")))))
    raw = {"input_asof_at": shadow.stamp(BASE + timedelta(seconds=seconds)),
        "inputs": {k: {"value": values[k], "status": "good"} for k in shadow.FEATURES},
        "clock_domain": DOMAIN, "clock_epoch": "test-epoch", "host_origin": ORIGIN,
        "transport": "readonly-cache-frame", "input_anchor": {"fixture": "synthetic"},
        "frozen_model_sha256": hashlib.sha256(shadow.FROZEN_MODEL_PATH.read_bytes()).hexdigest(),
        "frozen_prediction": 15.5, "unusable_reasons": [],
        "source_semantics": deepcopy(shadow.LEGACY_SOURCE_SEMANTICS)}
    for key, identity in shadow.LEGACY_SOURCE_SEMANTICS["members"].items():
        definition = {**identity, "value_type": "bool"}
        raw["inputs"][key].update(item_id=identity["item_id"], definition=definition,
            definition_id=definition_id(definition), source_id="synthetic-source", point_key=key,
            sample={"key": key, "node_id": identity["node_id"],
                    "definition_id": definition_id(definition), "value": values[key]})
    return shadow._LiveFrame(raw) if formal else raw


@pytest.fixture
def audited_synthetic_spec(monkeypatch):
    """Register only this test's synthetic evidence; never relabel real artifacts."""
    def create(artifact, **kwargs):
        assert artifact["model_version"] == "EXPERT8.1-test"
        monkeypatch.setitem(shadow.AUDITED_LEGACY_ARTIFACTS, shadow.digest(artifact),
                            shadow.digest(artifact.get("history_events", [])))
        return shadow.model_spec(artifact, **kwargs)
    return create


@pytest.fixture
def run(tmp_path, audited_synthetic_spec):
    clock = Clock()
    rows = []
    for index in range(6):
        rows.append({"assay_id": str(index + 1), "value": 15.0 + index * .1,
            "sample_at": shadow.stamp(BASE - timedelta(days=3) + timedelta(hours=12 * index)),
            "group_key": shadow.PRIMARY_GROUP, "features": {"level": 30.0 + index * 6, "coal_amount": 550.0 + index * 20},
            "frozen_prediction": 15.5, "provenance": {"kind": "synthetic_audit"}})
    artifact = {"model_version": "EXPERT8.1-test", "candidate_id": "C5", "config": {}, "history_events": rows}
    spec = audited_synthetic_spec(artifact, host_origin=ORIGIN, clock_domain=DOMAIN, clock_epoch="test-epoch")
    result = shadow.ShadowRun(tmp_path / "shadow.jsonl", spec, clock=clock)
    versions = result.capture_frozen_seed(artifact)
    result.seal_labels([v["event_hash"] for v in versions], anchor(clock, -1))
    return result, clock


def delayed_label(sample_seconds=3, assay_id="1000", **extra):
    return {"assay_id": assay_id, "value": 18.0, "sample_at": shadow.stamp(BASE + timedelta(seconds=sample_seconds)),
            "sample_precision": "second", "sample_clock_domain": DOMAIN,
            "sample_clock_evidence": {"confirmed_by": "synthetic_test", "clock_source": "production-host"},
            "source_semantics": deepcopy(shadow.LEGACY_SOURCE_SEMANTICS),
            "group_key": shadow.PRIMARY_GROUP, **extra}


def capture_and_seal(run, clock, label, at=3600):
    clock.host(at)
    version = run.capture_label(label)
    run.seal_labels([version["event_hash"]], anchor(clock, at))
    run.seal_associations(anchor(clock, at + .001))
    return version


def test_delayed_assay_scores_saved_prediction_and_only_calibrates_later(run):
    runtime, clock = run
    original = runtime.predict(frame())
    runtime.seal_prediction(original["event_hash"], anchor(clock, 1))
    before_bytes = runtime.journal.path.read_bytes()
    version = capture_and_seal(runtime, clock, delayed_label())
    score = runtime.score(version["event_hash"])
    assert score["payload"]["prediction_hash"] == original["event_hash"]
    assert score["payload"]["prediction"] == original["payload"]["result"]["prediction"]
    assert "1000" not in original["payload"]["state"]["training_ids"]
    assert runtime.journal.path.read_bytes().startswith(before_bytes)
    clock.host(3601)
    later = runtime.predict(frame(3601))
    assert "1000" in later["payload"]["state"]["training_ids"]
    assert runtime.events()[original["seq"] - 1] == original
    association = next(e for e in runtime.events() if e["kind"] == "label_association")
    envelope = association["payload"]["c5_association"]
    assert envelope["original_receipt"] == original["payload"]["c5_receipt"]
    assert envelope["original_receipt"]["sample_at"] != envelope["sample_at"]
    assert runtime._association_validator(runtime.events())(envelope) is True


def test_same_host_timestamp_excludes_label_even_with_earlier_local_sequence(tmp_path, audited_synthetic_spec):
    clock = Clock()
    row = {"assay_id": "100", "value": 16.0, "sample_at": shadow.stamp(BASE - timedelta(days=1)),
        "group_key": shadow.PRIMARY_GROUP, "features": {"level": 45.0, "coal_amount": 600.0}, "frozen_prediction": 15.5}
    artifact = {"model_version": "EXPERT8.1-test", "candidate_id": "C5", "config": {}, "history_events": [row]}
    runtime = shadow.ShadowRun(tmp_path / "same.jsonl", audited_synthetic_spec(artifact, host_origin=ORIGIN,
        clock_domain=DOMAIN, clock_epoch="test-epoch"), clock=clock)
    clock.host(10)
    version = runtime.capture_frozen_seed(artifact)[0]
    runtime.seal_labels([version["event_hash"]], anchor(clock, 10))
    simultaneous = runtime.predict(frame(10))
    after = runtime.predict(frame(10.001))
    assert "100" not in simultaneous["payload"]["state"]["training_ids"]
    assert "100" in after["payload"]["state"]["training_ids"]
    assert version["recorded_at"].startswith("2026-")
    assert "capture_at" not in version["payload"]


def test_revision_and_tombstone_preserve_predictions_and_stop_later_calibration(run):
    runtime, clock = run
    first = runtime.predict(frame())
    runtime.seal_prediction(first["event_hash"], anchor(clock, 1))
    version = capture_and_seal(runtime, clock, delayed_label())
    old_score = runtime.score(version["event_hash"])
    revised = capture_and_seal(runtime, clock, delayed_label(value=19.0), at=3602)
    new_score = runtime.score(revised["event_hash"])
    assert new_score["payload"]["prediction_hash"] == old_score["payload"]["prediction_hash"]
    assert old_score["payload"]["value"] == 18.0
    assert new_score["payload"]["value"] == 19.0
    tombstone = capture_and_seal(runtime, clock, {"assay_id": "1000", "deleted": True}, at=3604)
    clock.host(3605)
    later = runtime.predict(frame(3605))
    assert "1000" not in later["payload"]["state"]["training_ids"]
    assert first == runtime.events()[first["seq"] - 1]
    assert tombstone["payload"]["previous_version_hash"] == revised["event_hash"]


def test_group_change_or_unknown_revision_cannot_revive_old_version(run):
    runtime, clock = run
    label = {"assay_id": "1", "value": 22.0, "sample_at": shadow.stamp(BASE - timedelta(days=2)), "group_key": "unknown"}
    version = capture_and_seal(runtime, clock, label, at=2)
    clock.host(3)
    later = runtime.predict(frame(3))
    assert "1" not in later["payload"]["state"]["training_ids"]
    assert version["payload"]["version_number"] == 2


def test_prediction_and_label_idempotency_survive_restart(run):
    runtime, clock = run
    prediction = runtime.predict(frame(), key="test-key")
    size = runtime.journal.path.stat().st_size
    other = shadow.ShadowRun(runtime.journal.path, clock=clock)
    assert other.predict(frame(), key="test-key") == prediction
    assert other.journal.path.stat().st_size == size
    with pytest.raises(shadow.LedgerError, match="different input"):
        other.predict(frame(level=51), key="test-key")
    label = delayed_label()
    first = other.capture_label(label)
    assert other.capture_label(label) == first


@pytest.mark.parametrize("sample,precision,evidence,status", [
    (1, "second", {"confirmed_by": "test"}, "no_eligible_prior_prediction"),
    (31, "second", {"confirmed_by": "test"}, "no_eligible_prior_prediction"),
    (3, "minute", {"confirmed_by": "test"}, "sample_precision_unverified"),
    (3, "second", None, "sample_clock_unverified"),
])
def test_pairing_refuses_seal_boundary_stale_sample_or_unverified_clock(run, sample, precision, evidence, status):
    runtime, clock = run
    prediction = runtime.predict(frame())
    runtime.seal_prediction(prediction["event_hash"], anchor(clock, 1))
    version = capture_and_seal(runtime, clock, delayed_label(sample, sample_precision=precision, sample_clock_evidence=evidence))
    score = runtime.score(version["event_hash"])
    assert score["payload"]["status"] == "unmatched"
    assert score["payload"]["reason"] == status


def test_latest_eligible_prediction_is_fixed_without_looking_at_label_error(run):
    runtime, clock = run
    first = runtime.predict(frame())
    runtime.seal_prediction(first["event_hash"], anchor(clock, 1))
    clock.host(5)
    second = runtime.predict(frame(5, level=51))
    runtime.seal_prediction(second["event_hash"], anchor(clock, 6))
    version = capture_and_seal(runtime, clock, delayed_label(7))
    assert runtime.score(version["event_hash"])["payload"]["prediction_hash"] == second["event_hash"]


@pytest.mark.parametrize("group", [None, "", "unknown", "0,0,1,1,0,0"])
def test_sample_group_must_be_confirmed_and_match_the_issued_prediction(run, group):
    runtime, clock = run
    prediction = runtime.predict(frame())
    runtime.seal_prediction(prediction["event_hash"], anchor(clock, 1))
    version = capture_and_seal(runtime, clock, delayed_label(group_key=group))
    score = runtime.score(version["event_hash"])
    assert score["payload"]["status"] == "unmatched"
    clock.host(3602)
    later = runtime.predict(frame(3602))
    assert "1000" not in later["payload"]["state"]["training_ids"]


def test_label_known_before_prediction_cannot_receive_a_retrospective_score(run):
    runtime, clock = run
    clock.host(3)
    version = runtime.capture_label(delayed_label())
    prediction = runtime.predict(frame(3))
    runtime.seal_prediction(prediction["event_hash"], anchor(clock, 4))
    runtime.seal_labels([version["event_hash"]], anchor(clock, 5))
    assert runtime.score(version["event_hash"])["payload"]["status"] == "unmatched"


def test_clock_reversal_is_latched_and_old_host_seals_are_rejected(run):
    runtime, clock = run
    prediction = runtime.predict(frame())
    runtime.seal_prediction(prediction["event_hash"], anchor(clock, 1))
    later = runtime.predict(frame(2))
    with pytest.raises(shadow.ClockError, match="backwards"):
        runtime.seal_prediction(later["event_hash"], anchor(clock, .5))
    clock.host(3)
    with pytest.raises(shadow.ClockError, match="recorded fault"):
        runtime.predict(frame(3))
    assert any(e["kind"] == "clock_fault" for e in runtime.events())


def test_postseal_cannot_precede_input_or_durable_prediction(run):
    runtime, clock = run
    prediction = runtime.predict(frame(2))
    before_durable = anchor(clock, 3)
    before_durable["requested_at"] = shadow.stamp(BASE)
    with pytest.raises(shadow.ClockError, match="after"):
        runtime.seal_prediction(prediction["event_hash"], before_durable)
    with pytest.raises(shadow.ClockError, match="precedes"):
        runtime.seal_prediction(prediction["event_hash"], anchor(clock, 1))


def test_inflight_old_frame_and_overlapping_anchor_are_rejected_without_clock_fault(run):
    runtime, clock = run
    old_frame = frame()
    old_frame["input_anchor"] = anchor(clock, 0)
    first = runtime.predict(frame())
    runtime.seal_prediction(first["event_hash"], anchor(clock, 1))
    with pytest.raises(shadow.ClockError, match="stale or overlapping"):
        runtime.predict(old_frame)
    assert not any(e["kind"] == "clock_fault" for e in runtime.events())
    clock.host(3600)
    v1 = runtime.capture_label(delayed_label(assay_id="700"))
    v2 = runtime.capture_label(delayed_label(assay_id="701"))
    early_anchor = anchor(clock, 3600)
    later_anchor = anchor(clock, 3601)
    runtime.seal_labels([v2["event_hash"]], later_anchor)
    early_anchor["received_at"] = shadow.stamp(clock())
    with pytest.raises(shadow.ClockError, match="stale or overlapping"):
        runtime.seal_labels([v1["event_hash"]], early_anchor)
    assert not any(e["kind"] == "clock_fault" for e in runtime.events())


def test_unused_coal_factor_still_reports_feature_support(run):
    runtime, clock = run
    raw = frame()
    raw["inputs"]["coal_amount"]["value"] = 339.0
    issued = runtime.predict(raw)
    assert issued["payload"]["result"]["prediction"] is not None
    assert issued["payload"]["result"]["outside_predictors"] == []
    assert issued["payload"]["feature_support"]["coal_amount"]["within_normal_training_range"] is False


def test_38_second_notebook_skew_does_not_change_host_cutoffs_or_scoring(run):
    runtime, clock = run
    prediction = runtime.predict(frame())
    runtime.seal_prediction(prediction["event_hash"], anchor(clock, 1))
    assert core.parse_time(prediction["recorded_at"]) - core.parse_time(prediction["payload"]["fit_cutoff_at"]) >= timedelta(seconds=38)
    version = capture_and_seal(runtime, clock, delayed_label())
    assert runtime.score(version["event_hash"])["payload"]["status"] == "scored"


def test_file_timestamps_or_transport_strings_cannot_impersonate_formal_receipt(run):
    runtime, clock = run
    serialized = frame(formal=False)
    prediction = runtime.predict(serialized)
    assert prediction["payload"]["evidence_mode"] == "file_exploratory"
    with pytest.raises(shadow.ClockError, match="transport"):
        runtime.seal_prediction(prediction["event_hash"], dict(anchor(clock, 1)))
    bad = frame()
    bad["prediction_at"] = shadow.stamp(BASE - timedelta(days=1))
    with pytest.raises(ValueError, match="assigned"):
        runtime.predict(bad)


def test_two_system_and_user_excluded_197_are_unavailable(run):
    runtime, clock = run
    two = runtime.predict(frame(group="0,0,1,1,0,0"))
    assert two["payload"]["result"]["prediction"] is None
    assert two["payload"]["state"]["reason"] == "unsupported_group"
    version = capture_and_seal(runtime, clock, delayed_label(assay_id="197"))
    assert runtime.score(version["event_hash"])["payload"]["reason"] == "user_excluded_bad_sample_time"


def test_partial_line_and_hash_edit_fail_closed_without_rewriting(run):
    runtime, clock = run
    runtime.predict(frame())
    data = runtime.journal.path.read_bytes()
    runtime.journal.path.write_bytes(data + b'{"partial":')
    with pytest.raises(shadow.LedgerError, match="preserve"):
        runtime.events()
    assert runtime.journal.path.read_bytes() == data + b'{"partial":'
    runtime.journal.path.write_bytes(data.replace(b'EXPERT8.1-test', b'EXPERT8.1-edit'))
    with pytest.raises(shadow.LedgerError):
        runtime.events()


def test_concurrent_writer_is_refused_and_lock_releases_after_restart(run):
    runtime, clock = run
    with runtime.journal.transaction():
        with pytest.raises(shadow.LedgerError, match="locked"):
            shadow.ShadowRun(runtime.journal.path, clock=clock)
    assert shadow.ShadowRun(runtime.journal.path, clock=clock).summary()["events"] > 1


def test_cli_has_no_historical_prediction_time_switch():
    path = Path(__file__).resolve().parents[1] / "scripts/expert81_shadow.py"
    module_spec = importlib.util.spec_from_file_location("expert81_shadow_cli_test", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    with pytest.raises(SystemExit):
        module.parser().parse_args(["tick", "--ledger", "unused", "--at", "2000-01-01T00:00Z"])


def test_ordinary_label_cannot_inject_posthoc_audit_features_or_backdate_capture(run):
    runtime, clock = run
    clock.host(3)
    label = delayed_label(assay_id="500", sample_at=shadow.stamp(BASE - timedelta(days=1)),
        audit_features={"level": 50.0, "coal_amount": 600.0}, audit_frozen_prediction=15.0,
        audit_evidence={"kind": "fabricated"}, capture_at="2000-01-01T00:00:00Z", version_available_at="2000-01-01T00:00:00Z")
    version = runtime.capture_label(label)
    runtime.seal_labels([version["event_hash"]], anchor(clock, 3))
    clock.host(4)
    prediction = runtime.predict(frame(4))
    assert "500" not in prediction["payload"]["state"]["training_ids"]
    assert "audit_features" not in version["payload"]
    assert "capture_at" not in version["payload"]
    assert version["recorded_at"].startswith("2026-")
    with pytest.raises(ValueError, match="boolean"):
        runtime.capture_label({"assay_id": "501", "deleted": "false"})
    assert runtime.summary()["frozen_seed_count"] == 6


def test_seed_manifest_and_frozen_baseline_cannot_be_changed(run):
    runtime, clock = run
    with pytest.raises(shadow.LedgerError, match="manifest"):
        runtime.capture_frozen_seed({"history_events": []})
    altered = frame()
    altered["frozen_model_sha256"] = "b" * 64
    with pytest.raises(shadow.LedgerError, match="baseline"):
        runtime.predict(altered)


def test_sealed_label_retry_does_not_treat_its_old_anchor_as_clock_reversal(run):
    runtime, clock = run
    version = runtime.capture_label(delayed_label(assay_id="500"))
    clock.host(5)
    old = anchor(clock, 5)
    runtime.seal_labels([version["event_hash"]], old)
    clock.host(6)
    prediction = runtime.predict(frame(6))
    runtime.seal_prediction(prediction["event_hash"], anchor(clock, 7))
    size = runtime.journal.path.stat().st_size
    assert runtime.seal_labels([version["event_hash"]], old) is None
    assert runtime.journal.path.stat().st_size == size
    assert not any(e["kind"] == "clock_fault" for e in runtime.events())


def test_complete_line_after_fsync_failure_is_reflushed_before_idempotent_return(run, monkeypatch):
    runtime, clock = run
    actual_fsync = shadow.os.fsync
    calls = []
    def sometimes_fail(fd):
        calls.append(fd)
        if len(calls) == 3:
            raise OSError("synthetic fsync failure after a complete append")
        return actual_fsync(fd)
    monkeypatch.setattr(shadow.os, "fsync", sometimes_fail)
    with pytest.raises(OSError, match="synthetic"):
        runtime.predict(frame(), key="durability-test")
    before_retry = len(calls)
    returned = runtime.predict(frame(), key="durability-test")
    assert returned["payload"]["key"] == "durability-test"
    assert len(calls) > before_retry
    assert sum(e["kind"] == "prediction" for e in runtime.events()) == 1


@pytest.mark.parametrize("failure_kind", ["association_proposal", "label_association"])
def test_restart_completes_partial_association_without_new_fit_or_rebinding(run, monkeypatch, failure_kind):
    runtime, clock = run
    prediction = runtime.predict(frame())
    runtime.seal_prediction(prediction["event_hash"], anchor(clock, 1))
    clock.host(3600)
    version = runtime.capture_label(delayed_label())
    original_append = shadow._Transaction.append
    def fail_before_target(self, kind, payload):
        if kind == failure_kind:
            raise OSError("synthetic interruption after previous durable event")
        return original_append(self, kind, payload)
    lab_anchor = anchor(clock, 3600)
    if failure_kind == "association_proposal":
        with monkeypatch.context() as patch:
            patch.setattr(shadow._Transaction, "append", fail_before_target)
            with pytest.raises(OSError):
                runtime.seal_labels([version["event_hash"]], lab_anchor)
    else:
        runtime.seal_labels([version["event_hash"]], lab_anchor)
        with monkeypatch.context() as patch:
            patch.setattr(shadow._Transaction, "append", fail_before_target)
            with pytest.raises(OSError):
                runtime.seal_associations(anchor(clock, 3601))
    restarted = shadow.ShadowRun(runtime.journal.path, clock=clock)
    restarted.seal_labels([version["event_hash"]], lab_anchor)
    restarted.seal_associations(anchor(clock, 3602))
    associations = [e for e in restarted.events() if e["kind"] == "label_association"]
    assert len(associations) == 1
    envelope = associations[0]["payload"]["c5_association"]
    assert envelope["prediction_event_hash"] == prediction["event_hash"]
    assert envelope["original_receipt"] == prediction["payload"]["c5_receipt"]
    assert restarted._association_validator(restarted.events())(envelope) is True
    assert sum(e["kind"] == "prediction" for e in restarted.events()) == 1


def test_training_record_blobs_deduplicate_and_expand_to_exact_core_state(run):
    runtime, clock = run
    first = runtime.predict(frame())
    second = runtime.predict(frame(2))
    events = runtime.events()
    assert sum(e["kind"] == "calibration_blob" for e in events) == 1
    raw = [json.loads(line) for line in runtime.journal.path.read_text(encoding="utf-8").splitlines()]
    stored = next(e for e in raw if e["event_hash"] == second["event_hash"])
    assert "$calibration_records" in stored["payload"]["state"]["training_records"]
    expanded = next(e for e in events if e["event_hash"] == first["event_hash"])
    core.verify_state(expanded["payload"]["state"])
    core.validate_c5_receipt(expanded["payload"]["c5_receipt"], expanded["payload"]["input"]["input_asof_at"])
    assert expanded == first


def synthetic_cache(tmp_path, *, desliming_members=None):
    from app.telemetry.derived import FOUR_SYSTEM_MEMBERS, DESLIMING_MEMBERS
    from app.telemetry.domain import definition_id
    from app.hourly_brief.inputs import COAL_AMOUNT, LEVEL
    model_path = Path(__file__).resolve().parents[1] / "models/gpt_expert8/20261012/model.json"
    source = shadow.ReadOnlyCacheSource({"server_url": ORIGIN, "token": "test"}, model_path,
                                      clock_domain=DOMAIN, clock_epoch="test-epoch")
    items = [(COAL_AMOUNT, 600.0), (LEVEL, 50.0)]
    values = {"A": False, "B": True, "401": True, "402": True, "473": False, "474": False}
    members = DESLIMING_MEMBERS if desliming_members is None else desliming_members
    items += [(item, values[label]) for label, item in (*FOUR_SYSTEM_MEMBERS, *members)]
    points, current, lagged = [], [], []
    for index, (item, value) in enumerate(items):
        config = {"item_id": item, "node_id": "ns=2;s=" + item, "max_age_seconds": 30,
                  "unit": "%" if item == LEVEL else "t/h" if item == COAL_AMOUNT else ""}
        key = "p" + str(index)
        points.append({"source_id": "s1", "key": key, "config": config})
        sample = {"key": key, "node_id": config["node_id"], "definition_id": definition_id(config), "received_at": shadow.stamp(BASE),
                  "value": value, "quality": "good"}
        current.append(sample)
        if item == LEVEL:
            lagged.append(dict(sample, received_at=shadow.stamp(BASE - timedelta(seconds=90))))
    data = {"checked_at": shadow.stamp(BASE), "sources": {"s1": {"version": 1, "source_id": "s1", "owner": "owner",
        "published_at": shadow.stamp(BASE), "poll_seconds": 5, "state": "connected", "batches": [
            {"batch_id": "lagged", "source_id": "s1", "samples": lagged},
            {"batch_id": "current", "source_id": "s1", "samples": current}]}}}
    metadata = {"points": points, "sources": [{"source_id": "s1", "owner": "owner", "lease_until": shadow.stamp(BASE + timedelta(seconds=30)), "state": "connected"}],
                "revision": {"id": 1, "config": {"choices": {}}}}
    return source, data, metadata


def test_cache_extraction_retains_quality_lag_definition_and_frozen_model(tmp_path, monkeypatch):
    from app.telemetry import derived
    monkeypatch.setattr(derived, "DESLIMING_MEMBERS", derived.LEGACY_DESLIMING_MEMBERS)
    source, data, metadata = synthetic_cache(tmp_path)
    result = source.frame_from_cache(data, metadata, anchor(Clock(), 0))
    assert result["unusable_reasons"] == []
    assert result["inputs"]["level"]["observed_at"] == shadow.stamp(BASE - timedelta(seconds=90))
    assert result["inputs"]["level"]["lag_seconds"] == 90
    assert len(result["frozen_model_sha256"]) == 64
    assert result["frozen_prediction"] is not None


@pytest.mark.parametrize("failure", ["bad_latest", "definition_changed", "old_owner", "missing_lag", "old_nonlag"])
def test_cache_extraction_never_falls_back_around_bad_latest_or_wrong_definition(tmp_path, failure, monkeypatch):
    from app.telemetry import derived
    monkeypatch.setattr(derived, "DESLIMING_MEMBERS", derived.LEGACY_DESLIMING_MEMBERS)
    source, data, metadata = synthetic_cache(tmp_path)
    doc = data["sources"]["s1"]
    if failure == "bad_latest":
        bad = deepcopy(doc["batches"][0]["samples"][0])
        bad["quality"] = "bad"
        doc["batches"].insert(1, {"source_id": "s1", "batch_id": "bad-newest-lag", "samples": [bad]})
    elif failure == "definition_changed":
        metadata["points"][1]["config"]["item_id"] = "changed-definition"
    elif failure == "old_owner":
        doc["owner"] = "old-owner"
    elif failure == "missing_lag":
        doc["batches"][0]["samples"] = []
    elif failure == "old_nonlag":
        for sample in doc["batches"][1]["samples"]:
            if sample["key"] != "p1":
                sample["received_at"] = shadow.stamp(BASE - timedelta(seconds=11))
    result = source.frame_from_cache(data, metadata, anchor(Clock(), 0))
    assert result["unusable_reasons"]
    assert result["frozen_prediction"] is None


def test_runs_false_false_cache_cannot_use_old_seed_even_with_identical_group(run, tmp_path, monkeypatch):
    from app.telemetry import derived

    runtime, clock = run
    monkeypatch.setattr(derived, "DESLIMING_MEMBERS", (
        ("473", "TIA_3_3.TIA_3_3.473/RUNS"), ("474", "TIA_3_3.TIA_3_3.474/RUNS")))
    source, data, metadata = synthetic_cache(tmp_path)
    extracted = source.frame_from_cache(data, metadata, anchor(clock, 0))
    bits = ",".join(str(extracted["inputs"][k]["value"]) for k in shadow.STATE_KEYS)
    assert bits == shadow.PRIMARY_GROUP
    assert "unsupported_desliming_source_semantics" in extracted["unusable_reasons"]
    assert extracted["frozen_prediction"] is None
    before = runtime.journal.path.read_bytes()

    def forbidden_fit(*args, **kwargs):
        pytest.fail("an unsupported source must be rejected before fitting or constructing a receipt")

    monkeypatch.setattr(core, "fit_state", forbidden_fit)
    with pytest.raises(shadow.LedgerError, match="unsupported_desliming_source_semantics"):
        runtime.predict(extracted)
    assert runtime.journal.path.read_bytes() == before


def test_old_if_prod_cache_retains_explicit_source_evidence_and_valid_prediction(run, tmp_path, monkeypatch):
    from app.telemetry import derived

    runtime, clock = run
    monkeypatch.setattr(derived, "DESLIMING_MEMBERS", derived.LEGACY_DESLIMING_MEMBERS)
    source, data, metadata = synthetic_cache(tmp_path)
    extracted = source.frame_from_cache(data, metadata, anchor(clock, 0))
    prediction = runtime.predict(extracted)["payload"]
    assert prediction["result"]["status"] == "ok"
    assert prediction["source_semantics"] == shadow.LEGACY_SOURCE_SEMANTICS
    assert prediction["c5_receipt"] is not None
    assert all(r["provenance"]["source_semantics"] == shadow.LEGACY_SOURCE_SEMANTICS
               for r in prediction["state"]["training_records"])


@pytest.mark.parametrize("failure", ["missing_domain", "malformed_domain", "missing_definition", "malformed_definition",
                                     "missing_item", "missing_node", "wrong_sample_definition", "missing_sample", "wrong_point",
                                     "missing_raw_node", "wrong_raw_node", "raw_value_mismatch", "raw_nonbinary_value", "raw_string_value"])
def test_direct_frame_missing_or_malformed_source_evidence_is_rejected_before_fit(run, monkeypatch, failure):
    runtime, _ = run
    raw = frame()
    cell = raw["inputs"]["desliming473"]
    if failure == "missing_domain":
        raw.pop("source_semantics")
    elif failure == "malformed_domain":
        raw["source_semantics"] = "legacy"
    elif failure == "missing_definition":
        cell.pop("definition")
    elif failure == "malformed_definition":
        cell["definition"] = "legacy"
    elif failure == "missing_item":
        cell.pop("item_id")
    elif failure == "missing_node":
        cell["definition"].pop("node_id")
    elif failure == "wrong_sample_definition":
        cell["sample"]["definition_id"] = "b" * 64
    elif failure == "missing_sample":
        cell.pop("sample")
    elif failure == "wrong_point":
        cell["sample"]["key"] = "a-different-point"
    elif failure == "missing_raw_node":
        cell["sample"].pop("node_id")
    elif failure == "wrong_raw_node":
        cell["sample"]["node_id"] = "ns=2;s=TIA_3_3.TIA_3_3.473/RUNS"
    elif failure == "raw_value_mismatch":
        cell["sample"]["value"] = True
    elif failure == "raw_nonbinary_value":
        cell["sample"]["value"] = 2
    elif failure == "raw_string_value":
        cell["sample"]["value"] = "0"
    before = runtime.journal.path.read_bytes()
    monkeypatch.setattr(core, "fit_state", lambda *a, **kw: pytest.fail("source check must precede fit"))
    with pytest.raises(shadow.LedgerError, match="desliming_source"):
        runtime.predict(raw)
    assert runtime.journal.path.read_bytes() == before


def test_direct_frame_cannot_disguise_runs_definition_with_legacy_display_metadata(run, monkeypatch):
    from app.telemetry.domain import definition_id

    runtime, _ = run
    raw = frame()
    cell = raw["inputs"]["desliming473"]
    # The display item and claimed domain still say if_prod. The actual node is RUNS.
    cell["definition"]["node_id"] = "ns=2;s=TIA_3_3.TIA_3_3.473/RUNS"
    cell["definition_id"] = cell["sample"]["definition_id"] = definition_id(cell["definition"])
    monkeypatch.setattr(core, "fit_state", lambda *a, **kw: pytest.fail("RUNS must not reach the legacy fit"))
    with pytest.raises(shadow.LedgerError, match="unsupported_desliming_source_semantics"):
        runtime.predict(raw)


@pytest.mark.parametrize("semantics", [None, "legacy", {"schema": "RUNS", "members": {}}])
def test_changed_or_unknown_label_domain_suppresses_older_calibration_version(run, semantics):
    runtime, clock = run
    original = runtime.predict(frame())
    runtime.seal_prediction(original["event_hash"], anchor(clock, 1))
    first = capture_and_seal(runtime, clock, delayed_label())
    assert runtime.score(first["event_hash"])["payload"]["status"] == "scored"
    clock.host(3601)
    assert "1000" in runtime.predict(frame(3601))["payload"]["state"]["training_ids"]
    revised = capture_and_seal(runtime, clock, delayed_label(source_semantics=semantics), at=3602)
    score = runtime.score(revised["event_hash"])["payload"]
    assert score["status"] == "unmatched"
    assert score["reason"] == "sample_source_semantics_unverified"
    clock.host(3603)
    later = runtime.predict(frame(3603))["payload"]
    assert "1000" not in later["state"]["training_ids"]
    assert not any(e["kind"] == "association_proposal" and e["payload"]["version_hash"] == revised["event_hash"]
                   for e in runtime.events())


def test_frozen_seed_without_bound_source_evidence_cannot_enter_history(run):
    runtime, _ = run
    events = runtime.events()
    seed = next(e for e in events if e["kind"] == "label_version")
    seed["payload"]["audit_evidence"].pop("source_semantics_evidence")
    history = runtime._history(events, BASE)
    row = next(r for r in history if r["assay_id"] == seed["payload"]["assay_id"])
    assert row["deleted"] is True


def test_real_frozen_c7_binding_is_exact_and_unknown_artifacts_are_not_assumed_legacy():
    path = Path(__file__).resolve().parents[2] / "output/gpt-expert81-20261102/candidate.json"
    artifact = json.loads(path.read_text(encoding="utf-8"))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    spec = shadow.model_spec(artifact, host_origin=ORIGIN, clock_domain=DOMAIN)
    assert spec["source_semantics"] == shadow.LEGACY_SOURCE_SEMANTICS
    assert spec["source_semantics_evidence"]["artifact_hash"] == shadow.digest(artifact)
    artifact["source_semantics"] = deepcopy(shadow.LEGACY_SOURCE_SEMANTICS)
    with pytest.raises(shadow.LedgerError, match="no audited desliming source semantics"):
        shadow.model_spec(artifact, host_origin=ORIGIN, clock_domain=DOMAIN)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("missing", ["source_semantics", "source_semantics_evidence", "artifact_hash", "seed_manifest_hash"])
def test_run_without_complete_artifact_domain_evidence_cannot_resume(run, tmp_path, missing):
    runtime, clock = run
    spec = deepcopy(runtime.spec)
    spec.pop(missing)
    path = tmp_path / "incomplete-domain.jsonl"
    with shadow.Journal(path, clock=clock).transaction() as tx:
        tx.append("run_started", spec)
    before = path.read_bytes()
    with pytest.raises(shadow.LedgerError, match="source semantics"):
        shadow.ShadowRun(path, clock=clock)
    assert path.read_bytes() == before


def test_prior_runtime_ledger_is_not_appended_after_implementation_change(run, tmp_path):
    runtime, clock = run
    spec = deepcopy(runtime.spec)
    spec["runtime_sha256"] = "a" * 64
    path = tmp_path / "prior-runtime.jsonl"
    with shadow.Journal(path, clock=clock).transaction() as tx:
        tx.append("run_started", spec)
    before = path.read_bytes()
    with pytest.raises(shadow.LedgerError, match="implementation changed"):
        shadow.ShadowRun(path, clock=clock)
    assert path.read_bytes() == before


def test_c7_consumes_only_registry_verified_prelabel_association_receipts(run):
    runtime, clock = run
    for index in range(3):
        at = index * 4000
        clock.host(at)
        prediction = runtime.predict(frame(at, level=50 + index))
        runtime.seal_prediction(prediction["event_hash"], anchor(clock, at + 1))
        capture_and_seal(runtime, clock, delayed_label(at + 3, assay_id=str(1000 + index), value=15.8), at=at + 3600)
    events = runtime.events()
    cutoff = BASE + timedelta(seconds=12000)
    history = runtime._history(events, cutoff)
    valid = core.fit_state(history, cutoff, "C7", runtime.spec["config"], group_key=shadow.PRIMARY_GROUP,
                           association_validator=runtime._association_validator(events))
    assert len(valid["residual_receipt_ids"]) == 3
    with pytest.raises(ValueError, match="registry"):
        core.fit_state(history, cutoff, "C7", runtime.spec["config"], group_key=shadow.PRIMARY_GROUP)
    forged = deepcopy(history)
    row = next(r for r in forged if r.get("c5_receipt"))
    row["c5_receipt"]["original_receipt"]["prediction"] += .1
    row["c5_receipt"]["association_hash"] = shadow.digest({k: v for k, v in row["c5_receipt"].items() if k != "association_hash"})
    with pytest.raises(ValueError):
        core.fit_state(forged, cutoff, "C7", runtime.spec["config"], group_key=shadow.PRIMARY_GROUP,
                       association_validator=runtime._association_validator(events))
