"""Read-only, whitelisted comparison artifact. No database/model deserialization."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal

SUMMARY_PATH = Path(__file__).resolve().parents[2] / 'data/gpt_coarse_model_comparison.json'
PHASES = ('history_all', 'history_forward', 'future')


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, strict=True)


class DateRange(ClosedModel):
    start: str | None = None
    end: str | None = None

    @model_validator(mode='after')
    def ordered(self):
        first = date.fromisoformat(self.start) if self.start else None
        last = date.fromisoformat(self.end) if self.end else None
        if first and last and first > last:
            raise ValueError('date range is reversed')
        return self


class Score(ClosedModel):
    mae: float | None = None
    rmse: float | None = None
    bias: float | None = None
    r2: float | None = None
    hit1: int | None = None
    n: int = Field(default=0, ge=0)
    hit1_rate: float | None = None
    eligible_n: int | None = Field(default=None, ge=0)
    coverage: float | None = Field(default=None, ge=0, le=1)
    date_range: DateRange | None = None

    @model_validator(mode='after')
    def consistent(self):
        if self.n == 0:
            if any(x is not None for x in (self.mae, self.rmse, self.bias, self.r2, self.hit1, self.hit1_rate)):
                raise ValueError('unmeasured score must be null')
        elif (self.mae is None or self.mae < 0 or self.rmse is None or self.rmse < 0 or self.bias is None or self.hit1 is None
              or not 0 <= self.hit1 <= self.n or self.hit1_rate is None
              or not math.isclose(self.hit1_rate, self.hit1 / self.n, abs_tol=1e-10)):
            raise ValueError('inconsistent measured score')
        if self.r2 is not None and (self.n < 2 or self.r2 > 1):
            raise ValueError('invalid R squared')
        if self.eligible_n is not None:
            if self.n > self.eligible_n:
                raise ValueError('predictions exceed eligible targets')
            expected = self.n / self.eligible_n if self.eligible_n else None
            if (self.coverage is None) != (expected is None) or (expected is not None and not math.isclose(self.coverage, expected, abs_tol=1e-10)):
                raise ValueError('coverage denominator mismatch')
        elif self.coverage is not None:
            raise ValueError('coverage requires explicit eligible denominator')
        return self


class SystemScore(Score):
    normal: Score | None = None
    audited: Score | None = None
    review: Score | None = None
    train_date_range: DateRange | None = None
    fold_count: int = Field(default=0, ge=0)
    fold_description: str = ''
    training_note: str = ''
    status: Literal['available', 'missing', 'pending', 'unavailable'] = 'missing'
    reason_counts: dict[str, int] = Field(default_factory=dict)
    baseline_id: str | None = None
    baseline_label: str | None = None

    @model_validator(mode='after')
    def normal_count(self):
        if self.normal and self.normal.n > self.n:
            raise ValueError('normal count exceeds all count')
        if self.audited is not None and self.review is not None and self.audited.n + self.review.n != self.n:
            raise ValueError('audited/review counts must account for the measured pool')
        return self


class Metric(SystemScore):
    note: str = ''
    by_system: dict[Literal['0', '1', '2', '3', '4', 'unknown'], SystemScore] = Field(default_factory=dict)
    labelled_n: int | None = Field(default=None, ge=0)
    pending_label_n: int = Field(default=0, ge=0)
    attempted_n: int | None = Field(default=None, ge=0)
    prediction_n: int | None = Field(default=None, ge=0)
    prediction_coverage: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode='after')
    def availability(self):
        if (self.status == 'available') != (self.n > 0):
            raise ValueError('metric status disagrees with measured denominator')
        if self.n and sum(v.n for v in self.by_system.values()) != self.n:
            raise ValueError('system counts disagree')
        if self.normal and self.normal.n > self.n:
            raise ValueError('normal count exceeds all count')
        if self.labelled_n is not None and self.attempted_n != self.labelled_n + self.pending_label_n:
            raise ValueError('future attempted count must disclose pending labels')
        if self.prediction_n is not None:
            if not self.attempted_n or self.prediction_n > self.attempted_n:
                raise ValueError('invalid forecast availability count')
            if self.prediction_coverage is None or not math.isclose(self.prediction_coverage,self.prediction_n/self.attempted_n,abs_tol=1e-10):
                raise ValueError('forecast coverage denominator mismatch')
        return self


class Metrics(ClosedModel):
    history_all: Metric = Field(default_factory=Metric)
    history_forward: Metric = Field(default_factory=Metric)
    future: Metric = Field(default_factory=lambda: Metric(status='pending'))


class MethodDetail(ClosedModel):
    summary: str
    details: list[str] = Field(default_factory=list)


class Method(MethodDetail):
    by_system: dict[Literal['2', '3'], MethodDetail] = Field(default_factory=dict)


class Model(ClosedModel):
    id: str
    name: str
    version: str | None = None
    status: Literal['frozen', 'provisional', 'accepted', 'missing'] = 'missing'
    method: Method
    training_date_range: DateRange | None = None
    metrics: Metrics = Field(default_factory=Metrics)


class Models(ClosedModel):
    current: Model
    candidate: Model


class ScopeCount(ClosedModel):
    total_n: int = Field(default=0, ge=0)
    evaluable_n: int = Field(default=0, ge=0)
    audited_n: int = Field(default=0, ge=0)
    review_n: int = Field(default=0, ge=0)
    rejected_n: int = Field(default=0, ge=0)
    reasons: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode='after')
    def counts(self):
        if self.total_n != self.evaluable_n + self.rejected_n or self.evaluable_n != self.audited_n + self.review_n:
            raise ValueError('scope must account for all rows')
        if any(v < 0 for v in self.reasons.values()):
            raise ValueError('reason counts must be nonnegative')
        return self


class Scope(ScopeCount):
    description: str = ''
    data_cutoff: str | None = None
    by_system: dict[Literal['2', '3', 'unknown'], ScopeCount] = Field(default_factory=dict)

    @model_validator(mode='after')
    def system_counts(self):
        if self.total_n and not self.by_system:
            raise ValueError('scope must disclose per-system denominators')
        for key in ('total_n', 'evaluable_n', 'audited_n', 'review_n', 'rejected_n'):
            if sum(getattr(v, key) for v in self.by_system.values()) != getattr(self, key):
                raise ValueError('per-system scope counts disagree')
        return self


class PoolCount(ClosedModel):
    total_n: int = Field(ge=0)
    normal_n: int | None = Field(default=None, ge=0)
    audited_n: int | None = Field(default=None, ge=0)
    review_n: int | None = Field(default=None, ge=0)

    @model_validator(mode='after')
    def denominators(self):
        if any(value is not None and value > self.total_n for value in (self.normal_n,self.audited_n,self.review_n)):
            raise ValueError('subgroup denominator exceeds all targets')
        return self


class EvaluationPool(PoolCount):
    by_system: dict[Literal['0', '1', '2', '3', '4', 'unknown'], PoolCount] = Field(default_factory=dict)
    description: str = ''
    fold_description: str = ''
    train_date_range: DateRange | None = None
    eval_date_range: DateRange | None = None
    candidate_train_date_range: DateRange | None = None
    pending_label_n: int = Field(default=0, ge=0)
    prediction_available_n: dict[Literal['current', 'candidate'], int] = Field(default_factory=dict)
    baseline_id: str | None = None
    baseline_label: str | None = None
    candidate_baseline_id: str | None = None
    candidate_baseline_label: str | None = None
    training_note: str = ''

    @model_validator(mode='after')
    def system_denominators(self):
        if self.by_system and sum(value.total_n for value in self.by_system.values()) != self.total_n:
            raise ValueError('evaluation system denominator sum mismatch')
        if any(n < 0 or n > self.total_n + self.pending_label_n for n in self.prediction_available_n.values()):
            raise ValueError('prediction count exceeds labelled plus pending targets')
        return self


class CommonScore(ClosedModel):
    status: Literal['pending', 'available', 'unavailable'] = 'pending'
    n: int = Field(default=0, ge=0)
    eligible_n: int | None = Field(default=None, ge=0)
    coverage: float | None = Field(default=None, ge=0, le=1)
    current: Score = Field(default_factory=Score)
    candidate: Score = Field(default_factory=Score)

    @model_validator(mode='after')
    def same_pool(self):
        if self.current.n != self.n or self.candidate.n != self.n:
            raise ValueError('common comparison requires identical targets')
        if self.current.eligible_n != self.eligible_n or self.candidate.eligible_n != self.eligible_n:
            raise ValueError('common comparison denominator mismatch')
        if self.current.coverage != self.coverage or self.candidate.coverage != self.coverage:
            raise ValueError('common comparison coverage mismatch')
        if (self.status == 'available') != (self.n > 0):
            raise ValueError('common comparison status mismatch')
        return self


class FutureComparison(CommonScore):
    by_system: dict[Literal['0', '1', '2', '3', '4', 'unknown'], CommonScore] = Field(default_factory=dict)
    pending_label_n: int = Field(default=0, ge=0)
    baseline_id: str | None = None
    baseline_label: str | None = None
    candidate_baseline_id: str | None = None
    candidate_baseline_label: str | None = None
    note: str = '仅双方均有有效预测及真实标签的交集用于配对比较；各自全覆盖成绩见模型各自future。'


class Freeze(ClosedModel):
    status: Literal['missing', 'provisional', 'accepted'] = 'missing'
    model_version: str | None = None
    model_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    freeze_manifest_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    sealed_runtime_manifest_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    final_seal_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    sealed_at: str | None = None
    due_at: str | None = None

    @model_validator(mode='after')
    def accepted(self):
        if self.status == 'accepted' and not all((self.model_version, self.model_sha256, self.sealed_at, self.due_at)):
            raise ValueError('accepted freeze requires real seal/due/hash/version')
        if self.status != 'accepted' and (self.sealed_at or self.due_at):
            raise ValueError('provisional freeze has no genuine seal timestamp')
        if self.status == 'accepted':
            if parse_time(self.due_at) <= parse_time(self.sealed_at):
                raise ValueError('due must follow seal')
        return self


class Comparison(ClosedModel):
    schema_version: Literal[1] = 1
    generated_at: str | None = None
    valid_until: str | None = None
    status: Literal['ready', 'research', 'missing', 'invalid', 'stale']
    reason: str = ''
    scope: Scope = Field(default_factory=Scope)
    freeze: Freeze = Field(default_factory=Freeze)
    models: Models
    future_comparison: FutureComparison = Field(default_factory=FutureComparison)
    artifact_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='after')
    def paired(self):
        for phase in PHASES:
            left, right = (getattr(m.metrics, phase) for m in (self.models.current, self.models.candidate))
            if phase == 'future':
                if left.eligible_n != right.eligible_n or left.pending_label_n != right.pending_label_n:
                    raise ValueError('future scoring pools must disclose the same revealed target denominator')
                continue
            if left.n != right.n or left.by_system.keys() != right.by_system.keys():
                raise ValueError('models must share evaluation pool')
            if (left.normal.n if left.normal else 0) != (right.normal.n if right.normal else 0):
                raise ValueError('normal evaluation pools differ')
            for cohort in ('audited', 'review'):
                if (getattr(left, cohort).n if getattr(left, cohort) else 0) != (getattr(right, cohort).n if getattr(right, cohort) else 0):
                    raise ValueError('cohort evaluation pools differ')
            for system in left.by_system:
                if left.by_system[system].n != right.by_system[system].n:
                    raise ValueError('system pools differ')
            if left.eligible_n != right.eligible_n or left.coverage != right.coverage:
                raise ValueError('coverage pools differ')
            if phase == 'history_all' and left.n:
                expected = left.n / self.scope.total_n
                if any(m.coverage is None or not math.isclose(m.coverage, expected, abs_tol=1e-10) for m in (left, right)):
                    raise ValueError('coverage must disclose full history denominator')
            if phase == 'history_all' and left.n:
                if left.n != self.scope.evaluable_n or left.audited is None or left.review is None:
                    raise ValueError('history_all must disclose audited and review cohorts')
                if left.audited.n != self.scope.audited_n or left.review.n != self.scope.review_n:
                    raise ValueError('history cohort counts disagree with scope')
                for system in ('2', '3'):
                    group, count = left.by_system.get(system), self.scope.by_system.get(system)
                    if count and count.evaluable_n:
                        if group is None or group.n != count.evaluable_n or group.audited is None or group.review is None:
                            raise ValueError('history system cohort incomplete')
                        if group.audited.n != count.audited_n or group.review.n != count.review_n:
                            raise ValueError('system cohort counts disagree with scope')
        if (self.models.current.metrics.future.n or self.models.candidate.metrics.future.n) and self.freeze.status != 'accepted':
            raise ValueError('future scores require accepted seal')
        if self.future_comparison.n > min(self.models.current.metrics.future.n,self.models.candidate.metrics.future.n):
            raise ValueError('common pool exceeds independently scored model coverage')
        if self.status == 'ready' and self.freeze.status != 'accepted':
            raise ValueError('ready requires accepted seal')
        for value in (self.generated_at, self.valid_until, self.scope.data_cutoff):
            if value:
                parse_time(value)
        return self


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    return parsed.astimezone(timezone.utc)


def digest(payload: dict) -> str:
    unsigned = {k: v for k, v in payload.items() if k != 'artifact_sha256'}
    return hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def unavailable(status: str, reason: str) -> dict:
    models = Models(current=Model(id='EX8', name='现用模型', method=Method(summary='模型说明尚未加载')),
                    candidate=Model(id='EX81', name='候选模型', method=Method(summary='候选证据尚未加载')))
    return Comparison(status=status, reason=reason, models=models).model_dump()


def load_comparison() -> dict:
    """Only the fixed packaged artifact is read; invalid files never leak contents."""
    try:
        if SUMMARY_PATH.stat().st_size > 256_000:
            raise ValueError('oversized artifact')
        raw = json.loads(SUMMARY_PATH.read_text(encoding='utf-8'))
        result = Comparison.model_validate(raw)
        if result.artifact_sha256 != digest(result.model_dump()):
            raise ValueError('artifact hash mismatch')
        if result.valid_until and parse_time(result.valid_until) <= datetime.now(timezone.utc):
            return unavailable('stale', '比较证据已过有效期，请重新导出经核验的汇总。')
        return result.model_dump()
    except FileNotFoundError:
        return unavailable('missing', '比较证据尚未导出。')
    except (OSError, ValueError, TypeError, ZeroDivisionError):
        return unavailable('invalid', '比较证据格式或完整性校验失败。')
