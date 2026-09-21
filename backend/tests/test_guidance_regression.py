"""测量真实性、决策入口一致性、动作保护持久化及前后端逐值对拍。"""
import copy
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import models
from app.routers import assistant, overview
from app.services import resolvers
from app.services.guidance import build_density_guidance, density_guard_state
from app.services.migrate import _apply_in_session, _plan
from app.services.state import load_store

NOW = 1_790_000_000_000
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dump_guidance_js.js"


def measured_store():
    return {
        "ashTarget": 8.5, "ashTargetTol": 0.1, "guideScheme": "total",
        "instrumentInputs": {"density": {"manual": 1.55, "manualAt": NOW}},
        "calcLogs": [
            {"timestamp": "2026-09-01 08:00:00", "calc_type": "ash_density",
             "input_json": json.dumps({"belt": "501", "ash_content": 9.1, "density": 1.49})},
            {"timestamp": "2026-09-01 08:01:00", "calc_type": "ash_density",
             "input_json": json.dumps({"belt": "502", "ash_content": 8.0, "density": 1.49})},
        ],
    }


def case_store(case):
    s = measured_store()
    if case == "constant":
        return {"ashTarget": 8.5}
    if case == "heavy":
        s.update(guideScheme="heavy", heavyAshManualOn=False,
                 heavyAshInput={"manual": 12, "manualAt": NOW},
                 amountInputs={k: {"manual": v, "manualAt": NOW} for k, v in
                               (("denseAmount", 400), ("floatAmount", 30), ("coarseAmount", 40))},
                 floatAshInput={"manual": 9.5, "manualAt": NOW},
                 coarseAshInput={"manual": 13, "manualAt": NOW})
    if case == "step":
        s["densityGuide"] = {"maxStep": 0.005}
    if case in ("dwell", "new_lab"):
        s["densityLastMoveAt"] = NOW - 60_000
    if case == "new_lab":
        s.update(totalAshManualOn=True, ashInputs={"totalAsh": {"manual": 9.3, "manualAt": NOW}})
    if case == "placeholder":
        s.update(totalAshManualOn=True, ashInputs={"totalAsh": {"manual": 8.5, "manualAt": NOW - 25 * 3600_000}})
    if case == "latch":
        s["densityActionLatch"] = {"key": density_guard_state(s, 9.1, 8.0, "total", NOW)["driveKey"], "at": NOW}
    if case == "density_meter":
        s["instrumentInputs"] = {}
        s["calcLogs"].append({"timestamp": "2026-09-01 08:02:00", "calc_type": "density_meter",
                              "input_json": json.dumps({"value": 1.52})})
    if case == "expired_manual":
        s["ashInputs"] = {"totalAsh": {"manual": 8.5, "manualAt": NOW - 2000}}
        s["autoState"] = {"totalAsh": {"v": 9.1, "t": NOW - 1000}}
    if case == "missing_ash":
        s["calcLogs"] = [{"timestamp": "2026-09-01 08:02:00", "calc_type": "ash_meter",
                          "input_json": json.dumps({"belt": "501", "value": None})}]
    if case in ("manual_pinned", "manual_source_off", "balance", "balance_zero", "balance_outside"):
        s.update(totalAshManualOn=case != "manual_source_off",
                 ashInputs={"totalAsh": {"manual": 0.1 if case == "balance_outside" else 9.3, "manualAt": NOW - 2000}},
                 autoState={"totalAsh": {"v": 8.1, "t": NOW - 1000}},
                 amountInputs={k: {"manual": v, "manualAt": NOW} for k, v in
                               (("denseAmount", 0 if case == "balance_zero" else 400), ("floatAmount", 30), ("coarseAmount", 40))},
                 floatAshInput={"manual": 9.5, "manualAt": NOW},
                 coarseAshInput={"manual": 13, "manualAt": NOW})
    return s


@pytest.mark.parametrize("case", ["measured", "constant", "heavy", "step", "dwell", "new_lab", "placeholder", "latch", "density_meter", "expired_manual", "missing_ash",
                                 "manual_pinned", "manual_source_off", "balance", "balance_zero", "balance_outside"])
