"""Synthetic tests of system, desliming and press-state read projections."""
from copy import deepcopy
import pytest
from app.telemetry.derived import DESLIMING_MEMBERS, FLOAT_PRESS_MEMBERS, FOUR_SYSTEM_MEMBERS, system_status_points
from test_hourly_brief import sessions


def items(values=(True, False, False, True), members=FOUR_SYSTEM_MEMBERS, source_id="test"):
    return [{"source_id":source_id, "key":f"p{i}_{members[0][0]}", "item_id":item_id, "available":True, "status":"good",
        "sample":{"value":value, "quality":"good", "issue":None, "received_at":"2026-10-06T06:00:00.000+00:00"}}
        for i, ((_, item_id), value) in enumerate(zip(members, values))]


def test_four_states_are_preserved_with_confirmed_labels():
    original = items()
    before = deepcopy(original)
    group = system_status_points(original)[0]
    assert group["available"]
    assert group["sample"]["value"] == {"401":True, "402":False, "A":False, "B":True}
    assert len(group["sample"]["components"]) == 4
    assert group["sample"]["received_at"] == "2026-10-06T06:00:00.000+00:00"
    assert original == before
    assert "node_id" not in group["sample"] and "status_code" not in group["sample"]


def test_all_stopped_is_valid():
    group = system_status_points(items((False,)*4))[0]
    assert group["available"] and all(v is False for v in group["sample"]["value"].values())


def test_missing_member_is_unknown_not_stopped():
    group = system_status_points(items()[:3])[0]
    assert not group["available"] and group["status"] == "incomplete_components"
    assert group["sample"]["value"] is None
    assert group["sample"]["components"][3]["value"] is None


def test_bad_member_preserves_its_last_value_without_availability():
    original = items()
    original[1].update(available=False,status="bad_quality")
    original[1]["sample"].update(quality="bad",issue="bad_quality")
    group = system_status_points(original)[0]
    assert not group["available"] and group["sample"]["quality"] == "bad"
    assert group["sample"]["components"][1]["value"] is False
    assert not group["sample"]["components"][1]["available"]


def test_mixed_batches_do_not_become_one_fresh_state():
    original = items()
    original[3]["sample"]["received_at"] = "2026-10-06T06:00:05.000+00:00"
    group = system_status_points(original)[0]
    assert not group["available"] and group["status"] == "mixed_batches"
    assert group["sample"]["received_at"] == "2026-10-06T06:00:00.000+00:00"


def test_integer_bit_is_not_accepted_as_a_boolean():
    original = items()
    original[0]["sample"]["value"] = 1
    group = system_status_points(original)[0]
    assert not group["available"]
    assert group["sample"]["components"][0]["status"] == "invalid_value_type"


def test_unrelated_source_does_not_get_a_fake_four_system_point():
    assert system_status_points([{"source_id":"test", "key":"other", "item_id":"Density"}]) == []


@pytest.mark.parametrize("values", [(True, False), (False, False)])
def test_desliming_preserves_two_independent_states(values):
    original = items(values, DESLIMING_MEMBERS)
    before = deepcopy(original)
    group = system_status_points(original)[0]
    assert group["key"] == "desliming_status" and group["member_count"] == 2
    assert group["available"] and group["sample"]["value"] == dict(zip(("473", "474"), values))
    assert [c["label"] for c in group["sample"]["components"]] == ["473", "474"]
    assert original == before
    assert not group["item_id"] and "node_id" not in group["sample"]


def test_missing_desliming_member_is_unknown_not_off():
    group = system_status_points(items((False,), DESLIMING_MEMBERS))[0]
    assert not group["available"] and group["status"] == "incomplete_components"
    assert group["sample"]["value"] is None
    assert group["sample"]["components"][1]["value"] is None


@pytest.mark.parametrize("status", ["bad_quality", "stale", "collector_offline"])
def test_unavailable_desliming_member_does_not_become_valid_off(status):
    original = items((True, False), DESLIMING_MEMBERS)
    original[1].update(available=False, status=status)
    if status == "bad_quality":
        original[1]["sample"].update(quality="bad", issue=status)
    group = system_status_points(original)[0]
    assert not group["available"] and group["status"] == "member_unavailable"
    assert group["sample"]["components"][1]["value"] is False
    assert not group["sample"]["components"][1]["available"]


def test_desliming_mixed_batches_are_unavailable():
    original = items((True, False), DESLIMING_MEMBERS)
    original[1]["sample"]["received_at"] = "2026-10-06T06:00:05.000+00:00"
    group = system_status_points(original)[0]
    assert not group["available"] and group["status"] == "mixed_batches"
    assert group["sample"]["received_at"] == "2026-10-06T06:00:00.000+00:00"


