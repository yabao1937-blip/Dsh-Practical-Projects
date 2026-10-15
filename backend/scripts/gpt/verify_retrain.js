// 校验这次全量数据在前端真实训练路径与 Python 模型之间一致；不写 DS 基线。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {App, ctx} = require('../app_vm')();
const vm = require('node:vm');
const folder = path.resolve(process.argv[2]);
const records = JSON.parse(fs.readFileSync(path.join(folder, 'records.json'), 'utf8'));
const bundle = JSON.parse(fs.readFileSync(path.join(folder, 'models.json'), 'utf8'));
const report = JSON.parse(fs.readFileSync(path.join(folder, 'report.json'), 'utf8'));
const retrain = App._trainCoarseRows.bind(App);
vm.runInContext(fs.readFileSync(path.resolve(__dirname, '../../../frontend/js/coarse.js'), 'utf8'), ctx);
const page = vm.runInContext('CoarsePage', ctx);
App.store.coarseCoal = records;
App.store.coarseTolerance = .8;
App.store.coarseTrainRange = 'all';
App.store.coarseModel = bundle.models.sample;
function close(a, b, label) {
    assert(Number.isFinite(a) && Number.isFinite(b) && Math.abs(a - b) < 1e-6,
        `${label}: ${a} vs ${b}`);
}
for (const mode of ['sample', 'daily']) {
    const fitted = mode === 'daily' ? page._ensureDailyModel() : retrain(records);
    const expected = bundle.models[mode];
    assert.equal(fitted.production, expected.production);
    assert.equal(fitted.n, expected.n);
    for (const kind of ['mlr', 'pls']) {
        const actual = fitted[kind], want = expected[kind];
        for (const key of ['inputPolicy', 'inputBounds', 'config']) {
            assert.equal(JSON.stringify(actual[key]), JSON.stringify(want[key]), `${mode}/${kind}/${key}`);
        }
        close(actual.intercept, want.intercept, `${mode}/${kind}/intercept`);
        for (const key of ['coefs', 'imputeMeans', 'means', 'stds']) {
            actual[key].forEach((value, i) => close(value, want[key][i], `${mode}/${kind}/${key}/${i}`));
        }
        for (const key of ['r2', 'mae', 'rmse', 'q2', 'q2Time']) {
            close(actual.metrics[key], want.metrics[key], `${mode}/${kind}/${key}`);
        }
    }
    const actual = fitted[fitted.production].metrics.pipelineValidation;
    const want = report.results[mode].retrained.nestedTimeValidation;
    for (const key of ['r2', 'mae', 'rmse']) close(actual[key], want[key], `${mode}/validation/${key}`);
    console.log(`${mode}: ${fitted.n}, ${fitted.production}, JS/Python parity < 1e-6`);
}
