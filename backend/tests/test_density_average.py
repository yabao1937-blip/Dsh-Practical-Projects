"""Synthetic arithmetic mean of A303/A304; never connects to KEPServer."""
from copy import deepcopy
import math
import pytest
from app.telemetry.derived import DENSITY_AVERAGE_MEMBERS, density_average_points


def points(values=(1.4, 1.6), source="test"):
    return [{"source_id": source, "key": f"d{i}", "item_id": item_id, "unit": "", "available": True, "status": "good",
        "sample": {"value": value, "quality": "good", "issue": None, "received_at": "2026-10-07T01:00:00.000+00:00"}}
        for i, ((_, item_id), value) in enumerate(zip(DENSITY_AVERAGE_MEMBERS, values))]


def test_equal_weight_mean_preserves_sources_and_does_not_mutate():
    original = points()
    before = deepcopy(original)
    item = density_average_points(original)[0]
    assert item["kind"] == "numeric_average" and item["available"]
    assert item["sample"]["value"] == 1.5 and item["unit"] == ""
    assert [c["value"] for c in item["sample"]["components"]] == [1.4, 1.6]
    assert [c["weight"] for c in item["sample"]["components"]] == [.5, .5]
    assert item["sample"]["formula"] == "(A303/MD + A304/MD) ÷ 2"
    assert original == before and item["item_id"] == ""
    assert "node_id" not in item["sample"] and "status_code" not in item["sample"]


@pytest.mark.parametrize("values,expected", [((0, 0), 0), ((0, 1.6), .8), ((1.7e308, 1.7e308), 1.7e308)])
def test_zero_and_finite_large_values(values, expected):
    item = density_average_points(points(values))[0]
    assert item["available"] and item["sample"]["value"] == expected
    assert math.isfinite(item["sample"]["value"])


@pytest.mark.parametrize("bad", [True, "1.5", None, float("nan"), float("inf"), -float("inf")])
def test_invalid_member_is_not_averaged_or_replaced(bad):
    item = density_average_points(points((bad, 1.6)))[0]
    assert not item["available"] and item["sample"]["value"] is None
    assert item["sample"]["components"][0]["value"] is None


def test_missing_member_and_different_sources_are_not_combined():
    first, second = points(source="one"), points(source="two")
    for item in density_average_points([first[0], second[1]]):
        assert not item["available"] and item["status"] == "incomplete_components"
        assert item["sample"]["value"] is None


@pytest.mark.parametrize("status", ["bad_quality", "stale", "collector_offline"])
def test_unavailable_member_blocks_the_mean_but_keeps_last_reading(status):
    original = points()
    original[1].update(available=False, status=status)
    if status == "bad_quality":
        original[1]["sample"].update(quality="bad", issue=status)
    item = density_average_points(original)[0]
    assert not item["available"] and item["status"] == "member_unavailable"
    assert item["sample"]["value"] is None and item["sample"]["components"][1]["value"] == 1.6


def test_batches_units_and_time_are_checked():
    original = points()
    original[1]["sample"]["received_at"] = "2026-10-07T01:00:05.000+00:00"
    item = density_average_points(original)[0]
    assert not item["available"] and item["status"] == "mixed_batches"
    assert item["sample"]["received_at"] == "2026-10-07T01:00:00.000+00:00"
    original[1]["sample"]["received_at"] = "not a time"
    assert density_average_points(original)[0]["status"] == "incomplete_components"
    original = points()
    original[0]["unit"] = "g/cm³"
    assert density_average_points(original)[0]["status"] == "unit_mismatch"
    original[1]["unit"] = "g/cm³"
    assert density_average_points(original)[0]["unit"] == "g/cm³"


def test_setpoints_and_unrelated_items_do_not_create_the_point():
    original = points()
    for item in original:
        item["item_id"] += "_SET"
    assert density_average_points(original) == []
    assert density_average_points([]) == []
