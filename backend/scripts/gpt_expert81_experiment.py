"""Locked, laptop-only EXPERT8.1 experiment. No network or activation calls."""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import cmp_to_key
import csv
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.gpt_coarse.expert8 import predict as expert_predict
from app.gpt_coarse.experiments.expert81 import (
    BJ, CANDIDATE_IDS, DEFAULT_CONFIG, digest, fit_state, inside,
    last_observed_reference, make_c5_receipt, parse_time, predict_state, stamp,
)

LOCKED_PROTOCOL_SHA = "79afa9387be716b65a00a7cd672d8d8b7664a503a1db1cbb403407d151869374"
PROTOCOL_PATH = ROOT/"docs/verification/gpt-expert81-20261102.protocol.json"


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")


def verify_sources(protocol, root=ROOT):
    source = protocol["source"]
    manifest_path = root/source["phase1_manifest"]
    if file_hash(manifest_path) != source["phase1_manifest_sha256"]:
        raise ValueError("phase1 manifest hash mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    checked = {str(manifest_path.relative_to(root)): file_hash(manifest_path)}
    for name, spec in manifest["files"].items():
        path = manifest_path.parent/name
        if path.stat().st_size != spec["bytes"] or file_hash(path) != spec["sha256"]:
            raise ValueError(f"phase1 artifact hash mismatch: {name}")
        checked[str(path.relative_to(root))] = file_hash(path)
    for name, expected in manifest["source_scripts"].items():
        if file_hash(root/name) != expected:
            raise ValueError(f"phase1 source changed: {name}")
        checked[name] = expected
    for name, expected in [(source["frozen_expert8_model"], source["frozen_expert8_model_sha256"]),
                           ("backend/app/gpt_coarse/expert8.py", source["frozen_expert8_implementation_sha256"])]:
        if file_hash(root/name) != expected:
            raise ValueError(f"frozen EXPERT8 changed: {name}")
        checked[name] = expected
    return checked


def primary_rows(rows, protocol):
    selected = [r for r in rows if r["common_valid"] and r["eligibility"] == "strict"
                and r["fixed_factor_status"]["strict"] and r["six_switch_combination"] == protocol["cohort"]["primary_group_key"]]
    if [r["assay_id"] for r in selected] != protocol["cohort"]["primary_assay_ids"]:
        raise ValueError("primary cohort differs from locked IDs")
    return selected


def features(row):
    return {**row["fixed_features"], "level": row["levels"]["90"]["value"]}


def make_events(rows, frozen, protocol, mode):
    events = []
    for row in rows:
        values = features(row)
        estimate = expert_predict(values, frozen)
        if estimate is None:
            raise ValueError("frozen EXPERT8 rejected a primary row")
        event = {"assay_id": row["assay_id"], "version_id": f"{mode}:audit20261031:{row['assay_id']}",
                 "sample_at": row["sample_at"], "version_available_at": row["created_at"] if mode.startswith("historical_") else protocol["source"]["seed_snapshot_available_at"],
                 "value": row["ash"], "group_key": row["six_switch_combination"],
                 "features": {k: values[k] for k in ("level", "coal_amount")},
                 "frozen_prediction": estimate,
                 "provenance": {"kind": mode, "source_manifest_sha256": protocol["source"]["phase1_manifest_sha256"],
                                "initial_created_at": row["created_at"],
                                "availability_limitation": "created_at is not label-version availability" if mode.startswith("historical_") else "snapshot capture is notebook clock; no retrospective clock correction"}}
        events.append(event)
    return events


def cutoff_for(row, mode):
    sample = parse_time(row["sample_at"])
    if mode == "historical_midnight_proxy":
        return sample.astimezone(BJ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return sample


def score(records, *, raw=False):
    records = [r for r in records if r["prediction"] is not None]
    if not records:
        return {"n": 0, "hit1": 0, "hit2": 0, "MAE": None, "date_balanced_MAE": None}
    absolute = np.array([r["raw_abs_error" if raw else "abs_error"] for r in records])
    errors = np.array([r["raw_error" if raw else "error"] for r in records])
    n = len(records)
    hit1 = int(np.sum(absolute <= 1+1e-9)); hit2 = int(np.sum(absolute <= 2+1e-9))
    p = hit1/n; z = 1.96; denom = 1+z*z/n
    center = (p+z*z/(2*n))/denom
    radius = z*np.sqrt(p*(1-p)/n+z*z/(4*n*n))/denom
    dates = defaultdict(list)
    for r in records:
        dates[r["date"]].append(r["raw_abs_error" if raw else "abs_error"])
    return {"n": n, "dates": len(dates), "hit1": hit1, "hit2": hit2, "hit1_rate": p,
            "hit1_wilson95": [float(center-radius), float(center+radius)],
            "MAE": float(absolute.mean()), "date_balanced_MAE": float(np.mean([np.mean(v) for v in dates.values()])),
            "bias": float(errors.mean()), "median_absolute_error": float(np.median(absolute)),
            "p90_absolute_error": float(np.quantile(absolute, .9)), "max_absolute_error": float(absolute.max())}


def paired_bootstrap(records, candidate, reference, *, subset="all"):
    if subset == "normal":
        records = [r for r in records if r["normal"]]
    first = {r["assay_id"]: r for r in records if r["method"] == candidate and r["prediction"] is not None}
    second = {r["assay_id"]: r for r in records if r["method"] == reference and r["prediction"] is not None}
    grouped = defaultdict(list)
    for key in sorted(set(first)&set(second)):
        grouped[first[key]["date"]].append(first[key]["abs_error"]-second[key]["abs_error"])
    if not grouped:
        return {"n": 0, "dates": 0}
    arrays = list(grouped.values())
    sums = np.array([sum(x) for x in arrays]); counts = np.array([len(x) for x in arrays])
    rng = np.random.default_rng(20261102)
    selected = rng.integers(0, len(arrays), size=(10000, len(arrays)))
    draws = sums[selected].sum(axis=1)/counts[selected].sum(axis=1)
    return {"n": int(counts.sum()), "dates": len(arrays), "candidate": candidate, "reference": reference, "subset":subset,
            "candidate_minus_reference_MAE": float(sums.sum()/counts.sum()),
            "date_block_bootstrap95": np.quantile(draws, [.025,.975]).tolist(),
            "note": "descriptive for already-viewed historical development/replay"}


def summaries(predictions, selected=None):
    result = {}
    for mode in sorted({r["mode"] for r in predictions}):
        result[mode] = {}
        for phase in ("development", "later_already_viewed", "all_replay"):
            rows = [r for r in predictions if r["mode"] == mode and (phase == "all_replay" or r["phase"] == phase)]
            methods = sorted({r["method"] for r in rows})
            common = set.intersection(*[{r["assay_id"] for r in rows if r["method"] == m and r["prediction"] is not None} for m in methods]) if methods else set()
            section = {"common_ids": sorted(common), "methods": {}, "per_date": {}}
            for method in methods:
                native = [r for r in rows if r["method"] == method]
                paired = [r for r in native if r["assay_id"] in common]
                section["methods"][method] = {"eligible_n": len(native), "predicted_n": sum(r["prediction"] is not None for r in native),
                    "fallback_n": sum(bool(r["fallback"]) for r in native), "rejected_ids": [r["assay_id"] for r in native if r["prediction"] is None],
                    "native_all": score(native), "native_normal": score([r for r in native if r["normal"]]),
                    "common_all": score(paired), "common_normal": score([r for r in paired if r["normal"]]),
                    "raw_all": score(native, raw=True), "raw_normal": score([r for r in native if r["normal"]], raw=True)}
            for day in sorted({r["date"] for r in rows}):
                section["per_date"][day] = {m: {"all": score([r for r in rows if r["date"] == day and r["method"] == m]),
                    "normal": score([r for r in rows if r["date"] == day and r["method"] == m and r["normal"]])} for m in methods}
            if selected:
                section["paired_differences"] = {subset:{ref: paired_bootstrap(rows, selected, ref,subset=subset) for ref in ("frozen_expert8", "C0", "last_observed")} for subset in ("all","normal")}
            result[mode][phase] = section
    return result


def choose_candidate(predictions, protocol):
    dev = [r for r in predictions if r["mode"] == "historical_entry_proxy" and r["phase"] == "development"]
    baseline_ids = {r["assay_id"] for r in dev if r["method"] == "C0" and r["prediction"] is not None}
    ranking = []
    for candidate in CANDIDATE_IDS:
        own = [r for r in dev if r["method"] == candidate]
        predicted_ids = {r["assay_id"] for r in own if r["prediction"] is not None}
        if predicted_ids != baseline_ids or len(own) != len(baseline_ids):
            continue
        normal = score([r for r in own if r["normal"]]); all_score = score(own)
        if normal["MAE"] is None:
            continue
        ranking.append({"candidate_id": candidate, "normal_sample_MAE": normal["MAE"],
                        "all_sample_MAE": all_score["MAE"], "normal_date_balanced_MAE": normal["date_balanced_MAE"],
                        "complexity_rank": protocol["candidates"][candidate]["complexity_rank"], "development_ids": sorted(predicted_ids)})
    def compare(first, second):
        for key in ("normal_sample_MAE", "all_sample_MAE", "normal_date_balanced_MAE"):
            difference = first[key]-second[key]
            if abs(difference) > 1e-9:
                return -1 if difference < 0 else 1
        return first["complexity_rank"]-second["complexity_rank"]
    ranking.sort(key=cmp_to_key(compare))
    if not ranking:
        raise ValueError("no candidate satisfies the predeclared common-coverage gate")
    return {"selected_candidate_id": ranking[0]["candidate_id"], "ranking": ranking,
            "selection_scope": "development entry-time proxy only; not unseen or certified as-of"}


def run_history(rows, frozen, protocol):
    group = protocol["cohort"]["primary_group_key"]
    states = {}; predictions = []; receipt_ledgers = {}
    schedule = protocol["historical_experiment"]
    for mode in ("historical_entry_proxy", "historical_midnight_proxy"):
        events = make_events(rows, frozen, protocol, mode)
        config = {**deepcopy(DEFAULT_CONFIG), "receipt_mode": mode}
        receipts = []
        for row, event in zip(rows, events):
            cutoff = cutoff_for(row, mode)
            c5 = fit_state(events, cutoff, "C5", config, group_key=group, exclude_assay_id=row["assay_id"])
            states[c5["state_hash"]] = c5
            receipt = make_c5_receipt(c5, features(row), sample_at=row["sample_at"])
            if receipt:
                event["c5_receipt"] = receipt
                receipts.append({"assay_id": row["assay_id"], "label_version_id": event["version_id"],
                                 "label_available_at": event["version_available_at"], **receipt})
        receipt_ledgers[mode] = {"mode":mode, "formal_use_forbidden":True, "receipts":receipts}
        for row in rows:
            date = row["sample_local_date"]
            if not schedule["evaluation_start_local_date"] <= date < schedule["replay_end_exclusive_local_date"]:
                continue
            cutoff = cutoff_for(row, mode)
            fits = {method: fit_state(events, cutoff, method, config, group_key=group, exclude_assay_id=row["assay_id"]) for method in CANDIDATE_IDS}
            c0 = fits["C0"]
            for state in fits.values():
                states[state["state_hash"]] = state
            values = features(row)
            estimates = {method: predict_state(state, values, expert_predict(values, frozen)) for method, state in fits.items()}
            for name, value in (("frozen_expert8",expert_predict(values,frozen)), ("empirical_raw",30.5-.3*values["level"]), ("last_observed",last_observed_reference(c0))):
                estimates[name] = {"prediction":value,"raw_prediction":value,"state_hash":c0["state_hash"] if name == "last_observed" else None,"status":"ok" if value is not None else "unavailable","reason":None,"fallback":None,"outside_predictors":[]}
            for method, result in estimates.items():
                value = result["prediction"]
                record = {"mode":mode,"phase":"development" if date < schedule["selection_end_exclusive_local_date"] else "later_already_viewed",
                          "assay_id":row["assay_id"],"date":date,"sample_at":row["sample_at"],"fit_cutoff_at":stamp(cutoff),"method":method,
                          "actual":row["ash"],"normal":inside(row["ash"],c0["fence"]),"fence":c0["fence"],
                          "input_hash":digest({"assay_id":row["assay_id"],"sample_at":row["sample_at"],"features":values}),
                          **result,"error":None if value is None else value-row["ash"],"abs_error":None if value is None else abs(value-row["ash"]),
                          "raw_error":None if value is None else result["raw_prediction"]-row["ash"],
                          "raw_abs_error":None if value is None else abs(result["raw_prediction"]-row["ash"])}
                predictions.append(record)
    selection = choose_candidate(predictions, protocol)
    selected = selection["selected_candidate_id"]
    formal_events = make_events(rows, frozen, protocol, "frozen_snapshot")
    formal_cutoff = parse_time(protocol["source"]["seed_snapshot_available_at"])+timedelta(microseconds=1)
    state = fit_state(formal_events, formal_cutoff, selected, group_key=group)
    states[state["state_hash"]] = state
    artifact = {"schema":"expert81-offline-candidate-v1","model_version":"EXPERT8.1","status":"offline_candidate_not_activated",
                "candidate_id":selected,"protocol_sha256":LOCKED_PROTOCOL_SHA,"config":deepcopy(DEFAULT_CONFIG),
                "group_key":group,"seed_snapshot_available_at":protocol["source"]["seed_snapshot_available_at"],
                "seed_clock_limitation":"capture uses notebook clock, sample evidence uses host clock; no retrospective correction or certified shared-clock claim",
                "history_events":formal_events,"state":state,"formal_C7_receipt_count":0,
                "formal_receipt_rule":"no historical-proxy receipt is imported; future receipts must actually be persisted before their assay becomes available",
                "selection":selection,"limitations":["all historical training and October replay were previously viewed","new unseen-label holdout and prospective online accuracy remain unproven","unsupported two-system groups do not use three-system coefficients"]}
    artifact["artifact_hash"] = digest(artifact)
    results = {"schema":"expert81-experiment-result-v1","protocol_sha256":LOCKED_PROTOCOL_SHA,"selection":selection,
               "summary":summaries(predictions,selected),"predictions":predictions,
               "formal_artifact_state_hash":state["state_hash"],"formal_artifact_training_n":len(state["training_ids"]),
               "receipt_counts":{mode:len(v["receipts"]) for mode,v in receipt_ledgers.items()}}
    return results, artifact, states, receipt_ledgers


def write_csv(path, rows):
    keys = ["mode","phase","assay_id","date","sample_at","fit_cutoff_at","method","actual","normal","prediction","raw_prediction","error","abs_error","raw_error","raw_abs_error","status","reason","fallback","state_hash","input_hash"]
    with Path(path).open("w",encoding="utf-8-sig",newline="") as stream:
        writer=csv.DictWriter(stream,keys,extrasaction="ignore");writer.writeheader();writer.writerows(rows)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=ROOT/"output/gpt-expert81-20261102")
    args=parser.parse_args(argv)
    if file_hash(PROTOCOL_PATH) != LOCKED_PROTOCOL_SHA:
        raise ValueError("locked EXPERT8.1 protocol hash mismatch")
    protocol=json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    checked=verify_sources(protocol)
    source_dir=(ROOT/protocol["source"]["phase1_manifest"]).parent
    all_rows=json.loads((source_dir/"pairings.json").read_text(encoding="utf-8"))["rows"]
    rows=primary_rows(all_rows,protocol)
    frozen=json.loads((ROOT/protocol["source"]["frozen_expert8_model"]).read_text(encoding="utf-8"))["model"]
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError("refusing to overwrite an existing experiment output")
    results,artifact,states,receipts=run_history(rows,frozen,protocol)
    args.output.mkdir(parents=True,exist_ok=True)
    write_json(args.output/"result.json",results)
    write_json(args.output/"candidate.json",artifact)
    write_json(args.output/"states.json",states)
    write_json(args.output/"exploratory_receipts.json",receipts)
    write_json(args.output/"formal_seed_events.json",artifact["history_events"])
    write_csv(args.output/"predictions.csv",results["predictions"])
    write_json(args.output/"two_system_gaps.json",{"groups":{key:{"ids":[r["assay_id"] for r in all_rows if r["common_valid"] and r["six_switch_combination"]==key],"status":"not_fitted_do_not_borrow_three_system_parameters"} for key in sorted({r["six_switch_combination"] for r in all_rows if r["common_valid"] and r["system_count"]==2})}})
    if checked != verify_sources(protocol):
        raise ValueError("frozen sources changed during fitting")
    script_paths=[Path(__file__),ROOT/"backend/app/gpt_coarse/experiments/expert81.py",ROOT/"backend/tests/test_gpt_expert81_experiment.py"]
    manifest={"schema":"expert81-experiment-manifest-v1","protocol_sha256":LOCKED_PROTOCOL_SHA,
              "inputs":checked,"source_files":{str(p.relative_to(ROOT)):file_hash(p) for p in script_paths},
              "files":{p.name:{"bytes":p.stat().st_size,"sha256":file_hash(p)} for p in sorted(args.output.iterdir()) if p.is_file()}}
    write_json(args.output/"manifest.json",manifest)
    print(json.dumps({"output":str(args.output),"selected":results["selection"]["selected_candidate_id"],"manifest_sha256":file_hash(args.output/"manifest.json"),"source_files":manifest["source_files"]},ensure_ascii=False))


if __name__ == "__main__":
    main()
