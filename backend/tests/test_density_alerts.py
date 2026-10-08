"""Raw meter/setpoint deviation alarms, independent from held display values."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta

import pytest

from app.telemetry.density_alerts import density_alarm_checks
from app.telemetry.derived import DENSITY_AVERAGE_MEMBERS

NOW = datetime(2026, 10, 8, 3, tzinfo=timezone.utc)


def pair(label="A303", actual=1.0, target=1.48, source="kep"):
    item_id = dict(DENSITY_AVERAGE_MEMBERS)[label]
    return [dict(source_id=source, key=label+suffix, item_id=item_id+suffix, unit="", available=True,
        definition_id="v1", effective_density={"value":1.48},
        sample=dict(value=value, quality="good", issue=None, definition_id="v1", batch_id="same",
                    received_at=(NOW-timedelta(seconds=2)).isoformat()))
        for suffix, value in (("",actual),("_SET",target))]


def test_channels_are_independent_and_raw_one_triggers_despite_display_hold():
    items = pair() + pair("A304",1.477)
    before = deepcopy(items)
    a, b = density_alarm_checks(items, NOW)
    assert a["status"] == "abnormal" and a["deviation"] == .48
    assert a["actual"] == 1 and a["setpoint"] == 1.48
    assert "A303" in a["message"] and "0.480" in a["message"]
    assert b["status"] == "normal" and b["deviation"] == .003
    assert items == before


@pytest.mark.parametrize("actual,target,status", [(1.6,1.3,"normal"), (1.18,1.48,"normal"),
    (1.60001,1.3,"abnormal"), (1.17999,1.48,"abnormal"), (1.477,1.48,"normal"), (0,1.48,"abnormal")])
def test_strict_absolute_threshold_and_float_boundary(actual,target,status):
    assert density_alarm_checks(pair(actual=actual,target=target), NOW)[0]["status"] == status


@pytest.mark.parametrize("condition", ["missing","duplicate","offline","bad","none","bool","nan",
    "future","batch","time","unit","definition","other_source"])
def test_unverifiable_pair_is_unknown_not_recovered(condition):
    items = pair()
    if condition == "missing": items.pop()
    if condition == "duplicate": items.append(deepcopy(items[0]))
    if condition == "offline": items[0]["available"] = False
    if condition == "bad": items[0]["sample"]["quality"] = "bad"
    if condition in ("none","bool","nan"):
        items[0]["sample"]["value"] = {"none":None,"bool":True,"nan":float("nan")}[condition]
    if condition == "future": items[0]["sample"]["received_at"] = (NOW+timedelta(seconds=1)).isoformat()
    if condition == "batch": items[0]["sample"]["batch_id"] = "another"
    if condition == "time": items[0]["sample"]["received_at"] = NOW.isoformat()
    if condition == "unit": items[0]["unit"] = "kg/m3"
    if condition == "definition": items[0]["definition_id"] = "v2"
    if condition == "other_source": items[0]["source_id"] = "other"
    assert all(c["status"] == "unknown" for c in density_alarm_checks(items, NOW))


def test_recovery_and_unconfigured_channels():
    assert density_alarm_checks([], NOW) == []
    assert density_alarm_checks(pair(actual=1.0), NOW)[0]["status"] == "abnormal"
    assert density_alarm_checks(pair(actual=1.477), NOW)[0]["status"] == "normal"
