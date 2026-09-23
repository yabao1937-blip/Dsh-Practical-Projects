// 粗灰页面缓存、算法切换和训练同步的无网络回归。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {App, ctx, elements} = require('./app_vm')();
const seed = JSON.parse(fs.readFileSync(path.join(__dirname, '../data/train_js.json'), 'utf8'));
App.store.coarseCoal = structuredClone(seed.coarseCoal);
App.store.coarseTrainRange = 'all';
App.store.coarseTolerance = .8;
App.trainCoarseModel('all', 'gpt');
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../frontend/js/coarse.js'), 'utf8'), ctx);
const page = vm.runInContext('CoarsePage', ctx);
elements['coarse-view-mode'] = {value: 'day'};
const first = page._ensureDailyModel();
assert(first);
assert.equal(page._ensureDailyModel(), first);
App.store.coarseCoal[0].ash_content += .5;
const changed = page._ensureDailyModel();
assert.notEqual(changed, first);
assert.notEqual(changed.days[0].ash_content, first.days[0].ash_content);
page.viewModel = 'mlr';
assert.equal(page._dailyPred(changed.days[0], changed), App._coarsePredict(changed.days[0], changed.mlr));
page.viewModel = 'pls';
assert.equal(page._dailyPred(changed.days[0], changed), App._coarsePredict(changed.days[0], changed.pls));
const outside = {...changed.days[0], raw_ash: 99, level: -100};
for (const method of ['mlr', 'pls']) {
    page.viewModel = method;
    assert.equal(page._dailyPred(outside, changed), App._coarsePredict(outside, changed[method]));
    assert(App._coarseCoverage(outside, changed[method]).some(x => x.feature === 'raw_ash'));
}
const sampleModel = App.store.coarseModel.mlr;
assert.equal(App.predictCoarseAsh(outside, 'mlr'), App._coarsePredict(outside, sampleModel));
assert.equal(outside.raw_ash, 99);
assert.equal(App._coarseCoverage(outside, seed.coarseModel.mlr).length, 0);
App.store.coarseTrainRange = '30d';
const limited = page._ensureDailyModel();
assert(limited);
assert(limited.n < changed.n);
App.store.coarseCoal = App.store.coarseCoal.slice(0, 4);
assert.equal(page._ensureDailyModel(), null); // 不残留之前范围的模型
const rows = [{timestamp: '2026-02-30 10:00:00', ash_content: 12},
    {timestamp: '2026-01-01 10:00:00', ash_content: Infinity}];
assert.equal(App._coarseRows(rows).length, 0);

