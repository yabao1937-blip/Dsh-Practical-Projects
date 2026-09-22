const {test} = require('node:test');
const assert = require('node:assert/strict');
const M = require('../../frontend/pgpt/model.js');
require('../../frontend/pgpt/data.js');
require('../../frontend/pgpt/models.js');
const rows = globalThis.PGPT_DATA.rows;

test('scalar ridge matches analytic solution, handles constant and missing columns', () => {
    const data = [0, 1, 2, 3, 4].map(i => ({day: `2026-06-${16 + i}`, x: [i, 1, null], y: 2 + 3 * i}));
    const m = M.fit(data, 1);
    assert.ok(Math.abs(M.predict(m, [4, 1, null]) - 11) < 1e-10);
    assert.equal(m.coefficients[1], 0);
    assert.equal(m.coefficients[2], 0);
    assert.ok(Math.abs(M.predict(m, [null, 1, null]) - 8) < 1e-10);
});
test('source dates, midnight, duplicates and bad assay rows remain auditable', () => {
    assert.equal(rows.length, 155);
    assert.equal(globalThis.PGPT_DATA.duplicateCount, 113);
    assert.equal(M.aggregate(rows, 'sample').length, 152);
    assert.equal(M.aggregate(rows, 'day').length, 37);
    for (const day of ['2026-06-30', '2026-07-01', '2026-07-10']) assert.equal(rows.filter(r => r.day === day).length, 4);
    assert.equal(rows.find(r => r.id === '2026-08-23#4').time, '2026-08-24T00:50:00');
    assert.equal(rows.filter(r => r.day === '2026-08-30').length, 3);
    assert.equal(M.aggregate(rows, 'day').some(r => r.day === '2026-08-30'), false);
    assert.equal(new Set(rows.map(r => r.id)).size, rows.length);
});
test('day model is fitted independently, with arithmetic sample aggregation', () => {
    const june16 = M.aggregate(rows, 'day').find(r => r.day === '2026-06-16');
    assert.ok(Math.abs(june16.y - 14) < 1e-10);
    assert.notDeepEqual(PGPT_MODELS.sample.model.coefficients, PGPT_MODELS.day.model.coefficients);
    assert.equal(PGPT_MODELS.sample.model.count, 113);
    assert.equal(PGPT_MODELS.day.model.count, 26);
});
test('future labels and inputs cannot affect selected parameters or training preprocessing', () => {
    const changed = rows.map(r => r.day >= '2026-08-01' ? {...r, y: 90, x: r.x.map(() => 999)} : r);
    for (const mode of ['sample', 'day']) {
        const a = M.evaluate(rows, mode), b = M.evaluate(changed, mode);
        assert.deepEqual(a.model, b.model);
        assert.deepEqual(a.tuning, b.tuning);
        assert.deepEqual(a.model, PGPT_MODELS[mode].model);
        for (const fold of a.tuning.folds) assert.ok(fold.trainEnd < fold.testStart);
    }
});
test('undefined R2 is null and small training periods fail explicitly', () => {
    assert.equal(M.metrics([4, 4], [4, 5]).r2, null);
    assert.equal(M.metrics([], []).n, 0);
    assert.throws(() => M.select(rows.filter(r => r.day === '2026-06-16')), /10个生产日/);
});
