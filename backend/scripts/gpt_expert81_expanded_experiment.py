"""Fixed expanded-source EXPERT8.1 matrix; local immutable inputs only."""
from __future__ import annotations

import argparse
from collections import Counter,defaultdict
from copy import deepcopy
from datetime import timedelta,timezone
from functools import cmp_to_key
import csv
import hashlib
import json
from pathlib import Path
import sys

BACKEND=Path(__file__).resolve().parents[1]
ROOT=BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0,str(BACKEND))
from app.gpt_coarse.expert8 import predict as expert_predict
from app.gpt_coarse.experiments.expert81_expanded import (
    BJ,CANDIDATES,DEFAULT_CONFIG,FEATURES,available_rows,digest,fit_state,fit_two_system,
    group_of,inside,linear_raw,parse_time,pools,predict_state,stamp,
)
from scripts.gpt_expert81_experiment import score,paired_bootstrap

PROTOCOL_PATH=ROOT/"docs/verification/gpt-expert81-expanded-20261102.protocol.json"
LOCKED_PROTOCOL_SHA="4ef95f65c304b9f7ecc096b96658d262406ce19b539577c3c9515ca5da0f7842"
GROUP="0,1,1,1,0,0"


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path,value):
    path=Path(path)
    if path.exists():
        raise ValueError(f"refusing to overwrite expanded output: {path}")
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")


def checked_json(path,expected=None):
    if expected is not None and file_hash(path)!=expected:
        raise ValueError(f"immutable input hash mismatch: {path}")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def verify_manifest(path,expected,*,table_key):
    manifest=checked_json(path,expected)
    records={str(Path(path).relative_to(ROOT)):file_hash(path)}
    for name,spec in manifest[table_key].items():
        if isinstance(spec,str):
            target=Path(path).parent/name;wanted=spec
        else:
            target=ROOT/spec["path"] if "path" in spec else Path(path).parent/name
            wanted=spec["sha256"]
        if file_hash(target)!=wanted:
            raise ValueError(f"manifest member changed: {target}")
        records[str(target.relative_to(ROOT))]=wanted
    return records


def verify_inputs(protocol):
    inputs=protocol["input_manifests"]
    hashes={}
    for path_key,hash_key,table in [("source_inventory_path","source_inventory_sha256","outputs"),
                                  ("production_addendum_manifest_path","production_addendum_manifest_sha256","files"),
                                  ("phase1_manifest_path","phase1_manifest_sha256","files"),
                                  ("first_round_manifest_path","first_round_manifest_sha256","files")]:
        hashes.update(verify_manifest(ROOT/inputs[path_key],inputs[hash_key],table_key=table))
    immutable={"backend/app/gpt_coarse/experiments/expert81.py":inputs["first_round_selection_core_sha256"],
               "output/gpt-expert81-20261102/candidate.json":inputs["first_round_candidate_file_sha256"],
               "output/gpt-expert81-holdout-20261102/prediction-lock.json":inputs["prior_holdout_prediction_sha256"],
               "output/gpt-expert81-holdout-20261102/holdout-score.json":inputs["prior_holdout_score_sha256"],
               "backend/models/gpt_expert8/20261012/model.json":protocol["source_roles"]["full_workbook"]["working_tree_model_sha256"],
               "backend/data/gpt_coarse/recent_assays/20261013/joined.json":protocol["source_roles"]["recent_workbook"]["working_tree_joined_sha256"],
               "backend/app/gpt_coarse/expert8.py":"fbb9a31196721f98af6bd961f329cb5abc9f26ae1a77a9af828c0e24b038f17d",
               "backend/scripts/gpt_expert81_experiment.py":"795f5a3489ddd3ed72b9a054eae36c0d0388db6bf61b7648eef3b8403a98de9e"}
    for name,wanted in immutable.items():
        if file_hash(ROOT/name)!=wanted:
            raise ValueError(f"protected input/source changed: {name}")
        hashes[name]=wanted
    return hashes


