/* P-GPT standalone model. No imports from existing DS/GPT implementations. */
(function (root) {
    'use strict';
    const valid = v => typeof v === 'number' && Number.isFinite(v);
    const mean = a => a.reduce((s, v) => s + v, 0) / a.length;
    const median = a => { const s = a.slice().sort((a, b) => a - b); return s.length ? (s[Math.floor(s.length / 2)] + s[Math.floor((s.length - 1) / 2)]) / 2 : 0; };
    function aggregate(rows, mode) {
        const clean = rows.filter(r => valid(r.y) && r.y >= 0 && r.y <= 100);
        if (mode === 'sample') return clean.map(r => ({...r, count: 1}));
        if (mode !== 'day') throw new Error('未知模型粒度');
        const groups = new Map();
        clean.forEach(r => { if (!groups.has(r.day)) groups.set(r.day, []); groups.get(r.day).push(r); });
        return [...groups].sort(([a], [b]) => a.localeCompare(b)).map(([day, a]) => ({
            id: day, day, time: day, count: a.length, y: mean(a.map(r => r.y)),
            x: a[0].x.map((_, j) => { const col = a.map(r => r.x[j]).filter(valid); return col.length ? mean(col) : null; })
        }));
    }
    function solve(matrix, vector) {
        const a = matrix.map((r, i) => [...r, vector[i]]), n = a.length;
        for (let k = 0; k < n; k++) {
            let pivot = k;
            for (let i = k + 1; i < n; i++) if (Math.abs(a[i][k]) > Math.abs(a[pivot][k])) pivot = i;
            [a[k], a[pivot]] = [a[pivot], a[k]];
            if (Math.abs(a[k][k]) < 1e-12) throw new Error('模型矩阵不可解');
            const divisor = a[k][k];
            for (let j = k; j <= n; j++) a[k][j] /= divisor;
            for (let i = 0; i < n; i++) if (i !== k) {
                const factor = a[i][k];
                for (let j = k; j <= n; j++) a[i][j] -= factor * a[k][j];
            }
        }
        return a.map(r => r[n]);
    }
    function fit(rows, lambda) {
        if (rows.length < 5) throw new Error('有效训练样本不足5条');
        if (!valid(lambda) || lambda <= 0) throw new Error('正则参数必须为正数');
        const p = rows[0].x.length;
        const medians = Array.from({length: p}, (_, j) => median(rows.map(r => r.x[j]).filter(valid)));
        const filled = rows.map(r => r.x.map((v, j) => valid(v) ? v : medians[j]));
        const centers = medians.map((_, j) => mean(filled.map(r => r[j])));
        const scales = centers.map((m, j) => Math.sqrt(mean(filled.map(r => (r[j] - m) ** 2))));
        const z = filled.map(r => r.map((v, j) => scales[j] > 1e-9 ? (v - centers[j]) / scales[j] : 0));
        const intercept = mean(rows.map(r => r.y));
        const gram = centers.map((_, j) => centers.map((_, k) => mean(z.map(r => r[j] * r[k])) + (j === k ? lambda : 0)));
        const rhs = centers.map((_, j) => mean(z.map((r, i) => r[j] * (rows[i].y - intercept))));
        return {lambda, medians, centers, scales, coefficients: solve(gram, rhs), intercept,
            minimum: centers.map((_, j) => Math.min(...filled.map(r => r[j]))),
            maximum: centers.map((_, j) => Math.max(...filled.map(r => r[j]))),
            count: rows.length, start: rows[0].day, end: rows[rows.length - 1].day};
    }
    function predict(model, x) {
        if (!Array.isArray(x) || x.length !== model.centers.length) throw new Error('输入因素数量不一致');
        return model.intercept + model.coefficients.reduce((s, b, j) => s + b * (model.scales[j] > 1e-9 ? ((valid(x[j]) ? x[j] : model.medians[j]) - model.centers[j]) / model.scales[j] : 0), 0);
    }
    function metrics(y, prediction, tolerance = 0.8) {
        if (!y.length) return {n: 0, mae: null, rmse: null, r2: null, passRate: null};
        const m = mean(y), errors = y.map((v, i) => prediction[i] - v);
        const ss = y.reduce((s, v) => s + (v - m) ** 2, 0);
        const sse = errors.reduce((s, v) => s + v * v, 0);
        return {n: y.length, mae: mean(errors.map(Math.abs)), rmse: Math.sqrt(sse / y.length),
            r2: ss > 1e-12 ? 1 - sse / ss : null, passRate: mean(errors.map(v => Math.abs(v) <= tolerance ? 1 : 0))};
    }
    function select(rows) {
        const days = [...new Set(rows.map(r => r.day))].sort();
        if (days.length < 10) throw new Error('时间分块验证至少需要10个生产日');
        const initial = Math.floor(days.length / 2), width = Math.ceil((days.length - initial) / 3);
        const folds = [];
        for (let i = initial; i < days.length; i += width) {
            const testDays = days.slice(i, i + width);
            folds.push({train: rows.filter(r => r.day < testDays[0]), test: rows.filter(r => testDays.includes(r.day))});
        }
        const candidates = [0.01, 0.1, 1, 10, 100].map(lambda => {
            const actual = [], predictions = [];
            folds.forEach(f => { const m = fit(f.train, lambda); f.test.forEach(r => {actual.push(r.y); predictions.push(predict(m, r.x));}); });
            return {lambda, ...metrics(actual, predictions)};
        });
        candidates.sort((a, b) => a.rmse - b.rmse || b.lambda - a.lambda);
        return {lambda: candidates[0].lambda, validation: candidates[0], candidates,
            folds: folds.map(f => ({trainEnd: f.train.at(-1).day, testStart: f.test[0].day, testEnd: f.test.at(-1).day, trainN: f.train.length, testN: f.test.length}))};
    }
    function evaluate(raw, mode) {
        const rows = aggregate(raw, mode);
        const training = rows.filter(r => r.day >= '2026-06-01' && r.day < '2026-08-01');
        const forward = rows.filter(r => r.day >= '2026-08-01');
        const tuning = select(training), model = fit(training, tuning.lambda);
        const predictions = rows.map(r => ({id: r.id, day: r.day, y: r.y, prediction: predict(model, r.x),
            phase: r.day >= '2026-08-01' ? 'forward' : 'fit'}));
        return {mode, model, tuning, historical: metrics(training.map(r => r.y), training.map(r => predict(model, r.x))),
            forward: metrics(forward.map(r => r.y), forward.map(r => predict(model, r.x))),
            forwardBaseline: metrics(forward.map(r => r.y), forward.map(() => model.intercept)), predictions};
    }
    const api = {aggregate, fit, predict, metrics, select, evaluate};
    root.PGPTModel = api;
    if (typeof module !== 'undefined') module.exports = api;
})(globalThis);
