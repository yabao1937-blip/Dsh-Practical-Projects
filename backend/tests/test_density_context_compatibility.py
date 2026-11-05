"""Branch activation is evidence; fixed rules remain protected configuration."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.decision_hourly import quality_advice as qa
from app.decision_hourly.density_candidates import current as candidate_current
from app.decision_hourly.density_series import context_compatibility
from app.decision_hourly.models import DecisionCycleContext, DecisionCycleHour, GptHourlyDensityAnchor
from app.decision_hourly.service import read_hour
from app.decision_hourly.statistics import digest
from app.telemetry.domain import iso_time
from test_hourly_quality_advice import prepared, sessions, seeded, HOUR, ASH_START


def definition():
    return dict(policy='decision-hourly-v2', model=dict(id='frozen-model'),
        model_definition=dict(coefficients=[1, 2]), sources=dict(density='A304'),
        density_guide=dict(maxStep=.03), target=dict(min=8.5, max=9),
        point_definitions={'density':dict(definition_id='same', unit='g/cm³')},
        input_derivations=dict(coarse_amount=dict(version='wash-coarse-ratio-v2', divisor=24)))


def two_system(context, level=60):
    result = deepcopy(context)
    result['input_derivations']['coarse_ash'] = dict(qa.LEGACY_TWO_SYSTEM_ASH, level_observed=level)
    return result


@pytest.mark.parametrize('left,right', [('normal','two'), ('two','normal'), ('two','two')])
def test_branch_and_level_changes_reuse_fixed_contract_without_mutation(left, right):
    a, b = definition(), definition()
    if left == 'two':
        a = two_system(a, 64.2361068725586)
    if right == 'two':
        b = two_system(b, 70.57292175292969)
    before = deepcopy((a, b))
    assert qa.compatible_contexts(None, [], a, [b]) == (True, '')
    assert (a, b) == before


@pytest.mark.parametrize('key,value', [('version','unknown-v2'), ('input','other'),
    ('running_systems',3), ('level_threshold',31), ('above',13), ('below',16),
    ('factors','different'), ('unknown_parameter',1), ('level_other_observed',42)])
def test_all_fixed_and_unknown_rule_keys_remain_protected(key, value):
    current, historical = definition(), two_system(definition())
    historical['input_derivations']['coarse_ash'][key] = value
    assert not qa.compatible_contexts(None, [], current, [historical])[0]


@pytest.mark.parametrize('key,value', [('model',{'id':'other'}),
    ('model_definition',{'coefficients':[1,3]}), ('sources',{'density':'A303'}),
    ('density_guide',{'maxStep':.01}),
    ('point_definitions',{'density':{'definition_id':'different','unit':'g/cm³'}}),
    ('input_derivations',{'coarse_amount':{'version':'changed'}})])
def test_other_configuration_protections_are_unchanged(key, value):
    historical = two_system(definition())
    historical[key] = value
    assert not qa.compatible_contexts(None, [], definition(), [historical])[0]


@pytest.mark.parametrize('value', [None, {}, [], 'unknown'])
def test_missing_or_invalid_derivation_evidence_is_not_compatible(value):
    historical = definition()
    historical['input_derivations'] = value
    assert not qa.compatible_contexts(None, [], definition(), [historical])[0]
    assert not qa.compatible_contexts(None, [], historical, [definition()])[0]


@pytest.mark.parametrize('value', [None, {}, [], 'unknown'])
def test_present_but_invalid_coarse_ash_contract_is_not_legacy_absence(value):
    historical = definition()
    historical['input_derivations']['coarse_ash'] = value
    assert not qa.compatible_contexts(None, [], definition(), [historical])[0]


def test_missing_context_row_or_hour_evidence_still_blocks():
    missing = SimpleNamespace(get=lambda *args:None)
    assert not qa.compatible_contexts(missing, [set()], definition())[0]
    assert not qa.compatible_contexts(missing, [{'missing'}], definition())[0]
    assert not qa.compatible_contexts(None, [], definition(), [None])[0]


def test_series_uses_same_compatibility_without_altering_original_keys():
    a, b = definition(), two_system(definition())
    cache = dict(contexts={'original-key':a}, anchors={iso_time(HOUR):dict(context=b)})
    before = deepcopy(cache)
    assert context_compatibility(None, dict(context_key='original-key', sampled_at=iso_time(HOUR)), cache) == (True, '')
    assert cache == before


def test_fixed_0955_case_unblocks_reference_and_candidate_without_writing_history(sessions, prepared, monkeypatch):
    """09:55 production values: mixed 57+3 branches, ash inside .05 deadband."""
    current, settlement, base, _ = prepared
    monkeypatch.setenv('DMCS_DENSITY_EXPERIMENT', '1')
    settlement['item'].update(value=8.450800091947093, coverage=.9124105555555555)
    current['inputs']['density']['value'] = 1.5145833492279053
    current['execution_basis']['value'] = 1.5099999904632568
    for part in current['execution_basis']['components'].values():
        part['value'] = 1.5099999904632568
    with sessions.begin() as db:
        anchor = db.get(GptHourlyDensityAnchor, iso_time(HOUR))
        anchor.snapshot = {**anchor.snapshot, 'basis':deepcopy(current['execution_basis'])}
        original = db.get(DecisionCycleContext, base['context_key']).definition
        contexts = [original] + [two_system(original, v) for v in
            (64.2361068725586, 66.89814758300781, 70.57292175292969)]
        keys = []
        for context in contexts:
            key = digest(context)
            if not db.get(DecisionCycleContext, key):
                db.add(DecisionCycleContext(key=key, definition=context))
            keys.append(key)
        db.get(DecisionCycleHour, iso_time(ASH_START)).snapshot = {**base,
            'evidence':dict(segments=[dict(state=dict(context_key=k)) for k in keys])}
    with sessions() as db:
        before = list(db.connection().connection.driver_connection.iterdump())
        with monkeypatch.context() as old:
            # Exact old definition comparison: no branch/observation normalization.
            old.setattr(qa, 'comparison_definition', lambda d:{k:v for k,v in d.items()
                if k not in {'target', 'point_definitions'}})
            blocked = read_hour(db, HOUR, current)
            assert blocked['suggested_density']['value'] is None
            assert 'configuration' in blocked['advice_blockers']
        allowed = read_hour(db, HOUR, current)
        assert allowed['suggested_density']['value'] == 1.510
        assert allowed['advice_blockers'] == []
        result = candidate_current(db, views=(current, allowed, HOUR))
        assert result['candidate']['value'] == 1.510
        assert result['candidate']['status'] == 'hold'
        assert result['candidate']['reason_codes'] == ['target_band']
        assert list(db.connection().connection.driver_connection.iterdump()) == before
