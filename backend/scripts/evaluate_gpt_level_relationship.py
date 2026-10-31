"""Run the locked, local-only assay comparison; never loads production configuration."""
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
from app.gpt_coarse.experiments.level_relationship import run_experiment


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--audit", type=Path, default=ROOT / "output/gpt-database-retrain-20261031/audit.json")
    parser.add_argument("--protocol", type=Path, default=ROOT / "docs/verification/gpt-level-relationship-20261031.protocol.json")
    parser.add_argument("--output", type=Path, default=ROOT / "output/gpt-level-relationship-20261031")
    args = parser.parse_args()
    # Fail rather than overwrite evidence from the first tail inspection.
    if args.output.exists():
        raise SystemExit("Output already exists; use a new directory to preserve the first evaluation")
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    frozen_path = ROOT / "backend/models/gpt_expert8/20261012/model.json"
    frozen_bytes = frozen_path.read_bytes()
    if hashlib.sha256(frozen_bytes).hexdigest() != protocol["artifact_sha256"]:
        raise SystemExit("Frozen production artifact has changed")
    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    result = run_experiment(audit, protocol, json.loads(frozen_bytes)["model"])
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["artifact_sha256"] = protocol["artifact_sha256"]
    result["runtime"] = {"python": sys.version, "numpy": numpy.__version__, "scipy": scipy.__version__}
    result["source_sha256"] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in [Path(__file__), ROOT / "backend/app/gpt_coarse/experiments/level_relationship.py",
                                         ROOT / "backend/app/gpt_coarse/expert8.py"]}
    args.output.mkdir(parents=True)
    (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (args.output / "protocol.json").write_bytes(args.protocol.read_bytes())
    with (args.output / "predictions.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(result["predictions"][0]))
        writer.writeheader()
        writer.writerows(result["predictions"])
    print(json.dumps({"output": str(args.output), "dataset_sha256": result["dataset_sha256"],
                      "groups": {k: {"train_n": len(v["training_ids"]), "tail_n": len(v["tail_ids"])}
                                 for k, v in result["groups"].items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