(async () => {
    // DS 原版回归 + 往返切换不重训、不覆盖另一版本。
    App.store.coarseCoal = seed.coarseCoal;
    App.store.coarseTrainRange = 'jun_jul';
    assert.equal(App.trainCoarseModel('jun_jul', 'gpt'), true);
    let gpt = App.store.coarseModel;
    assert.equal(await App.switchCoarseEngine('ds'), true);
    const ds = App.store.coarseModel;
    assert.equal(App.coarseEngine(), 'ds');
    const dsSnapshot = JSON.stringify(App.store.coarseModelVariants.ds);
    assert.equal(App.trainCoarseModel('jun_jul', 'gpt'), true);
    gpt = App.store.coarseModel;
    assert.equal(JSON.stringify(App.store.coarseModelVariants.ds), dsSnapshot);
    assert.equal(App.store.coarseModel.mlr.metrics.trainingRevision, 'gpt-coverage-robust-20260922');
    assert.equal(await App.switchCoarseEngine('ds'), true);
    for (const method of ['mlr', 'pls']) {
        assert(Math.abs(ds[method].metrics.r2 - seed.coarseModel[method].metrics.r2) < 1e-6);
        assert(Math.abs(ds[method].metrics.q2 - seed.coarseModel[method].metrics.q2) < 1e-6);
        ds[method].coefs.forEach((v, j) => assert(Math.abs(v - seed.coarseModel[method].coefs[j]) < 1e-6));
    }
    const dsDaily = page._ensureDailyModel();
    assert(dsDaily && !dsDaily.mlr.metrics.version);
    // 固定夹具在原版 2668ec1 日模型上的结果；容差 1e-6。
    assert(Math.abs(dsDaily.mlr.metrics.r2 - .7725384424407182) < 1e-6);
    assert(Math.abs(dsDaily.mlr.metrics.q2 - .4877032665509692) < 1e-6);
    assert(Math.abs(dsDaily.pls.metrics.r2 - .79965773887981) < 1e-6);
    assert(Math.abs(dsDaily.pls.metrics.q2 - .4525513805231349) < 1e-6);
    assert(dsDaily.days.every(d => [0, 1].includes(d.desliming473)));
    const historyN = App.store.coarseModelHistory.length;
    assert.equal(await App.switchCoarseEngine('gpt'), true);
    assert.equal(App.coarseEngine(), 'gpt');
    assert.equal(App.store.coarseModel, gpt);
    assert.equal(App.store.coarseModelVariants.ds, ds);
    const gptDaily = page._ensureDailyModel();
    assert.equal(gptDaily.mlr.metrics.version, 2);
    assert.notEqual(dsDaily, gptDaily);
    assert.equal(await App.switchCoarseEngine('ds'), true);
    assert.equal(App.store.coarseModel, ds);
    assert.equal(App.store.coarseModelHistory.length, historyN);
    const saved = JSON.parse(JSON.stringify(App.store));
    App.store = saved; // 模拟重新加载，版本由模型事实推断。
    assert.equal(App.coarseEngine(), 'ds');
    const savedGpt = JSON.stringify(App.store.coarseModelVariants.gpt);
    assert.equal(App.trainCoarseModel('jun_jul', 'ds'), true);
    assert.equal(JSON.stringify(App.store.coarseModelVariants.gpt), savedGpt);
    App.store.coarseCoal = seed.coarseCoal.slice(0, 4);
    App.store.coarseTrainRange = '30d';
    assert.equal(await App.switchCoarseEngine('gpt'), false);
    assert.equal(App.coarseEngine(), 'ds');

    ctx.window.location.protocol = 'http:';
    App.store._revision = 'before';
    App.store.coarseCoal = seed.coarseCoal;
    const result = App._trainCoarseRows(App.store.coarseCoal);
    App.flushMirrorNow = async () => ({ok: true, revision: 'before'});
    let skipMirror = null, called = 0;
    App.saveStore = skip => { skipMirror = skip; };
    ctx.window.Api = {retrainCoarseModel: async (range, revision, engine) => {
        assert.equal(engine, 'gpt');
        assert.equal(revision, 'before'); called++;
        return {ok: true, revision: 'after', coarseModel: result};
    }};
    assert.equal(await App.retrainCoarseModelAsync('all', 'gpt'), true);
    assert.equal(App.store._revision, 'after');
    assert.equal(skipMirror, true);
    App.flushMirrorNow = async () => ({ok: false});
    assert.equal(await App.retrainCoarseModelAsync('all'), false);
    assert.equal(called, 1);
    App.flushMirrorNow = async () => ({ok: true, revision: 'after'});
    ctx.window.Api.retrainCoarseModel = async () => ({ok: true, coarseModel: result});
    assert.equal(await App.retrainCoarseModelAsync('all', 'ds'), false); // 旧后端不能把 GPT 冒充 DS。
    assert.equal(App.coarseEngine(), 'gpt');
    assert.equal(App.store._revision, 'after');
    ctx.window.Api.retrainCoarseModel = async () => {
        App.store.coarseCoal[0].ash_content += 1;
        return {ok: true, revision: 'unexpected', coarseModel: result};
    };
    assert.equal(await App.retrainCoarseModelAsync('all'), false);
    assert.equal(App.store._revision, 'after');
    assert.equal(App._mirrorConflict, true);
    console.log('Coarse DS/GPT: original parity, switching, persistence, daily cache, range and training sync passed');
})().catch(e => { console.error(e); process.exitCode = 1; });
