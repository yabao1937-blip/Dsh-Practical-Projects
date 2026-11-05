"""Pure fixed EXPERT8.1 expanded-source candidates; no I/O or activation.

Rows are normalized by the experiment adapter and retain canonical identity,
label-version availability, source, quality and condition. The current source
is the reference domain. Historical replay remains an explicitly marked proxy.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math

import numpy as np

from . import expert81 as first_core

FEATURES = ("level", "coal_amount", "sysA", "sysB", "sys401", "sys402", "desliming473", "desliming474")
SWITCHES = FEATURES[2:]
CANDIDATES = tuple(f"E{i}" for i in range(8))
BJ = timezone(timedelta(hours=8))
DEFAULT_CONFIG = {
    "minimum_current_normal": 5, "minimum_current_dates": 2,
    "legacy_mass_ratio": .25, "approximate_mass_ratio": .25,
    "iqr_floor_pp": 1., "iqr_multiplier": 1.5,
    "huber_delta_pp": 1., "ridge_lambda": 1.,
    "centers": {"level": 50., "coal_amount": 600., **{k: 0. for k in SWITCHES}},
    "scales": {"level": 10., "coal_amount": 100., **{k: 1. for k in SWITCHES}},
    "maximum_iterations": 3000, "coefficient_tolerance": 1e-10, "gradient_max": 1e-6,
    "bounds": [0., 100.], "mode": "historical_entry_proxy",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def parse_time(value):
    return first_core.parse_time(value)


def stamp(value):
    return first_core.stamp(value)


def finite(value, name="value"):
    return first_core.finite(value, name)


def configuration(config=None):
    result = deepcopy(DEFAULT_CONFIG)
    if config:
        if set(config)-set(result):
            raise ValueError("undeclared expanded configuration key")
        result.update(deepcopy(config))
    for key in ("minimum_current_normal", "minimum_current_dates", "legacy_mass_ratio", "approximate_mass_ratio",
                "iqr_floor_pp", "iqr_multiplier", "huber_delta_pp", "ridge_lambda", "maximum_iterations",
                "coefficient_tolerance", "gradient_max"):
        if finite(result[key], key) <= 0:
            raise ValueError("expanded configuration values must be positive")
    if result["mode"] not in ("historical_entry_proxy", "snapshot_diagnostic", "formal"):
        raise ValueError("undeclared availability mode")
    for key in FEATURES:
        finite(result["centers"][key],key)
        if finite(result["scales"][key],key)<=0:
            raise ValueError("positive feature scales required")
    lo,hi=map(finite,result["bounds"])
    if not 0<=lo<hi<=100:
        raise ValueError("invalid expanded ash bounds")
    return result


def normalize_features(values):
    result = {k: finite(values[k], k) for k in FEATURES}
    if any(result[k] not in (0., 1.) for k in SWITCHES):
        raise ValueError("switch values must be explicitly binary")
    return result


def group_of(values):
    return ",".join(str(int(values[k])) for k in SWITCHES)


def available_rows(rows, cutoff, *, exclude_record_id=None):
    """Version merge precedes time/group/quality eligibility.

    No unavailable value is read or hashed into an earlier fitted state.
    """
    cutoff = parse_time(cutoff)
    latest = {}
    for raw in rows:
        identity = str(raw["record_id"])
        if exclude_record_id is not None and identity == str(exclude_record_id):
            continue
        at = parse_time(raw["available_at"])
        if at >= cutoff:
            continue
        if not raw.get("version_id") or not raw.get("provenance"):
            raise ValueError("version and availability provenance required")
        if identity in latest:
            prior = latest[identity]
            previous = parse_time(prior["available_at"])
            if at == previous and digest(raw) != digest(prior):
                raise ValueError("ambiguous simultaneous expanded label versions")
            if at < previous:
                continue
        latest[identity] = raw
    selected = []
    for raw in latest.values():
        if raw.get("deleted") or raw.get("quality") not in ("legacy_complete", "strict", "minute_approximation"):
            continue
        if raw["domain"] != "legacy":
            if raw.get("sample_at") is None or parse_time(raw["sample_at"]) >= cutoff:
                continue
            if parse_time(raw["sample_at"]) > parse_time(raw["available_at"]):
                raise ValueError("available label precedes its sample")
        row = deepcopy(raw)
        row["ash"] = finite(row["ash"], "ash")
        if not 0 < row["ash"] <= 100:
            raise ValueError("invalid expanded ash value")
        row["features"] = normalize_features(row["features"])
        if group_of(row["features"]) != row["group_key"]:
            raise ValueError("group does not match recorded switches")
        if int(sum(row["features"][k] for k in SWITCHES[:4])) != row["system_count"]:
            raise ValueError("system count does not match switches")
        row["available_at"] = stamp(row["available_at"])
        selected.append(row)
    return sorted(selected, key=lambda r: (r.get("sample_at") or "", str(r["record_id"]), str(r["version_id"])))


def fence(rows, cfg):
    if not rows:
        return None
    q1, q3 = map(float, np.quantile([r["ash"] for r in rows], [.25, .75], method="linear"))
    spread = max(q3-q1, cfg["iqr_floor_pp"])
    return {"q1": q1, "q3": q3, "lower": q1-cfg["iqr_multiplier"]*spread,
            "upper": q3+cfg["iqr_multiplier"]*spread}


def inside(value, bounds):
    return bounds is not None and bounds["lower"] <= value <= bounds["upper"]


def pools(rows, group_key, cfg):
    current = [r for r in rows if r["domain"] == "current" and r["quality"] == "strict" and r["group_key"] == group_key]
    bounds = fence(current, cfg)
    normal = [r for r in current if inside(r["ash"], bounds)]
    legacy = [r for r in rows if r["domain"] == "legacy" and r["system_count"] >= 3]
    legacy_fences = {str(count): fence([r for r in legacy if r["system_count"] == count], cfg)
                     for count in sorted({r["system_count"] for r in legacy})}
    legacy_normal = [r for r in legacy if inside(r["ash"], legacy_fences[str(r["system_count"])])]
    approximate = [r for r in rows if r["domain"] in ("current", "recent_excel")
                   and r["quality"] == "minute_approximation" and r["group_key"] == group_key]
    approximate_normal = [r for r in approximate if inside(r["ash"], bounds)]
    return {"current_raw": current, "current": normal, "current_fence": bounds,
            "legacy_raw": legacy, "legacy": legacy_normal, "legacy_fences": legacy_fences,
            "approximate_raw": approximate, "approximate": approximate_normal}


def design(rows, predictors, domains, cfg):
    return np.array([[1.]+[(r["features"][key]-cfg["centers"][key])/cfg["scales"][key] for key in predictors]
                     +[float(r["domain"] == domain) for domain in domains] for r in rows], dtype=float)


def weighted_huber(rows, predictors, cfg, *, weights=None, source_domains=False, targets=None):
    if not rows:
        raise ValueError("no fitting rows")
    if weights is None:
        weights = [1.]*len(rows)
    weights = np.array([finite(w, "weight") for w in weights], dtype=float)
    if len(weights) != len(rows) or np.any(weights <= 0):
        raise ValueError("positive weights required for every real row")
    domains = sorted({r["domain"] for r in rows}-{ "current" }) if source_domains else []
    if source_domains and not any(r["domain"] == "current" for r in rows):
        raise ValueError("reference-coded source model requires current reference rows")
    x = design(rows, predictors, domains, cfg)
    y = np.array([r["ash"] for r in rows] if targets is None else [finite(v) for v in targets], dtype=float)
    if len(y) != len(rows):
        raise ValueError("target count differs from row count")
    penalty = np.diag([0.]+[cfg["ridge_lambda"]]*len(predictors)+[0.]*len(domains))
    coefficients = np.zeros(x.shape[1])
    coefficients[0] = first_core.weighted_median(y.tolist(), weights.tolist())
    delta = cfg["huber_delta_pp"]
    converged = False
    for iteration in range(1, int(cfg["maximum_iterations"])+1):
        residual = y-x@coefficients
        effective = weights*np.minimum(1., delta/np.maximum(np.abs(residual), 1e-300))
        updated = np.linalg.solve(x.T@(effective[:, None]*x)+penalty, x.T@(effective*y))
        if not np.all(np.isfinite(updated)):
            raise ValueError("nonfinite expanded regression solution")
        step = float(np.max(np.abs(updated-coefficients)))
        coefficients = updated
        if step <= cfg["coefficient_tolerance"]:
            converged = True
            break
    residual = y-x@coefficients
    gradient = -(x.T@(weights*np.clip(residual, -delta, delta)))+penalty@coefficients
    gradient_max = float(np.max(np.abs(gradient)))
    if not converged or gradient_max > cfg["gradient_max"]:
        raise ValueError("expanded Huber-ridge convergence/gradient check failed")
    loss = np.where(np.abs(residual) <= delta, .5*residual**2, delta*(np.abs(residual)-.5*delta))
    return {"kind": "linear", "predictors": list(predictors), "source_domains": domains,
            "columns": ["current_reference_intercept" if source_domains else "intercept", *predictors, *[f"source:{d}" for d in domains]],
            "coefficients": coefficients.tolist(), "centers": cfg["centers"], "scales": cfg["scales"],
            "iterations": iteration, "gradient_max": gradient_max,
            "objective": float(np.sum(weights*loss)+.5*coefficients@penalty@coefficients),
            "unpenalized_columns": ["current_reference_intercept" if source_domains else "intercept", *[f"source:{d}" for d in domains]],
            "design_rank": int(np.linalg.matrix_rank(x)), "coefficient_count": x.shape[1],
            "design_condition": None if not math.isfinite(float(np.linalg.cond(x))) else float(np.linalg.cond(x)),
            "row_weights": weights.tolist()}


def linear_raw(model, values, domain="current"):
    predictors = model["predictors"]
    vector = [1.]+[(values[k]-model["centers"][k])/model["scales"][k] for k in predictors]
    vector += [float(domain == d) for d in model["source_domains"]]
    return finite(sum(a*b for a, b in zip(vector, model["coefficients"])), "raw prediction")


def _audit(rows):
    return [{k: row.get(k) for k in ("record_id", "version_id", "source", "domain", "quality", "sample_at",
                                    "available_at", "group_key", "system_count", "ash", "features", "provenance")}
            for row in rows]


def _seal(state):
    state["state_hash"] = digest(state)
    return state


def verify_state(state):
    if state.get("state_hash") != digest({k:v for k,v in state.items() if k != "state_hash"}):
        raise ValueError("expanded state hash mismatch")


def _current_dates(rows):
    return {parse_time(r["sample_at"]).astimezone(BJ).date().isoformat() for r in rows}


def _legacy_response(training, cfg):
    return weighted_huber(training, FEATURES, cfg)


def fit_state(rows, cutoff, candidate_id, *, group_key, exclude_record_id=None, config=None):
    if candidate_id not in CANDIDATES:
        raise ValueError("undeclared expanded candidate")
    cfg = configuration(config)
    at = parse_time(cutoff)
    state = {"schema": "expert81-expanded-state-v1", "candidate_id": candidate_id, "group_key": group_key,
             "cutoff": stamp(at), "mode": cfg["mode"], "config": cfg, "status": "unavailable", "reason": None,
             "model": None, "fence": None, "legacy_fences": {}, "current_raw_ids": [], "current_normal_ids": [],
             "current_excluded_ids": [], "legacy_raw_ids": [], "legacy_normal_ids": [], "legacy_excluded_ids": [],
             "approximate_raw_ids": [], "approximate_normal_ids": [], "used_rows": [], "weight_sums": {},
             "current_ranges": {}, "excluded_target_id": exclude_record_id}
    if sum(int(x) for x in group_key.split(",")[:4]) < 3:
        state["reason"] = "two_system_requires_independent_branch"
        return _seal(state)
    available = available_rows(rows, at, exclude_record_id=exclude_record_id)
    pool = pools(available, group_key, cfg)
    current = pool["current"]; legacy = pool["legacy"]; approx = pool["approximate"]
    state.update(fence=pool["current_fence"], legacy_fences=pool["legacy_fences"],
                 current_raw_ids=[r["record_id"] for r in pool["current_raw"]],
                 current_normal_ids=[r["record_id"] for r in current],
                 current_excluded_ids=[r["record_id"] for r in pool["current_raw"] if r not in current],
                 legacy_raw_ids=[r["record_id"] for r in pool["legacy_raw"]],
                 legacy_normal_ids=[r["record_id"] for r in legacy],
                 legacy_excluded_ids=[r["record_id"] for r in pool["legacy_raw"] if r not in legacy],
                 approximate_raw_ids=[r["record_id"] for r in pool["approximate_raw"]],
                 approximate_normal_ids=[r["record_id"] for r in approx])
    if len(current) < cfg["minimum_current_normal"] or len(_current_dates(current)) < cfg["minimum_current_dates"]:
        state["reason"] = "insufficient_own_group_current_history"
        return _seal(state)
    state["current_ranges"] = {k:{"minimum":min(r["features"][k] for r in current),
                                   "maximum":max(r["features"][k] for r in current)} for k in FEATURES}
    used = current
    try:
        if candidate_id == "E0":
            model = {"kind":"constant", "value":float(np.median([r["ash"] for r in current]))}
        elif candidate_id == "E1":
            model = weighted_huber(current, ["level"], cfg)
        elif candidate_id == "E2":
            events = [{"assay_id":r["assay_id"], "version_id":r["version_id"], "sample_at":r["sample_at"],
                       "version_available_at":r["available_at"], "value":r["ash"], "group_key":group_key,
                       "features":{k:r["features"][k] for k in ("level","coal_amount")},
                       "frozen_prediction":r["frozen_prediction"], "provenance":r["provenance"],
                       **({"c5_receipt":r["c5_receipt"]} if r.get("c5_receipt") else {})}
                      for r in pool["current_raw"]]
            receipt_mode = "historical_entry_proxy" if cfg["mode"] == "historical_entry_proxy" else "formal"
            control = first_core.fit_state(events, at, "C7", {"receipt_mode":receipt_mode}, group_key=group_key)
            if control["status"] != "ok":
                raise ValueError(control["reason"])
            model = {"kind":"first_C7", "state":control}
        elif candidate_id in ("E3", "E4", "E7"):
            selected_legacy = [r for r in legacy if r.get("old177")] if candidate_id == "E4" else legacy
            if not selected_legacy:
                raise ValueError("missing_eligible_legacy_prior")
            selected_approx = approx if candidate_id == "E7" else []
            legacy_mass = cfg["legacy_mass_ratio"]*len(current)
            approx_mass = cfg["approximate_mass_ratio"]*len(current) if selected_approx else 0.
            used = current+selected_legacy+selected_approx
            weights = [1.]*len(current)+[legacy_mass/len(selected_legacy)]*len(selected_legacy)
            weights += [approx_mass/len(selected_approx)]*len(selected_approx) if selected_approx else []
            model = weighted_huber(used, FEATURES, cfg, weights=weights, source_domains=True)
            state["weight_sums"] = {"current_strict":float(len(current)), "legacy":float(legacy_mass),
                                    "approximate":float(approx_mass), "actual_total":float(sum(weights))}
        else:
            if not legacy:
                raise ValueError("missing_eligible_legacy_prior")
            prior = _legacy_response(legacy, cfg)
            residual = [r["ash"]-linear_raw(prior,r["features"],"legacy") for r in current]
            model = {"kind":"legacy_calibrated", "legacy_response":prior, "calibration":None}
            if candidate_id == "E5":
                model["calibration"] = {"kind":"constant", "value":float(np.median(residual))}
            else:
                model["calibration"] = weighted_huber(current,["level","coal_amount"],cfg,targets=residual)
            used = current+legacy
            state["weight_sums"] = {"legacy_response_training":float(len(legacy)), "current_calibration":float(len(current))}
        state.update(model=model, status="ok", reason=None, used_rows=_audit(used))
        if not state["weight_sums"]:
            state["weight_sums"] = {"current_strict":float(len(current))}
    except (ValueError, np.linalg.LinAlgError) as exc:
        state["reason"] = str(exc)
    return _seal(state)


def predict_state(state, features):
    verify_state(state)
    result = {"candidate_id":state["candidate_id"],"state_hash":state["state_hash"],"status":state["status"],
              "reason":state["reason"],"prediction":None,"raw_prediction":None,"outside_current_ranges":[]}
    if state["status"] != "ok":
        return result
    try:
        values = normalize_features(features)
        if group_of(values) != state["group_key"]:
            raise ValueError("target group differs from fitted current calibration group")
        model = state["model"]
        if model["kind"] == "constant":
            raw = model["value"]
        elif model["kind"] == "first_C7":
            previous = first_core.predict_state(model["state"],values)
            if previous["prediction"] is None:
                raise ValueError(previous["reason"])
            raw = previous["raw_prediction"]
        elif model["kind"] == "legacy_calibrated":
            base = linear_raw(model["legacy_response"],values,"legacy")
            correction = model["calibration"]
            shift = correction["value"] if correction["kind"] == "constant" else linear_raw(correction,values)
            result.update(legacy_raw=base,current_correction_raw=shift)
            raw = base+shift
        else:
            raw = linear_raw(model,values)
        raw = finite(raw,"raw prediction")
        lo,hi=state["config"]["bounds"]
        result.update(raw_prediction=raw,prediction=min(hi,max(lo,raw)),
                      outside_current_ranges=[k for k,b in state["current_ranges"].items() if not b["minimum"]<=values[k]<=b["maximum"]])
    except (ValueError,KeyError,TypeError) as exc:
        result.update(status="unavailable",reason=str(exc))
    return result


def fit_two_system(rows, cutoff, *, group_key, method, exclude_record_id=None, config=None):
    if method not in ("T0","T1") or sum(int(x) for x in group_key.split(",")[:4]) != 2:
        raise ValueError("two-system exact group and declared method required")
    cfg=configuration(config)
    available=available_rows(rows,cutoff,exclude_record_id=exclude_record_id)
    own=[r for r in available if r["domain"]=="current" and r["quality"]=="strict" and r["group_key"]==group_key]
    bounds=fence(own,cfg);normal=[r for r in own if inside(r["ash"],bounds)]
    state={"schema":"expert81-expanded-state-v1","candidate_id":method,"group_key":group_key,"cutoff":stamp(cutoff),
           "config":cfg,"mode":cfg["mode"],"scope":"independent_two_system_group","fence":bounds,
           "status":"unavailable","reason":"insufficient_own_two_system_history","model":None,
           "current_raw_ids":[r["record_id"] for r in own],"current_normal_ids":[r["record_id"] for r in normal],
           "used_rows":[],"current_ranges":{},"low_support":True,"excluded_target_id":exclude_record_id}
    dates=_current_dates(normal)
    if len(normal)>=3 and (method=="T0" or len(dates)>=2):
        try:
            state["model"]={"kind":"constant","value":float(np.median([r["ash"] for r in normal]))} if method=="T0" else weighted_huber(normal,["level"],cfg)
            state.update(status="ok",reason=None,used_rows=_audit(normal),sample_date_count=len(dates),
                         current_ranges={k:{"minimum":min(r["features"][k] for r in normal),"maximum":max(r["features"][k] for r in normal)} for k in FEATURES})
            state["level_unique_count"]=len({r["features"]["level"] for r in normal})
        except (ValueError,np.linalg.LinAlgError) as exc:
            state["reason"]=str(exc)
    return _seal(state)
