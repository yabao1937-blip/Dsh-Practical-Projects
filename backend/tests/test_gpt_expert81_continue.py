"""Continuation contracts: information timing, same-group fences, fixed transforms."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.gpt_coarse.experiments import expert81_expanded as core
from scripts.gpt_expert81_continue_experiment import fit_candidate, predict, select

AT = datetime(2026, 11, 2, 4, tzinfo=timezone.utc)
GROUP = "0,1,1,1,0,0"


def records():
    result = []
    for domain, count in (("current", 6), ("legacy", 20)):
        for i in range(count):
            features = dict(level=30.+i*2, coal_amount=500.+i*7, sysA=int(domain == "legacy"), sysB=1,
                            sys401=1, sys402=1, desliming473=0, desliming474=0)
            sample = AT-timedelta(days=4, hours=i*8) if domain == "current" else AT-timedelta(days=40)
            result.append(dict(record_id=f"{domain}:{i}", assay_id=i, domain=domain, source=domain,
                version_id=f"{domain}:v{i}", sample_at=sample.isoformat(), available_at=(sample+timedelta(hours=2)).isoformat(),
                ash=10.+.2*i, features=features, group_key=core.group_of(features), system_count=3+int(domain == "legacy"),
                quality="strict" if domain == "current" else "legacy_complete", old177=True,
                frozen_prediction=12., provenance={"kind": "synthetic"}))
    return result


@pytest.mark.parametrize("method", ["F0", "F1", "F2", "F3"])
def test_future_target_and_revision_cannot_change_continuation_state(method):
    rows = records()
    target = deepcopy(rows[0]); target.update(record_id="target", ash=99.)
    future = deepcopy(rows[0]); future.update(record_id="future", available_at=(AT+timedelta(seconds=1)).isoformat(), ash=99.)
    kwargs = dict(group=GROUP, target_id="target")
    before = fit_candidate(rows+[target, future], AT, method, **kwargs)
    target["ash"] = float("nan"); future["ash"] = float("nan")
    after = fit_candidate(rows+[target, future], AT, method, **kwargs)
    assert before == after == fit_candidate(rows, AT, method, **kwargs)
    assert before["status"] == "ok"


def test_shared_fence_shrink_and_equal_raw_blend():
    rows = records()
    states = {m: fit_candidate(rows, AT, m, group=GROUP) for m in ("C7", "E5", "F0", "F1", "F2", "F3")}
    assert all(s["fence"] == states["C7"]["fence"] for s in states.values())
    assert all(s["current_normal_ids"] == states["C7"]["current_normal_ids"] for s in states.values())
    e5 = states["E5"]["model"]["legacy_response"]["coefficients"]
    f2 = states["F2"]["model"]["legacy_response"]["coefficients"]
    assert f2[0] == e5[0]
    assert np.allclose(f2[1:], np.array(e5[1:])*.5)
    x = rows[0]["features"]
    assert predict(states["F3"], x)["raw_prediction"] == pytest.approx(.5*(predict(states["C7"], x)["raw_prediction"]+predict(states["E5"], x)["raw_prediction"]))
    assert predict(states["F3"], {**x, "sys402": 0})["prediction"] is None


def test_blend_rejects_when_legacy_component_cannot_fit():
    rows = [r for r in records() if r["domain"] == "current"]
    state = fit_candidate(rows, AT, "F3", group=GROUP)
    assert state["status"] == "unavailable"
    assert predict(state, rows[0]["features"])["prediction"] is None


def test_selection_gate_uses_both_periods_and_falls_back():
    rows = []
    for phase in ("early_development", "main_development"):
        for method in ("C7", "E5", "F0", "F1", "F2", "F3"):
            error = .5 if method == "C7" else (1.2 if phase == "early_development" else .1)
            rows.append(dict(phase=phase, method=method, normal=True, prediction=10.+error,
                             raw_prediction=10.+error, error=error, abs_error=error, raw_abs_error=error, date="2026-10-20"))
    assert select(rows)["selected"] == "C7"