def normalize_inventory(inventory,mode,frozen_model,receipt_by_id):
    """Project versions into one declared information budget, without imputation."""
    events=[]
    for raw in inventory:
        source_type=raw["source_type"]
        if source_type=="manual_db_77":
            if raw["assay_id"]==197:
                continue
            domain="current"
        elif source_type=="full_workbook_185":
            domain="legacy"
        elif source_type=="recent_workbook_21":
            domain="recent_excel"
        else:
            raise ValueError("unknown inventory source type")
        identity=str(raw["canonical_record_id"])
        values=raw["features"]
        quality="legacy_complete" if domain=="legacy" else raw["eligibility"]
        base={"record_id":identity,"assay_id":raw.get("assay_id"),"source":source_type,"domain":domain,
              "quality":quality,"sample_at":raw["sample_at"],"available_at":raw["available_at"],
              "version_id":raw["raw_record_hash"],"ash":raw["value"],"features":values,
              "group_key":raw["six_switch_group"],"system_count":raw["system_count"],
              "old177":bool(raw["old177_membership"]),
              "provenance":{"kind":raw["availability_evidence_kind"],"record_id":raw["record_id"],
                            "record_hash":raw["raw_record_hash"],"source_type":source_type,
                            "availability_clock_domain":raw["availability_clock_domain"],
                            "availability_evidence":raw["availability_evidence"]}}
        if domain!="current":
            events.append(base)
            continue
        if raw["value"] is None or raw["available_at"] is None:
            raise ValueError("nonexcluded current row lacks captured value/version availability")
        if quality in ("strict","minute_approximation"):
            base["frozen_prediction"]=expert_predict(values,frozen_model)
        events.append(base)
        if not raw["old_frozen68_membership"]:
            continue
        historical=deepcopy(base)
        historical.update(quality=raw["frozen_eligibility"],ash=raw["frozen_value"],features=raw["frozen_features"],
                          sample_at=raw["frozen_sample_at"],available_at=raw["initial_created_at"] if mode=="historical_entry_proxy" else raw["frozen_first_capture_at"])
        if historical["features"] and all(historical["features"].get(k) is not None for k in FEATURES):
            historical["group_key"]=group_of(historical["features"])
            historical["system_count"]=int(sum(historical["features"][k] for k in FEATURES[2:6]))
            historical["frozen_prediction"]=expert_predict(historical["features"],frozen_model)
        else:
            historical.update(group_key=None,system_count=None)
        historical["version_id"]=f"frozen68:{raw['assay_id']}:{mode}"
        historical["provenance"]={"kind":"original_frozen68_created_proxy" if mode=="historical_entry_proxy" else "original_frozen68_snapshot",
                                  "source_ref":raw["frozen_value_version_ref"],"initial_created_at":raw["initial_created_at"],
                                  "historical_revisions_not_reconstructed":True}
        if mode=="historical_entry_proxy" and raw["assay_id"] in receipt_by_id:
            historical["c5_receipt"]=deepcopy(receipt_by_id[raw["assay_id"]])
        events.append(historical)
    return events


def record(row,method,result,phase,normal):
    value=result["prediction"];raw=result.get("raw_prediction",value);actual=row["ash"]
    return {"phase":phase,"assay_id":row.get("assay_id"),"record_id":row["record_id"],
            "sample_at":row["sample_at"],"date":parse_time(row["sample_at"]).astimezone(BJ).date().isoformat(),
            "method":method,"actual":actual,"normal":normal,"feature_quality":row["quality"],
            "prediction":value,"raw_prediction":raw,"status":result.get("status","ok"),"reason":result.get("reason"),
            "state_hash":result.get("state_hash"),"outside_current_ranges":result.get("outside_current_ranges",[]),
            "error":None if value is None else value-actual,"abs_error":None if value is None else abs(value-actual),
            "raw_error":None if raw is None else raw-actual,"raw_abs_error":None if raw is None else abs(raw-actual)}


def evaluate_target(events,target,phase,mode,states,frozen_model):
    at=parse_time(target["sample_at"]);group=target["group_key"];cfg={"mode":mode}
    fits={method:fit_state(events,at,method,group_key=group,exclude_record_id=target["record_id"],config=cfg) for method in CANDIDATES}
    for state in fits.values():
        states[state["state_hash"]]=state
    bounds=fits["E0"]["fence"];normal=inside(target["ash"],bounds)
    output=[record(target,method,predict_state(state,target["features"]),phase,normal) for method,state in fits.items()]
    own=pools(available_rows(events,at,exclude_record_id=target["record_id"]),group,{**deepcopy(DEFAULT_CONFIG),"mode":mode})["current_raw"]
    last=own[-1]["ash"] if own else None
    for method,value in [("frozen_expert8",expert_predict(target["features"],frozen_model)),
                         ("empirical_raw",30.5-.3*target["features"]["level"]),("last_assay",last)]:
        output.append(record(target,method,{"prediction":value,"raw_prediction":value,"status":"ok" if value is not None else "unavailable","reason":None if value is not None else "no_prior_current_assay"},phase,normal))
    return output


