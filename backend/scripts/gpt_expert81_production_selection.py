"""Production-oriented reselection from saved110; no optimizer or training call."""
from __future__ import annotations

from datetime import datetime, timezone
from functools import cmp_to_key
import hashlib
import json
from pathlib import Path
from statistics import mean

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/"output/gpt-expert81-db-primary-20261103"
OUT = BASE/"production-selection"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    if path.exists(): raise ValueError("refusing to overwrite production selection evidence")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")


def bootstrap(rows, reference, *, count=10000):
    identities = {r["record_id"] for r in rows}
    other = {r["record_id"]:r for r in reference if r["record_id"] in identities}
    days = sorted({r["date"] for r in rows})
    blocks = [[abs(r["prediction"]-r["actual"])-abs(other[r["record_id"]]["prediction"]-r["actual"]) for r in rows if r["date"] == day] for day in days]
    rng = np.random.default_rng(20261103)
    values = []
    for _ in range(count):
        sampled = rng.integers(0,len(days),len(days))
        errors = [v for i in sampled for v in blocks[i]]
        values.append(mean(errors))
    difference = mean(v for block in blocks for v in block)
    return {"candidate_minus_reference_MAE": difference, "date_block_percentile95": np.quantile(values,[.025,.975]).tolist(),
            "dates": len(days), "paired_n": len(rows), "resamples": count, "seed": 20261103,
            "interpretation": "descriptive development uncertainty after model selection; not an independent significance test"}


def main():
    protocol = json.loads((OUT/"protocol.json").read_text(encoding="utf-8"))
    for name,wanted in protocol["input_files"].items():
        if sha(ROOT/name) != wanted: raise ValueError("production selection input differs: "+name)
    result = json.loads((BASE/"capacity-round2/results.json").read_text(encoding="utf-8"))
    first = json.loads((BASE/"results.json").read_text(encoding="utf-8"))
    phase = "forward_date_current_version_diagnostic"
    records = [r for r in result["combined_predictions"] if r["phase"] == phase and r["system_count"] == "3"]
    methods = sorted({r["method"] for r in records})
    if len(methods) != 110: raise ValueError("not all110 saved candidates")
    table = []
    for method in methods:
        rows = [r for r in records if r["method"] == method]
        if sorted(r["record_id"] for r in rows) != protocol["common_forward_target_record_ids"]:
            raise ValueError("candidate coverage differs on fixed48 targets")
        days = sorted({r["date"] for r in rows})
        score = result["summary"]["3"][phase][method]
        loo = result["summary"]["3"]["leave_one_date_out"][method]
        complexity = next(c["complexity"] for c in result["selections"]["3"]["ranking"] if c["method"] == method)
        table.append({"method": method, "forward_all_MAE": score["all"]["MAE"], "forward_normal_MAE": score["normal"]["MAE"],
                      "forward_hit1": score["all"]["hit1"], "forward_n": len(rows), "forward_dates": len(days),
                      "date_balanced_MAE": mean(mean(r["abs_error"] for r in rows if r["date"] == day) for day in days),
                      "LODO_all_MAE": loo["all"]["MAE"], "LODO_hit1": loo["all"]["hit1"], "LODO_n": loo["all"]["n"],
                      "complexity": complexity})
    def compare(a,b):
        for key in ("forward_all_MAE", "date_balanced_MAE", "LODO_all_MAE", "forward_normal_MAE"):
            d = a[key]-b[key]
            if abs(d) > protocol["numerical_tie_tolerance_pp"]: return -1 if d < 0 else 1
        if a["forward_hit1"] != b["forward_hit1"]: return -1 if a["forward_hit1"] > b["forward_hit1"] else 1
        if a["complexity"] != b["complexity"]: return -1 if a["complexity"] < b["complexity"] else 1
        return -1 if a["method"] < b["method"] else 1 if a["method"] > b["method"] else 0
    table.sort(key=cmp_to_key(compare)); selected = table[0]["method"]
    selected_rows = [r for r in records if r["method"] == selected]
    ex8 = [r for r in first["baseline_predictions"] if r["method"] == "EX8_native_algorithm" and r["system_count"] == "3"]
    comparisons = {"EX8_native": bootstrap(selected_rows,ex8)}
    for item in table[1:5]:
        comparisons[item["method"]] = bootstrap(selected_rows,[r for r in records if r["method"] == item["method"]])
    write(OUT/"results.json", {"schema": "expert81-production-reselection-v1", "selected_three_system": selected,
          "selected_two_system": "retain_current14_15_lookup", "ranking": table, "paired_uncertainty": comparisons,
          "ranking_interpretation": "post-exposure development selection after explicit new user objective; no new blind test or optimization",
          "fits_performed": 0, "selected_at_utc": datetime.now(timezone.utc).isoformat(), "protocol_sha256": sha(OUT/"protocol.json")})
    print(json.dumps({"selected_three_system": selected, "selected_two_system": "retain_current14_15_lookup", "top5": table[:5], "uncertainty": comparisons}, ensure_ascii=False))


if __name__ == "__main__": main()
