"""Synthetic-only tests of the four-system read projection."""
from copy import deepcopy
from app.telemetry.derived import FOUR_SYSTEM_MEMBERS, system_status_points


def items(values=(True, False, False, True)):
    return [{"source_id":"test", "key":f"p{i}", "item_id":item_id, "available":True, "status":"good",
        "sample":{"value":value, "quality":"good", "issue":None, "received_at":"2026-10-06T06:00:00.000+00:00"}}
        for i, ((_, item_id), value) in enumerate(zip(FOUR_SYSTEM_MEMBERS, values))]


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