def choose(predictions,protocol):
    own=[r for r in predictions if r["phase"]=="main_development"]
    ids=protocol["evaluation"]["main_development_ids"]
    ranking=[]
    for method in CANDIDATES:
        rows=[r for r in own if r["method"]==method]
        if sorted(r["assay_id"] for r in rows if r["prediction"] is not None)!=sorted(ids):
            continue
        normal=score([r for r in rows if r["normal"]]);all_score=score(rows)
        if normal["MAE"] is not None:
            ranking.append({"candidate_id":method,"normal_sample_MAE":normal["MAE"],"all_sample_MAE":all_score["MAE"],
                            "normal_day_MAE":normal["date_balanced_MAE"],"complexity":protocol["candidates"][method]["complexity"],
                            "n":all_score["n"],"normal_n":normal["n"]})
    def compare(a,b):
        for key in ("normal_sample_MAE","all_sample_MAE","normal_day_MAE"):
            d=a[key]-b[key]
            if abs(d)>1e-9:return -1 if d<0 else 1
        return a["complexity"]-b["complexity"]
    ranking.sort(key=cmp_to_key(compare))
    if not ranking:raise ValueError("no expanded candidate has required development coverage")
    return {"candidate_id":ranking[0]["candidate_id"],"ranking":ranking,"selection_ids":ids,"scope":"declared main development only"}


def summarize(predictions,selected):
    output={}
    for phase in sorted({r["phase"] for r in predictions}):
        rows=[r for r in predictions if r["phase"]==phase];methods=sorted({r["method"] for r in rows})
        own={"methods":{},"per_date":{}}
        for method in methods:
            records=[r for r in rows if r["method"]==method]
            own["methods"][method]={"eligible_n":len(records),"predicted_n":sum(r["prediction"] is not None for r in records),
                                    "rejected_ids":[r["assay_id"] for r in records if r["prediction"] is None],
                                    "all":score(records),"normal":score([r for r in records if r["normal"]]),
                                    "raw_all":score(records,raw=True),"raw_normal":score([r for r in records if r["normal"]],raw=True)}
        for day in sorted({r["date"] for r in rows}):
            own["per_date"][day]={method:score([r for r in rows if r["method"]==method and r["date"]==day]) for method in methods}
        own["paired_differences"]={subset:{ref:paired_bootstrap(rows,selected,ref,subset=subset)
                                               for ref in ("E0","E1","E2","frozen_expert8","last_assay") if ref in methods}
                                   for subset in ("all","normal")}
        output[phase]=own
    return output


