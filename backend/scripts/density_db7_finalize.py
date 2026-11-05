"""Local-only derivation/diagnostics from frozen DB7 inputs; no database access."""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path

from app.decision_hourly.density_series import with_gap_markers,plot_indices,channel_points
from app.gpt_density.candidate import timestamp


def write(path,value):
    raw=json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
    temporary=path.with_suffix('.tmp')
    temporary.write_bytes(raw)
    temporary.replace(path)
    return dict(path=path.name,sha256=sha256(raw).hexdigest(),bytes=len(raw))


def main():
    root=Path(__file__).resolve().parents[2]/'output/density-optimization-db7-20261104'
    result=json.loads((root/'series.json').read_text(encoding='utf-8'))
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    advice=json.loads((root/'raw/advice-revisions.json').read_text(encoding='utf-8'))
    known={p['at']:p for p in advice}
    points=[]
    for artifact in manifest['artifacts']:
        if artifact['path'].startswith('recalculated-'):
            raw=(root/'raw'/artifact['path']).read_bytes()
            if sha256(raw).hexdigest()!=artifact['sha256']:
                raise ValueError('Immutable replay artifact changed')
            points.extend(json.loads(raw))
    points=with_gap_markers(points)
    reasons=Counter()
    anchor_future=[]
    for p in points:
        ref=known.get(p['source'].get('reference_known_at'))
        if ref:
            captured=timestamp((ref.get('basis') or {}).get('captured_at'))
            if captured is None or captured>timestamp(p['at']):
                anchor_future.append(p['slot'])
        if 'reference_unavailable' in p['reason_codes']:
            if ref and ref.get('value') is None:
                key='+'.join(ref.get('blockers') or []) or 'original-missing'
            else:
                key='context-protection-on-formerly-valid-reference'
            reasons[key]+=1
    if anchor_future:
        raise ValueError('Historical basis was not yet captured: '+str(anchor_future[:5]))
    selected=[points[i] for i in plot_indices(points)]
    gaps=[dict(at=p['at'],slot=p['slot'],seconds=p['sample_gap_seconds']) for p in points if p['break_before']]
    diagnostics=dict(reference_unavailable=dict(reasons),future_anchor_count=0,
        reference_unavailable_meaning='缺少当时可用计算依据或被来源/参数保护，不等同数据库无数据',
        real_sample_gaps=gaps,gap_detection='before downsampling: > max(15 seconds, 1.5 * recorded cadence)',
        unaltered_rule_sha256=result['source']['candidate_sha256'])
    result.update(suggestions=selected,channels=dict(A304=channel_points(selected,'setpoint'),actual=channel_points(selected,'actual')),
        diagnostics=diagnostics)
    result['summary']['plot_samples']=len(selected)
    result['source']['downsampling']='real-point extrema, status/quality boundaries and explicit original sample-gap edges; no interpolation'
    result['source']['actual_plot_policy']='validated actual_reading only; invalid raw values retained for audit, never drawn as valid'
    manifest.update(series=write(root/'series.json',result),summary=result['summary'],diagnostics=diagnostics)
    write(root/'manifest.json',manifest)
    write(root/'backend/diagnostics.json',diagnostics)
    print(json.dumps(dict(plot_samples=len(selected),gap_count=len(gaps),reference_unavailable=dict(reasons),
        future_anchor_count=0,series_sha256=manifest['series']['sha256']),ensure_ascii=False))


if __name__=='__main__':
    main()
