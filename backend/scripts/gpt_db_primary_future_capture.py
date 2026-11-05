"""Seven-day frozen-model capture: metadata/inputs first, exact locked labels last.

Default CLI mode is a local plan. Only --execute opens the existing read-only
bridge. No training, fitting, production writes, fallback DB, or automatic retry.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import math
from pathlib import Path
import re
import sys
import time

PUBLIC = ("coal_amount", "sysA", "sysB", "sys401", "sys402")
DESLIMING = ("desliming473", "desliming474")
FEATURES = (*PUBLIC, *DESLIMING, "level")
LABEL_KEYS = {"actual", "ash", "target", "label", "value", "ash_value", "manual_value", "ash_content"}
DEF_FIELDS = ("node_id", "unit", "scale", "offset", "value_type", "minimum", "maximum")
ORIGIN = "http://10.255.2.7:8000"
META_SQL = (
    "SELECT id,observed_at,created_at,time_basis FROM manual_measurements "
    "INDEXED BY ix_manual_measurement_metric_id WHERE metric=? AND id>? "
    "AND observed_at>? AND observed_at<=? AND created_at>? ORDER BY id LIMIT ?"
)
CLOCK_SQL = "SELECT strftime('%Y-%m-%dT%H:%M:%f+00:00','now') AS host_checked_at"
HISTORY_SQL = (
    "WITH inputs(assay_id,feature,namespace,source_id,point_key,at,lower_at) AS (VALUES {slots}) "
    "SELECT i.assay_id,i.feature,i.namespace,i.at,i.source_id AS requested_source_id,"
    "i.point_key AS requested_point_key,r.id,r.source_id,r.point_key,r.batch_id,r.received_at,r.sample "
    "FROM inputs i LEFT JOIN telemetry_readings r ON r.id=("
    "SELECT h.id FROM telemetry_readings h INDEXED BY ix_telemetry_point_time "
    "WHERE h.source_id=i.source_id AND h.point_key=i.point_key AND h.received_at>=i.lower_at "
    "AND h.received_at<=i.at ORDER BY h.received_at DESC,h.id DESC LIMIT 1) LIMIT 3"
)
LOCK_SCHEMA = "expert81-frozen-label-free-prediction-lock-v1"
INPUT_SCHEMA = "expert81-future-input-manifest-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUT_RUNTIME_SOURCES = frozenset({"backend/scripts/gpt_db_primary_future_capture.py", "backend/point_metadata.py", "backend/remote_sqlite.py", "backend/remote_mode.py"})
EX8_PRODUCTION_PARAMETER_SHA256 = "c720efc10952802f42e3ec4261ab3b13966aefd89b34f7a21f7906c13f4362a0"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def dt(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("unzoned timestamp refused")
    return parsed.astimezone(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def check_read_only(sql):
    """Defense in depth: callers can only send single SELECT/EXPLAIN templates."""
    if ";" in sql or "--" in sql or "/*" in sql:
        raise ValueError("multiple statements/comments refused")
    words = {word.upper() for word in re.findall(r"\b[A-Za-z_]+\b", sql)}
    if not sql.lstrip().upper().startswith(("SELECT ", "WITH ", "EXPLAIN QUERY PLAN ")):
        raise ValueError("write SQL refused")
    if words & {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "ATTACH", "DETACH", "PRAGMA", "VACUUM", "REPLACE", "CREATE", "REINDEX", "ANALYZE"}:
        raise ValueError("write SQL refused")


def bounded_member(directory, name):
    directory = Path(directory).resolve()
    member = (directory / name).resolve()
    if member.parent != directory or not member.is_file():
        raise ValueError("manifest member must be an existing direct child")
    return member


def verify_files(directory, files):
    if not files:
        raise ValueError("empty file manifest refused")
    for name, expected in files.items():
        member = bounded_member(directory, name)
        if sha(member) != expected["sha256"] or member.stat().st_size != expected["bytes"]:
            raise ValueError(f"member hash/size mismatch: {name}")


def verify_input_runtime_dependencies(freeze, seal):
    """Enforce the final-sealed four-source closure before constructing transport."""
    path = Path(freeze) / "input-runtime-dependencies.json"
    if seal.get("input_runtime_dependencies_manifest_sha256") != sha(path):
        raise ValueError("input runtime dependency manifest/final seal mismatch")
    manifest = read(path)
    if manifest.get("schema") != "expert81-input-runtime-project-dependencies-v1" or set(manifest.get("files", {})) != INPUT_RUNTIME_SOURCES:
        raise ValueError("input runtime dependency closure must contain exactly four approved sources")
    root = PROJECT_ROOT.resolve()
    for relative, expected in manifest["files"].items():
        source = (root / relative).resolve()
        if not source.is_relative_to(root) or not source.is_file() or source.stat().st_size != expected["bytes"] or sha(source) != expected["sha256"]:
            raise ValueError("input runtime dependency source hash/size mismatch: " + relative)
    return sha(path)


def production_contract(contract):
    contracts = contract.get("baseline_contracts", {})
    selected = contracts.get("future_production", contracts.get("EX8_PRODUCTION_RUNS"))
    if selected is None:
        return None  # Older legacy-only evidence remains reproducible, never upgraded.
    if (selected.get("id", "EX8_PRODUCTION_RUNS") != "EX8_PRODUCTION_RUNS"
            or selected.get("namespace", selected.get("input_namespace")) != "runs_v1"
            or selected.get("parameter_file") != "ex8-production-model.json"
            or selected.get("parameter_sha256") != EX8_PRODUCTION_PARAMETER_SHA256):
        raise ValueError("unsupported EX8_PRODUCTION_RUNS frozen contract")
    return selected


def verify_freeze(freeze, contract_path, now=None):
    freeze = Path(freeze)
    manifest = read(freeze / "freeze-manifest.json")
    verify_files(freeze, manifest["files"])
    required = {"model.joblib", "model.json", "environment.json", "inference_core.py", "predict.py", "protocol.json", "training-scope.json"}
    if not required.issubset(manifest["files"]):
        raise ValueError("incomplete formal freeze")
    seal = read(freeze / "final-seal.json")
    if seal["freeze_manifest_sha256"] != sha(freeze / "freeze-manifest.json"):
        raise ValueError("final seal/freeze mismatch")
    dependency_manifest_sha256 = verify_input_runtime_dependencies(freeze, seal)
    frozen, due = dt(seal["frozen_at_utc"]), dt(seal["due_at_utc"])
    if due - frozen != timedelta(hours=168):
        raise ValueError("formal window must be exactly 168 hours")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.astimezone(timezone.utc) < due:
        raise ValueError("seven-day window not due")
    excluded = seal["exposed_assay_ids"]
    if not excluded or any(type(sid) is not int or sid <= 0 for sid in excluded) or len(excluded) != len(set(excluded)):
        raise ValueError("complete unique exposed DB IDs required in final seal")
    if seal["input_source_contract_sha256"] != sha(contract_path):
        raise ValueError("sealed source contract hash mismatch")
    contract = read(contract_path)
    if contract["schema"] != "expert81-future-source-contract-v1" or contract["source_id"] != "kepserver-main":
        raise ValueError("unsupported frozen source contract")
    production_contract(contract)
    if production_contract(contract) is not None and frozen < dt(contract["cutover_utc"]):
        raise ValueError("production RUNS future freeze must follow the effective cutover")
    if contract["rules"] != {"level_lag_seconds": 90, "strict_max_age_seconds": 30, "minute_max_age_seconds": 65, "history_lookback_seconds": 120}:
        raise ValueError("unsupported source timing rules")
    expected = {*PUBLIC, "level", "legacy_desliming473", "legacy_desliming474", "runs_desliming473", "runs_desliming474"}
    if set(contract["points"]) != expected:
        raise ValueError("incomplete point namespaces")
    for name, binding in contract["points"].items():
        identity = hashlib.sha256(json.dumps({k: binding["config"].get(k) for k in DEF_FIELDS}, sort_keys=True).encode()).hexdigest()
        if identity != binding["definition_id"]:
            raise ValueError(f"point definition hash mismatch: {name}")
        if name.startswith("legacy_") and not binding["config"]["node_id"].endswith("/if_prod"):
            raise ValueError("legacy source identity refused")
        if name.startswith("runs_") and not binding["config"]["node_id"].endswith("/RUNS"):
            raise ValueError("RUNS source identity refused")
    return {"manifest": manifest, "seal": seal, "contract": contract, "frozen": frozen, "due": due,
            "freeze_manifest_sha256": sha(freeze / "freeze-manifest.json"), "model_file_sha256": sha(freeze / "model.joblib"),
            "source_contract_sha256": sha(contract_path), "final_seal_sha256": sha(freeze / "final-seal.json"),
            "input_runtime_dependencies_manifest_sha256": dependency_manifest_sha256}


class RemoteTransport:
    """Lazy existing authenticated read-only bridge; no credentials are persisted."""
    def __init__(self):
        from remote_mode import settings
        from remote_sqlite import RemoteConnection
        config = settings()
        if not config or config["server_url"] != ORIGIN:
            raise ValueError("expected production read-only bridge origin")
        self.http_receipts = []
        receipts = self.http_receipts

        class LoggedConnection(RemoteConnection):
            def _request(self, method, suffix, body=None):
                record = {"method": method, "suffix": suffix}
                receipts.append(record)
                started = time.monotonic()
                try:
                    result = super()._request(method, suffix, body)
                    record["status"] = "success"
                    return result
                except Exception as exc:
                    record.update(status="failed", error_type=type(exc).__name__, error_code=getattr(exc, "error_code", None))
                    raise
                finally:
                    record["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        self.connection = LoggedConnection(config, timeout=3)

    def query(self, sql, params):
        check_read_only(sql)
        cursor = self.connection.execute(sql, params)
        try:
            if cursor._remote_id is not None:
                raise ValueError("unexpected paging refused; reduce the explicit capture budget")
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def metadata(self, key):
        return self.connection._request("GET", "/point-metadata/" + key)

    def close(self):
        self.connection.close()


class Session:
    def __init__(self, transport, max_sql=400, max_metadata=64, max_seconds=90):
        self.transport, self.max_sql, self.max_metadata, self.max_seconds = transport, max_sql, max_metadata, max_seconds
        self.receipts, self.metadata_catalog = [], {}
        self.started = time.monotonic()

    def guard(self):
        if time.monotonic() - self.started > self.max_seconds:
            raise ValueError("capture time budget exhausted; no retry")

    def query(self, sql, params, cap):
        self.guard()
        check_read_only(sql)
        if len(self.receipts) >= self.max_sql:
            raise ValueError("SQL budget exhausted; no silent truncation")
        record = {"sql": sql, "parameters": list(params), "cap": cap}
        self.receipts.append(record)
        started = time.monotonic()
        try:
            rows = self.transport.query(sql, params)
            record["rows"] = rows
            if len(rows) > cap:
                raise ValueError("SQL row budget exceeded")
            return rows
        except Exception as exc:
            record["failure"] = type(exc).__name__
            raise
        finally:
            record["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
            if record["elapsed_ms"] > 3000:
                raise ValueError("request exceeded three seconds; no retry")

    def fetch_metadata(self, key):
        self.guard()
        if key not in self.metadata_catalog:
            if not re.fullmatch("[0-9a-f]{64}", key) or len(self.metadata_catalog) >= self.max_metadata:
                raise ValueError("metadata identity/budget refused")
            started = time.monotonic()
            value = self.transport.metadata(key)
            if time.monotonic() - started > 3:
                raise ValueError("metadata exceeded three seconds; no retry")
            if hashlib.sha256(canonical(value)).hexdigest() != key:
                raise ValueError("immutable metadata hash mismatch")
            self.metadata_catalog[key] = value
        return self.metadata_catalog[key]


def bind(contract, feature, namespace):
    key = feature if feature in PUBLIC or feature == "level" else ("legacy_" if namespace == "legacy_if_prod" else "runs_") + feature
    return contract["points"][key]


def targets_for(metadata, contract):
    targets = []
    for meta in metadata:
        sampled = dt(meta["observed_at"])
        primary = "legacy_if_prod" if sampled < dt(contract["cutover_utc"]) else "runs_v1"
        for feature in FEATURES:
            namespaces = ("legacy_if_prod", "runs_v1") if feature in DESLIMING else ("public",)
            for namespace in namespaces:
                binding = bind(contract, feature, namespace)
                at = sampled - timedelta(seconds=90 if feature == "level" else 0)
                lower = at - timedelta(seconds=30 if feature == "level" else 120)
                targets.append({"assay_id": meta["id"], "feature": feature, "namespace": namespace,
                                "source_id": contract["source_id"], "point_key": binding["point_key"],
                                "at": stamp(at), "lower_at": stamp(lower), "production_namespace": primary})
    return targets


def history_queries(targets):
    queries = []
    for index in range(0, len(targets), 3):
        group = targets[index:index + 3]
        sql = HISTORY_SQL.format(slots=",".join("(?,?,?,?,?,?,?)" for _ in group))
        params = [target[key] for target in group for key in ("assay_id", "feature", "namespace", "source_id", "point_key", "at", "lower_at")]
        queries.append({"sql": sql, "parameters": params, "cap": len(group)})
    return queries


def evidence_for(record, contract, catalog):
    feature, namespace = record["feature"], record["namespace"]
    binding = bind(contract, feature, namespace)
    ev = {"status": "missing_recent_history", "feature": feature, "namespace": namespace, "at": record["at"],
          "source_id": contract["source_id"], "point_key": binding["point_key"], "value": None}
    if record.get("id") is None:
        return ev
    raw = record["sample"]
    sample = catalog.loads(raw) if isinstance(raw, str) else catalog.unpack(raw)
    age = (dt(record["at"]) - dt(record["received_at"])).total_seconds()
    ev.update(reading_id=record["id"], batch_id=record["batch_id"], received_at=record["received_at"],
              source_at=sample.get("source_at"), age_seconds=age, quality=sample.get("quality"),
              definition_id=sample.get("definition_id"), node_id=sample.get("node_id"),
              raw_sample_sha256=hashlib.sha256(canonical(sample)).hexdigest(), status_code=sample.get("status_code"))
    status = "good"
    if (record["source_id"] != contract["source_id"] or record["point_key"] != binding["point_key"]
            or sample.get("key") != binding["point_key"] or sample.get("node_id") != binding["config"]["node_id"]
            or sample.get("definition_id") != binding["definition_id"] or sample.get("received_at") != record["received_at"]):
        status = "identity_or_timestamp_mismatch"
    elif (sample.get("quality") != "good" or sample.get("issue") is not None
          or type(sample.get("status_code")) is not int or sample["status_code"] != 0):
        status = "bad_quality"
    elif feature not in ("coal_amount", "level"):
        if type(sample.get("raw_value")) is not bool or type(sample.get("value")) is not bool or sample["raw_value"] is not sample["value"]:
            status = "raw_boolean_required"
    elif (type(sample.get("raw_value")) not in (int, float) or type(sample.get("value")) not in (int, float)
          or not math.isfinite(sample["raw_value"]) or not math.isfinite(sample["value"])):
        status = "invalid_numeric"
    if status == "good":
        if age < 0 or age > (30 if feature == "level" else 65):
            status = "age_outside_frozen_contract"
        elif feature == "coal_amount" and sample["value"] <= 10:
            status = "stopped"
        elif feature == "level" and not 0 <= sample["value"] <= 100:
            status = "invalid_level"
        elif age > 30:
            status = "minute_approximation"
    maximum_source_age = binding["config"].get("max_source_age_seconds")
    if status in ("good", "minute_approximation") and maximum_source_age:
        if sample.get("source_at") is None or (dt(record["at"]) - dt(sample["source_at"])).total_seconds() > maximum_source_age:
            status = "stale_or_missing_source_time"
    ev["status"] = status
    # Retain bad raw inputs only in evidence; do not pass them as numeric factors.
    ev["value"] = int(sample["value"]) if type(sample.get("value")) is bool else sample.get("value")
    return ev


def quality(fixed, level):
    items = list(fixed.values()) + [level]
    if level["status"] != "good" or any(e["status"] not in ("good", "minute_approximation") for e in items):
        return "incomplete"
    batches = {(e.get("source_id"), e.get("batch_id"), e.get("received_at")) for e in fixed.values()}
    if len(batches) != 1 or any(not e.get("batch_id") for e in fixed.values()):
        return "mixed_batches"
    return "strict" if all(e["status"] == "good" for e in items) else "minute_approximation"


def reconstruct_inputs(evidence, contract, namespace_override=None):
    from point_metadata import Catalog
    catalog = Catalog(fetch=lambda key: evidence["metadata_catalog"][key], compact_writes=False)
    expected_targets = targets_for(evidence["metadata"], contract)
    keys = {(t["assay_id"], t["feature"], t["namespace"]): t for t in expected_targets}
    records = evidence["raw_records"]
    if len(records) != len(keys):
        raise ValueError("missing/duplicate raw feature evidence")
    per_id = {}
    seen = set()
    for record in records:
        key = (record["assay_id"], record["feature"], record["namespace"])
        if key not in keys or key in seen:
            raise ValueError("unknown/duplicate raw feature identity")
        expected = keys[key]
        if record["at"] != expected["at"] or record["requested_source_id"] != expected["source_id"] or record["requested_point_key"] != expected["point_key"]:
            raise ValueError("raw evidence requested target mismatch")
        seen.add(key)
        ev = evidence_for(record, contract, catalog)
        per_id.setdefault(record["assay_id"], {})[(record["feature"], record["namespace"])] = ev
    rows = []
    for meta in evidence["metadata"]:
        namespace = namespace_override or ("legacy_if_prod" if dt(meta["observed_at"]) < dt(contract["cutover_utc"]) else "runs_v1")
        all_ev = per_id[meta["id"]]
        fixed = {f: all_ev[(f, "public")] for f in PUBLIC}
        level = all_ev[("level", "public")]
        public_quality = quality(fixed, level)
        semantic = {**fixed, **{f: all_ev[(f, namespace)] for f in DESLIMING}}
        features = {f: ev["value"] if ev["status"] in ("good", "minute_approximation") else None for f, ev in semantic.items()}
        features["level"] = level["value"] if level["status"] == "good" else None
        rows.append({"record_id": f"db:manual_measurements:{meta['id']}", "assay_id": meta["id"],
                     "sample_at": meta["observed_at"], "date": dt(meta["observed_at"]).astimezone(timezone(timedelta(hours=8))).date().isoformat(),
                     "public_quality": public_quality, "feature_namespace": namespace,
                     "semantic_quality": quality(semantic, level), "features": features})
    return rows


def ex8_availability_rows(legacy_rows):
    """Never evaluate frozen EX8 against RUNS or label a native input as missing."""
    return [{"record_id": row["record_id"], "assay_id": row["assay_id"], "sample_at": row["sample_at"],
             "native_quality": row["semantic_quality"], "prediction": None,
             "status": "requires_native_prediction" if row["semantic_quality"] in ("strict", "minute_approximation") else "unavailable",
             "reason": "native_input_available_requires_actual_frozen_EX8_prediction_before_reveal" if row["semantic_quality"] in ("strict", "minute_approximation")
                       else "native_legacy_if_prod_not_pairable_no_RUNS_substitution"} for row in legacy_rows]


def reconstruct_production_inputs(evidence, contract):
    """Independent strict archived RUNS projection of the actual production routes."""
    from point_metadata import Catalog
    catalog = Catalog(fetch=lambda key: evidence["metadata_catalog"][key], compact_writes=False)
    rows = reconstruct_inputs(evidence, contract, namespace_override="runs_v1")
    per_id = {}
    for record in evidence["raw_records"]:
        if record["namespace"] in ("public", "runs_v1"):
            per_id.setdefault(record["assay_id"], {})[record["feature"]] = evidence_for(record, contract, catalog)
    for row in rows:
        observed = per_id[row["assay_id"]]
        valid = {feature: observed[feature]["status"] == "good"
                 and observed[feature].get("age_seconds") is not None
                 and 0 <= observed[feature]["age_seconds"] <= 30 for feature in FEATURES}
        def same_group(names):
            cells = [observed[name] for name in names]
            return (all(valid[name] for name in names) and all(cell.get("batch_id") and cell.get("received_at") for cell in cells)
                    and len({(cell.get("source_id"), cell.get("batch_id"), cell.get("received_at")) for cell in cells}) == 1)
        systems = ("sysA", "sysB", "sys401", "sys402")
        sys_good = same_group(systems)
        count = int(sum(observed[name]["value"] for name in systems)) if sys_good else None
        if count is None:
            reason = "production_system_feedback_unavailable"
        elif not valid["level"]:
            reason = "production_level_t90_unavailable"
        elif count != 2 and (not all(valid.values()) or not same_group(DESLIMING)):
            reason = "production_non_two_system_inputs_not_strict"
        else:
            reason = None
        row.update(baseline_id="EX8_PRODUCTION_RUNS", production_system_count=count,
                   production_quality="eligible" if reason is None else "unavailable", production_reason=reason,
                   production_feature_status={feature: observed[feature]["status"] for feature in FEATURES},
                   production_features_valid=valid,
                   production_policy_route=("two_system_fixed14_15" if count == 2 else "non_two_system_EX8_RUNS") if reason is None else None)
    return rows


def production_availability_rows(rows):
    return [{"record_id": row["record_id"], "assay_id": row["assay_id"], "sample_at": row["sample_at"],
             "production_system_count": row["production_system_count"], "production_quality": row["production_quality"],
             "status": "requires_production_prediction" if row["production_quality"] == "eligible" else "unavailable",
             "reason": row["production_reason"] or "awaiting_frozen_EX8_PRODUCTION_RUNS_prediction"} for row in rows]


def baseline_manifest_contracts(contract):
    entries = {"EX8_NATIVE_LEGACY": {"namespace": "legacy_if_prod", "inputs": "baseline-ex8-inputs.json", "availability_lock": "baseline-ex8-lock.json"}}
    if production_contract(contract) is not None:
        entries["EX8_PRODUCTION_RUNS"] = {"namespace": "runs_v1", "inputs": "production-ex8-inputs.json", "availability_lock": "production-ex8-lock.json", "parameter_sha256": EX8_PRODUCTION_PARAMETER_SHA256}
    return entries


def production_ex8_result(row, model):
    result = {"ex8_production_prediction": None, "ex8_production_reason": row["production_reason"],
              "current_production_prediction": None, "production_policy_route": None}
    if row["production_quality"] != "eligible":
        return result
    count = row["production_system_count"]
    if count == 2:
        value = 14. if row["features"]["level"] >= 30 else 15.
    else:
        features = row["features"]
        values = [features[feature] for feature in (*PUBLIC, *DESLIMING)]
        clipped = [min(bounds[1], max(bounds[0], value)) for value, bounds in zip(values, model["bounds"])]
        correction = 2. * math.tanh(sum(coef * (value - reference) / scale for coef, value, reference, scale in zip(model["coefs"], clipped, model["reference"], model["scale"])) / 2.)
        value = 30.5 - .3 * min(75., max(25., features["level"])) + correction
    if not math.isfinite(value) or not 0 <= value <= 100:
        result["ex8_production_reason"] = "production_EX8_output_outside_0_100"
        return result
    result.update(ex8_production_prediction=value, current_production_prediction=value, ex8_production_reason=None,
                  production_policy_route="two_system_fixed14_15" if count == 2 else "non_two_system_EX8_RUNS")
    return result


def native_ex8_result(row, model):
    """Independent frozen formula; use true legacy evidence, never candidate RUNS."""
    result = {"ex8_native_prediction": None, "ex8_native_reason": "native_legacy_if_prod_inputs_unavailable", "old_production_policy_prediction": None}
    values = row["features"]
    if row["public_quality"] not in ("strict", "minute_approximation"):
        return result
    try:
        systems = [float(values[key]) for key in ("sysA", "sysB", "sys401", "sys402")]
        level, coal = float(values["level"]), float(values["coal_amount"])
        if not all(value in (0., 1.) for value in systems) or not math.isfinite(level) or not math.isfinite(coal) or not 0 <= level <= 100 or coal <= 10:
            return result
        count = int(sum(systems))
        if count == 2:
            result.update(old_production_policy_prediction=14. if level >= 30. else 15., ex8_native_reason="two_system_actual_old_production_used14_15_lookup")
            return result
        if count != 3 or row["feature_namespace"] != "legacy_if_prod" or row["semantic_quality"] not in ("strict", "minute_approximation"):
            return result
        desliming = [float(values[key]) for key in DESLIMING]
        if not all(value in (0., 1.) for value in desliming):
            return result
        factors = [coal, *systems, *desliming]
        for key in ("bounds", "coefs", "reference", "scale"):
            if len(model[key]) != 7:
                raise ValueError("native EX8 parameter dimension mismatch")
        if any(not math.isfinite(float(value)) or float(value) <= 0 for value in model["scale"]):
            raise ValueError("native EX8 scale invalid")
        clipped = [min(bounds[1], max(bounds[0], value)) for value, bounds in zip(factors, model["bounds"])]
        correction = 2. * math.tanh(sum(coef * (value - ref) / scale for coef, value, ref, scale in zip(model["coefs"], clipped, model["reference"], model["scale"])) / 2.)
        prediction = 30.5 - .3 * min(75., max(25., level)) + correction
        if not math.isfinite(prediction):
            raise ValueError("native EX8 output nonfinite")
        result.update(ex8_native_prediction=prediction, ex8_native_reason=None, old_production_policy_prediction=prediction)
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError("native EX8 parameters/inputs invalid") from exc
    return result


def verify_native_predictions(freeze, collection, lock_path, context, lock, legacy_inputs):
    """Validate the separately sealed final entry and recompute its native baseline."""
    freeze, collection, lock_path = Path(freeze), Path(collection), Path(lock_path)
    runtime_path = freeze / "sealed-runtime-manifest.json"
    if context["seal"].get("sealed_runtime_manifest_sha256") != sha(runtime_path):
        raise ValueError("native EX8 runtime/final seal mismatch")
    runtime = read(runtime_path)
    if not {"predict-sealed.py", "ex8-native-model.json"}.issubset(runtime["files"]):
        raise ValueError("native EX8 sealed runtime incomplete")
    verify_files(freeze, runtime["files"])
    expected = {"sealed_runtime_manifest_sha256": sha(runtime_path),
                "native_EX8_model_sha256": sha(freeze / "ex8-native-model.json"),
                "native_EX8_input_file_sha256": sha(collection / "baseline-ex8-inputs.json")}
    for field, value in expected.items():
        if lock.get(field) != value:
            raise ValueError(f"native EX8 lock hash mismatch: {field}")
    selected_name = lock.get("selected_only_lock_file", lock_path.stem + ".selected-only.json")
    selected_path = bounded_member(lock_path.parent, selected_name)
    if lock.get("selected_only_lock_sha256") != sha(selected_path):
        raise ValueError("selected-only predecessor lock hash mismatch")
    selected = read(selected_path)
    native_fields = {"ex8_native_prediction", "ex8_native_reason", "old_production_policy_prediction", "system_count",
                     "ex8_production_prediction", "ex8_production_reason", "current_production_prediction", "production_policy_route", "production_system_count",
                     "comparison_system_count_consistent", "comparison_system_count_reason"}
    final_extra = {"selected_only_lock_sha256", "selected_only_lock_file", "selected_predicted_at_utc", "native_EX8_model_sha256", "native_EX8_input_file_sha256", "sealed_runtime_manifest_sha256", "baseline_contract", "runtime", "final_lock_write_contract",
                   "production_EX8_input_file_sha256", "production_EX8_model_sha256", "production_EX8_availability_lock_sha256", "production_baseline_contract_id", "comparison_baseline_field", "production_comparison_note"}
    selected_projection = {key: value for key, value in lock.items() if key not in final_extra}
    if "selected_predicted_at_utc" in lock:
        if lock["selected_predicted_at_utc"] != selected["predicted_at_utc"] or dt(lock["selected_predicted_at_utc"]) > dt(lock["predicted_at_utc"]):
            raise ValueError("selected/final prediction lock chronology mismatch")
        selected_projection["predicted_at_utc"] = lock["selected_predicted_at_utc"]
    selected_projection["rows"] = [{key: value for key, value in row.items() if key not in native_fields} for row in lock["rows"]]
    if selected_projection != selected:
        raise ValueError("final lock changed original candidate predictions/provenance")
    model = read(freeze / "ex8-native-model.json")["model"]
    for source, prediction in zip(legacy_inputs, lock["rows"]):
        expected_result = native_ex8_result(source, model)
        for field, value in expected_result.items():
            if field not in prediction:
                raise ValueError("native EX8 prediction/rejection missing")
            actual = prediction[field]
            if value is None or isinstance(value, str):
                if actual != value:
                    raise ValueError("native EX8 result inconsistent with legacy input")
            elif type(actual) not in (int, float) or not math.isfinite(actual) or not math.isclose(actual, value, rel_tol=1e-12, abs_tol=1e-10):
                raise ValueError("native EX8 prediction disagrees with sealed formula")


def verify_production_predictions(freeze, collection, context, lock, evidence):
    if production_contract(context["contract"]) is None:
        return
    collection, freeze = Path(collection), Path(freeze)
    rows = reconstruct_production_inputs(evidence, context["contract"])
    if read(collection / "production-ex8-inputs.json")["rows"] != rows:
        raise ValueError("production EX8 inputs differ from independent raw RUNS evidence")
    availability = read(collection / "production-ex8-lock.json")
    expected = {"input_file_sha256": sha(collection / "production-ex8-inputs.json"), "parameter_sha256": EX8_PRODUCTION_PARAMETER_SHA256,
                "input_evidence_sha256": sha(collection / "input-evidence.json"), "source_contract_sha256": context["source_contract_sha256"]}
    if (availability.get("schema") != "expert81-production-EX8-RUNS-availability-lock-v1"
            or availability.get("labels_read") is not False or availability.get("model_prediction_performed") is not False
            or any(availability.get(key) != value for key, value in expected.items())
            or availability.get("rows") != production_availability_rows(rows)):
        raise ValueError("production EX8 availability/input/parameter hash mismatch")
    runtime = read(freeze / "sealed-runtime-manifest.json")
    if "ex8-production-model.json" not in runtime["files"] or sha(freeze / "ex8-production-model.json") != EX8_PRODUCTION_PARAMETER_SHA256:
        raise ValueError("production EX8 parameters not exact frozen c720 bytes")
    if (lock.get("production_EX8_input_file_sha256") != expected["input_file_sha256"]
            or lock.get("production_EX8_model_sha256") != EX8_PRODUCTION_PARAMETER_SHA256
            or lock.get("production_EX8_availability_lock_sha256") != sha(collection / "production-ex8-lock.json")
            or lock.get("production_baseline_contract_id") != "EX8_PRODUCTION_RUNS"
            or lock.get("comparison_baseline_field") != "current_production_prediction"):
        raise ValueError("production EX8 final lock contract/hash mismatch")
    model = read(freeze / "ex8-production-model.json")["model"]
    for source, saved in zip(rows, lock["rows"]):
        for field, value in production_ex8_result(source, model).items():
            if field not in saved:
                raise ValueError("production EX8 locked result field missing")
            actual = saved[field]
            if value is None or isinstance(value, str):
                if actual != value:
                    raise ValueError("production EX8 result differs from strict RUNS evidence")
            elif type(actual) not in (int, float) or not math.isfinite(actual) or not math.isclose(actual, value, rel_tol=1e-12, abs_tol=1e-10):
                raise ValueError("production EX8 result differs from frozen production formula")
        if "production_system_count" in saved and saved["production_system_count"] != source["production_system_count"]:
            raise ValueError("production EX8 system count mismatch")
        # Candidate count was independently checked from raw public factors by
        # verify_candidate_predictions, never infer agreement from a saved flag.
        candidate_count, production_count = saved["system_count"], source["production_system_count"]
        consistent = (candidate_count == production_count) if candidate_count is not None and production_count is not None else None
        reason = "candidate_and_production_feedback_counts_differ" if consistent is False else None
        if ("comparison_system_count_consistent" not in saved or saved["comparison_system_count_consistent"] is not consistent
                or "comparison_system_count_reason" not in saved or saved["comparison_system_count_reason"] != reason):
            raise ValueError("comparison system count agreement/reason differs from independent evidence")


def runtime_versions():
    packages = {"numpy": "numpy", "scipy": "scipy", "sklearn": "scikit-learn", "joblib": "joblib", "threadpoolctl": "threadpoolctl"}
    return {"python": sys.version, **{name: importlib.metadata.version(package) for name, package in packages.items()}}


def verify_candidate_predictions(freeze, context, inputs, lock):
    """Recompute predictions, so two synchronously forged ledgers cannot pass."""
    freeze = Path(freeze)
    actual_runtime, expected_runtime = runtime_versions(), read(freeze / "environment.json")
    mismatches = {name: {"expected": expected_runtime.get(name), "actual": actual} for name, actual in actual_runtime.items() if expected_runtime.get(name) != actual}
    if mismatches:
        raise ValueError("candidate runtime version mismatch; refused before model deserialization")
    if "sealed_runtime_manifest_sha256" in context["seal"] and lock.get("runtime") != actual_runtime:
        raise ValueError("candidate lock runtime differs from actual frozen environment")
    import joblib
    # These exact files were verified against the final seal before this function.
    model = joblib.load(freeze / "model.joblib")
    if model.get("model_version") != context["manifest"]["model_version"] or model.get("db_exposure_cutoff") != context["manifest"]["db_exposure_cutoff"]:
        raise ValueError("candidate serialized model identity/cutoff mismatch")
    spec = importlib.util.spec_from_file_location("reveal_frozen_candidate_inference", freeze / "inference_core.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    before = io.BytesIO(); joblib.dump(model, before, compress=3)
    independent_inputs = copy.deepcopy(inputs)
    expected = module.predict_rows(model, independent_inputs)
    after = io.BytesIO(); joblib.dump(model, after, compress=3)
    if before.getvalue() != after.getvalue() or independent_inputs != inputs:
        raise ValueError("candidate inference mutated frozen state/inputs")
    verify_files(freeze, context["manifest"]["files"])
    if len(expected) != len(lock["rows"]):
        raise ValueError("candidate recomputation row count mismatch")
    for source, result, saved in zip(inputs, expected, lock["rows"]):
        for field, value in result.items():
            if field not in saved:
                raise ValueError("candidate prediction lock field missing: " + field)
            observed = saved[field]
            if field in ("prediction", "raw_prediction") and value is not None:
                if type(observed) not in (int, float) or not math.isfinite(observed) or not math.isclose(observed, value, rel_tol=1e-12, abs_tol=1e-10):
                    raise ValueError("candidate prediction differs from frozen model: " + field)
            elif observed != value:
                raise ValueError("candidate result differs from frozen inference: " + field)
        try:
            systems = [float(source["features"][key]) for key in ("sysA", "sysB", "sys401", "sys402")]
            count = int(sum(systems)) if all(value in (0., 1.) for value in systems) else None
        except (KeyError, ValueError, TypeError):
            count = None
        if "sealed_runtime_manifest_sha256" in context["seal"] or "system_count" in saved:
            if "system_count" not in saved or saved["system_count"] != count:
                raise ValueError("candidate system_count differs from trusted public feedback")


def validate_metadata(metadata, context, max_samples):
    if len(metadata) > max_samples:
        raise ValueError("metadata sample cap reached; refuse incomplete capture and choose explicit larger reviewed budget")
    ids = [m["id"] for m in metadata]
    if ids != sorted(set(ids)):
        raise ValueError("metadata IDs must be unique and ordered")
    excluded = set(context["seal"]["exposed_assay_ids"])
    for meta in metadata:
        if set(meta) != {"id", "observed_at", "created_at", "time_basis"}:
            raise ValueError("metadata projection mismatch or possible labels")
        if (type(meta["id"]) is not int or meta["id"] <= max(excluded) or meta["id"] in excluded
                or not context["frozen"] < dt(meta["observed_at"]) <= context["due"]
                or not context["frozen"] < dt(meta["created_at"])):
            raise ValueError("metadata outside frozen window or exposed identity")


def prepare_inputs(freeze, contract_path, output, *, execute=False, transport_factory=RemoteTransport, now=None, max_samples=100, max_sql=400):
    context = verify_freeze(freeze, contract_path, now)
    if not 1 <= max_samples <= 100 or not 4 <= max_sql <= 400:
        raise ValueError("unsupported bounded capture budget")
    frozen, due = stamp(context["frozen"]), stamp(context["due"])
    params = ["coarse_slime_ash", max(context["seal"]["exposed_assay_ids"]), frozen, due, frozen, max_samples + 1]
    plan = {"schema": "expert81-future-input-plan-v1", "mode": "execute" if execute else "prepared_no_requests",
            "window": {"frozen_at_utc": frozen, "due_at_utc": due}, "metadata_sql": META_SQL, "parameters": params,
            "max_samples": max_samples, "max_sql": max_sql, "max_metadata": 64, "max_seconds": 90,
            "timeout_seconds_per_http": 3, "retries": 0, "labels_read": False,
            "exposed_assay_ids": context["seal"]["exposed_assay_ids"],
            **{k: context[k] for k in ("freeze_manifest_sha256", "model_file_sha256", "source_contract_sha256", "final_seal_sha256")}}
    if not execute:
        return plan
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    write(output / "collection-plan.json", plan)
    session = Session(transport_factory(), max_sql=max_sql)
    failure = None
    evidence = None
    try:
        explain = session.query("EXPLAIN QUERY PLAN " + META_SQL, params, 4)
        if not explain or any("SEARCH " not in e["detail"] or "ix_manual_measurement_metric_id" not in e["detail"] for e in explain):
            raise ValueError("unsafe metadata index plan")
        clock = session.query(CLOCK_SQL, [], 1)
        if len(clock) != 1 or dt(clock[0]["host_checked_at"]) < context["due"]:
            raise ValueError("host clock has not reached due window")
        metadata = session.query(META_SQL, params, max_samples + 1)
        validate_metadata(metadata, context, max_samples)
        queries = history_queries(targets_for(metadata, context["contract"]))
        if len(queries) + len(session.receipts) + bool(queries) > max_sql:
            raise ValueError("insufficient SQL budget; refuse before feature requests")
        write(output / "feature-query-plan.json", {"queries": queries, "target_count": len(targets_for(metadata, context["contract"]))})
        if queries:
            query = queries[0]
            explain = session.query("EXPLAIN QUERY PLAN " + query["sql"], query["parameters"], 12)
            details = [e["detail"] for e in explain]
            if not any("SEARCH h USING" in d and "ix_telemetry_point_time" in d for d in details) or any("SCAN h" in d or "SCAN r" in d or "TEMP B-TREE" in d for d in details):
                raise ValueError("unsafe telemetry index plan")
        raw_records = []
        for query in queries:
            raw_records.extend(session.query(query["sql"], query["parameters"], query["cap"]))
        # Expand compact references through the exact immutable, hash-checked catalog.
        from point_metadata import Catalog
        catalog = Catalog(fetch=session.fetch_metadata, compact_writes=False)
        for record in raw_records:
            if record.get("id") is not None:
                catalog.loads(record["sample"]) if isinstance(record["sample"], str) else catalog.unpack(record["sample"])
        evidence = {"schema": "expert81-future-raw-input-evidence-v1", "metadata": metadata, "raw_records": raw_records,
                    "metadata_catalog": session.metadata_catalog, "host_checked_at": clock[0]["host_checked_at"],
                    "clock_domains": {"host_checked_at": "production SQL clock", "sample_at": "submitted sampling time; physical precision/synchronization unverified", "capture": "client UTC; not certified synchronized"}}
        rows = reconstruct_inputs(evidence, context["contract"])
        write(output / "input-evidence.json", evidence)
        write(output / "inputs.json", {"rows": rows})
        legacy_rows = reconstruct_inputs(evidence, context["contract"], namespace_override="legacy_if_prod")
        write(output / "baseline-ex8-inputs.json", {"rows": legacy_rows})
        write(output / "baseline-ex8-lock.json", {"schema": "expert81-native-EX8-availability-lock-v1",
              "model_identity": "frozen_EXPERT8_native_legacy_if_prod", "model_prediction_performed": False,
              "input_file_sha256": sha(output / "baseline-ex8-inputs.json"), "input_evidence_sha256": sha(output / "input-evidence.json"),
              "source_contract_sha256": context["source_contract_sha256"], "labels_read": False,
              "locked_at_utc": stamp(datetime.now(timezone.utc)), "rows": ex8_availability_rows(legacy_rows)})
        if production_contract(context["contract"]) is not None:
            production_rows = reconstruct_production_inputs(evidence, context["contract"])
            write(output / "production-ex8-inputs.json", {"rows": production_rows})
            write(output / "production-ex8-lock.json", {"schema": "expert81-production-EX8-RUNS-availability-lock-v1",
                  "model_identity": "EX8_PRODUCTION_RUNS", "model_prediction_performed": False, "labels_read": False,
                  "parameter_sha256": EX8_PRODUCTION_PARAMETER_SHA256, "input_file_sha256": sha(output / "production-ex8-inputs.json"),
                  "input_evidence_sha256": sha(output / "input-evidence.json"), "source_contract_sha256": context["source_contract_sha256"],
                  "locked_at_utc": stamp(datetime.now(timezone.utc)), "rows": production_availability_rows(production_rows)})
    except Exception as exc:
        failure = {"error_type": type(exc).__name__, "reason": str(exc) if isinstance(exc, ValueError) else "transport_or_decode_failure_no_retry"}
        raise
    finally:
        close_error = None
        try:
            session.transport.close()
        except Exception as exc:
            close_error = exc
            failure = failure or {"error_type": type(exc).__name__, "reason": "read_transaction_close_failed"}
        finally:
            write(output / "query-receipts.json", session.receipts)
            write(output / "http-receipts.json", getattr(session.transport, "http_receipts", []))
            files = {p.name: {"bytes": p.stat().st_size, "sha256": sha(p)} for p in sorted(output.iterdir()) if p.is_file()}
            write(output / "input-manifest.json", {"schema": INPUT_SCHEMA, "status": "complete" if failure is None and evidence is not None else "failed_no_retry", "failure": failure,
                  "files": files, "labels_read": False, "fits_performed": 0,
                  "input_file_sha256": sha(output / "inputs.json") if (output / "inputs.json").exists() else None,
                  **{k: context[k] for k in ("freeze_manifest_sha256", "model_file_sha256", "source_contract_sha256", "final_seal_sha256")},
                  "assay_ids": [m["id"] for m in evidence["metadata"]] if evidence else [], "window": plan["window"],
                  "baseline_contracts": baseline_manifest_contracts(context["contract"]),
                  "comparison_baseline_field": "current_production_prediction" if production_contract(context["contract"]) is not None else "ex8_native_prediction",
                  "captured_at_utc": stamp(datetime.now(timezone.utc)), "production_mutations": False})
        if close_error is not None:
            raise close_error
    return read(output / "input-manifest.json")


def verify_collection(freeze, contract_path, collection, lock_path, now=None):
    context = verify_freeze(freeze, contract_path, now)
    collection = Path(collection)
    manifest_path = collection / "input-manifest.json"
    manifest = read(manifest_path)
    if manifest["schema"] != INPUT_SCHEMA or manifest["status"] != "complete" or manifest["labels_read"] is not False:
        raise ValueError("complete label-free collection required")
    required = {"inputs.json", "input-evidence.json", "collection-plan.json", "feature-query-plan.json", "query-receipts.json", "http-receipts.json", "baseline-ex8-inputs.json", "baseline-ex8-lock.json"}
    if production_contract(context["contract"]) is not None:
        required.update({"production-ex8-inputs.json", "production-ex8-lock.json"})
    if not required.issubset(manifest["files"]):
        raise ValueError("complete input/evidence/receipt chain required")
    if (manifest.get("baseline_contracts") != baseline_manifest_contracts(context["contract"])
            or manifest.get("comparison_baseline_field") != ("current_production_prediction" if production_contract(context["contract"]) is not None else "ex8_native_prediction")):
        raise ValueError("collection EX8 namespace/comparison contracts mismatch")
    verify_files(collection, manifest["files"])
    for field in ("freeze_manifest_sha256", "model_file_sha256", "source_contract_sha256", "final_seal_sha256"):
        if manifest[field] != context[field]:
            raise ValueError(f"collection seal mismatch: {field}")
    expected_window = {"frozen_at_utc": stamp(context["frozen"]), "due_at_utc": stamp(context["due"])}
    if manifest["window"] != expected_window:
        raise ValueError("collection window differs from final seal")
    collection_plan = read(collection / "collection-plan.json")
    if collection_plan["window"] != expected_window or collection_plan["exposed_assay_ids"] != context["seal"]["exposed_assay_ids"]:
        raise ValueError("collection plan differs from sealed exposure/window")
    if manifest["input_file_sha256"] != sha(collection / "inputs.json"):
        raise ValueError("input file hash mismatch")
    evidence = read(collection / "input-evidence.json")
    if dt(evidence["host_checked_at"]) < context["due"]:
        raise ValueError("input evidence predates due host clock")
    validate_metadata(evidence["metadata"], context, 100)
    inputs = read(collection / "inputs.json")["rows"]
    if inputs != reconstruct_inputs(evidence, context["contract"]):
        raise ValueError("factors/quality inconsistent with raw source evidence")
    legacy_inputs = read(collection / "baseline-ex8-inputs.json")["rows"]
    baseline = read(collection / "baseline-ex8-lock.json")
    if legacy_inputs != reconstruct_inputs(evidence, context["contract"], namespace_override="legacy_if_prod"):
        raise ValueError("EX8 native inputs inconsistent with true legacy evidence")
    if (baseline.get("schema") != "expert81-native-EX8-availability-lock-v1" or baseline.get("labels_read") is not False
            or baseline.get("input_file_sha256") != sha(collection / "baseline-ex8-inputs.json")
            or baseline.get("input_evidence_sha256") != sha(collection / "input-evidence.json")
            or baseline.get("source_contract_sha256") != context["source_contract_sha256"]
            or baseline.get("rows") != ex8_availability_rows(legacy_inputs)):
        raise ValueError("EX8 native availability lock mismatch")
    ids = [row["assay_id"] for row in inputs]
    if ids != manifest["assay_ids"]:
        raise ValueError("collection ID ledger mismatch")
    receipts = read(collection / "query-receipts.json")
    if any("failure" in receipt for receipt in receipts):
        raise ValueError("failed input query receipt")
    for receipt in receipts:
        check_read_only(receipt["sql"])
        if re.search(r"\b(value|actual|ash|label|target)\b", receipt["sql"], re.I):
            raise ValueError("label projection in prepare receipt refused")
        if len(receipt["rows"]) > receipt["cap"]:
            raise ValueError("invalid input receipt cap")
    # The raw observations must be the actual returned SQL rows, not a second mutable copy.
    metadata_receipts = [r for r in receipts if r["sql"] == META_SQL]
    history_rows = [row for r in receipts if r["sql"].startswith("WITH inputs(") for row in r["rows"]]
    if len(metadata_receipts) != 1 or metadata_receipts[0]["rows"] != evidence["metadata"] or history_rows != evidence["raw_records"]:
        raise ValueError("raw evidence/query receipt mismatch")
    lock = read(lock_path)
    if lock.get("schema") != LOCK_SCHEMA or lock.get("labels_read") is not False or type(lock.get("fits_performed")) is not int or lock["fits_performed"] != 0 or lock.get("provenance_verified") is not True:
        raise ValueError("verified pure-predict lock required before labels")
    expected = {"input_file_sha256": sha(collection / "inputs.json"), "input_manifest_sha256": sha(manifest_path),
                "freeze_manifest_sha256": context["freeze_manifest_sha256"], "model_file_sha256": context["model_file_sha256"],
                "final_seal_sha256": context["final_seal_sha256"], "source_contract_sha256": context["source_contract_sha256"]}
    for field, value in expected.items():
        if lock.get(field) != value:
            raise ValueError(f"prediction lock hash mismatch: {field}")
    if lock.get("model_version") != context["manifest"]["model_version"] or lock.get("frozen_training_cutoff") != context["manifest"]["db_exposure_cutoff"]:
        raise ValueError("prediction model version/cutoff mismatch")
    if dt(lock["predicted_at_utc"]) < dt(manifest["captured_at_utc"]) or dt(lock["predicted_at_utc"]) > (now or datetime.now(timezone.utc)):
        raise ValueError("prediction lock chronology refused")
    locked_rows = lock.get("rows", [])
    if len(locked_rows) != len(inputs):
        raise ValueError("prediction lock must include rejected inputs too")
    for source, prediction in zip(inputs, locked_rows):
        if not {"record_id", "sample_at", "prediction", "raw_prediction", "status", "reason", "outside_training_ranges", "route"}.issubset(prediction):
            raise ValueError("incomplete prediction row schema")
        if prediction.get("record_id") != source["record_id"] or prediction.get("sample_at") != source["sample_at"]:
            raise ValueError("prediction lock ordered identity mismatch")
        if prediction.get("assay_id", source["assay_id"]) != source["assay_id"]:
            raise ValueError("prediction assay ID mismatch")
        value = prediction.get("prediction")
        raw = prediction.get("raw_prediction")
        if prediction["status"] not in ("ok", "unavailable"):
            raise ValueError("unknown prediction row status")
        if prediction["status"] == "ok" and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100
                                             or type(raw) not in (int, float) or not math.isfinite(raw)):
            raise ValueError("invalid locked prediction")
        if prediction["status"] == "unavailable" and (value is not None or raw is not None):
            raise ValueError("rejected prediction must not contain numeric output")
    if "sealed_runtime_manifest_sha256" in context["seal"] or "native_EX8_model_sha256" in lock:
        verify_native_predictions(freeze, collection, lock_path, context, lock, legacy_inputs)
    elif any(row["status"] != "unavailable" for row in baseline["rows"]):
        raise ValueError("native EX8 input exists; obtain actual native EX8 prediction lock before labels, never invent unavailability")
    verify_candidate_predictions(freeze, context, reconstruct_inputs(evidence, context["contract"]), lock)
    verify_production_predictions(freeze, collection, context, lock, evidence)
    return context, manifest, inputs, lock


def reveal_labels(freeze, contract_path, collection, lock_path, output, *, execute=False, transport_factory=RemoteTransport, now=None):
    context, manifest, inputs, lock = verify_collection(freeze, contract_path, collection, lock_path, now)
    ids = [row["assay_id"] for row in inputs]
    queries = [{"sql": "SELECT id,value,observed_at,created_at,time_basis FROM manual_measurements INDEXED BY ix_manual_measurement_metric_id WHERE metric=? AND id IN (" + ",".join("?" for _ in ids[start:start + 20]) + ") ORDER BY id LIMIT 20",
                "parameters": ["coarse_slime_ash", *ids[start:start + 20]], "cap": len(ids[start:start + 20])} for start in range(0, len(ids), 20)]
    plan = {"schema": "expert81-future-label-reveal-plan-v1", "mode": "execute" if execute else "prepared_no_requests",
            "prediction_lock_sha256": sha(lock_path), "input_manifest_sha256": sha(Path(collection) / "input-manifest.json"),
            "assay_ids": ids, "queries": queries, "window": manifest["window"],
            "evaluation_status": "future_time_holdout_replay_not_certified_real_time_prospective",
            "availability_caution": "Lock exists before this read; that does not establish prediction preceded original report or label availability. created_at is initial entry, not final report/version time.",
            "retries": 0, "timeout_seconds_per_http": 3}
    if not execute:
        return plan
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    write(output / "label-reveal-plan.json", plan)
    session = Session(transport_factory(), max_sql=6)
    records, failure = [], None
    try:
        for query in queries:
            records.extend(session.query(query["sql"], query["parameters"], query["cap"]))
        if [row["id"] for row in records] != ids:
            raise ValueError("label IDs missing/changed; no silent score denominator change")
        for source, record in zip(inputs, records):
            if dt(record["observed_at"]) != dt(source["sample_at"]):
                raise ValueError("sampling time changed after input lock")
        captured = stamp(datetime.now(timezone.utc))
        write(output / "labels.json", {"schema": "expert81-future-label-capture-v1", "records": records,
              "label_versions": {str(row["id"]): hashlib.sha256(canonical(row)).hexdigest() for row in records},
              "captured_at_utc": captured, "capture_clock": "client UTC; not synchronized certification",
              "prediction_lock_sha256": sha(lock_path), "evaluation_status": plan["evaluation_status"],
              "availability_caution": plan["availability_caution"]})
    except Exception as exc:
        failure = {"error_type": type(exc).__name__, "reason": str(exc) if isinstance(exc, ValueError) else "transport_failure_no_retry"}
        raise
    finally:
        close_error = None
        try:
            session.transport.close()
        except Exception as exc:
            close_error = exc
            failure = failure or {"error_type": type(exc).__name__, "reason": "read_transaction_close_failed"}
        finally:
            write(output / "query-receipts.json", session.receipts)
            write(output / "http-receipts.json", getattr(session.transport, "http_receipts", []))
            write(output / "label-capture-manifest.json", {"status": "complete" if failure is None else "failed_no_retry", "failure": failure,
                  "prediction_lock_sha256": sha(lock_path), "files": {p.name: {"bytes": p.stat().st_size, "sha256": sha(p)} for p in sorted(output.iterdir()) if p.is_file()}})
        if close_error is not None:
            raise close_error
    return read(output / "label-capture-manifest.json")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare-inputs", "reveal-labels"))
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--source-contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="explicitly execute the bounded read-only plan")
    parser.add_argument("--dry-run", action="store_true", help="validate and show plan; never open transport")
    parser.add_argument("--collection", type=Path)
    parser.add_argument("--prediction-lock", type=Path)
    args = parser.parse_args(argv)
    if args.execute and args.dry_run:
        parser.error("choose execute or dry-run")
    if args.stage == "prepare-inputs":
        result = prepare_inputs(args.freeze, args.source_contract, args.output, execute=args.execute)
    else:
        if args.collection is None or args.prediction_lock is None:
            parser.error("reveal-labels requires collection and prediction lock")
        result = reveal_labels(args.freeze, args.source_contract, args.collection, args.prediction_lock, args.output, execute=args.execute)
    print(json.dumps({key: value for key, value in result.items() if key not in ("files", "queries")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