def independent_two_system(events,targets,formal_events,formal_at,states):
    predictions=[];final={}
    groups=sorted({r["group_key"] for r in targets if r["system_count"]==2 and r["quality"]=="strict"})
    for group in groups:
        own=[r for r in targets if r["group_key"]==group and r["quality"]=="strict"]
        for target in own:
            for method in ("T0","T1"):
                state=fit_two_system(events,target["sample_at"],group_key=group,method=method,exclude_record_id=target["record_id"])
                states[state["state_hash"]]=state
                prediction=record(target,method,predict_state(state,target["features"]),f"two_system:{group}",inside(target["ash"],state["fence"]))
                predictions.append(prediction)
            value=14. if target["features"]["level"]>=30 else 15.
            predictions.append(record(target,"existing_two_system_lookup",{"prediction":value,"raw_prediction":value},f"two_system:{group}",False))
        final[group]={}
        for method in ("T0","T1"):
            state=fit_two_system(formal_events,formal_at,group_key=group,method=method,config={"mode":"formal"})
            states[state["state_hash"]]=state;final[group][method]=state
    return predictions,final


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=ROOT/"output/gpt-expert81-expanded-20261102")
    args=parser.parse_args(argv);out=args.output
    if file_hash(PROTOCOL_PATH)!=LOCKED_PROTOCOL_SHA:raise ValueError("expanded protocol is not locked")
    protocol=checked_json(PROTOCOL_PATH)
    if protocol["status"]!="locked before expanded fitting":raise ValueError("protocol still draft")
    checked=verify_inputs(protocol)
    if (out/"result.json").exists() or (out/"candidate.json").exists():raise ValueError("refusing to overwrite a completed expanded matrix")
    inventory_path=(ROOT/protocol["input_manifests"]["source_inventory_path"]).parent/"inventory_rows.jsonl"
    inventory=[json.loads(line) for line in inventory_path.read_text(encoding="utf-8").splitlines()]
    if len(inventory)!=283 or len({r["canonical_record_id"] for r in inventory})!=283:
        raise ValueError("inventory/dedup identities differ from the locked283-row source roles")
    frozen=checked_json(ROOT/"backend/models/gpt_expert8/20261012/model.json")["model"]
    old_receipts=checked_json(ROOT/"output/gpt-expert81-20261102/exploratory_receipts.json")["historical_entry_proxy"]["receipts"]
    receipt_by_id={r["assay_id"]:{k:v for k,v in r.items() if k not in ("assay_id","label_version_id","label_available_at")}
                   for r in old_receipts}
    historical=normalize_inventory(inventory,"historical_entry_proxy",frozen,receipt_by_id)
    snapshots=normalize_inventory(inventory,"snapshot_diagnostic",frozen,{})
    formal=normalize_inventory(inventory,"formal",frozen,{})
    historical_targets={r["assay_id"]:r for r in historical if r["domain"]=="current" and r["version_id"].startswith("frozen68:")}
    diagnostic_ids=protocol["evaluation"]["revealed_new_diagnostic_strict_ids"]+protocol["evaluation"]["revealed_new_diagnostic_approximate_ids"]
    # Targets are the exact previously frozen inputs and first-revealed labels,
    # not later revised current values or information from future captures.
    locked=checked_json(ROOT/"output/gpt-expert81-holdout-20261102/prediction-lock.json")
    scores=checked_json(ROOT/"output/gpt-expert81-holdout-20261102/holdout-score.json")
    target_actual={r["assay_id"]:r["actual"] for r in scores["records"] if r["method"]=="C7"}
    dbid={r["assay_id"]:r for r in snapshots if r["domain"]=="current" and not r["version_id"].startswith("frozen68:")}
    new_targets={}
    for entry in locked["rows"]:
        if entry["assay_id"] in diagnostic_ids:
            target=deepcopy(dbid[entry["assay_id"]]);target.update(ash=target_actual[entry["assay_id"]],features=entry["features"],sample_at=entry["sample_at"])
            new_targets[entry["assay_id"]]=target
    states={};predictions=[]
    phases={"cold_start":[11,15,35,43,47],"early_development":protocol["evaluation"]["early_development_ids"],
            "main_development":protocol["evaluation"]["main_development_ids"],"later_diagnostic":protocol["evaluation"]["later_diagnostic_ids"]}
    for phase,ids in phases.items():
        for identity in ids:predictions.extend(evaluate_target(historical,historical_targets[identity],phase,"historical_entry_proxy",states,frozen))
    for phase,ids in [("revealed_strict_diagnostic",protocol["evaluation"]["revealed_new_diagnostic_strict_ids"]),
                      ("revealed_approximate_diagnostic",protocol["evaluation"]["revealed_new_diagnostic_approximate_ids"])]:
        for identity in ids:predictions.extend(evaluate_target(snapshots,new_targets[identity],phase,"snapshot_diagnostic",states,frozen))
    selection=choose(predictions,protocol);selected=selection["candidate_id"]
    formal_at=parse_time(protocol["source_roles"]["database"]["current_capture_at"])+timedelta(microseconds=1)
    final_states={method:fit_state(formal,formal_at,method,group_key=GROUP,config={"mode":"formal"}) for method in CANDIDATES}
    for state in final_states.values():states[state["state_hash"]]=state
    current_now=[r for r in available_rows(formal,formal_at) if r["domain"]=="current" and r["quality"]=="strict" and r["group_key"]==GROUP]
    descriptive=[]
    for target in current_now:
        for method,state in final_states.items():
            descriptive.append(record(target,method,predict_state(state,target["features"]),"current_descriptive_in_sample",inside(target["ash"],state["fence"])))
    two_predictions,two_states=independent_two_system(historical,list(historical_targets.values()),formal,formal_at,states)
    predictions.extend(two_predictions)
    source_count={source:sum(r["source_type"]==source for r in inventory) for source in sorted({r["source_type"] for r in inventory})}
    model={"schema":"expert81-expanded-offline-candidate-v1","model_family":"EXPERT8.1-expanded","status":"offline_not_activated",
           "candidate_id":selected,"state":final_states[selected],"fixed_candidate_states":final_states,"two_system_states":two_states,
           "protocol_sha256":LOCKED_PROTOCOL_SHA,"input_inventory_sha256":protocol["input_manifests"]["source_inventory_sha256"],
           "training_events":formal,"selection":selection,"source_counts_raw":source_count,
           "limitations":["no independent new test remains in this round","exposed tail and new-six scores are diagnostics only",
                          "same-group current support required; source-switch confounding persists","two-system groups are separate low-support references"]}
    model["artifact_hash"]=digest(model)
    results={"schema":"expert81-expanded-results-v1","protocol_sha256":LOCKED_PROTOCOL_SHA,"selection":selection,
             "summary":summarize(predictions,selected),"descriptive_summary":summarize(descriptive,selected),
             "predictions":predictions,"descriptive_predictions":descriptive,"raw_source_counts":source_count,
             "canonical_source_rows":len({r["canonical_record_id"] for r in inventory}),"normalized_version_events":len(formal),
             "formal_main_raw_current":len(final_states[selected]["current_raw_ids"]),
             "formal_main_normal_current":len(final_states[selected]["current_normal_ids"]),
             "formal_main_legacy_raw":len(final_states[selected]["legacy_raw_ids"]),
             "formal_main_legacy_normal":len(final_states[selected]["legacy_normal_ids"])}
    out.mkdir(parents=True,exist_ok=True)
    write_json(out/"result.json",results);write_json(out/"candidate.json",model);write_json(out/"states.json",states)
    write_json(out/"normalized_roles.json",{"historical_entry_proxy":historical,"snapshot_diagnostic":snapshots,"formal":formal,
                                          "note":"additional frozen/current versions are not additional raw source cases"})
    keys=["phase","assay_id","record_id","date","method","actual","normal","prediction","raw_prediction","error","abs_error","raw_error","raw_abs_error","status","reason","state_hash"]
    with (out/"predictions.csv").open("w",encoding="utf-8-sig",newline="") as stream:
        writer=csv.DictWriter(stream,keys,extrasaction="ignore");writer.writeheader();writer.writerows(predictions)
    if checked!=verify_inputs(protocol):raise ValueError("immutable inputs changed during expanded fitting")
    sources=[Path(__file__),ROOT/"backend/app/gpt_coarse/experiments/expert81_expanded.py",ROOT/"backend/tests/test_gpt_expert81_expanded.py",PROTOCOL_PATH]
    snapshot={}
    for source in sources:
        dest=out/"source_snapshot"/source.relative_to(ROOT);dest.parent.mkdir(parents=True,exist_ok=True)
        if dest.exists():raise ValueError("expanded source snapshot already exists")
        dest.write_bytes(source.read_bytes());snapshot[str(source.relative_to(ROOT))]=file_hash(dest)
    manifest={"schema":"expert81-expanded-manifest-v1","protocol_sha256":LOCKED_PROTOCOL_SHA,"inputs":checked,
              "source_files":snapshot,"files":{f.name:{"bytes":f.stat().st_size,"sha256":file_hash(f)} for f in sorted(out.iterdir()) if f.is_file()}}
    write_json(out/"manifest.json",manifest)
    print(json.dumps({"selected":selected,"ranking":selection["ranking"],"manifest_sha256":file_hash(out/"manifest.json"),
                      "candidate_sha256":file_hash(out/"candidate.json"),"candidate_internal_hash":model["artifact_hash"]},ensure_ascii=False))


if __name__=="__main__":main()
