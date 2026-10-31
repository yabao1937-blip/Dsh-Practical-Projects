"""Run the predeclared normal-sample study locally; no database or model registry writes."""
from __future__ import annotations
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import numpy
import scipy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.gpt_coarse.experiments.normal_ash import run


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--audit", type=Path, default=ROOT / "output/gpt-database-retrain-20261031/audit.json")
    parser.add_argument("--protocol", type=Path, default=ROOT / "docs/verification/gpt-normal-ash-20261031.protocol.json")
    parser.add_argument("--output", type=Path, default=ROOT / "output/gpt-normal-ash-20261031")
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Output exists; choose another directory to preserve first scoring evidence")
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    frozen_path = ROOT / "backend/models/gpt_expert8/20261012/model.json"
    data = frozen_path.read_bytes()
    if hashlib.sha256(data).hexdigest() != protocol["artifact_sha256"]:
        raise SystemExit("Frozen EXPERT8 artifact changed")
    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    result = run(audit, protocol, json.loads(data)["model"])
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["runtime"] = {"python": sys.version, "numpy": numpy.__version__, "scipy": scipy.__version__}
    result["source_sha256"] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in [Path(__file__), ROOT / "backend/app/gpt_coarse/experiments/normal_ash.py",
                                         ROOT / "backend/app/gpt_coarse/experiments/level_relationship.py"]}
    args.output.mkdir(parents=True)
    (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (args.output / "protocol.json").write_bytes(args.protocol.read_bytes())
    for filename, records in (("predictions.csv", result["predictions"]), ("metrics.csv", result["metrics"])):
        keys = list(dict.fromkeys(k for r in records for k in r))
        with (args.output / filename).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=keys)
            writer.writeheader()
            writer.writerows(records)
    selected = result["selected_algorithm"]
    (args.output / "candidate-offline-only.json").write_text(json.dumps({"status": "offline_only_not_activated",
        "algorithm": selected, "protocol": protocol, "fit": result["final_candidates"]["3"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "selected_algorithm": selected,
                      "prediction_records": len(result["predictions"]), "skipped_records": len(result["skipped"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