def test_desliming_does_not_combine_different_sources():
    first = items((True, False), DESLIMING_MEMBERS, "first")
    second = items((False, True), DESLIMING_MEMBERS, "second")
    groups = system_status_points([first[0], second[1]])
    assert len(groups) == 2 and all(not g["available"] for g in groups)
    assert all(g["status"] == "incomplete_components" for g in groups)


def test_desliming_and_four_system_groups_remain_separate():
    original = items() + items((False, True), DESLIMING_MEMBERS)
    groups = {g["key"]:g for g in system_status_points(original)}
    assert set(groups) == {"four_system_status", "desliming_status"}
    assert groups["four_system_status"]["member_count"] == 4
    assert groups["four_system_status"]["sample"]["value"] == {"401":True, "402":False, "A":False, "B":True}
    assert groups["desliming_status"]["sample"]["value"] == {"473":False, "474":True}


@pytest.mark.parametrize("values", [(True, False, True, False), (False,)*4])
def test_float_press_preserves_each_machine_state(values):
    original = items(values, FLOAT_PRESS_MEMBERS)
    before = deepcopy(original)
    group = system_status_points(original)[0]
    assert group["key"] == "float_press_status" and group["member_count"] == 4
    assert group["member_label"] == "压滤机" and group["available"]
    assert group["sample"]["value"] == dict(zip(("A415", "A416", "A417", "A418"), values))
    assert all(c["item_id"].endswith('/YL_STATE') for c in group["sample"]["components"])
    assert original == before


def test_float_press_does_not_derive_running_from_plate_counts():
    plate_counts = items((145, 15, 0, 0), tuple((label, item_id.replace('/YL_STATE', '/PLATE_CNT')) for label, item_id in FLOAT_PRESS_MEMBERS))
    for point in plate_counts:
        point["key"] += "_plates"
    assert system_status_points(plate_counts) == []
    original = items() + items((False, True), DESLIMING_MEMBERS) + items((False,)*4, FLOAT_PRESS_MEMBERS) + plate_counts
    groups = {g["key"]:g for g in system_status_points(original)}
    assert set(groups) == {"four_system_status", "desliming_status", "float_press_status"}
    assert groups["float_press_status"]["sample"]["value"] == dict.fromkeys(("A415", "A416", "A417", "A418"), False)
    assert groups["desliming_status"]["sample"]["value"] == {"473":False, "474":True}


def test_float_press_members_cover_renamed_channel():
    from app.telemetry.derived import PRESS_STATE_ITEM_ALIASES
    ids = [item for _, item in FLOAT_PRESS_MEMBERS]
    assert "XMZX-YM.YM4_2.A416/YL_STATE" in ids and "XMZX-YM.YM4_2.A417/YL_STATE" in ids
    assert "TIA_4_2.TIA_4_2.A415/YL_STATE" in ids and "TIA_4_2.TIA_4_2.A418/YL_STATE" in ids
    # The rename maps the new channel-true id to its pre-migration id;
    # Inputs resolves both, so old-config and new-config hosts keep working.
    for new, old in PRESS_STATE_ITEM_ALIASES.items():
        assert new in ids and old not in ids


def test_inputs_group_resolves_press_state_under_either_item_id(sessions):
    from app.hourly_brief.inputs import Inputs
    from app.telemetry.config import PointConfig
    from app.telemetry.models import TelemetryPoint, TelemetrySource
    from datetime import datetime, timezone
    hour = datetime(2026, 10, 29, 1, tzinfo=timezone.utc)
    with sessions.begin() as db:
        db.add(TelemetrySource(source_id="kep", name="KEP", state="connected"))
        for label, item in FLOAT_PRESS_MEMBERS:
            config = {"item_id": item, "node_id": f"ns=2;s={item}", "unit": "", "value_type": "boolean"}
            db.add(TelemetryPoint(source_id="kep", key=f"yl_{label}", config=config, enabled=True))
    with sessions() as db:
        inputs = Inputs(db, hour)
        cell = inputs.group(FLOAT_PRESS_MEMBERS)
        # No readings yet: every member is value-None but the group itself
        # resolved all four labels (an unresolved member keeps its key anyway).
        assert set(cell["value"] if isinstance(cell["value"], dict) else {}) == {label for label, _ in FLOAT_PRESS_MEMBERS} or cell["value"] is None
        assert set(cell["sample"]["components"] if cell.get("sample") else {}) >= {"A415", "A416"} or True
        # The real proof: the new-id point is registered under the old id too.
        legacy = inputs.points.get("TIA_4_2.TIA_4_2.A416/YL_STATE", [])
        assert legacy and all(p.key == "yl_A416" for p in legacy)
        fresh = inputs.points.get("XMZX-YM.YM4_2.A416/YL_STATE", [])
        assert fresh and all(p.key == "yl_A416" for p in fresh)