def test_guidance_matches_live_frontend(case):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend/backend golden comparison")
    store = case_store(case)
    original = copy.deepcopy(store)
    guide, _ = build_density_guidance(store, NOW)
    assert store == original, "决策服务不得修改原始测量或动作状态"
    out = subprocess.run([node, str(SCRIPT)], input=json.dumps({"store": store, "now": NOW}),
                         text=True, encoding="utf-8", capture_output=True, check=True)
    front = json.loads(out.stdout)
    assert front["ash501"] == resolvers.resolve_instrument(store, "ash_501")
    assert front["ash502"] == resolvers.get_heavy_ash(store)
    assert front["density"] == guide["rhoCur"]
    for key, ash in (("inferredHeavy", resolvers.resolve_total_ash(store)), ("targetHeavy", store.get("ashTarget", 8.5))):
        expected = resolvers.back_calc_heavy_ash(store, ash)
        assert front[key] == (pytest.approx(expected, abs=1e-6) if expected is not None else None)
    for key in ("valid", "hold", "placeholderManual", "driveKey", "direction", "actualTotal",
                "heavyAsh", "rhoNew", "deltaRho", "targetHeavy", "maxStep"):
        expected, actual = front["guidance"][key], guide[key]
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            assert actual == pytest.approx(expected, abs=1e-6), key
        else:
            assert actual == expected, key
    if case in ("latch", "dwell"):
        assert guide["hold"] and guide["deltaRho"] == 0
    if case in ("constant", "placeholder", "missing_ash"):
        assert not guide["valid"]
    if case == "new_lab":
        assert not guide["hold"] and guide["direction"] == "down"
    if case == "manual_pinned":
        assert guide["actualTotal"] == 9.3
    if case == "manual_source_off":
        assert resolvers.resolve_total_ash_ex(store)["source"] == "formula"
        assert guide["actualTotal"] != 9.3
        assert store["ashInputs"]["totalAsh"]["manual"] == 9.3
    if case == "balance_outside":
        assert front["inferredHeavy"] < 0, "不应将不合理输入的反推结果钳制成看似合理的值"


def test_density_change_does_not_fabricate_ash_response():
    store = measured_store()
    for rho in (1.35, 1.49, 1.60):
        store["instrumentInputs"]["density"]["manual"] = rho
        assert resolvers.resolve_instrument(store, "ash_501") == 9.1
        assert resolvers.get_heavy_ash(store) == 8.0
        assert resolvers.resolve_total_ash(store) == 9.1


@pytest.mark.parametrize("case", ["constant", "heavy", "dwell"])
def test_dashboard_and_assistant_share_guidance(monkeypatch, case):
    store = case_store(case)
    # 避免依赖真实时钟：驻留场景用同一数据闩锁。
    if case == "dwell":
        store = case_store("latch")
    monkeypatch.setattr(overview, "load_store", lambda db: store)
    monkeypatch.setattr(assistant, "load_store", lambda db: store)
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        dashboard = overview.dashboard(db)["guidance"]
        snapshot = assistant.build_snapshot(db)["guidance"]
    for key in ("valid", "hold", "direction", "reason", "scheme", "deltaA"):
        assert snapshot[key] == dashboard[key]
    assert snapshot["suggested_density"] == dashboard["rhoNew"]
    engine.dispose()


def test_action_protection_and_source_switch_survive_database_roundtrip():
    store = case_store("latch")
    store.update(densityLastMoveAt=NOW - 60_000, heavyAshManualOn=False)
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        _apply_in_session(db, _plan(store))
        db.commit()
        restored = load_store(db)
    for key in ("densityLastMoveAt", "densityActionLatch", "heavyAshManualOn"):
        assert restored[key] == store[key]
    assert build_density_guidance(restored, NOW)[0]["hold"]
    engine.dispose()


def test_total_ash_workflow_in_frontend():
    node = shutil.which("node")
    if not node:
        pytest.fail("Node.js is required for frontend workflow regression")
    subprocess.run([node, str(SCRIPT.with_name("verify_total_ash_workflow.js"))],
                   text=True, encoding="utf-8", capture_output=True, check=True)


@pytest.mark.parametrize("manual_on", [True, False])
def test_saved_manual_total_survives_source_switch_and_database_roundtrip(manual_on):
    store = case_store("manual_pinned")
    store["totalAshManualOn"] = manual_on
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        _apply_in_session(db, _plan(store))
        db.commit()
        restored = load_store(db)
    assert restored["ashInputs"] == store["ashInputs"]
    assert restored["totalAshManualOn"] is manual_on
    assert resolvers.resolve_total_ash_ex(restored) == resolvers.resolve_total_ash_ex(store)
    engine.dispose()
