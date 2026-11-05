"""Actual prepare->predict->reveal records file->copied score CLI, synthetic only."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


def test_actual_copied_score_consumes_reveal_records_file(tmp_path):
    tests=Path(__file__).resolve().parent
    spec=importlib.util.spec_from_file_location("score_file_chain_helpers",tests/"test_gpt_db_primary_future_capture.py")
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    case=helper.case.__wrapped__(tmp_path)
    freeze=helper.install_production_runtime(case)
    backend=tests.parent
    (freeze/"score.py").write_bytes((backend/"scripts/score_gpt_expert81_frozen.py").read_bytes())
    helper.save(freeze/"model.json",{"3":{"fence":{"lower":6.,"upper":17.}}})
    manifest=helper.capture.read(freeze/"freeze-manifest.json")
    manifest["files"]["model.json"]={"sha256":helper.capture.sha(freeze/"model.json"),"bytes":(freeze/"model.json").stat().st_size}
    helper.save(freeze/"freeze-manifest.json",manifest)
    runtime=helper.capture.read(freeze/"sealed-runtime-manifest.json")
    runtime["files"]["score.py"]={"sha256":helper.capture.sha(freeze/"score.py"),"bytes":(freeze/"score.py").stat().st_size}
    helper.save(freeze/"sealed-runtime-manifest.json",runtime)
    seal=helper.capture.read(freeze/"final-seal.json")
    seal.update(freeze_manifest_sha256=helper.capture.sha(freeze/"freeze-manifest.json"),sealed_runtime_manifest_sha256=helper.capture.sha(freeze/"sealed-runtime-manifest.json"))
    helper.save(freeze/"final-seal.json",seal)
    collection,_=helper.prepare(case)
    lock=helper.final_prediction_lock(case,collection)
    helper.capture.reveal_labels(freeze,case["contract_path"],collection,lock,tmp_path/"labels",execute=True,now=case["now"],transport_factory=lambda:helper.FakeTransport(case))
    labels=tmp_path/"labels/labels.json"
    assert "records" in json.loads(labels.read_text(encoding="utf-8"))
    output=tmp_path/"actual-file-score.json"
    process=subprocess.run([sys.executable,str(freeze/"score.py"),"--freeze",str(freeze),"--prediction-lock",str(lock),"--labels",str(labels),"--output",str(output)],capture_output=True,text=True,timeout=40)
    assert process.returncode==0,process.stdout+process.stderr
    scored=json.loads(output.read_text(encoding="utf-8"))
    assert scored["fits_performed"]==scored["new_predictions_performed"]==0
    assert scored["summary"]["combined"]["all"]["predicted_n"]==1
    assert scored["current_summary"]["all"]["predicted_n"]==1
    assert scored["current_baseline_id"]=="EX8_PRODUCTION_RUNS"
