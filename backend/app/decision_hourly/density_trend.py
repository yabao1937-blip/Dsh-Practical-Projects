"""Small indexed evidence queries; no minute-history scans or invented advice."""
import json
from bisect import bisect_right
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select, text

from ..hourly_brief.inputs import business_time, numeric
from ..telemetry.domain import iso_time
from .models import DensityAction, GptHourlyDensityAnchor, GptHourlyDensityAdvice

MAX_RECORDS = 8000


def period(now, hours, start, end):
    if start is None and end is None:
        return now-timedelta(hours=hours), now
    if start is None or end is None:
        raise HTTPException(422, '自定义范围请同时提供开始和结束时间')
    if any(t.tzinfo is None or t.second or t.microsecond for t in (start, end)):
        raise HTTPException(422, '时间须包含时区，最小单位为分钟')
    if end <= start or end-start > timedelta(days=90):
        raise HTTPException(422, '结束须晚于开始，自定义范围最长 90 天')
    end = min(end, now)
    if end <= start:
        raise HTTPException(422, '所选范围尚未发生')
    return start, end


def bounded(db, statement):
    rows = list(db.execute(statement.limit(MAX_RECORDS+1)))
    if len(rows) > MAX_RECORDS:
        raise HTTPException(422, '此范围记录较多，请缩小时间范围后查看，避免遗漏调密点')
    return rows


def compare_actions(actions, suggestions):
    """Compare with advice that existed at the post-adjustment observation."""
    by_hour = {}
    for point in suggestions:
        hour = iso_time(business_time(point['apply_start']))
        times, points = by_hour.setdefault(hour, ([], []))
        times.append(iso_time(business_time(point['at'])))
        points.append(point)
    for action in actions:
        observed = business_time(action['after_at'] or action['at'])
        action['comparison_at'] = iso_time(observed)
        hour = iso_time(observed.replace(minute=0, second=0, microsecond=0))
        times, points = by_hour.get(hour, ([], []))
        index = bisect_right(times, iso_time(observed))-1
        point = points[index] if index >= 0 else None
        action['hourly_advice'] = None if point is None else dict(
            value=point['value'] if numeric(point.get('value')) else None,
            generated_at=point['at'], apply_start=point['apply_start'],
            status=point.get('status'), reason=point.get('reason'))
        action['difference'] = (round(action['after']-point['value'], 6)
            if point and numeric(point.get('value')) and numeric(action['after']) else None)


def read(db, now, hours=168, start=None, end=None):
    start, end = period(now, hours, start, end)
    lower, upper = iso_time(start), iso_time(end)
    channels = {'A304':[]}
    # Components' keys and numbers survive compact metadata storage; ItemIDs
    # are deliberately not used to guess one channel for the whole range.
    anchors = bounded(db, select(GptHourlyDensityAnchor.start, GptHourlyDensityAnchor.captured_at,
        GptHourlyDensityAnchor.snapshot['basis']['components'].as_string()).where(
        GptHourlyDensityAnchor.start >= iso_time(start.replace(minute=0, second=0, microsecond=0)),
        GptHourlyDensityAnchor.start <= upper).order_by(GptHourlyDensityAnchor.start))
    for apply_start, captured, raw in anchors:
        parts = json.loads(raw) if isinstance(raw, str) else raw or {}
        for channel, part in parts.items():
            at = part.get('observed_at') or captured
            if channel in channels and lower <= iso_time(business_time(at)) <= upper and numeric(part.get('value')):
                channels[channel].append(dict(at=at, value=part['value'], origin='anchor',
                    apply_start=apply_start, break_before=False))
    e = DensityAction.evidence
    actions = []
    for identifier, detected, channel, before, after, before_at, after_at, continuous in bounded(db, select(
            DensityAction.id, DensityAction.detected_at, DensityAction.channel,
            e['before']['pair']['setpoint']['value'].as_float(), e['after']['pair']['setpoint']['value'].as_float(),
            e['before']['sampled_at'].as_string(), e['after']['sampled_at'].as_string(),
            e['continuous'].as_boolean()).where(DensityAction.detected_at >= lower,
            DensityAction.detected_at <= upper, DensityAction.channel == 'A304').order_by(DensityAction.detected_at, DensityAction.id)):
        actions.append(dict(id=identifier, at=detected, channel=channel, before=before, after=after,
                            before_at=before_at, after_at=after_at, continuous=bool(continuous)))
        if channel not in channels:
            continue
        # Never fabricate a before timestamp. Legacy evidence without one still
        # contributes its observed after value at detection time.
        for at, value, origin in ((before_at, before, 'before'), (after_at or detected, after, 'change')):
            if at and lower <= iso_time(business_time(at)) <= upper and numeric(value):
                channels[channel].append(dict(at=at, value=value, origin=origin, action_id=identifier,
                    break_before=origin == 'change' and not continuous))
    for key, points in channels.items():
        unique = {}
        for point in sorted(points, key=lambda p:business_time(p['at'])):
            key_at = (iso_time(business_time(point['at'])), point['value'])
            old = unique.get(key_at)
            if old:
                chosen = old if old['origin'] == 'change' else point
                point = dict(chosen, break_before=old['break_before'] or point['break_before'])
            unique[key_at] = point
        channels[key] = list(unique.values())
    available = bool(db.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gpt_hourly_density_advice'")).first())
    suggestions = []
    comparison_advice = []
    if available:
        # Minute-range table rows still need earlier advice from their own
        # hour. Fetch a bounded indexed context without adding it to the plot.
        context_start = business_time(min([start] + [business_time(a['after_at'] or a['at']) for a in actions]))
        context_lower = iso_time(context_start.replace(minute=0, second=0, microsecond=0))
        for row in bounded(db, select(GptHourlyDensityAdvice.generated_at, GptHourlyDensityAdvice.apply_start,
                GptHourlyDensityAdvice.snapshot).where(GptHourlyDensityAdvice.generated_at >= context_lower,
                GptHourlyDensityAdvice.generated_at <= upper).order_by(GptHourlyDensityAdvice.generated_at, GptHourlyDensityAdvice.id)):
            point = dict(at=row[0], apply_start=row[1], **row[2])
            if (point.get('basis') or {}).get('channels') != ['A304']:
                point.update(value=None, status='different_basis', reason='该时段不是 A304 独立设定基准')
            comparison_advice.append(point)
            if iso_time(business_time(point['at'])) >= lower:
                suggestions.append(point)
    compare_actions(actions, comparison_advice)
    return dict(policy='density-trend-v2', generated_at=iso_time(now),
        period=dict(start=lower, end=upper), channels=channels, suggestions=suggestions,
        actions=actions, total_actions=len(actions), archive_available=available,
        archive_note='未存档的小时建议留空；不以旧统计值代替，也不按当前参数回算历史。' if available else
            '主机尚未启用真实小时建议存档，历史建议留空；测试端不会向主机补写数据。')
