"""Offline coal-response candidates. Deliberately not a production model kind.

Labels can train curves, but a prediction's gate sees only released older assays.
No database, telemetry lookup, model activation, or shared DS code is imported.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
import math

import numpy as np
from scipy.optimize import lsq_linear
from scipy.special import logsumexp

from .. import expert8 as inference
from .expert8 import fit as fit_expert8

KIND = 'gpt-coal-response-offline-v1'
TZ = timezone(timedelta(hours=8))


def instant(value):
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)


def historical_rows(rows):
    """Keep source timestamps; old midnight dates have not been independently resolved."""
    return [dict({k: v for k, v in r.items() if k != 'raw_ash'},
                 id=f"old:{r['source_row']}", sample_at=instant(r['timestamp']).isoformat(),
                 available_at=(instant(r['day'])+timedelta(days=2)).isoformat()) for r in rows]


def purge(training, validation):
    cutoff = min(instant(r['sample_at']) for r in validation)
    kept = [r for r in training if instant(r['available_at']) <= cutoff
            and instant(r['sample_at']) < cutoff]
    if not kept:
        raise ValueError('No released training labels before validation')
    return kept


def eligible(history, row, memory_days=14):
    now = instant(row['sample_at'])
    return sorted([h for h in history if h['id'] != row['id']
                   and now-timedelta(days=memory_days) <= instant(h['sample_at']) < now
                   and instant(h['available_at']) <= now], key=lambda h: (h['sample_at'], h['id']))


def date_weights(rows, normalize=True):
    counts = Counter(r['day'] for r in rows)
    weights = np.array([1/counts[r['day']] for r in rows], dtype=float)
    return weights*len(rows)/weights.sum() if normalize else weights


def decayed_weights(rows, now, half_life):
    return date_weights(rows, False)*np.array([
        2**(-(now-instant(r['sample_at'])).total_seconds()/86400/half_life) for r in rows])


def design(rows):
    return np.array([[1., (min(75, max(25, r['level']))-50)/25] for r in rows])


def correction(row, base):
    detail = inference.explain(row, base)
    if detail is None:
        raise ValueError('Invalid eight-factor record')
    return detail['correction']


def log_likelihood(errors, sigma):
    # Common Student-t constants cancel in responsibilities and gate odds.
    return -math.log(sigma)-2.5*np.log1p((errors/sigma)**2/4)


def fit_curves(rows, base, components, prior_strength):
    if components not in (1, 2, 3) or prior_strength <= 0:
        raise ValueError('Invalid response-curve experiment configuration')
    x = design(rows)
    y = np.array([r['ash_content']-correction(r, base) for r in rows])
    w = date_weights(rows)
    anchor = np.array([15.5, -7.5])
    residuals = y-x@anchor
    attempts = []
    for spread in (0.6, 1.0, 1.4):
        theta = np.tile(anchor, (components, 1))
        theta[:, 0] += spread*np.quantile(residuals, (np.arange(components)+.5)/components)
        theta[:, 0] = np.clip(theta[:, 0], 5, 35)
        sigma = max(1.5, float(np.std(residuals)))
        mixing = np.ones(components)/components
        converged = False
        for iteration in range(120):
            errors = y[:, None]-x@theta.T
            logs = log_likelihood(errors, sigma)+np.log(mixing)
            resp = np.exp(logs-logsumexp(logs, axis=1)[:, None])
            precision = 5/(4+(errors/sigma)**2)
            old = np.r_[theta.flatten(), sigma, mixing]
            for k in range(components):
                root_w = np.sqrt(w*resp[:, k]*precision[:, k])
                a = np.vstack((root_w[:, None]*x, math.sqrt(prior_strength)*np.eye(2)))
                b = np.r_[root_w*y, math.sqrt(prior_strength)*anchor]
                solution = lsq_linear(a, b, bounds=([5, -15], [35, 0]), tol=1e-10)
                if not solution.success:
                    raise ValueError('Response curve fit failed')
                theta[k] = solution.x
            errors = y[:, None]-x@theta.T
            # The same scale is used by all components; no tiny-variance class can capture a single outlier.
            penalized_sse = np.sum(w[:, None]*resp*precision*errors**2)+prior_strength*np.sum((theta-anchor)**2)
            sigma = max(1.5, math.sqrt(float(penalized_sse/w.sum())))
            mixing = (np.sum(w[:, None]*resp, axis=0)+2)/(w.sum()+2*components)
            if np.max(abs(np.r_[theta.flatten(), sigma, mixing]-old)) < 1e-5:
                converged = True
                break
        logs = log_likelihood(y[:, None]-x@theta.T, sigma)+np.log(mixing)
        score = float(-w@logsumexp(logs, axis=1)+.5*prior_strength*np.sum((theta-anchor)**2)/sigma**2
                      -2*np.log(mixing).sum())
        attempts.append((score, theta.copy(), mixing.copy(), sigma, converged, iteration+1))
    settled = [a for a in attempts if a[4]]
    score, theta, mixing, sigma, converged, iterations = min(settled or attempts, key=lambda a: a[0])
    order = np.argsort(theta[:, 0])
    theta, mixing = theta[order], mixing[order]
    logs = log_likelihood(y[:, None]-x@theta.T, sigma)+np.log(mixing)
    assignments = np.argmax(logs, axis=1)
    support = []
    for k in range(components):
        selected = [r for i, r in enumerate(rows) if assignments[i] == k]
        levels = [r['level'] for r in selected]
        bounds = [min(levels), max(levels)] if levels else [None, None]
        span = max(levels)-min(levels) if levels else 0
        days = len({r['day'] for r in selected})
        support.append(dict(rows=len(selected), days=days, level_range=bounds,
                            supported=len(selected) >= 15 and days >= 6 and span >= 15))
    return dict(kind=KIND, components=components, prior_strength=prior_strength,
                curves=theta.tolist(), mixing=mixing.tolist(), sigma=sigma, support=support,
                identifiable=converged and all(s['supported'] for s in support), converged=converged,
                iterations=iterations, objective=score, fit_n=len(rows))


def curve_values(row, base, model):
    return (design([row])@np.array(model['curves']).T)[0]+correction(row, base)


def bias_prediction(row, base, history, half_life, cap):
    prior = inference.predict(row, base)
    known = eligible(history, row)
    weights = decayed_weights(known, instant(row['sample_at']), half_life)
    residuals = np.array([np.clip(h['ash_content']-inference.predict(h, base), -6, 6) for h in known])
    shift = float(np.clip(weights@residuals/(3+weights.sum()), -cap, cap))
    return dict(value=prior+shift, shift=shift, history_ids=[h['id'] for h in known],
                history_n=len(known), fallback='EXPERT8' if not known else None)


def response_prediction(row, base, model, single_curve, history):
    """No ash_content access on row; every assay likelihood uses this frozen base/model."""
    fallback = float(curve_values(row, base, single_curve)[0])
    if model['components'] == 1:
        return dict(value=fallback, history_ids=[], type=None, reason='single_curve', fallback_base=None)
    known = eligible(history, row)
    weights = decayed_weights(known, instant(row['sample_at']), 3)
    logs = np.log(model['mixing'])
    if known:
        likelihoods = np.array([log_likelihood(h['ash_content']-curve_values(h, base, model), model['sigma'])
                                for h in known])
        logs = logs+weights@likelihoods
    posterior = np.exp(logs-logsumexp(logs))
    best = int(np.argmax(posterior))
    levels = [h['level'] for h in known]
    bounds = model['support'][best]['level_range']
    reason = None
    if not model['converged']:
        reason = 'curve_fit_not_converged'
    elif not model['identifiable']:
        reason = 'training_components_not_supported'
    elif len(known) < 3:
        reason = 'fewer_than_3_released_assays_in_14_days'
    elif max(levels)-min(levels) < 10:
        reason = 'recent_assay_level_span_below_10'
    elif float(max(posterior)) < .75:
        reason = 'posterior_below_0.75'
    elif not bounds[0] <= row['level'] <= bounds[1] or not 25 <= row['level'] <= 75:
        reason = 'outside_selected_response_level_support'
    return dict(value=fallback if reason else float(posterior@curve_values(row, base, model)),
                type=None if reason else best+1, posterior=posterior.tolist(), reason=reason,
                fallback_base='K1' if reason else None, history_ids=[h['id'] for h in known],
                history_n=len(known))


def fit_bundle(rows, configs):
    base = fit_expert8(rows, .1)
    models = {c['name']: fit_curves(rows, base, c['k'], c['prior'])
              for c in configs if c['family'] in ('K1', 'K2', 'K3')}
    if any(m['components'] == 1 and not m['converged'] for m in models.values()):
        raise ValueError('Single-curve fallback did not converge; stop evaluation')
    return dict(kind=KIND, base=base, curves=models,
                training_ids=[r['id'] for r in rows],
                training_max_available_at=max(r['available_at'] for r in rows),
                training_mean=float(np.mean([r['ash_content'] for r in rows])))


def evaluate(rows, bundle, configs, history, fallback_name):
    result = {}
    for config in configs:
        name, family = config['name'], config['family']
        predictions = []
        for r in rows:
            if family == 'EXPERT8':
                detail = dict(value=inference.predict(r, bundle['base']), history_ids=[])
            elif family == 'mean':
                detail = dict(value=bundle['training_mean'], history_ids=[])
            elif family == 'bias':
                detail = bias_prediction(r, bundle['base'], history, config['half_life'], config['cap'])
            else:
                single = bundle['curves'][name if family == 'K1' else fallback_name]
                detail = response_prediction(r, bundle['base'], bundle['curves'][name], single, history)
            predictions.append(dict(id=r['id'], sample_at=r['sample_at'], day=r['day'],
                                    level=r['level'], measured=r['ash_content'], **detail))
        result[name] = dict(metrics=metrics(predictions), predictions=predictions)
    return result


def metrics(predictions):
    errors = np.array([p['value']-p['measured'] for p in predictions])
    days = sorted({p['day'] for p in predictions})
    return dict(n=len(predictions), mae=float(np.mean(abs(errors))), rmse=float(np.sqrt(np.mean(errors**2))),
                bias=float(np.mean(errors)), day_balanced_mae=float(np.mean([
                    np.mean([abs(p['value']-p['measured']) for p in predictions if p['day'] == d]) for d in days])),
                classified_n=sum(p.get('type') is not None for p in predictions),
                fallback_n=sum(p.get('fallback_base') == 'K1' for p in predictions))
