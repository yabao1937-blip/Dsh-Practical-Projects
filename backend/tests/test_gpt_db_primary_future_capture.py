"""Entire capture/reveal chain on synthetic files and an injected read transport."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

import pytest

from scripts import gpt_db_primary_future_capture as capture


def save(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


@pytest.fixture
def case(tmp_path):
    import joblib
    import numpy as np
    from pathlib import Path
    from sklearn.dummy import DummyRegressor
    frozen = datetime(2026, 11, 3, 6, tzinfo=timezone.utc)
    due = frozen + timedelta(hours=168)
    freeze = tmp_path / "freeze"; freeze.mkdir()
    points = {}
    for name in (*capture.PUBLIC, "level", "legacy_desliming473", "legacy_desliming474", "runs_desliming473", "runs_desliming474"):
        numeric = name in ("coal_amount", "level")
        suffix = "/if_prod" if name.startswith("legacy_") else "/RUNS" if name.startswith("runs_") else ""
        config = {"node_id": "ns=2;s=" + name + suffix, "unit": "", "scale": 1.0, "offset": 0.0,
                  "value_type": "number" if numeric else "boolean", "minimum": None, "maximum": None,
                  "max_age_seconds": 30, "max_source_age_seconds": None}
        definition = hashlib.sha256(json.dumps({k: config[k] for k in capture.DEF_FIELDS}, sort_keys=True).encode()).hexdigest()
        points[name] = {"point_key": "test_" + name, "definition_id": definition, "config": config}
    contract = {"schema": "expert81-future-source-contract-v1", "source_id": "kepserver-main",
                "cutover_utc": "2026-11-03T05:18:15.125+00:00", "points": points,
                "rules": {"level_lag_seconds": 90, "strict_max_age_seconds": 30, "minute_max_age_seconds": 65, "history_lookback_seconds": 120}}
    contract_path = tmp_path / "source_contract.json"; save(contract_path, contract)
    names = ("model.joblib", "model.json", "environment.json", "inference_core.py", "predict.py", "protocol.json", "training-scope.json")
    for name in names:
        (freeze / name).write_text("synthetic frozen member " + name, encoding="utf8")
    backend = Path(capture.__file__).resolve().parents[1]
    estimator = DummyRegressor(strategy="constant", constant=10.)
    estimator.constant_ = np.array([[10.]]); estimator.n_outputs_ = 1; estimator.n_features_in_ = 6
    public = ("level", *capture.PUBLIC)
    bundle = {"model_version": "synthetic-v1", "db_exposure_cutoff": "2026-11-03T05:50:58.710+00:00",
              "branches": {"3": {"status": "ok", "model": {"kind": "estimator", "features": list(public), "estimator": estimator}, "ranges": {f: {"min": 0, "max": 1000} for f in public}}}}
    joblib.dump(bundle, freeze / "model.joblib")
    save(freeze / "environment.json", capture.runtime_versions())
    (freeze / "inference_core.py").write_bytes((backend / "app/gpt_coarse/experiments/expert81_db_primary_predict.py").read_bytes())
    (freeze / "predict.py").write_bytes((backend / "scripts/predict_gpt_expert81_frozen.py").read_bytes())
    manifest = {"model_version": "synthetic-v1", "db_exposure_cutoff": "2026-11-03T05:50:58.710+00:00",
                "files": {name: {"sha256": capture.sha(freeze / name), "bytes": (freeze / name).stat().st_size} for name in names}}
    save(freeze / "freeze-manifest.json", manifest)
    dependencies = {"schema": "expert81-input-runtime-project-dependencies-v1", "files": {name: {"sha256": capture.sha(capture.PROJECT_ROOT / name), "bytes": (capture.PROJECT_ROOT / name).stat().st_size} for name in capture.INPUT_RUNTIME_SOURCES}}
    save(freeze / "input-runtime-dependencies.json", dependencies)
    seal = {"freeze_manifest_sha256": capture.sha(freeze / "freeze-manifest.json"), "frozen_at_utc": capture.stamp(frozen),
            "due_at_utc": capture.stamp(due), "exposed_assay_ids": [11, 197, 211], "input_source_contract_sha256": capture.sha(contract_path),
            "input_runtime_dependencies_manifest_sha256": capture.sha(freeze / "input-runtime-dependencies.json")}
    save(freeze / "final-seal.json", seal)
    return {"root": tmp_path, "freeze": freeze, "contract_path": contract_path, "contract": contract,
            "frozen": frozen, "due": due, "now": due + timedelta(hours=2)}


class FakeTransport:
    def __init__(self, case, *, metadata=None, age=1, bad_feature=None, bad_index=False, fail_query=None, compact=False, native_available=False,
                 system_bits=None, feature_ages=None, missing_features=(), mixed_feature=None):
        self.case, self.age, self.bad_feature, self.bad_index, self.fail_query, self.compact = case, age, bad_feature, bad_index, fail_query, compact
        sample = case["frozen"] + timedelta(hours=1)
        self.metadata_rows = metadata if metadata is not None else [{"id": 212, "observed_at": capture.stamp(sample), "created_at": capture.stamp(sample + timedelta(minutes=10)), "time_basis": "production-time-v1"}]
        self.calls, self.closed, self.http_receipts, self.catalog = [], False, [], {}
        self.native_available = native_available
        self.system_bits, self.feature_ages, self.missing_features, self.mixed_feature = system_bits or {"sysA": False, "sysB": True, "sys401": True, "sys402": True}, feature_ages or {}, set(missing_features), mixed_feature

    def query(self, sql, params):
        capture.check_read_only(sql)
        self.calls.append((sql, list(params)))
        if self.fail_query is not None and len(self.calls) == self.fail_query:
            raise TimeoutError("synthetic transport timeout")
        if sql.startswith("EXPLAIN"):
            if "telemetry_readings" in sql:
                return [{"detail": "SCAN h" if self.bad_index else "SEARCH h USING INDEX ix_telemetry_point_time (source_id=? AND point_key=? AND received_at>?)"}]
            return [{"detail": "SEARCH manual_measurements USING INDEX ix_manual_measurement_metric_id (metric=? AND id>?)"}]
        if sql == capture.CLOCK_SQL:
            return [{"host_checked_at": capture.stamp(self.case["now"])}]
        if sql == capture.META_SQL:
            return deepcopy(self.metadata_rows)
        if sql.startswith("WITH inputs("):
            rows = []
            for i in range(0, len(params), 7):
                sid, feature, namespace, source, key, at, lower = params[i:i + 7]
                binding = capture.bind(self.case["contract"], feature, namespace)
                if feature in self.missing_features or namespace == "legacy_if_prod" and not self.native_available:
                    rows.append({"assay_id": sid, "feature": feature, "namespace": namespace, "at": at,
                                 "requested_source_id": source, "requested_point_key": key, "id": None,
                                 "source_id": None, "point_key": None, "batch_id": None, "received_at": None, "sample": None})
                    continue
                age = self.feature_ages.get(feature, 1 if feature == "level" else self.age)
                received = capture.stamp(capture.dt(at) - timedelta(seconds=age))
                value = 650.0 if feature == "coal_amount" else 50.0 if feature == "level" else self.system_bits[feature] if feature in self.system_bits else namespace != "legacy_if_prod"
                sample = {"key": key, "node_id": binding["config"]["node_id"], "definition_id": binding["definition_id"],
                          "value": value, "raw_value": value, "quality": "bad" if feature == self.bad_feature else "good",
                          "issue": None, "status_code": 0, "received_at": received, "source_at": received}
                if self.compact:
                    fields = {"node_id": sample.pop("node_id")}
                    entry = {"format": "point-meta-tree-v1", "patches": [[[], fields]]}
                    ref = hashlib.sha256(capture.canonical(entry)).hexdigest(); self.catalog[ref] = entry; sample["$point_meta"] = ref
                rows.append({"assay_id": sid, "feature": feature, "namespace": namespace, "at": at,
                             "requested_source_id": source, "requested_point_key": key, "id": sid * 100 + i,
                             "source_id": source, "point_key": key, "batch_id": ("mixed-" if feature == self.mixed_feature else "batch-") + received,
                             "received_at": received, "sample": json.dumps(sample)})
            return rows
        if sql.startswith("SELECT id,value,"):
            requested = params[1:]
            return [{**m, "value": 10.5} for m in self.metadata_rows if m["id"] in requested]
        raise AssertionError("unexpected SQL " + sql)

    def metadata(self, key):
        return self.catalog[key]

    def close(self):
        self.closed = True


def prepare(case, **transport_kwargs):
    transport = FakeTransport(case, **transport_kwargs)
    output = case["root"] / "collection"
    capture.prepare_inputs(case["freeze"], case["contract_path"], output, execute=True, transport_factory=lambda: transport, now=case["now"])
    return output, transport


def predict_lock(case, collection):
    manifest = capture.read(collection / "input-manifest.json")
    inputs = capture.read(collection / "inputs.json")["rows"]
    lock = {"schema": capture.LOCK_SCHEMA, "labels_read": False, "fits_performed": 0, "provenance_verified": True,
            "predicted_at_utc": capture.stamp(capture.dt(manifest["captured_at_utc"]) + timedelta(seconds=1)),
            "input_file_sha256": capture.sha(collection / "inputs.json"), "input_manifest_sha256": capture.sha(collection / "input-manifest.json"),
            "model_version": "synthetic-v1", "frozen_training_cutoff": "2026-11-03T05:50:58.710+00:00",
            **{k: manifest[k] for k in ("freeze_manifest_sha256", "model_file_sha256", "source_contract_sha256", "final_seal_sha256")},
            "rows": [{"record_id": r["record_id"], "assay_id": r["assay_id"], "sample_at": r["sample_at"], "public_quality": r["public_quality"], "prediction": 10.0, "raw_prediction": 10.0, "status": "ok", "reason": None, "route": "independent_system_count:3", "outside_training_ranges": []} for r in inputs]}
    path = case["root"] / "prediction-lock.json"; save(path, lock)
    return path


def test_default_plan_opens_no_transport_and_does_not_create_output(case):
    output = case["root"] / "unused"
    result = capture.prepare_inputs(case["freeze"], case["contract_path"], output, now=case["now"], transport_factory=lambda: pytest.fail("dry-run transport opened"))
    assert result["mode"] == "prepared_no_requests" and not output.exists()


def test_prepare_metadata_then_bounded_input_queries_never_label_sql(case):
    collection, transport = prepare(case, compact=True)
    assert transport.closed
    assert all(not re.search(r"\b(value|target|label|actual|ash)\b", sql, re.I) for sql, _ in transport.calls)
    assert all("INDEXED BY ix_telemetry_point_time" in sql and "LIMIT 3" in sql for sql, _ in transport.calls if sql.startswith("WITH"))
    rows = capture.read(collection / "inputs.json")["rows"]
    assert rows[0]["public_quality"] == "strict" and rows[0]["features"]["level"] == 50.0
    assert rows[0]["feature_namespace"] == "runs_v1"
    assert not capture.LABEL_KEYS.intersection(rows[0]) and not capture.LABEL_KEYS.intersection(rows[0]["features"])
    assert capture.read(collection / "input-evidence.json")["metadata_catalog"]


@pytest.mark.parametrize("age,bad_feature,quality", [(40, None, "minute_approximation"), (66, None, "incomplete"), (1, "sys401", "incomplete")])
def test_bad_quality_and_age_never_silently_promoted(case, age, bad_feature, quality):
    collection, _ = prepare(case, age=age, bad_feature=bad_feature)
    assert capture.read(collection / "inputs.json")["rows"][0]["public_quality"] == quality


def test_reveal_requires_existing_verified_prediction_before_any_label_request(case):
    collection, before = prepare(case)
    with pytest.raises(FileNotFoundError):
        capture.reveal_labels(case["freeze"], case["contract_path"], collection, case["root"] / "missing-lock.json", case["root"] / "labels", execute=True, now=case["now"], transport_factory=lambda: pytest.fail("opened before lock"))
    lock = predict_lock(case, collection)
    transport = FakeTransport(case)
    result = capture.reveal_labels(case["freeze"], case["contract_path"], collection, lock, case["root"] / "labels", execute=True, now=case["now"], transport_factory=lambda: transport)
    assert result["status"] == "complete" and transport.closed
    assert len(transport.calls) == 1 and "id IN (?)" in transport.calls[0][0]
    assert transport.calls[0][1] == ["coarse_slime_ash", 212]
    assert not any("SELECT id,value" in sql for sql, _ in before.calls)
    assert capture.read(case["root"] / "labels" / "labels.json")["evaluation_status"] == "future_time_holdout_replay_not_certified_real_time_prospective"


@pytest.mark.parametrize("field", ["model_file_sha256", "input_file_sha256", "input_manifest_sha256", "freeze_manifest_sha256", "source_contract_sha256", "final_seal_sha256"])
def test_wrong_locked_hash_blocks_label_transport(case, field):
    collection, _ = prepare(case)
    path = predict_lock(case, collection); lock = capture.read(path); lock[field] = "0" * 64; save(path, lock)
    with pytest.raises(ValueError, match="hash mismatch"):
        capture.reveal_labels(case["freeze"], case["contract_path"], collection, path, case["root"] / "labels", execute=True, now=case["now"], transport_factory=lambda: pytest.fail("labels accessed with bad hash"))


def test_input_factor_tampering_detected_even_with_updated_file_hash_and_lock(case):
    collection, _ = prepare(case)
    inputs = capture.read(collection / "inputs.json"); inputs["rows"][0]["features"]["level"] = 99.0; save(collection / "inputs.json", inputs)
    manifest = capture.read(collection / "input-manifest.json")
    manifest["files"]["inputs.json"] = {"sha256": capture.sha(collection / "inputs.json"), "bytes": (collection / "inputs.json").stat().st_size}
    manifest["input_file_sha256"] = capture.sha(collection / "inputs.json"); save(collection / "input-manifest.json", manifest)
    lock = predict_lock(case, collection)
    with pytest.raises(ValueError, match="inconsistent with raw source evidence"):
        capture.reveal_labels(case["freeze"], case["contract_path"], collection, lock, case["root"] / "labels", execute=True, now=case["now"], transport_factory=lambda: pytest.fail("labels read on inconsistent factors"))


@pytest.mark.parametrize("change", ["old_id", "before_window", "after_window", "label_projection"])
def test_metadata_window_and_id_boundaries_fail_before_features(case, change):
    transport = FakeTransport(case); rows = deepcopy(transport.metadata_rows)
    if change == "old_id": rows[0]["id"] = 197
    elif change == "before_window": rows[0]["observed_at"] = capture.stamp(case["frozen"])
    elif change == "after_window": rows[0]["observed_at"] = capture.stamp(case["due"] + timedelta(seconds=1))
    else: rows[0]["value"] = 99.0
    transport.metadata_rows = rows
    with pytest.raises(ValueError):
        capture.prepare_inputs(case["freeze"], case["contract_path"], case["root"] / "collection", execute=True, now=case["now"], transport_factory=lambda: transport)
    assert not any(sql.startswith("WITH") for sql, _ in transport.calls) and transport.closed


def test_window_due_required_and_explicit_sample_cap_refuses_truncation(case):
    with pytest.raises(ValueError, match="not due"):
        capture.prepare_inputs(case["freeze"], case["contract_path"], case["root"] / "unused", now=case["due"] - timedelta(seconds=1))
    transport = FakeTransport(case)
    second = {**transport.metadata_rows[0], "id": 213}; transport.metadata_rows.append(second)
    with pytest.raises(ValueError, match="sample cap"):
        capture.prepare_inputs(case["freeze"], case["contract_path"], case["root"] / "collection", execute=True, now=case["now"], max_samples=1, transport_factory=lambda: transport)
    assert not any(sql.startswith("WITH") for sql, _ in transport.calls)


def test_bad_index_plan_stops_before_telemetry_reads(case):
    transport = FakeTransport(case, bad_index=True)
    with pytest.raises(ValueError, match="unsafe telemetry index"):
        capture.prepare_inputs(case["freeze"], case["contract_path"], case["root"] / "collection", execute=True, now=case["now"], transport_factory=lambda: transport)
    assert not any(sql.startswith("WITH") for sql, _ in transport.calls)


def test_timeout_stops_once_closes_and_preserves_failed_receipts(case):
    transport = FakeTransport(case, fail_query=3)
    with pytest.raises(TimeoutError):
        capture.prepare_inputs(case["freeze"], case["contract_path"], case["root"] / "collection", execute=True, now=case["now"], transport_factory=lambda: transport)
    assert len(transport.calls) == 3 and transport.closed
    assert capture.read(case["root"] / "collection" / "input-manifest.json")["status"] == "failed_no_retry"


@pytest.mark.parametrize("mutation", ["unverified", "wrong_id", "missing_row", "missing_field", "numeric_rejection"])
def test_incomplete_or_unverified_lock_refused_before_labels(case, mutation):
    collection, _ = prepare(case); path = predict_lock(case, collection); lock = capture.read(path)
    if mutation == "unverified": lock["provenance_verified"] = False
    elif mutation == "wrong_id": lock["rows"][0]["record_id"] = "db:manual_measurements:999"
    elif mutation == "missing_row": lock["rows"] = []
    elif mutation == "missing_field": del lock["rows"][0]["route"]
    else: lock["rows"][0]["status"] = "unavailable"
    save(path, lock)
    with pytest.raises(ValueError):
        capture.reveal_labels(case["freeze"], case["contract_path"], collection, path, case["root"] / "labels", execute=True, now=case["now"], transport_factory=lambda: pytest.fail("invalid lock accessed labels"))


def test_close_failure_is_not_a_complete_collection(case):
    transport = FakeTransport(case)
    def failed_close():
        raise RuntimeError("synthetic close failure")
    transport.close = failed_close
    with pytest.raises(RuntimeError, match="close failure"):
        capture.prepare_inputs(case["freeze"], case["contract_path"], case["root"] / "collection", execute=True, now=case["now"], transport_factory=lambda: transport)
    assert capture.read(case["root"] / "collection" / "input-manifest.json")["status"] == "failed_no_retry"


def test_native_EX8_missing_is_locked_without_RUNS_substitution(case):
    collection, _ = prepare(case)
    native = capture.read(collection / "baseline-ex8-inputs.json")["rows"][0]
    assert native["feature_namespace"] == "legacy_if_prod" and native["features"]["desliming473"] is None
    candidate = capture.read(collection / "inputs.json")["rows"][0]
    assert candidate["features"]["desliming473"] == 1
    baseline = capture.read(collection / "baseline-ex8-lock.json")
    assert baseline["rows"][0]["status"] == "unavailable" and baseline["rows"][0]["prediction"] is None


def test_native_EX8_pairable_requires_real_prediction_not_fake_unavailability(case):
    collection, _ = prepare(case, native_available=True); lock = predict_lock(case, collection)
    with pytest.raises(ValueError, match="native EX8 input exists"):
        capture.reveal_labels(case["freeze"], case["contract_path"], collection, lock, case["root"] / "labels", execute=True, now=case["now"], transport_factory=lambda: pytest.fail("read labels before EX8 native prediction"))


def test_actual_pure_predict_CLI_contract_closes_synthetic_chain_without_fitting(case):
    """Exercise the real separate predict entry point, not a hand-made ledger."""
    import joblib
    import numpy as np
    from pathlib import Path
    from sklearn.dummy import DummyRegressor
    from scripts.predict_gpt_expert81_frozen import main as predict_main
    backend = Path(capture.__file__).resolve().parents[1]
    freeze = case["freeze"]
    estimator = DummyRegressor(strategy="constant", constant=10.0)
    # Set a synthetic frozen state directly; no estimator.fit call occurs.
    estimator.constant_ = np.array([[10.0]]); estimator.n_outputs_ = 1; estimator.n_features_in_ = 6
    public = ("level", *capture.PUBLIC)
    bundle = {"model_version": "synthetic-v1", "db_exposure_cutoff": "2026-11-03T05:50:58.710+00:00",
              "branches": {"3": {"status": "ok", "model": {"kind": "estimator", "features": list(public), "estimator": estimator},
                                  "ranges": {f: {"min": 0, "max": 1000} for f in public}}}}
    joblib.dump(bundle, freeze / "model.joblib")
    (freeze / "inference_core.py").write_bytes((backend / "app/gpt_coarse/experiments/expert81_db_primary_predict.py").read_bytes())
    (freeze / "predict.py").write_bytes((backend / "scripts/predict_gpt_expert81_frozen.py").read_bytes())
    (freeze / "source_contract.json").write_bytes(case["contract_path"].read_bytes())
    manifest = capture.read(freeze / "freeze-manifest.json")
    manifest["files"] = {p.name: {"sha256": capture.sha(p), "bytes": p.stat().st_size} for p in freeze.iterdir() if p.name not in ("freeze-manifest.json", "final-seal.json")}
    save(freeze / "freeze-manifest.json", manifest)
    seal = capture.read(freeze / "final-seal.json"); seal["freeze_manifest_sha256"] = capture.sha(freeze / "freeze-manifest.json"); save(freeze / "final-seal.json", seal)
    collection, _ = prepare(case)
    lock = case["root"] / "real-predict-lock.json"
    predict_main(["--freeze", str(freeze), "--inputs", str(collection / "inputs.json"), "--input-manifest", str(collection / "input-manifest.json"), "--output", str(lock)])
    ledger = capture.read(lock)
    assert ledger["provenance_verified"] is True and ledger["rows"][0]["prediction"] == 10.0 and ledger["fits_performed"] == 0
    transport = FakeTransport(case)
    result = capture.reveal_labels(freeze, case["contract_path"], collection, lock, case["root"] / "labels", execute=True, now=case["now"], transport_factory=lambda: transport)
    assert result["status"] == "complete" and len(transport.calls) == 1


def install_final_sealed_runtime(case):
    """A synthetic final-freeze with the real final entry and fixed native params."""
    import joblib
    import numpy as np
    from pathlib import Path
    from sklearn.dummy import DummyRegressor
    backend = Path(capture.__file__).resolve().parents[1]
    old = case["freeze"]; final = case["root"] / "arbitrary-final-freeze"; old.rename(final); case["freeze"] = final
    estimator = DummyRegressor(strategy="constant", constant=10.)
    estimator.constant_ = np.array([[10.]]); estimator.n_outputs_ = 1; estimator.n_features_in_ = 6
    public = ("level", *capture.PUBLIC)
    bundle = {"model_version": "synthetic-v1", "db_exposure_cutoff": "2026-11-03T05:50:58.710+00:00",
              "branches": {"3": {"status": "ok", "model": {"kind": "estimator", "features": list(public), "estimator": estimator}, "ranges": {f: {"min": 0, "max": 1000} for f in public}}}}
    joblib.dump(bundle, final / "model.joblib")
    (final / "inference_core.py").write_bytes((backend / "app/gpt_coarse/experiments/expert81_db_primary_predict.py").read_bytes())
    (final / "predict.py").write_bytes((backend / "scripts/predict_gpt_expert81_frozen.py").read_bytes())
    (final / "source_contract.json").write_bytes(case["contract_path"].read_bytes())
    manifest = capture.read(final / "freeze-manifest.json")
    manifest["files"] = {p.name: {"sha256": capture.sha(p), "bytes": p.stat().st_size} for p in final.iterdir() if p.name not in ("freeze-manifest.json", "final-seal.json")}
    save(final / "freeze-manifest.json", manifest)
    # Reproduce the already frozen legacy-only contract with its actual sealed
    # entry; new production tests replace this with the current production entry.
    legacy_entry = capture.PROJECT_ROOT / "output/gpt-expert81-db-primary-20261103/final-freeze/predict-sealed.py"
    (final / "predict-sealed.py").write_bytes(legacy_entry.read_bytes())
    # A nonzero native desliming coefficient detects accidental RUNS substitution.
    model = {"bounds": [[0., 1000.], *[[0., 1.]] * 6], "coefs": [0., 0., 0., 0., 0., 1., 0.], "reference": [0.] * 7, "scale": [1.] * 7}
    save(final / "ex8-native-model.json", {"model": model})
    runtime = {"files": {name: {"sha256": capture.sha(final / name), "bytes": (final / name).stat().st_size} for name in ("predict-sealed.py", "ex8-native-model.json")}}
    save(final / "sealed-runtime-manifest.json", runtime)
    seal = capture.read(final / "final-seal.json"); seal["freeze_manifest_sha256"] = capture.sha(final / "freeze-manifest.json")
    seal["sealed_runtime_manifest_sha256"] = capture.sha(final / "sealed-runtime-manifest.json"); save(final / "final-seal.json", seal)
    return final


def final_prediction_lock(case, collection):
    import importlib.util
    spec = importlib.util.spec_from_file_location("synthetic_real_sealed_entry", case["freeze"] / "predict-sealed.py")
    entry = importlib.util.module_from_spec(spec); spec.loader.exec_module(entry)
    path = case["root"] / "final-prediction-lock.json"
    entry.main(["--freeze", str(case["freeze"]), "--inputs", str(collection / "inputs.json"), "--input-manifest", str(collection / "input-manifest.json"), "--output", str(path)])
    return path


@pytest.mark.parametrize("native_available", [False, True])
def test_real_final_entry_native_EX8_and_RUNS_coexistence_chain(case, native_available):
    install_final_sealed_runtime(case)
    collection, _ = prepare(case, native_available=native_available); lock_path = final_prediction_lock(case, collection)
    lock = capture.read(lock_path)
    assert lock["rows"][0]["prediction"] == 10.
    candidate = capture.read(collection / "inputs.json")["rows"][0]
    native = capture.read(collection / "baseline-ex8-inputs.json")["rows"][0]
    assert candidate["feature_namespace"] == "runs_v1" and candidate["features"]["desliming473"] == 1
    assert native["feature_namespace"] == "legacy_if_prod" and native["features"]["desliming473"] == (0 if native_available else None)
    assert lock["rows"][0]["ex8_native_prediction"] == (15.5 if native_available else None)
    assert capture.read(collection / "baseline-ex8-lock.json")["model_prediction_performed"] is False
    transport = FakeTransport(case, native_available=native_available)
    result = capture.reveal_labels(case["freeze"], case["contract_path"], collection, lock_path, case["root"] / "labels", execute=True, now=case["now"], transport_factory=lambda: transport)
    assert result["status"] == "complete" and len(transport.calls) == 1


@pytest.mark.parametrize("field", ["native_EX8_input_file_sha256", "native_EX8_model_sha256", "sealed_runtime_manifest_sha256", "selected_only_lock_sha256"])
def test_final_native_hash_tampering_blocks_labels(case, field):
    install_final_sealed_runtime(case); collection, _ = prepare(case, native_available=True)
    path = final_prediction_lock(case, collection); lock = capture.read(path); lock[field] = "0" * 64; save(path, lock)
    with pytest.raises(ValueError, match="hash mismatch"):
        capture.reveal_labels(case["freeze"], case["contract_path"], collection, path, case["root"] / "labels", execute=True, now=case["now"], transport_factory=lambda: pytest.fail("tampered native lock read labels"))


def test_final_native_prediction_must_match_legacy_not_RUNS(case):
    install_final_sealed_runtime(case); collection, _ = prepare(case, native_available=True)
    path = final_prediction_lock(case, collection); lock = capture.read(path)
    lock["rows"][0]["ex8_native_prediction"] = 15.5 + 2. * __import__("math").tanh(.5)
    lock["rows"][0]["old_production_policy_prediction"] = lock["rows"][0]["ex8_native_prediction"]; save(path, lock)
    with pytest.raises(ValueError, match="sealed formula"):
        capture.reveal_labels(case["freeze"], case["contract_path"], collection, path, case["root"] / "labels", execute=True, now=case["now"], transport_factory=lambda: pytest.fail("RUNS substitution read labels"))


def test_final_native_model_member_tampering_blocks_labels(case):
    install_final_sealed_runtime(case); collection, _ = prepare(case, native_available=True); path = final_prediction_lock(case, collection)
    modelpath = case["freeze"] / "ex8-native-model.json"; modelpath.write_text('{"model":{}}', encoding="utf8")
    with pytest.raises(ValueError, match="member hash/size mismatch"):
        capture.reveal_labels(case["freeze"], case["contract_path"], collection, path, case["root"] / "labels", execute=True, now=case["now"], transport_factory=lambda: pytest.fail("changed model read labels"))


@pytest.mark.parametrize("field,value", [("prediction", 11.), ("raw_prediction", 11.), ("route", "forged_route"), ("reason", "forged_reason"), ("status", "forged_status")])
def test_candidate_and_selected_synchronous_tampering_rehashed_still_refused(case, field, value):
    install_final_sealed_runtime(case); collection, _ = prepare(case, native_available=True)
    path = final_prediction_lock(case, collection); lock = capture.read(path)
    selected_path = path.with_name(path.stem + ".selected-only.json"); selected = capture.read(selected_path)
    lock["rows"][0][field] = value; selected["rows"][0][field] = value
    save(selected_path, selected); lock["selected_only_lock_sha256"] = capture.sha(selected_path); save(path, lock)
    with pytest.raises(ValueError):
        capture.reveal_labels(case["freeze"], case["contract_path"], collection, path, case["root"] / "labels", execute=True, now=case["now"], transport_factory=lambda: pytest.fail("synchronously forged locks read labels"))


def test_system_count_tampering_refused_even_with_unchanged_candidate_values(case):
    install_final_sealed_runtime(case); collection, _ = prepare(case, native_available=True)
    path = final_prediction_lock(case, collection); lock = capture.read(path); lock["rows"][0]["system_count"] = 2; save(path, lock)
    with pytest.raises(ValueError, match="system_count"):
        capture.reveal_labels(case["freeze"], case["contract_path"], collection, path, case["root"] / "labels", execute=True, now=case["now"], transport_factory=lambda: pytest.fail("wrong system count read labels"))


def test_reveal_runtime_guard_refuses_before_model_load_and_label_transport(case, monkeypatch):
    import joblib
    install_final_sealed_runtime(case); collection, _ = prepare(case, native_available=True); path = final_prediction_lock(case, collection)
    runtime = capture.runtime_versions(); runtime["numpy"] = "different-version"
    monkeypatch.setattr(capture, "runtime_versions", lambda: runtime)
    monkeypatch.setattr(joblib, "load", lambda *args, **kwargs: pytest.fail("deserialized before runtime guard"))
    with pytest.raises(ValueError, match="runtime version mismatch"):
        capture.reveal_labels(case["freeze"], case["contract_path"], collection, path, case["root"] / "labels", execute=True, now=case["now"], transport_factory=lambda: pytest.fail("runtime mismatch read labels"))


def test_candidate_reverification_does_not_fit_or_modify_any_lock(case, monkeypatch):
    from sklearn.dummy import DummyRegressor
    install_final_sealed_runtime(case); collection, _ = prepare(case, native_available=True); path = final_prediction_lock(case, collection)
    selected = path.with_name(path.stem + ".selected-only.json"); before = [capture.sha(path), capture.sha(selected)]
    monkeypatch.setattr(DummyRegressor, "fit", lambda *args, **kwargs: pytest.fail("fit during reveal"))
    capture.verify_collection(case["freeze"], case["contract_path"], collection, path, now=case["now"])
    assert [capture.sha(path), capture.sha(selected)] == before


@pytest.mark.parametrize("relative", ["backend/point_metadata.py", "backend/scripts/gpt_db_primary_future_capture.py", "backend/remote_sqlite.py", "backend/remote_mode.py"])
def test_sealed_project_runtime_source_mutation_refused_without_transport(case, monkeypatch, relative):
    from pathlib import Path
    synthetic_root = case["root"] / "runtime-project"
    for name in capture.INPUT_RUNTIME_SOURCES:
        destination = synthetic_root / name; destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((capture.PROJECT_ROOT / name).read_bytes())
    changed = synthetic_root / relative; changed.write_bytes(changed.read_bytes() + b"\n# synthetic changed runtime\n")
    monkeypatch.setattr(capture, "PROJECT_ROOT", synthetic_root)
    with pytest.raises(ValueError, match="dependency source hash/size mismatch"):
        capture.prepare_inputs(case["freeze"], case["contract_path"], case["root"] / "collection", execute=True, now=case["now"], transport_factory=lambda: pytest.fail("changed input runtime opened transport"))


def test_dependency_manifest_tampering_refused_without_transport(case):
    path = case["freeze"] / "input-runtime-dependencies.json"; manifest = capture.read(path)
    manifest["files"]["backend/point_metadata.py"]["sha256"] = "0" * 64; save(path, manifest)
    with pytest.raises(ValueError, match="manifest/final seal mismatch"):
        capture.prepare_inputs(case["freeze"], case["contract_path"], case["root"] / "collection", execute=True, now=case["now"], transport_factory=lambda: pytest.fail("changed dependency manifest opened transport"))


def test_dependency_capture_hash_tampering_refused_even_with_manifest_resealed(case):
    path = case["freeze"] / "input-runtime-dependencies.json"; manifest = capture.read(path)
    manifest["files"]["backend/scripts/gpt_db_primary_future_capture.py"]["sha256"] = "0" * 64; save(path, manifest)
    seal = capture.read(case["freeze"] / "final-seal.json"); seal["input_runtime_dependencies_manifest_sha256"] = capture.sha(path); save(case["freeze"] / "final-seal.json", seal)
    with pytest.raises(ValueError, match="dependency source hash/size mismatch"):
        capture.prepare_inputs(case["freeze"], case["contract_path"], case["root"] / "collection", execute=True, now=case["now"], transport_factory=lambda: pytest.fail("wrong sealed capture hash opened transport"))


def install_production_runtime(case):
    final = install_final_sealed_runtime(case)
    contract = deepcopy(case["contract"])
    contract["baseline_contracts"] = {"history": {"id": "EX8_NATIVE_LEGACY", "namespace": "legacy_if_prod"},
        "future_production": {"id": "EX8_PRODUCTION_RUNS", "namespace": "runs_v1", "parameter_file": "ex8-production-model.json",
                              "parameter_sha256": capture.EX8_PRODUCTION_PARAMETER_SHA256, "evaluation_scope": "future_only"}}
    case["contract"] = contract; save(case["contract_path"], contract); save(final / "source_contract.json", contract)
    original = capture.PROJECT_ROOT / "backend/models/gpt_expert8/20261012/model.json"
    # Immutable original c720 parameters, synthetic future signals only; no fit.
    assert capture.sha(original) == capture.EX8_PRODUCTION_PARAMETER_SHA256
    (final / "ex8-production-model.json").write_bytes(original.read_bytes())
    (final / "predict-sealed.py").write_bytes((capture.PROJECT_ROOT / "backend/scripts/predict_gpt_expert81_sealed.py").read_bytes())
    manifest = capture.read(final / "freeze-manifest.json")
    manifest["files"]["source_contract.json"] = {"sha256": capture.sha(final / "source_contract.json"), "bytes": (final / "source_contract.json").stat().st_size}
    save(final / "freeze-manifest.json", manifest)
    runtime = capture.read(final / "sealed-runtime-manifest.json")
    runtime["files"]["predict-sealed.py"] = {"sha256": capture.sha(final / "predict-sealed.py"), "bytes": (final / "predict-sealed.py").stat().st_size}
    runtime["files"]["ex8-production-model.json"] = {"sha256": capture.sha(final / "ex8-production-model.json"), "bytes": (final / "ex8-production-model.json").stat().st_size}
    save(final / "sealed-runtime-manifest.json", runtime)
    seal = capture.read(final / "final-seal.json")
    seal.update(freeze_manifest_sha256=capture.sha(final / "freeze-manifest.json"), input_source_contract_sha256=capture.sha(case["contract_path"]), sealed_runtime_manifest_sha256=capture.sha(final / "sealed-runtime-manifest.json"))
    save(final / "final-seal.json", seal)
    return final


def test_future_production_RUNS_and_legacy_are_independent_complete_chain(case):
    install_production_runtime(case); collection, transport = prepare(case)
    production = capture.read(collection / "production-ex8-inputs.json")["rows"][0]
    legacy = capture.read(collection / "baseline-ex8-inputs.json")["rows"][0]
    assert production["baseline_id"] == "EX8_PRODUCTION_RUNS" and production["feature_namespace"] == "runs_v1"
    assert production["production_quality"] == "eligible" and production["features"]["desliming473"] == 1
    assert legacy["features"]["desliming473"] is None
    manifest = capture.read(collection / "input-manifest.json")
    assert set(manifest["baseline_contracts"]) == {"EX8_NATIVE_LEGACY", "EX8_PRODUCTION_RUNS"}
    assert manifest["comparison_baseline_field"] == "current_production_prediction"
    lock_path = final_prediction_lock(case, collection); lock = capture.read(lock_path)
    assert lock["rows"][0]["ex8_native_prediction"] is None and lock["rows"][0]["ex8_production_prediction"] is not None
    assert lock["production_EX8_model_sha256"] == capture.EX8_PRODUCTION_PARAMETER_SHA256
    reveal_transport = FakeTransport(case)
    result = capture.reveal_labels(case["freeze"], case["contract_path"], collection, lock_path, case["root"] / "labels", execute=True, now=case["now"], transport_factory=lambda: reveal_transport)
    assert result["status"] == "complete" and len(reveal_transport.calls) == 1


@pytest.mark.parametrize("count", range(5))
def test_production_all_actual_system_counts_zero_through_four(case, count):
    install_production_runtime(case)
    systems = {key: index < count for index, key in enumerate(("sysA", "sysB", "sys401", "sys402"))}
    collection, _ = prepare(case, system_bits=systems); lock_path = final_prediction_lock(case, collection)
    result = capture.read(lock_path)["rows"][0]
    assert result["production_system_count"] == count and result["current_production_prediction"] is not None
    assert result["production_policy_route"] == ("two_system_fixed14_15" if count == 2 else "non_two_system_EX8_RUNS")
    if count in (0, 1, 4):
        assert result["prediction"] is None  # Candidate own scope stays independent.
    capture.verify_collection(case["freeze"], case["contract_path"], collection, lock_path, now=case["now"])


def test_production_two_system_ignores_missing_coal_and_desliming(case):
    install_production_runtime(case)
    systems = {"sysA": False, "sysB": False, "sys401": True, "sys402": True}
    collection, _ = prepare(case, system_bits=systems, missing_features=("coal_amount", "desliming473", "desliming474"))
    production = capture.read(collection / "production-ex8-inputs.json")["rows"][0]
    assert production["production_quality"] == "eligible" and production["features"]["coal_amount"] is None
    lock_path = final_prediction_lock(case, collection); row = capture.read(lock_path)["rows"][0]
    assert row["current_production_prediction"] == 14. and row["ex8_production_prediction"] == 14. and row["prediction"] is None
    capture.verify_collection(case["freeze"], case["contract_path"], collection, lock_path, now=case["now"])


@pytest.mark.parametrize("kwargs,reason", [({"age": 40}, "production_system_feedback_unavailable"),
    ({"feature_ages": {"coal_amount": 40}}, "production_non_two_system_inputs_not_strict"),
    ({"feature_ages": {"level": -6}}, "production_level_t90_unavailable"),
    ({"mixed_feature": "sys401"}, "production_system_feedback_unavailable"),
    ({"mixed_feature": "desliming473"}, "production_non_two_system_inputs_not_strict")])
def test_production_strict_age_future_age_and_group_batches_are_not_approximated(case, kwargs, reason):
    install_production_runtime(case); collection, _ = prepare(case, **kwargs)
    production = capture.read(collection / "production-ex8-inputs.json")["rows"][0]
    assert production["production_quality"] == "unavailable" and production["production_reason"] == reason
    lock_path = final_prediction_lock(case, collection); row = capture.read(lock_path)["rows"][0]
    assert row["current_production_prediction"] is None and row["ex8_production_reason"] == reason
    capture.verify_collection(case["freeze"], case["contract_path"], collection, lock_path, now=case["now"])


def test_production_inputs_and_all_rehashed_locks_still_need_raw_RUNS_evidence(case):
    install_production_runtime(case); collection, _ = prepare(case)
    inputs_path = collection / "production-ex8-inputs.json"; inputs = capture.read(inputs_path)
    inputs["rows"][0]["features"]["desliming473"] = 0; save(inputs_path, inputs)
    availability_path = collection / "production-ex8-lock.json"; availability = capture.read(availability_path)
    availability["input_file_sha256"] = capture.sha(inputs_path); save(availability_path, availability)
    manifest_path = collection / "input-manifest.json"; manifest = capture.read(manifest_path)
    for name in ("production-ex8-inputs.json", "production-ex8-lock.json"):
        manifest["files"][name] = {"sha256": capture.sha(collection / name), "bytes": (collection / name).stat().st_size}
    save(manifest_path, manifest)
    lock_path = final_prediction_lock(case, collection)  # Genuine final wrapper locks forged file inputs.
    with pytest.raises(ValueError, match="independent raw RUNS evidence"):
        capture.reveal_labels(case["freeze"], case["contract_path"], collection, lock_path, case["root"] / "labels", execute=True, now=case["now"], transport_factory=lambda: pytest.fail("rehashed production forgery read labels"))


def test_production_numerical_lock_forgery_refused_before_labels(case):
    install_production_runtime(case); collection, _ = prepare(case); path = final_prediction_lock(case, collection)
    lock = capture.read(path); lock["rows"][0]["ex8_production_prediction"] = 99.; lock["rows"][0]["current_production_prediction"] = 99.; save(path, lock)
    with pytest.raises(ValueError, match="production formula"):
        capture.reveal_labels(case["freeze"], case["contract_path"], collection, path, case["root"] / "labels", execute=True, now=case["now"], transport_factory=lambda: pytest.fail("forged production value read labels"))


def test_production_comparison_count_flags_are_recomputed_before_labels(case):
    install_production_runtime(case); collection, _ = prepare(case); path = final_prediction_lock(case, collection)
    lock = capture.read(path)
    assert lock["rows"][0]["comparison_system_count_consistent"] is True
    lock["rows"][0]["comparison_system_count_consistent"] = False
    lock["rows"][0]["comparison_system_count_reason"] = "candidate_and_production_feedback_counts_differ"
    save(path, lock)
    with pytest.raises(ValueError, match="agreement/reason differs from independent evidence"):
        capture.reveal_labels(case["freeze"], case["contract_path"], collection, path, case["root"] / "labels", execute=True, now=case["now"], transport_factory=lambda: pytest.fail("forged count agreement read labels"))


@pytest.mark.parametrize("sql", ["UPDATE x SET y=1", "DELETE FROM x", "SELECT 1; DROP TABLE x", "WITH x AS (SELECT 1) DELETE FROM x", "PRAGMA journal_mode=WAL", "SELECT 1 -- hidden"])
def test_write_sql_and_multiple_statement_refused(sql):
    with pytest.raises(ValueError):
        capture.check_read_only(sql)
