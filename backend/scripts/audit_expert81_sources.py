"""Build a local, lossless source inventory; never fit a model or query production."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import subprocess
from collections import Counter, defaultdict
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

import openpyxl


ROOT = Path(__file__).resolve().parents[2]
FEATURES = (
    "coal_amount", "sysA", "sysB", "sys401", "sys402",
    "desliming473", "desliming474", "level",
)
SWITCHES = FEATURES[1:7]
MAIN_GROUP = "0,1,1,1,0,0"
LEGACY_COMMIT = "617491576e23533761cff5d2506aaa41c92b6f3e"
RECENT_COMMIT = "c0b83a157c0b13cc6cfa00af605f0b2970e1707f"
MODEL_REL = "backend/models/gpt_expert8/20261012/model.json"
JOIN_REL = "backend/data/gpt_coarse/recent_assays/20261013/joined.json"
ADDENDUM = "output/gpt-expert81-expanded-20261102/production_addendum"
OUT_REL = "output/gpt-expert81-expanded-20261102/source_inventory"
REPORT_REL = "docs/verification/coarse-ash-sample-inventory-20261102.md"
SOURCE_DIR = Path("C:/Users/25925/Desktop/web(2)导入数据")


def plain(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    return value


def canonical(value: Any) -> bytes:
    return json.dumps(plain(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def iso(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp lacks timezone: {value}")
    return parsed.isoformat()


def same_time(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and (
        datetime.fromisoformat(iso(a)) == datetime.fromisoformat(iso(b))
    )


def finite(value: Any) -> int | float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if math.isfinite(value) else None
    return None


def feature_values(values: dict[str, Any]) -> dict[str, int | float | None]:
    result = {}
    for name in FEATURES:
        value = values.get(name)
        if name in SWITCHES and isinstance(value, str):
            value = {"开": 1, "停": 0, "关": 0}.get(value)
        result[name] = finite(value)
        if name in SWITCHES and result[name] not in (None, 0, 1):
            raise ValueError(f"Unknown switch value for {name}: {value}")
    return result


def capture_at(doc: dict[str, Any]) -> str:
    for key in ("first_complete_capture_at", "captured_at_capture_process_clock",
                "captured_at", "capture_at", "first_value_capture_at"):
        if isinstance(doc.get(key), str):
            return iso(doc[key])
    raise ValueError(f"No capture timestamp in {list(doc)}")


def data_rows(doc: Any) -> list[dict[str, Any]]:
    if isinstance(doc, list):
        return doc
    if isinstance(doc.get("raw_row"), dict):
        return [doc["raw_row"]]
    for key in ("raw_rows", "rows", "records"):
        if isinstance(doc.get(key), list):
            return doc[key]
    raise ValueError("Expected an explicit rows/raw_rows array")


class Inputs:
    def __init__(self, root: Path):
        self.root = root
        self.registry: dict[str, dict[str, Any]] = {}
        self.original_bytes: dict[Path, bytes] = {}

    def file(self, name: str, path: Path) -> bytes:
        path = path.resolve()
        data = path.read_bytes()
        self.original_bytes[path] = data
        try:
            display = path.relative_to(self.root).as_posix()
        except ValueError:
            display = path.as_posix()
        self.registry[name] = {
            "path": display, "sha256": digest(data), "bytes": len(data),
        }
        return data

    def json(self, name: str, path: Path) -> Any:
        return json.loads(self.file(name, path).decode("utf-8-sig"))

    def workbook(self, name: str, path: Path) -> list[list[Any]]:
        data = self.file(name, path)
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            return [list(row) for row in book["Sheet1"].iter_rows(values_only=True)]
        finally:
            book.close()

    def git_json(self, name: str, commit: str, rel: str, current: Any) -> Any:
        def git(*args: str) -> bytes:
            return subprocess.run(
                ["git", *args], cwd=self.root, check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ).stdout
        data = git("show", f"{commit}:{rel}")
        obj = json.loads(data)
        if obj != current:
            raise ValueError(f"Git archive is not semantically equal: {name}")
        author, committer = git(
            "show", "-s", "--format=%aI|%cI", commit,
        ).decode().strip().split("|")
        if author != committer:
            raise ValueError(f"Conflicting archive timestamps: {name}")
        self.registry[name] = {
            "kind": "git_archival_presence", "commit": commit, "path": rel,
            "blob_id": git("rev-parse", f"{commit}:{rel}").decode().strip(),
            "sha256": digest(data), "bytes": len(data),
            "author_at": author, "committer_at": committer,
            "available_at": iso(committer),
            "clock_domain": "git-commit-author-and-committer-clock",
            "semantic_equal_current_file": True,
            "not_a_field_lab_report_timestamp": True,
        }
        return obj

    def ref(self, name: str) -> dict[str, Any]:
        return {"source": name, **self.registry[name]}

    def unchanged(self) -> None:
        for path, expected in self.original_bytes.items():
            if path.read_bytes() != expected:
                raise ValueError(f"Source changed during inventory: {path}")


def base_record(source_type: str, source_row: int, record_id: str,
                raw: Any, value: Any, observed: Any, normalized: str | None,
                features: dict[str, Any], quality: str, pairing: str,
                ref: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "expert81-source-record-v1",
        "record_id": record_id, "source_key": record_id,
        "source_type": source_type, "source_row": source_row,
        "source_row_id": source_row,
        "raw_observed_at": plain(observed), "sample_at_raw": plain(observed),
        "normalized_time": iso(normalized), "sample_at": iso(normalized),
        "time_precision": "stored time fields; physical second precision unverified",
        "date_status": "source_timestamp",
        "time_inference_flags": [], "label": finite(value),
        "value": finite(value), "ash_content": finite(value),
        "features": feature_values(features),
        "feature_quality": pairing, "eligibility": quality,
        "quality": {"sample_eligibility": quality, "pairing": pairing},
        "feature_provenance": {}, "provenance": [ref],
        "available_at": None, "availability_at": None,
        "availability_evidence_kind": None, "availability_clock_domain": None,
        "availability_evidence": [], "first_capture_at": None,
        "latest_capture_at": None,
        "old177_membership": False, "old177_source_row": None,
        "old_import_stopped_row": False, "old_frozen68_membership": False,
        "frozen_value": None, "frozen_features": None,
        "frozen_value_version_ref": None, "frozen_first_capture_at": None,
        "current_metadata_capture_at": None,
        "value_changed_since_frozen": None,
        "value_changed_since_prior_6_capture": None,
        "raw_record_hash": digest(canonical(raw)),
        "exclusion_reasons": [], "training_cautions": [],
        "duplicate_group": None, "canonical_record_id": record_id,
        "match_basis": "source-row identity; no cross-source merge",
        "duplicate_suspected": False,
    }


def availability(row: dict[str, Any], when: str, kind: str, clock: str,
                 evidence: dict[str, Any]) -> None:
    row.update(available_at=iso(when), availability_at=iso(when),
               first_capture_at=iso(when), latest_capture_at=iso(when),
               availability_evidence_kind=kind, availability_clock_domain=clock)
    row["availability_evidence"].append({
        "kind": kind, "at": iso(when), "clock_domain": clock,
        "initial_created_at_is_not_version_availability": True,
        "does_not_certify_field_report_time_or_clock_sync": True,
        "evidence": evidence,
    })


def build_full(inputs: Inputs, workbook: list[list[Any]], model: dict[str, Any]) -> list[dict[str, Any]]:
    audit = model["source"]["original_audit"]
    archived = {r["source_row"]: r for r in model["rows"]}
    stopped = {r["source_row"]: r for r in audit["stopped_rows"]}
    all_archive = {**archived, **stopped}
    if set(all_archive) != set(range(4, 189)) or len(archived) != 177:
        raise ValueError("Frozen legacy source-row partition changed")
    if model["source"]["source_workbook_sha256"] != inputs.registry["full_workbook"]["sha256"]:
        raise ValueError("Full workbook is not the archived version")
    repairs = {r["row"]: r for r in audit["date_repairs"]}
    col = dict(zip(FEATURES, (3, 4, 5, 6, 7, 8, 9, 11)))
    result = []
    for n in range(4, 189):
        raw, old = workbook[n - 1][:12], all_archive[n]
        features = feature_values({f: raw[c] for f, c in col.items()})
        if any(features[f] != old[f] for f in FEATURES) or raw[10] != old["ash_content"]:
            raise ValueError(f"Legacy nine-field mismatch at workbook row {n}")
        row = base_record(
            "full_workbook_185", n, f"xlsx:full185:Sheet1:r{n}",
            raw, raw[10], {"date_cell": raw[0], "time_cell": plain(raw[1])},
            old["timestamp"].replace(" ", "T") + "+08:00",
            features, "source_table", "source_table",
            {**inputs.ref("full_workbook"), "sheet": "Sheet1", "row": n},
        )
        row["date_status"] = "archive_sequence_repair" if n in repairs else "raw_month_day_user_year_2026"
        row["time_inference_flags"] = ["year_2026_user_confirmed", "workbook_local_time_basis_not_clock_certification"]
        if n in repairs:
            row["time_inference_flags"].append("numeric_date_tail_zero_repair_not_field_verified")
            row["date_repair_evidence"] = repairs[n]
        row["old177_membership"] = n in archived
        row["old177_source_row"] = n if n in archived else None
        row["old_import_stopped_row"] = n in stopped
        row["historical_exclusion_reason"] = (
            "old importer coal_amount <= 10; not an expanded-training deletion rule"
            if n in stopped else None
        )
        row["feature_provenance"] = {
            f: {"kind": "original_workbook_cell", "column": openpyxl.utils.get_column_letter(c + 1),
                "source_row": n, "level_time_basis": "user supplied t-90; physical lag unverified" if f == "level" else None}
            for f, c in col.items()
        }
        archive = inputs.ref("git_legacy_model")
        row["provenance"].append(archive)
        availability(row, archive["available_at"], "git_archival_presence",
                     archive["clock_domain"], archive)
        result.append(row)
    return result


def build_recent(inputs: Inputs, workbook: list[list[Any]], joined: dict[str, Any]) -> list[dict[str, Any]]:
    if joined["source_sha256"] != inputs.registry["recent_workbook"]["sha256"]:
        raise ValueError("Recent workbook differs from archived join")
    by_source = {r["source_row"]: r for r in joined["rows"]}
    if len(by_source) != 21 or joined["summary"]["offline_complete"] != 5:
        raise ValueError("Expected 21 recent rows and five approximate complete joins")
    result, group = [], None
    for n, raw in enumerate(workbook, 1):
        if n < 3 or not any(v is not None for v in raw):
            continue
        if raw[0] is not None:
            group = raw[0]
        old = by_source[n]
        if raw[2] != old["ash_content"] or plain(raw[1]) != old["source_time"]:
            raise ValueError(f"Recent label/time mismatch at row {n}")
        complete = old["offline_complete"]
        row = base_record(
            "recent_workbook_21", n, f"xlsx:recent21:Sheet1:r{n}",
            raw, raw[2], {"date_cell": raw[0], "group_date": group, "time_cell": plain(raw[1])},
            old["sample_at"], old["features"],
            "minute_approximation" if complete else "incomplete",
            "minute_approximation" if complete else "incomplete",
            {**inputs.ref("recent_workbook"), "sheet": "Sheet1", "row": n},
        )
        row["date_status"] = "archived_join_normalization_not_field_reverified"
        row["time_inference_flags"] = ["year_2026_user_confirmed"]
        if raw[0] is None:
            row["time_inference_flags"].append("worksheet_date_group_carry_forward")
        if str(group) == "10.1":
            row["time_inference_flags"].append("10.1_to_10.10_archived_order_inference")
        if old["sample_at"][:10] != old["production_day"]:
            row["time_inference_flags"].append(
                "00:18_next_day_user_confirmed" if n == 6 else "midnight_next_day_from_archived_join_policy"
            )
        row["feature_provenance"] = old["matches"]
        row["quality"].update({
            "archived_offline_complete": complete,
            "archived_strict_complete": old["strict_30s_complete"],
            "reasons": old["reasons"], "policy": joined["policy"],
        })
        row["training_cautions"].append("approximate augmentation only" if complete else "missing factors are not imputed")
        archive = inputs.ref("git_recent_join")
        row["provenance"].extend([inputs.ref("recent_join"), archive])
        availability(row, archive["available_at"], "git_archival_presence",
                     archive["clock_domain"], archive)
        row["report_available_at"] = old["report_available_at"]
        result.append(row)
    if len(result) != 21 or sum(all(r["features"][f] is not None for f in FEATURES) for r in result) != 5:
        raise ValueError("Recent source coverage changed")
    return result


def find_feature_record(doc: Any, assay_id: int) -> dict[str, Any]:
    if isinstance(doc, dict):
        if doc.get("assay_id", doc.get("id")) == assay_id and isinstance(doc.get("features"), dict):
            return doc
        for value in doc.values():
            try:
                return find_feature_record(value, assay_id)
            except LookupError:
                pass
    elif isinstance(doc, list):
        for value in doc:
            try:
                return find_feature_record(value, assay_id)
            except LookupError:
                pass
    raise LookupError(f"No feature record for {assay_id}")


def build_db(inputs: Inputs, metadata: dict[str, Any], current: dict[str, Any],
             frozen: dict[str, Any], labels6: dict[str, Any],
             feature7: dict[str, Any], feature182: dict[str, Any],
             label182: dict[str, Any]) -> list[dict[str, Any]]:
    meta_rows = data_rows(metadata)
    current_rows = data_rows(current)
    meta = {r["id"]: r for r in meta_rows}
    latest = {r["id"]: r for r in current_rows}
    if len(meta) != 77 or len(latest) != 76 or set(latest) != set(meta) - {197}:
        raise ValueError("Require exactly 77 metadata and authorized 76 values excluding 197")
    if max(meta) > 202 or 197 in latest:
        raise ValueError("Unauthorized new/excluded label in snapshot")
    old68 = {r["id"]: r for r in frozen["rows"]}
    old6 = {r["id"]: r for r in data_rows(labels6)}
    old182 = {r["id"]: r for r in data_rows(label182)}
    f7 = {r["assay_id"]: r for r in feature7["rows"]}
    nearby_by_id = {
        assay_id: warning
        for warning in frozen["summary"]["nearby_labels"]
        for assay_id in warning["ids"]
    }
    extra182 = find_feature_record(feature182, 182)
    when, meta_when = capture_at(current), capture_at(metadata)
    clock = current.get("clock_domain", "capture-process-clock; not synchronized with production host")
    result = []
    for assay_id, raw_meta in sorted(meta.items()):
        if assay_id != 197:
            label_row = latest[assay_id]
            if any(raw_meta.get(k) != label_row.get(k) for k in ("observed_at", "created_at", "metric", "time_basis")):
                raise ValueError(f"Metadata identity changed between captures: {assay_id}")
        else:
            label_row = None
        evidence = old68.get(assay_id) or f7.get(assay_id)
        feature_source = "frozen68" if assay_id in old68 else "new_feature7"
        if assay_id == 182:
            evidence, feature_source = extra182, "id182_features"
        features = evidence.get("features", {}) if evidence else {}
        sample_in_evidence = evidence.get("observed_at", evidence.get("sample_at")) if evidence else None
        time_match = evidence is not None and same_time(sample_in_evidence, raw_meta["observed_at"])
        if not time_match:
            features = {}
        quality = evidence.get("eligibility", evidence.get("feature_eligibility", "incomplete")) if evidence else "user_excluded"
        pairing = evidence.get("pairing", "incomplete") if evidence else "incomplete"
        row = base_record(
            "manual_db_77", assay_id, f"db:manual_measurements:{assay_id}",
            {"metadata": raw_meta, "value_snapshot_row": label_row},
            None if label_row is None else label_row["value"],
            raw_meta["observed_at"], raw_meta["observed_at"],
            features, quality, pairing,
            {**inputs.ref("current_metadata"), "assay_id": assay_id},
        )
        row.update(assay_id=assay_id, metric=raw_meta["metric"],
                   initial_created_at=raw_meta["created_at"], time_basis=raw_meta["time_basis"],
                   current_metadata_capture_at=meta_when,
                   date_status="database_timestamp_preserved_no_legacy_offset_reinterpretation")
        row["feature_provenance"] = evidence.get("evidence", {}) if time_match else {}
        row["quality"].update({
            "feature_source": feature_source if evidence else None,
            "feature_source_time_matches_current": time_match,
            "label_flags": old68.get(assay_id, {}).get("label_flags", []),
            "source_coverage_reason": evidence.get("coverage_reason") if evidence else None,
        })
        row["sample_current_vs_frozen_metadata_changes"] = []
        row["old_frozen68_membership"] = assay_id in old68
        if assay_id in old68:
            old = old68[assay_id]
            row.update(
                frozen_value=old["value"], frozen_features=old["features"],
                frozen_sample_at=old["observed_at"], frozen_eligibility=old["eligibility"],
                frozen_first_capture_at=iso(frozen["captured_at"]),
                frozen_value_version_ref={**inputs.ref("frozen68"), "assay_id": assay_id},
                value_changed_since_frozen=(label_row["value"] != old["value"]),
            )
            row["sample_current_vs_frozen_metadata_changes"] = [
                k for k in ("observed_at", "created_at", "time_basis")
                if raw_meta.get(k) != old.get(k)
            ]
        if assay_id in old6:
            row["value_changed_since_prior_6_capture"] = label_row["value"] != old6[assay_id]["value"]
        if assay_id == 197:
            row["exclusion_reasons"].append("user_confirmed_wrong_sample_time; value never read")
            row["training_cautions"].append("metadata only; neither label nor features inferred")
        else:
            row["provenance"].append({**inputs.ref("current_values"), "assay_id": assay_id})
            availability(row, when, "current_complete_value_version_snapshot", clock,
                         {**inputs.ref("current_values"), "assay_id": assay_id})
            prior = None
            if assay_id in old68:
                prior = (iso(frozen["captured_at"]), inputs.ref("frozen68"))
            elif assay_id in old6:
                prior = (capture_at(labels6), inputs.ref("new_labels6"))
            elif assay_id in old182:
                prior = (capture_at(label182), inputs.ref("id182_label"))
            if prior:
                row["prior_same_record_capture_at"] = prior[0]
                row["prior_value_capture_ref"] = prior[1]
                row["first_capture_scope"] = (
                    "current snapshot version first captured now; prior record captures are separate; "
                    "equal values do not prove an unedited version timeline"
                )
            row["training_cautions"].append("current value is not backdated to initial_created_at")
        if evidence:
            row["provenance"].append({**inputs.ref(feature_source), "assay_id": assay_id})
        if assay_id == 191:
            row["exclusion_reasons"].append("stopped_input_191_not_for_training_per_root_scope")
        if row["quality"]["label_flags"]:
            row["training_cautions"].append("source label review flags retained; pairing alone is not sample eligibility")
        if assay_id in nearby_by_id:
            warning = nearby_by_id[assay_id]
            row.update(
                duplicate_suspected=True,
                duplicate_group="frozen68-nearby:" + ",".join(map(str, sorted(warning["ids"]))),
                match_basis="frozen audit nearby-label identity warning only; not merged",
                duplicate_evidence=warning,
            )
        result.append(row)
    return result


def assign_roles(rows: list[dict[str, Any]]) -> None:
    times = defaultdict(list)
    for row in rows:
        fs = row["features"]
        bits = [fs[k] for k in SWITCHES]
        group = ",".join(str(int(v)) for v in bits) if all(v in (0, 1) for v in bits) else None
        count = int(sum(fs[k] for k in FEATURES[1:5])) if all(fs[k] in (0, 1) for k in FEATURES[1:5]) else None
        complete = all(fs[k] is not None for k in FEATURES)
        label_known = row["value"] is not None
        basic = complete and label_known and not row["exclusion_reasons"]
        row.update(six_switch_group=group, system_count=count,
                   eight_features_complete=complete, label_known=label_known,
                   low_load_flag=fs["coal_amount"] is not None and fs["coal_amount"] <= 10)
        row["group_suitability"] = {
            "three_system_structural_candidate": basic and count == 3,
            "four_system_structural_candidate": basic and count == 4,
            "two_system_independent_structural_candidate": basic and count == 2,
            "current_main_three_system_exact_group": basic and group == MAIN_GROUP,
            "final_training_selection": "defer to frozen research protocol; retain source eligibility and review flags",
            "low_load_is_not_an_automatic_global_exclusion": True,
        }
        roles = ["raw_source_registry", "not_new_independent_validation"]
        if label_known:
            roles.append("known_label_version")
        if complete:
            roles.append("eight_factor_observation")
        else:
            roles.append("missing_factors_not_imputed")
        if row["old177_membership"]:
            roles.append("frozen_baseline_training_member")
        if row["old_import_stopped_row"]:
            roles.append("old_import_low_load_row_retained")
        if row["old_frozen68_membership"]:
            roles.append("historical_development_uses_frozen_value_and_features")
        if row["eligibility"] == "label_review":
            roles.append("label_identity_or_time_requires_review_not_proven_wrong")
        if row["source_type"] == "recent_workbook_21" and complete:
            roles.append("five_approximate_augmentation_candidate")
        if count == 2:
            roles.append("two_system_independent_group_only")
        elif count == 4:
            roles.append("four_system_exact_group_only")
        elif count == 3:
            roles.append("main_three_system" if group == MAIN_GROUP else "other_three_system_exact_group_only")
        if row["exclusion_reasons"]:
            roles.append("excluded_from_training_current_scope")
        row["data_roles"] = roles
        if row["normalized_time"]:
            times[iso(row["normalized_time"])].append(row)
    for stamp, group in times.items():
        if len(group) > 1:
            name = "same-time-suspected:" + digest(stamp.encode())[:16]
            for row in group:
                row.update(duplicate_group=name, duplicate_suspected=True,
                           match_basis="same normalized timestamp only; no identity proof; not merged")


def summarize(rows: list[dict[str, Any]], inputs: Inputs) -> dict[str, Any]:
    sources = {}
    for source in ("full_workbook_185", "recent_workbook_21", "manual_db_77"):
        part = [r for r in rows if r["source_type"] == source]
        sources[source] = {
            "raw_rows": len(part), "labels_known": sum(r["label_known"] for r in part),
            "eight_features_complete": sum(r["eight_features_complete"] for r in part),
            "eligibility": dict(Counter(r["eligibility"] for r in part)),
            "system_count": dict(Counter(str(r["system_count"]) for r in part)),
            "six_switch_groups": dict(Counter(str(r["six_switch_group"]) for r in part)),
            "structural_fit_candidates_not_final_selection": sum(
                r["eight_features_complete"] and r["label_known"] and not r["exclusion_reasons"] for r in part
            ),
        }
    return {
        "schema": "expert81-source-inventory-summary-v1",
        "raw_source_rows": len(rows), "source_counts": sources,
        "raw_excluding_user_197": sum(r.get("assay_id") != 197 for r in rows),
        "known_labels": sum(r["label_known"] for r in rows),
        "old177_training_members": sum(r["old177_membership"] for r in rows),
        "old_import_stopped_fullbook_rows_retained": sum(r["old_import_stopped_row"] for r in rows),
        "labels_missing_record_ids": [r["record_id"] for r in rows if not r["label_known"]],
        "eight_features_missing_record_ids": [r["record_id"] for r in rows if not r["eight_features_complete"]],
        "training_exclusions": {r["record_id"]: r["exclusion_reasons"] for r in rows if r["exclusion_reasons"]},
        "value_changes_since_frozen68": [
            r["assay_id"] for r in rows if r["value_changed_since_frozen"] is True
        ],
        "value_changes_since_prior6": [
            r["assay_id"] for r in rows if r["value_changed_since_prior_6_capture"] is True
        ],
        "confirmed_cross_source_merges": 0,
        "physical_unique_independent_sample_count": None,
        "suspected_duplicate_rows": sum(r["duplicate_suspected"] for r in rows),
        "suspected_same_timestamp_rows": sum(
            r["match_basis"].startswith("same normalized timestamp") for r in rows
        ),
        "label_review_records": [
            {"record_id": r["record_id"], "flags": r["quality"].get("label_flags", [])}
            for r in rows if r["eligibility"] == "label_review"
        ],
        "scope": "185 full workbook + 21 recent workbook + 77 DB metadata; versions do not add rows",
        "label_scope": "current snapshot id<=202 and id<>197; 191 captured but not trained",
        "current_values_available_at": next(
            r["available_at"] for r in rows
            if r["source_type"] == "manual_db_77" and r["label_known"]
        ),
        "current_values_clock_domain": next(
            r["availability_clock_domain"] for r in rows
            if r["source_type"] == "manual_db_77" and r["label_known"]
        ),
        "source_registry": inputs.registry,
    }


def validate(rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    if len(rows) != 283 or len({r["record_id"] for r in rows}) != 283:
        raise ValueError("Inventory must retain exactly 283 distinct source identities")
    if [summary["source_counts"][k]["raw_rows"] for k in (
        "full_workbook_185", "recent_workbook_21", "manual_db_77",
    )] != [185, 21, 77]:
        raise ValueError("Wrong source partition")
    excluded = next(r for r in rows if r.get("assay_id") == 197)
    if excluded["value"] is not None or any(v is not None for v in excluded["features"].values()):
        raise ValueError("197 label/features must remain unread/None")
    if summary["known_labels"] != 282 or summary["old177_training_members"] != 177:
        raise ValueError("Known-value or baseline membership count mismatch")
    full_low = [r for r in rows if r["old_import_stopped_row"]]
    if len(full_low) != 8 or any(r["exclusion_reasons"] for r in full_low):
        raise ValueError("Old eight low-load rows must not be automatically deleted")
    for row in rows:
        if tuple(row["features"]) != FEATURES:
            raise ValueError("Feature schema changed")
        if any(finite(v) is None for v in row["features"].values() if v is not None):
            raise ValueError("Nonfinite factor")
        if not row["canonical_record_id"] == row["record_id"]:
            raise ValueError("No identity-based cross-source deduplication was authorized")


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(plain(value), ensure_ascii=False, indent=2,
                               sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def write_outputs(root: Path, out: Path, rows: list[dict[str, Any]],
                  summary: dict[str, Any], inputs: Inputs) -> dict[str, Any]:
    allowed = (root / OUT_REL).resolve()
    if out.resolve() != allowed:
        raise ValueError("Output directory must be the owned source_inventory path")
    out.mkdir(parents=True, exist_ok=True)
    jsonl = out / "inventory_rows.jsonl"
    jsonl.write_bytes(b"".join(canonical(row) + b"\n" for row in rows))
    columns = [
        "record_id", "source_type", "source_row", "normalized_time", "value",
        *FEATURES, "six_switch_group", "system_count", "feature_quality", "eligibility",
        "date_status", "time_inference_flags", "available_at",
        "availability_evidence_kind", "availability_clock_domain",
        "old177_membership", "old_import_stopped_row", "old_frozen68_membership",
        "frozen_value", "value_changed_since_frozen", "low_load_flag",
        "exclusion_reasons", "duplicate_group", "canonical_record_id", "match_basis",
        "raw_record_hash",
    ]
    with (out / "inventory_rows.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            flat = {k: row.get(k) for k in columns}
            flat.update(row["features"])
            for key in ("time_inference_flags", "exclusion_reasons"):
                flat[key] = json.dumps(flat[key], ensure_ascii=False)
            writer.writerow(flat)
    write_json(out / "inventory_summary.json", summary)
    manifest = {
        "schema": "expert81-source-inventory-manifest-v1",
        "script": {"path": "backend/scripts/audit_expert81_sources.py",
                   "sha256": digest(Path(__file__).read_bytes())},
        "raw_rows": 283, "raw_excluding_197": 282,
        "sources": inputs.registry,
        "outputs": {
            name: {"path": (out / name).relative_to(root).as_posix(),
                   "sha256": digest((out / name).read_bytes())}
            for name in ("inventory_rows.jsonl", "inventory_rows.csv", "inventory_summary.json")
        },
        "validation": "283 source identities, 282 known labels, no imputation/deduplication/model fit",
    }
    write_json(out / "inventory_manifest.json", manifest)
    return manifest


def write_report(root: Path, summary: dict[str, Any], manifest: dict[str, Any]) -> None:
    source = summary["source_counts"]
    tick = chr(96)
    lines = [
        "# 粗精煤泥统一原始来源清单（2026-11-02）",
        "",
        "本轮仅建清单和数据角色，不拟合、不激活模型、不访问生产；来源读取由本地脚本完成，生产读取由指定生产代理执行。",
        "",
        "原始来源行数283＝全汇总表185＋近期Excel21＋主机77。用户排除ID197后282只是来源记录数，不是独立去重数或合格训练数；旧68/新6等版本不重复加行。",
        "",
        "|来源|原记录|当前已知标签|8因子完整|原资格分类|",
        "|---|---:|---:|---:|---|",
    ]
    for key, title in (("full_workbook_185", "全汇总185"), ("recent_workbook_21", "近期21"), ("manual_db_77", "主机77")):
        item = source[key]
        lines.append(f"|{title}|{item['raw_rows']}|{item['labels_known']}|{item['eight_features_complete']}|{json.dumps(item['eligibility'], ensure_ascii=False)}|")
    lines += [
        "",
        "|来源|三系统|四系统|二系统|系统数未知|当前主组六开关精确一致|",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for key, title in (("full_workbook_185", "全汇总185"), ("recent_workbook_21", "近期21"), ("manual_db_77", "主机77")):
        item = source[key]
        counts = item["system_count"]
        lines.append(f"|{title}|{counts.get('3', 0)}|{counts.get('4', 0)}|{counts.get('2', 0)}|{counts.get('None', 0)}|{item['six_switch_groups'].get(MAIN_GROUP, 0)}|")
    lines += [
        "",
        "全表三分表汇总只计一次。185行含冻结EXPERT8训练177行及旧导入coal_amount<=10的8行；8行全部保留，旧标记不是扩展自动删样规则，也不是现场物理停产证明。当前主三系统按0,1,1,1,0,0精确组合识别；其他三系统、四系统及二系统单列，不能只按系统数迁移系数。",
        "",
        "近期原Excel21行与既有joined的源SHA一致；5行八因子完整但均为分钟近似、0行strict，其余16行因子留None。不插值、不按相同灰分补因子。10.1到10/10沿用已存行序推断；午夜次日保留原join政策和00:18用户确认标志，其他推断不伪称源端复核。",
        "",
        "生产范围更新：主代理先批准182补录，随后明确替代为一次coarse_slime_ash、id<=202且id<>197的当前76值快照。77行metadata保留197，197的灰分和因子均None且用户排除；191当前值获准保留，但stopped输入资格拒绝训练。未读>202未来标签。当前值版本只能从本次完整捕获起有证，不按原created_at回填。",
        f"当前76值完整捕获时刻：{summary['current_values_available_at']}；时钟域：{summary['current_values_clock_domain']}。77条metadata快照为北京时间14:11:59.771；这些capture时刻不同，不能将metadata读取当作当时已经捕获value。",
        "",
        "旧68冻结标签/因子、既有6首次完整捕获及182首次捕获分别保留引用；当前76值与旧版本比较另记。相同值/metadata不证明没有中间编辑。历史development继续使用旧68冻结版本及原代理规则，不能用当前版本替代旧日。",
        "旧68中7条label_review是待核，不是已证明错误：原审计包括相邻同标签/冲突标签身份疑点及一条采样时间晚于初录时间。原flag并非模型残差筛选；其记录和值全保留，不能只看pairing为strict就把它们当strict样本。扩大试验按冻结协议处理这7条，不从原始清单删除。",
        "具体旧警报：40/37相差12秒且同标签，38/39相差5.537秒且同标签，52/51相差28秒且标签冲突；6行标为疑似身份重复/冲突但不合并。156为采样晚于初录的时间待核。这里只沿用原审计警报，不新增相近时间删样阈值。",
        "",
        "可用时刻证据：",
        "",
        f"- 旧185值/因子在Git commit {tick}{LEGACY_COMMIT}{tick}（2026-10-12 11:17:45+08:00）完整入档：177 rows＋8 stopped。Git blob原字节SHA为00422c6fc73ecd55743659101bb66c887e725b4598671299fe742e6dbfea582d；当前模型原字节SHA为c720efc10952802f42e3ec4261ab3b13966aefd89b34f7a21f7906c13f4362a0，两者JSON语义相同，不能混称同一字节哈希。",
        f"- 近期21及5完整近似因子在Git commit {tick}{RECENT_COMMIT}{tick}（2026-10-13 14:50:06+08:00）已入档，blob SHA为689a6e041602e84d953dc8ab31023f63da28bf356ba703115cca2e002be4ba5a；其JSON语义与当前joined相同。",
        "- Git归档时刻用于已有版本存在的证据，不是实际化验报告时刻，也不是主机/现场时钟已同步。主机当前标签available_at用完整capture时刻及其clock_domain；本任务不校时。",
        "",
        "清单字段包含稳定source_key/record_id、原始/规范时间及推断标志、标签与8因子、原资格与逐因子证据、可用版本/时钟域、旧177训练标记、旧68版本、系统组合/适用角色、排除原因和疑似重复标志。只因相同灰分或相近时间不合并；每条canonical_record_id保持自己的来源身份，物理独立样本数仍未知。",
        "",
        f"当前与旧68比较的值改变ID：{summary['value_changes_since_frozen68']}；与既有6比较的值改变ID：{summary['value_changes_since_prior6']}。结论仅针对两份捕获的值，不声称未发生中间修订。",
        "",
        "[逐行JSONL](../../output/gpt-expert81-expanded-20261102/source_inventory/inventory_rows.jsonl)、[CSV](../../output/gpt-expert81-expanded-20261102/source_inventory/inventory_rows.csv)、[汇总](../../output/gpt-expert81-expanded-20261102/source_inventory/inventory_summary.json)、[冻结清单](../../output/gpt-expert81-expanded-20261102/source_inventory/inventory_manifest.json)。输入原字节SHA、Git证据与输出SHA均在manifest。",
        "",
        "本轮角色为训练来源/已见诊断或缺口登记，未分配新的独立盲测集。最终模型样本选择须按冻结研究协议、原eligibility与确切工况执行；283/282不可直接作为拟合或验收分母。",
        "",
    ]
    (root / REPORT_REL).write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--full-workbook", type=Path, default=SOURCE_DIR / "粗精煤泥灰分影响因素(全).xlsx")
    parser.add_argument("--recent-workbook", type=Path, default=SOURCE_DIR / "近期粗精煤泥灰分.xlsx")
    parser.add_argument("--current-values", type=Path)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    inputs = Inputs(root)
    full = inputs.workbook("full_workbook", args.full_workbook)
    recent = inputs.workbook("recent_workbook", args.recent_workbook)
    model = inputs.json("frozen_model", root / MODEL_REL)
    joined = inputs.json("recent_join", root / JOIN_REL)
    inputs.git_json("git_legacy_model", LEGACY_COMMIT, MODEL_REL, model)
    inputs.git_json("git_recent_join", RECENT_COMMIT, JOIN_REL, joined)
    frozen = inputs.json("frozen68", root / "output/gpt-factor-phase1-20261102/input_audit.json")
    labels6 = inputs.json("new_labels6", root / "output/gpt-expert81-holdout-20261102/labels-first-capture.json")
    feature7 = inputs.json("new_feature7", root / "output/gpt-expert81-holdout-20261102/feature-evidence.json")
    metadata = inputs.json("current_metadata", root / ADDENDUM / "live-manual-coarse-metadata-20261102T141159.json")
    current = inputs.json("current_values", args.current_values or root / ADDENDUM / "current76-values-snapshot.json")
    feature182 = inputs.json("id182_features", root / ADDENDUM / "id182-feature-evidence.json")
    label182 = inputs.json("id182_label", root / ADDENDUM / "id182-label-first-capture.json")
    rows = (
        build_full(inputs, full, model)
        + build_recent(inputs, recent, joined)
        + build_db(inputs, metadata, current, frozen, labels6, feature7, feature182, label182)
    )
    producer_path = root / ADDENDUM / "db-current-source-rows.jsonl"
    aligned_bytes = inputs.file("producer_aligned_db77", producer_path)
    aligned = {
        r["id"]: r for r in (
            json.loads(line) for line in aligned_bytes.decode("utf-8-sig").splitlines() if line
        )
    }
    if len(aligned) != 77:
        raise ValueError("Producer aligned metadata count changed")
    for row in rows:
        if row["source_type"] != "manual_db_77":
            continue
        producer = aligned[row["assay_id"]]
        if (row["value"] != producer["label"]["value"]
                or row["features"] != producer["features"]
                or row["eligibility"] != producer["eligibility"]):
            raise ValueError(f"Independent DB join differs from producer: {row['assay_id']}")
    assign_roles(rows)
    summary = summarize(rows, inputs)
    validate(rows, summary)
    inputs.unchanged()
    if not args.check_only:
        manifest = write_outputs(root, root / OUT_REL, rows, summary, inputs)
        write_report(root, summary, manifest)
        inputs.unchanged()
    print(json.dumps({
        "raw_rows": summary["raw_source_rows"],
        "raw_excluding_197": summary["raw_excluding_user_197"],
        "known_labels": summary["known_labels"],
        "sources": summary["source_counts"],
        "value_changes_since_frozen68": summary["value_changes_since_frozen68"],
        "check_only": args.check_only,
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
